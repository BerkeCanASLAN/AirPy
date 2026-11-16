"""Discovers AirPlay speakers and streams live PC audio through pyatv.

pyatv normally plays by decoding a file or stream. Here the decoding step is skipped:
``LiveSource`` reads raw PCM straight from the shared ``Timeline``. The read position is derived
from pyatv's RTP timeline (``head_ts``), so streams that go to several speakers at the same time
stay locked to each other.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import Callable
from dataclasses import dataclass, field

import pyatv
from pyatv.const import PairingRequirement, Protocol
from pyatv.interface import MediaMetadata
from pyatv.protocols import raop as _raop
from pyatv.protocols.raop import stream_client as _stream_client
from pyatv.protocols.raop.audio_source import AudioSource
from pyatv.protocols.raop.protocols import StreamContext
from pyatv.support import rtsp as _rtsp
from pyatv.support.metadata import EMPTY_METADATA

from .audio import CHANNELS, RATE, Timeline, now_pos

_LOG = logging.getLogger(__name__)

# Latency has two parts:
#  * speaker buffer (_latency_frames): how long after a packet is sent it is played
#  * read margin (_read_delay): how long captured audio waits before it goes into a packet;
#    capture arrives in 10 ms blocks with a few ms of jitter, so it cannot be zero.
LATENCY_MIN_MS = 50
LATENCY_MAX_MS = 2000
LATENCY_DEFAULT_MS = 1500
_READ_DELAY_TIGHT = int(RATE * 0.040)  # low latency: 8 ms packet + 10 ms block + jitter margin
_READ_DELAY_SAFE = int(RATE * 0.100)

_latency_frames: int = int(RATE * LATENCY_DEFAULT_MS / 1000)
_read_delay: int = _READ_DELAY_SAFE


def set_latency_ms(milliseconds: int) -> None:
    """Choose the speaker buffer (ms) for streams that start from now on."""
    global _latency_frames, _read_delay
    ms = max(LATENCY_MIN_MS, min(LATENCY_MAX_MS, int(milliseconds)))
    _latency_frames = int(RATE * ms / 1000)
    _read_delay = _READ_DELAY_TIGHT if ms <= 600 else _READ_DELAY_SAFE


def total_latency_ms(milliseconds: int) -> int:
    """Approximate total latency to show the user (buffer + read margin + capture)."""
    ms = max(LATENCY_MIN_MS, min(LATENCY_MAX_MS, int(milliseconds)))
    read = (_READ_DELAY_TIGHT if ms <= 600 else _READ_DELAY_SAFE) * 1000 // RATE
    return ms + read + 15


# -- Small additions to pyatv -------------------------------------------
# 1) open_source: if a ready-made AudioSource is passed, use it as is.
_orig_open_source = _raop.open_source


async def _open_source(source, sample_rate, channels, sample_size):
    if isinstance(source, AudioSource):
        return source
    return await _orig_open_source(source, sample_rate, channels, sample_size)


_raop.open_source = _open_source

# 2) send_audio: bind the live source to the stream's timeline.
_orig_send_audio = _stream_client.StreamClient.send_audio


async def _send_audio(self, source, metadata=EMPTY_METADATA, /, volume=None):
    if isinstance(source, LiveSource):
        source.bind(self.context)
    return await _orig_send_audio(self, source, metadata, volume=volume)


_stream_client.StreamClient.send_audio = _send_audio

# 3) StreamContext.reset: apply the chosen latency.
_orig_reset = StreamContext.reset


def _reset(self) -> None:
    _orig_reset(self)
    self.latency = _latency_frames


StreamContext.reset = _reset

# 4) SETUP: pyatv advertises 250 ms (11025 frames) as the lowest latency; if a smaller
#    buffer is chosen, lower the advertised minimum too so the receiver does not reject it.
_orig_rtsp_setup = _rtsp.RtspSession.setup


async def _rtsp_setup(self, headers=None, body=None):
    if isinstance(body, dict):
        for stream in body.get("streams", ()):
            if isinstance(stream, dict) and "latencyMin" in stream:
                stream["latencyMin"] = min(int(stream["latencyMin"]), _latency_frames)
    return await _orig_rtsp_setup(self, headers=headers, body=body)


_rtsp.RtspSession.setup = _rtsp_setup


class LiveSource(AudioSource):
    """Endless audio source that reads from the Timeline."""

    def __init__(
        self, timeline: Timeline, on_start: Callable[[], None] | None = None
    ) -> None:
        self._timeline = timeline
        self._on_start = on_start
        self._ctx: StreamContext | None = None
        self._started = False
        self._stopped = False

    def bind(self, ctx: StreamContext) -> None:
        self._ctx = ctx

    def stop(self) -> None:
        self._stopped = True

    async def readframes(self, nframes: int) -> bytes:
        if self._stopped:
            return AudioSource.NO_FRAMES
        if not self._started:
            self._started = True
            if self._on_start:
                self._on_start()
        head = self._ctx.head_ts if self._ctx is not None else now_pos()
        return self._timeline.read(head - _read_delay, nframes)

    async def get_metadata(self) -> MediaMetadata:
        return EMPTY_METADATA

    @property
    def sample_rate(self) -> int:
        return RATE

    @property
    def channels(self) -> int:
        return CHANNELS

    @property
    def sample_size(self) -> int:
        return 2

    @property
    def duration(self) -> int:
        return 0


# -- Discovery ---------------------------------------------------------------


@dataclass
class Speaker:
    """A single AirPlay speaker seen on the network."""

    id: str
    name: str
    address: str
    model: str
    group_name: str = ""
    tsid: str = ""  # stereo pair id (the same on both members)
    leader: bool = True
    blocked: str = ""  # if not empty, the reason streaming is not possible
    needs_pairing: bool = False  # waiting for PIN pairing
    conf: object = field(default=None, repr=False, compare=False)


@dataclass
class Target:
    """One row in the UI: a single speaker or a stereo pair."""

    key: str
    name: str
    kind: str  # "single" | "pair"
    model: str
    speakers: list[Speaker]

    @property
    def blocked(self) -> str:
        for speaker in self.speakers:
            if speaker.blocked:
                return speaker.blocked
        return ""

    @property
    def pairable(self) -> Speaker | None:
        """First member waiting for pairing (None if there is none)."""
        for speaker in self.speakers:
            if speaker.needs_pairing:
                return speaker
        return None


# Credentials of paired devices: device id -> pyatv credentials string
_credentials: dict[str, str] = {}


def set_credentials(store: dict[str, str]) -> None:
    """Load the saved pairing credentials (when the engine starts and after pairing)."""
    _credentials.clear()
    _credentials.update(store)


def apply_credentials(speaker: "Speaker") -> None:
    """Apply saved credentials, if any, to the speaker's configuration."""
    credentials = _credentials.get(speaker.id)
    if not credentials or speaker.conf is None:
        return
    for protocol in (Protocol.RAOP, Protocol.AirPlay):
        if speaker.conf.get_service(protocol) is not None:
            speaker.conf.set_credentials(protocol, credentials)
    if speaker.needs_pairing:
        speaker.needs_pairing = False
        speaker.blocked = ""


def _speaker_from_conf(conf) -> Speaker | None:
    raop = conf.get_service(Protocol.RAOP)
    if raop is None or not conf.identifier:
        return None
    airplay = conf.get_service(Protocol.AirPlay)
    props = dict(airplay.properties) if airplay else {}

    blocked = ""
    needs_pairing = False
    if raop.pairing == PairingRequirement.Disabled:
        blocked = "Access is off: in the Home app set speaker access to 'Anyone on the Same Network'"
    elif raop.pairing == PairingRequirement.Unsupported:
        blocked = "Access is limited to the home owner: set it to 'Anyone on the Same Network'"
    elif raop.pairing == PairingRequirement.Mandatory:
        blocked = "Pairing required: press Pair to enter the code shown on the screen"
        needs_pairing = True
    elif raop.requires_password:
        blocked = "Password protected (not supported yet)"

    info = conf.device_info
    model = getattr(info, "model_str", None) or str(getattr(info, "raw_model", "") or "")
    speaker = Speaker(
        id=str(conf.identifier),
        name=str(conf.name),
        address=str(conf.address),
        model=str(model),
        group_name=str(props.get("gpn", "")),
        tsid=str(props.get("tsid", "")),
        leader=str(props.get("igl", "1")) == "1",
        blocked=blocked,
        needs_pairing=needs_pairing,
        conf=conf,
    )
    apply_credentials(speaker)
    return speaker


async def scan_speakers(timeout: float = 4.0) -> list[Speaker]:
    """Scan the network for AirPlay audio targets."""
    loop = asyncio.get_running_loop()
    confs = await pyatv.scan(loop, timeout=timeout)
    speakers = []
    for conf in confs:
        speaker = _speaker_from_conf(conf)
        if speaker is not None:
            speakers.append(speaker)
    return speakers


def build_targets(speakers: list[Speaker]) -> list[Target]:
    """Turn speakers into targets; two that share the same ``tsid`` form a stereo pair."""
    by_tsid: dict[str, list[Speaker]] = {}
    for speaker in speakers:
        if speaker.tsid:
            by_tsid.setdefault(speaker.tsid, []).append(speaker)

    targets: list[Target] = []
    used: set[str] = set()
    for tsid, members in by_tsid.items():
        if len(members) != 2:
            continue
        members.sort(key=lambda s: (not s.leader, s.name))
        name = members[0].group_name or members[0].name
        targets.append(Target(f"pair:{tsid}", name, "pair", members[0].model, members))
        used.update(m.id for m in members)
    for speaker in speakers:
        if speaker.id not in used:
            targets.append(
                Target(speaker.id, speaker.name, "single", speaker.model, [speaker])
            )
    targets.sort(key=lambda t: (t.kind != "pair", t.name.lower()))
    return targets


# -- Pairing ----------------------------------------------------------


class PairingSession:
    """PIN pairing: ``begin`` makes the device show a code, ``finish`` verifies it."""

    def __init__(self, speaker: Speaker) -> None:
        self.speaker = speaker
        self._handler = None

    async def begin(self) -> None:
        loop = asyncio.get_running_loop()
        self._handler = await pyatv.pair(self.speaker.conf, Protocol.RAOP, loop)
        await asyncio.wait_for(self._handler.begin(), timeout=15)
        if not self._handler.device_provides_pin:
            raise RuntimeError("This device does not show a code on screen; this kind of pairing is not supported")

    async def finish(self, pin: str) -> str:
        """Submit the code; on success return the credentials string to store."""
        if self._handler is None:
            raise RuntimeError("pairing was not started")
        self._handler.pin(pin.strip())
        await asyncio.wait_for(self._handler.finish(), timeout=20)
        credentials = self._handler.service.credentials
        if not self._handler.has_paired or not credentials:
            raise RuntimeError("The code was not accepted")
        return str(credentials)

    async def close(self) -> None:
        handler, self._handler = self._handler, None
        if handler is not None:
            try:
                await handler.close()
            except Exception:
                pass


# -- Idle volume control ---------------------------------------------------


class IdleVolume:
    """Reads/sets the speaker's volume while nothing is streaming.

    Opens a short-lived control connection (the AirPlay remote-control channel); consecutive
    slider changes reuse the same connection, which closes after being idle.
    """

    _HOLD = 4.0  # how long (s) the connection stays open after the last request

    def __init__(self, speaker: Speaker, on_volume: Callable[[], None]) -> None:
        self.speaker = speaker
        self.volume: float | None = None
        self._on_volume = on_volume
        self._want: float | None = None
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None

    def request_read(self) -> None:
        self._ensure_worker()

    def request_set(self, level: float) -> None:
        level = max(0.0, min(100.0, float(level)))
        self._want = level
        self.volume = level
        self._wake.set()
        self._ensure_worker()

    def cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def _ensure_worker(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._worker())

    async def _worker(self) -> None:
        loop = asyncio.get_running_loop()
        atv = None
        try:
            if self.speaker.conf.get_service(Protocol.AirPlay) is None:
                return  # no remote-control channel (old AirPlay 1 speaker)
            atv = await asyncio.wait_for(
                pyatv.connect(self.speaker.conf, loop, protocol=Protocol.AirPlay), timeout=8
            )
            if self._want is None:
                await asyncio.sleep(1.2)  # let the device send its first status update
                if self._want is None:
                    self.volume = float(atv.audio.volume)
                    self._on_volume()
            while True:
                if self._want is not None:
                    level, self._want = self._want, None
                    try:
                        await asyncio.wait_for(atv.audio.set_volume(level), timeout=6)
                    except TimeoutError:
                        pass  # the device sent no acknowledgement; the value may still have been delivered
                    continue
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self._HOLD)
                except TimeoutError:
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _LOG.debug("Idle volume control failed (%s): %r", self.speaker.name, exc)
        finally:
            if atv is not None:
                try:
                    await asyncio.gather(*atv.close(), return_exceptions=True)
                except Exception:
                    pass


# -- Stream ----------------------------------------------------------------

STATE_CONNECTING = "connecting"
STATE_STREAMING = "streaming"
STATE_ERROR = "error"
STATE_STOPPED = "stopped"


def _friendly_error(exc: BaseException) -> str:
    text = str(exc) or exc.__class__.__name__
    lowered = text.lower()
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "The speaker did not respond (timeout)"
    if isinstance(exc, ConnectionRefusedError) or "refused" in lowered:
        return "Connection refused"
    if isinstance(exc, OSError) and not isinstance(exc, ConnectionError):
        return f"Network error: {text}"
    if "auth" in lowered or "pair" in lowered or "403" in lowered or "470" in lowered:
        return "The speaker denied access (check the access setting in the Home app)"
    return text


friendly_error = _friendly_error


class DeviceStream:
    """A continuous live stream to one speaker (reconnects when it drops)."""

    def __init__(
        self,
        speaker: Speaker,
        timeline: Timeline,
        on_state: Callable[[str, str, str], None],
        on_volume: Callable[[str, float], None],
    ) -> None:
        self.speaker = speaker
        self._timeline = timeline
        self._on_state = on_state
        self._on_volume = on_volume
        self._task: asyncio.Task | None = None
        self._atv = None
        self._source: LiveSource | None = None
        self._stopping = False
        self._pending_volume: float | None = None
        self.state = STATE_STOPPED
        self.volume: float | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping = False
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        self._stopping = True
        task = self._task
        if task is None:
            return
        if self._source is not None:
            self._source.stop()
        if self._atv is not None and self.state == STATE_STREAMING:
            try:
                await self._atv.remote_control.stop()
            except Exception:
                pass
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=4.0)
        except TimeoutError:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        except (asyncio.CancelledError, Exception):
            pass
        self._task = None
        self._set_state(STATE_STOPPED)

    async def set_volume(self, level: float) -> None:
        level = max(0.0, min(100.0, float(level)))
        self.volume = level
        if self._atv is not None and self.state == STATE_STREAMING:
            try:
                await self._atv.audio.set_volume(level)
            except Exception as exc:
                _LOG.debug("Could not set the volume (%s): %s", self.speaker.name, exc)
        else:
            self._pending_volume = level

    # -- internals -----------------------------------------------------
    def _set_state(self, state: str, message: str = "") -> None:
        self.state = state
        self._on_state(self.speaker.id, state, message)

    def _started(self) -> None:
        self._set_state(STATE_STREAMING)
        asyncio.get_running_loop().create_task(self._after_start())

    async def _after_start(self) -> None:
        atv = self._atv
        if atv is None:
            return
        try:
            if self._pending_volume is not None:
                level, self._pending_volume = self._pending_volume, None
                await atv.audio.set_volume(level)
            self.volume = float(atv.audio.volume)
            self._on_volume(self.speaker.id, self.volume)
        except Exception as exc:
            _LOG.debug("Could not read the initial volume: %s", exc)

    async def _refresh_address(self) -> None:
        """The speaker's IP may have changed; find it again by its id."""
        try:
            loop = asyncio.get_running_loop()
            confs = await pyatv.scan(loop, identifier=self.speaker.id, timeout=3)
        except Exception:
            return
        for conf in confs:
            fresh = _speaker_from_conf(conf)
            if fresh is not None:
                self.speaker.conf = fresh.conf
                self.speaker.address = fresh.address

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        backoff = 2.0
        metadata = MediaMetadata(
            title="PC Audio", artist=socket.gethostname(), album="AirPy"
        )
        while not self._stopping:
            self._set_state(STATE_CONNECTING)
            started_at = loop.time()
            error = ""
            try:
                self._atv = await pyatv.connect(
                    self.speaker.conf, loop, protocol=Protocol.RAOP
                )
                self._source = LiveSource(self._timeline, on_start=self._started)
                await self._atv.stream.stream_file(self._source, metadata=metadata)
                if not self._stopping:
                    error = "The stream ended unexpectedly"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _LOG.warning("Stream error (%s): %r", self.speaker.name, exc)
                error = _friendly_error(exc.__cause__ or exc)
            finally:
                atv, self._atv, self._source = self._atv, None, None
                if atv is not None:
                    try:
                        await asyncio.gather(*atv.close(), return_exceptions=True)
                    except Exception:
                        pass
            if self._stopping:
                break
            self._set_state(STATE_ERROR, error)
            if loop.time() - started_at > 60:
                backoff = 2.0  # if it ran for a long time, retry quickly
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
            await self._refresh_address()
