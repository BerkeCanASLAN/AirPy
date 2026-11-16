"""Captures Windows system audio (WASAPI loopback) and writes it to a shared timeline.

All AirPlay streams read from the same ``Timeline`` object by absolute sample position. That way
streams going to several speakers match the same sound to the same moment with sample accuracy
(no phase drift in a stereo pair).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

import numpy as np
from pyatv.protocols.raop import timing

_LOG = logging.getLogger(__name__)

RATE = 44100  # AirPlay target sample rate
CHANNELS = 2

# If the capture clock and the system clock drift apart by this much (after silence, a stall)
# it re-anchors (samples). Smaller drift is corrected without a break by the servo below.
_REANCHOR = int(RATE * 0.040)
# Clock-drift servo: once the smoothed error passes this threshold one sample per block
# is dropped/repeated (inaudible); it stops when the error falls below the lower threshold.
_SERVO_START = RATE * 0.0015
_SERVO_STOP = RATE * 0.0004
_SERVO_SMOOTH = 0.05


def now_pos() -> int:
    """Absolute sample position of right now (same base as pyatv's RTP timeline)."""
    return timing.ntp2ts(timing.ntp_now(), RATE)


class Timeline:
    """Ring buffer addressed by absolute sample position (s16 big-endian stereo)."""

    def __init__(self, seconds: float = 3.0) -> None:
        self._size = int(RATE * seconds)
        self._buf = np.zeros((self._size, CHANNELS), dtype=">i2")
        self._lock = threading.Lock()
        self._wpos: int | None = None
        self._error = 0.0  # smoothed (timeline - system clock) difference, in samples
        self._correcting = False

    def clear(self) -> None:
        with self._lock:
            self._wpos = None
            self._error = 0.0
            self._correcting = False

    def write(self, frames: np.ndarray, end_pos: int) -> None:
        """Write the ``frames`` block so that its last sample lines up with time ``end_pos``.

        Consecutive blocks are appended end to end by sample count; if the sound card's clock runs
        slower/faster than the system clock, the difference is closed by dropping or repeating one
        sample per block, so the read margin can stay small.
        """
        n = len(frames)
        if n == 0:
            return
        if n > self._size:
            frames = frames[-self._size :]
            n = self._size
        with self._lock:
            wpos = self._wpos
            error = 0 if wpos is None else (wpos + n) - end_pos
            if wpos is None or abs(error) > _REANCHOR:
                new = end_pos - n
                if wpos is not None and new > wpos:
                    self._zero(wpos, new - wpos)  # silence gap
                wpos = new
                self._error = 0.0
                self._correcting = False
            else:
                self._error += _SERVO_SMOOTH * (error - self._error)
                magnitude = abs(self._error)
                if magnitude > _SERVO_START:
                    self._correcting = True
                elif magnitude < _SERVO_STOP:
                    self._correcting = False
                if self._correcting and n > 2:
                    middle = n // 2
                    if self._error > 0:  # timeline is ahead: drop one sample
                        frames = np.delete(frames, middle, axis=0)
                        self._error -= 1.0
                    else:  # behind: repeat one sample
                        frames = np.insert(frames, middle, frames[middle], axis=0)
                        self._error += 1.0
            self._put(wpos, frames)
            self._wpos = wpos + len(frames)

    def read(self, pos: int, n: int) -> bytes:
        """Read ``n`` frames from position ``pos``; silence is returned where there is no data."""
        out = np.zeros((n, CHANNELS), dtype=">i2")
        with self._lock:
            wpos = self._wpos
            if wpos is not None:
                lo = max(pos, wpos - self._size)
                hi = min(pos + n, wpos)
                if hi > lo:
                    out[lo - pos : hi - pos] = self._get(lo, hi - lo)
        return out.tobytes()

    def _put(self, pos: int, frames: np.ndarray) -> None:
        i = pos % self._size
        n = len(frames)
        first = min(n, self._size - i)
        self._buf[i : i + first] = frames[:first]
        if first < n:
            self._buf[: n - first] = frames[first:]

    def _get(self, pos: int, n: int) -> np.ndarray:
        i = pos % self._size
        first = min(n, self._size - i)
        if first == n:
            return self._buf[i : i + n]
        return np.concatenate((self._buf[i:], self._buf[: n - first]))

    def _zero(self, pos: int, n: int) -> None:
        if n >= self._size:
            self._buf[:] = 0
            return
        i = pos % self._size
        first = min(n, self._size - i)
        self._buf[i : i + first] = 0
        if first < n:
            self._buf[: n - first] = 0


def _to_stereo(x: np.ndarray, channels: int) -> np.ndarray:
    """Downmix any channel layout to stereo float32."""
    if channels == 1:
        return np.repeat(x.reshape(-1, 1), 2, axis=1)
    x = x.reshape(-1, channels)
    if channels == 2:
        return x
    left = x[:, 0].copy()
    right = x[:, 1].copy()
    if channels >= 3:  # orta kanal
        left += 0.7071 * x[:, 2]
        right += 0.7071 * x[:, 2]
    if channels >= 6:  # rear/surround (5.1: FL FR FC LFE BL BR)
        left += 0.7071 * x[:, 4]
        right += 0.7071 * x[:, 5]
    if channels >= 8:  # 7.1 side channels
        left += 0.7071 * x[:, 6]
        right += 0.7071 * x[:, 7]
    return np.stack((left, right), axis=1)


class LoopbackCapture:
    """Background thread that listens to the default output device and writes to the ``Timeline``."""

    def __init__(
        self, timeline: Timeline, on_error: Callable[[str], None] | None = None
    ) -> None:
        self._timeline = timeline
        self._on_error = on_error
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._peak = 0.0
        self.device_name = ""

    # -- public interface ------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="airpy-capture", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=5)
        self._thread = None
        self._peak = 0.0
        self._timeline.clear()

    def take_peak(self) -> float:
        """Peak level (0..1) seen since the last read; reset when read."""
        peak, self._peak = self._peak, 0.0
        return peak

    # -- internals -----------------------------------------------------
    def _run(self) -> None:
        try:
            import comtypes

            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except Exception:  # may already be initialised
            pass
        while not self._stop.is_set():
            try:
                self._session()
            except Exception as exc:
                _LOG.warning("Audio capture error: %s", exc)
                if self._on_error:
                    self._on_error(str(exc))
                self._stop.wait(2.0)

    def _session(self) -> None:
        import pyaudiowpatch as pyaudio
        import soxr

        pa = pyaudio.PyAudio()
        stream = None
        try:
            dev = pa.get_default_wasapi_loopback()
            channels = int(dev["maxInputChannels"])
            rate = int(dev["defaultSampleRate"])
            self.device_name = str(dev["name"]).replace(" [Loopback]", "")
            resampler = (
                soxr.ResampleStream(rate, RATE, CHANNELS, dtype="float32", quality="HQ")
                if rate != RATE
                else None
            )
            _LOG.info(
                "Capturing: %s (%d Hz, %d channels)", self.device_name, rate, channels
            )

            def callback(in_data, frame_count, time_info, status):
                try:
                    self._process(in_data, channels, resampler)
                except Exception:
                    _LOG.exception("Could not process an audio block")
                return (None, pyaudio.paContinue)

            stream = pa.open(
                format=pyaudio.paFloat32,
                channels=channels,
                rate=rate,
                input=True,
                input_device_index=dev["index"],
                frames_per_buffer=max(rate // 100, 64),
                stream_callback=callback,
            )
            default_id = default_output_id()
            while not self._stop.wait(1.0):
                if not stream.is_active():
                    raise RuntimeError("the audio stream stopped unexpectedly")
                if default_output_id() != default_id:
                    _LOG.info("The default output device changed, reopening")
                    return
        finally:
            if stream is not None:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
            pa.terminate()

    def _process(self, in_data: bytes, channels: int, resampler) -> None:
        end_pos = now_pos()
        x = np.frombuffer(in_data, dtype=np.float32)
        if x.size == 0:
            return
        stereo = _to_stereo(x, channels)
        if resampler is not None:
            stereo = resampler.resample_chunk(stereo)
            if len(stereo) == 0:
                return
        peak = float(np.max(np.abs(stereo)))
        if peak > self._peak:
            self._peak = peak
        pcm = (np.clip(stereo, -1.0, 1.0) * 32767.0).astype(">i2")
        self._timeline.write(pcm, end_pos)


# -- Windows output device helpers (pycaw) ---------------------------


def _speakers():
    import comtypes
    from pycaw.pycaw import AudioUtilities

    try:
        comtypes.CoInitialize()
    except Exception:
        pass
    return AudioUtilities.GetSpeakers()


def default_output_id() -> str:
    """Id of the default output device (to notice changes)."""
    try:
        return str(_speakers().id)
    except Exception:
        return ""


def get_local_mute() -> bool | None:
    try:
        return bool(_speakers().EndpointVolume.GetMute())
    except Exception as exc:
        _LOG.debug("Could not read the mute state: %s", exc)
        return None


def set_local_mute(muted: bool) -> bool:
    """Mute/unmute the PC's default speakers. True on success."""
    try:
        _speakers().EndpointVolume.SetMute(1 if muted else 0, None)
        return True
    except Exception as exc:
        _LOG.warning("Could not mute the speakers: %s", exc)
        return False


def mute_default_output() -> str | None:
    """Mute the default output. Return the device id only if WE muted it.

    If the user had already muted it, nothing is touched and None is returned (so it is not
    unmuted later).
    """
    try:
        device = _speakers()
        if device.EndpointVolume.GetMute():
            return None
        device.EndpointVolume.SetMute(1, None)
        return str(device.id)
    except Exception as exc:
        _LOG.warning("Could not mute the speakers: %s", exc)
        return None


def unmute_output(device_id: str) -> None:
    """Unmute the device muted by ``mute_default_output`` (even if the default changed)."""
    try:
        device = _speakers()
        if str(device.id) != device_id:
            from pycaw.pycaw import AudioUtilities

            for candidate in AudioUtilities.GetAllDevices():
                if str(candidate.id) == device_id:
                    device = candidate
                    break
        device.EndpointVolume.SetMute(0, None)
    except Exception as exc:
        _LOG.warning("Could not unmute the speakers: %s", exc)
