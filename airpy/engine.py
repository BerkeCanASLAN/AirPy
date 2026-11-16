"""The app's background engine: discovery, streams, audio capture and file sharing.

The engine runs an asyncio loop in its own thread. The UI calls the thread-safe methods on
``Engine``; the engine reports events through the ``emit`` callback (the UI turns them into
Qt signals).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from pathlib import Path

import numpy as np

from . import __version__, airplay, audio, net
from .airplay import DeviceStream, IdleVolume, PairingSession, Speaker, Target
from .config import Config
from .localapi import LocalApi
from .share import ShareServer

_LOG = logging.getLogger(__name__)

_FORGET_AFTER = 6  # a speaker not seen in this many scans drops off the list


class Engine:
    def __init__(self, config: Config, emit: Callable[[str, dict], None]) -> None:
        self.config = config
        self._emit_cb = emit
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

        self._timeline = audio.Timeline()
        self._capture = audio.LoopbackCapture(self._timeline, on_error=self._capture_error)

        self._speakers: dict[str, Speaker] = {}
        self._missed: dict[str, int] = {}
        self._targets: list[Target] = []
        self._active: set[str] = set()
        self._streams: dict[str, DeviceStream] = {}
        self._stream_state: dict[str, tuple[str, str]] = {}
        self._scanning = False
        self._scan_wakeup: asyncio.Event | None = None
        self._muted_device: str | None = None
        self._share: ShareServer | None = None
        self._tasks: list[asyncio.Task] = []
        self._capture_message = ""
        self._reconcile_lock: asyncio.Lock | None = None
        self._idle: dict[str, IdleVolume] = {}
        self._idle_read_at: dict[str, float] = {}
        self._pairing: tuple[str, PairingSession] | None = None
        self._mdns = net.MdnsName("airpy")
        self._fine_timer = False
        self._share_url = ""
        self._local_api: LocalApi | None = None

    # ------------------------------------------------------------------
    # Methods called from the UI thread
    # ------------------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._thread_main, name="airpy-engine", daemon=True)
        self._thread.start()
        self._ready.wait(10)

    def shutdown(self) -> None:
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        future = asyncio.run_coroutine_threadsafe(self._shutdown(), loop)
        try:
            future.result(timeout=8)
        except Exception as exc:
            _LOG.warning("Shutdown did not fully complete: %r", exc)
        self._restore_mute()
        loop.call_soon_threadsafe(loop.stop)
        if self._thread:
            self._thread.join(timeout=3)

    def _submit(self, coro) -> None:
        loop = self._loop
        if loop is None or not loop.is_running():
            coro.close()
            return
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        future.add_done_callback(self._log_failure)

    @staticmethod
    def _log_failure(future) -> None:
        if not future.cancelled() and future.exception() is not None:
            _LOG.error("Engine operation failed", exc_info=future.exception())

    def rescan(self) -> None:
        self._submit(self._request_scan())

    def set_target_active(self, key: str, active: bool) -> None:
        self._submit(self._set_target_active(key, active))

    def set_volume(self, key: str, level: float) -> None:
        self._submit(self._set_volume(key, level))

    def set_option(self, name: str, value) -> None:
        self._submit(self._set_option(name, value))

    def share_files(self, paths: list[str]) -> None:
        self._submit(self._share_files(paths))

    def share_text(self, text: str) -> None:
        self._submit(self._share_text(text))

    def remove_share_item(self, item_id: str) -> None:
        self._submit(self._remove_share_item(item_id))

    def clear_share_items(self) -> None:
        self._submit(self._clear_share_items())

    def play_test_tone(self) -> None:
        self._submit(self._play_test_tone())

    def pair_begin(self, key: str) -> None:
        self._submit(self._pair_begin(key))

    def pair_submit(self, key: str, pin: str) -> None:
        self._submit(self._pair_submit(key, pin))

    def pair_cancel(self) -> None:
        self._submit(self._pair_close())

    # ------------------------------------------------------------------
    # Engine thread
    # ------------------------------------------------------------------
    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        net.install_lan_binding(loop)
        self._loop = loop
        self._scan_wakeup = asyncio.Event()
        loop.create_task(self._main())
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()

    def _emit(self, event: str, payload: dict) -> None:
        try:
            self._emit_cb(event, payload)
        except Exception:
            _LOG.exception("Could not deliver event: %s", event)

    async def _main(self) -> None:
        airplay.set_latency_ms(self.config.get("latency_ms"))
        airplay.set_credentials(self.config.get("credentials"))
        if self.config.get("auto_connect"):
            self._active = set(self.config.get("selected"))
        if self.config.get("share_enabled"):
            await self._start_share()
        await self._start_local_api()
        self._tasks.append(asyncio.create_task(self._scan_loop()))
        self._tasks.append(asyncio.create_task(self._level_loop()))

    async def _shutdown(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        streams, self._streams = list(self._streams.values()), {}
        await asyncio.gather(*(s.stop() for s in streams), return_exceptions=True)
        self._capture.stop()
        self._set_fine_timer(False)
        for idle in self._idle.values():
            idle.cancel()
        self._idle.clear()
        await self._pair_close()
        await self._mdns.stop()
        if self._local_api is not None:
            await self._local_api.stop()
        if self._share is not None:
            await self._share.stop()

    # -- Local API for the browser extension ------------------------------------
    async def _start_local_api(self) -> None:
        api = LocalApi(self._sync_info)
        try:
            await api.start()
        except Exception as exc:
            _LOG.warning("Could not start the local API: %s", exc)
            return
        self._local_api = api
        self._emit("localapi", {"port": api.port})

    def _sync_info(self) -> dict:
        """Status read by the extension: while streaming, the picture should be delayed by this many ms."""
        streaming = any(s.state == airplay.STATE_STREAMING for s in self._streams.values())
        buffer_ms = int(self.config.get("latency_ms"))
        return {
            "app": "AirPy",
            "version": __version__,
            "streaming": streaming,
            "buffer_ms": buffer_ms,
            "latency_ms": airplay.total_latency_ms(buffer_ms),
        }

    # -- Discovery ------------------------------------------------------------
    async def _request_scan(self) -> None:
        self._idle_read_at.clear()  # a manual refresh refreshes the volume levels too
        if self._scan_wakeup is not None:
            self._scan_wakeup.set()

    async def _scan_loop(self) -> None:
        assert self._scan_wakeup is not None
        while True:
            await self._scan()
            host = self._mdns.host
            await self._mdns.refresh()
            if self._mdns.host != host or self._current_share_url() != self._share_url:
                self._emit_share()  # the network/Tailscale address changed: refresh the QR code
            keys = {t.key for t in self._targets}
            interval = 12.0 if (self._active - keys) or not keys else 60.0
            try:
                await asyncio.wait_for(self._scan_wakeup.wait(), timeout=interval)
            except TimeoutError:
                pass
            self._scan_wakeup.clear()

    async def _scan(self) -> None:
        self._scanning = True
        self._emit_targets()
        try:
            found = await airplay.scan_speakers(timeout=4.0)
        except Exception as exc:
            _LOG.warning("Scan failed: %r", exc)
            found = None
        self._scanning = False
        if found is not None:
            seen = set()
            for fresh in found:
                seen.add(fresh.id)
                self._missed[fresh.id] = 0
                known = self._speakers.get(fresh.id)
                if known is None:
                    self._speakers[fresh.id] = fresh
                else:  # streams hold the same object, so update it in place
                    for attr in (
                        "name", "address", "model", "group_name", "tsid", "leader",
                        "blocked", "needs_pairing", "conf",
                    ):
                        setattr(known, attr, getattr(fresh, attr))
            for speaker_id in list(self._speakers):
                if speaker_id in seen:
                    continue
                self._missed[speaker_id] = self._missed.get(speaker_id, 0) + 1
                if self._missed[speaker_id] >= _FORGET_AFTER and speaker_id not in self._streams:
                    del self._speakers[speaker_id]
                    self._missed.pop(speaker_id, None)
            self._targets = airplay.build_targets(list(self._speakers.values()))
        await self._reconcile()
        self._refresh_idle()

    # -- Idle volume -------------------------------------------------
    def _idle_for(self, target: Target) -> IdleVolume | None:
        """The target's idle volume controller (the leader, for a pair)."""
        if target.blocked or not target.speakers:
            return None
        leader = target.speakers[0]
        idle = self._idle.get(target.key)
        if idle is None or idle.speaker is not leader:
            previous = idle.volume if idle is not None else None
            if idle is not None:
                idle.cancel()
            idle = IdleVolume(leader, self._emit_targets)
            idle.volume = previous
            self._idle[target.key] = idle
        return idle

    def _refresh_idle(self) -> None:
        """Read the volume of targets that are not streaming (at most once every 5 minutes)."""
        now = asyncio.get_running_loop().time()
        keys = set()
        for target in self._targets:
            keys.add(target.key)
            idle = self._idle_for(target)
            if idle is None or any(s.id in self._streams for s in target.speakers):
                continue
            if now - self._idle_read_at.get(target.key, -1e9) > 300:
                self._idle_read_at[target.key] = now
                idle.request_read()
        for key in [k for k in self._idle if k not in keys]:
            self._idle.pop(key).cancel()

    # -- Pairing ---------------------------------------------------------
    async def _pair_close(self) -> None:
        pairing, self._pairing = self._pairing, None
        if pairing is not None:
            await pairing[1].close()

    async def _pair_begin(self, key: str) -> None:
        target = next((t for t in self._targets if t.key == key), None)
        speaker = target.pairable if target is not None else None
        if target is None or speaker is None:
            return
        await self._pair_close()
        session = PairingSession(speaker)
        self._pairing = (key, session)
        try:
            await session.begin()
        except Exception as exc:
            _LOG.warning("Could not start pairing (%s): %r", speaker.name, exc)
            await self._pair_close()
            self._emit("pair", {"key": key, "name": target.name, "stage": "error",
                                "message": airplay.friendly_error(exc)})
            return
        self._emit("pair", {"key": key, "name": target.name, "stage": "pin", "message": ""})

    async def _pair_submit(self, key: str, pin: str) -> None:
        if self._pairing is None or self._pairing[0] != key:
            return
        session = self._pairing[1]
        name = session.speaker.name
        try:
            credentials = await session.finish(pin)
        except Exception as exc:
            _LOG.warning("Pairing failed (%s): %r", name, exc)
            await self._pair_close()
            self._emit("pair", {"key": key, "name": name, "stage": "error",
                                "message": airplay.friendly_error(exc)})
            return
        await self._pair_close()
        store = self.config.get("credentials")
        store[session.speaker.id] = credentials
        self.config.set("credentials", store)
        airplay.set_credentials(store)
        airplay.apply_credentials(session.speaker)
        _LOG.info("Paired: %s", name)
        self._emit("pair", {"key": key, "name": name, "stage": "done", "message": ""})
        await self._reconcile()
        self._refresh_idle()

    # -- Streams ----------------------------------------------------------
    def _speakers_for(self, target: Target) -> list[Speaker]:
        if target.kind == "pair" and self.config.get("pair_mode") == "leader":
            return target.speakers[:1]
        return list(target.speakers)

    async def _reconcile(self) -> None:
        """Match the running streams to the active targets (one run at a time)."""
        if self._reconcile_lock is None:
            self._reconcile_lock = asyncio.Lock()
        async with self._reconcile_lock:
            await self._reconcile_locked()

    async def _reconcile_locked(self) -> None:
        wanted: dict[str, Speaker] = {}
        for target in self._targets:
            if target.key in self._active and not target.blocked:
                for speaker in self._speakers_for(target):
                    wanted[speaker.id] = speaker

        stale = [sid for sid in self._streams if sid not in wanted]
        stopping = [self._streams.pop(sid) for sid in stale]
        if stopping:
            await asyncio.gather(*(s.stop() for s in stopping), return_exceptions=True)
            for sid in stale:
                self._stream_state.pop(sid, None)

        if wanted and not self._capture.running:
            self._capture_message = ""
            self._set_fine_timer(True)
            self._capture.start()
        for sid, speaker in wanted.items():
            if sid not in self._streams:
                stream = DeviceStream(speaker, self._timeline, self._on_stream_state, self._on_stream_volume)
                self._streams[sid] = stream
                stream.start()
        if not self._streams and self._capture.running:
            self._capture.stop()
            self._set_fine_timer(False)
        self._apply_mute()
        self._emit_targets()

    def _set_fine_timer(self, enabled: bool) -> None:
        """Raise the Windows timer resolution to 1 ms while streaming.

        At the default ~15.6 ms resolution 8 ms audio packets go out two at a time and late; with a
        small speaker buffer this turns directly into dropouts.
        """
        if enabled == self._fine_timer:
            return
        try:
            import ctypes

            winmm = ctypes.windll.winmm
            (winmm.timeBeginPeriod if enabled else winmm.timeEndPeriod)(1)
            self._fine_timer = enabled
        except Exception as exc:
            _LOG.debug("Could not set the timer resolution: %r", exc)

    def _on_stream_state(self, speaker_id: str, state: str, message: str) -> None:
        if speaker_id in self._streams:
            self._stream_state[speaker_id] = (state, message)
            self._emit_targets()

    def _on_stream_volume(self, speaker_id: str, volume: float) -> None:
        for target in self._targets:  # when the stream ends the slider shows the last value
            if any(s.id == speaker_id for s in target.speakers) and target.key in self._idle:
                self._idle[target.key].volume = volume
        self._emit_targets()

    async def _set_target_active(self, key: str, active: bool) -> None:
        if active:
            self._active.add(key)
        else:
            self._active.discard(key)
        self.config.set("selected", sorted(self._active))
        await self._reconcile()

    async def _set_volume(self, key: str, level: float) -> None:
        target = next((t for t in self._targets if t.key == key), None)
        if target is None:
            return
        streams = [self._streams[s.id] for s in target.speakers if s.id in self._streams]
        idle = self._idle_for(target)
        if streams:
            if idle is not None:
                idle.volume = float(level)
            await asyncio.gather(*(s.set_volume(level) for s in streams), return_exceptions=True)
        elif idle is not None:
            idle.request_set(level)  # when not streaming, set it over the control channel

    async def _set_option(self, name: str, value) -> None:
        self.config.set(name, value)
        if name == "pair_mode":
            await self._reconcile()
        elif name == "latency_ms":
            airplay.set_latency_ms(value)
            await self._restart_streams()
        elif name == "share_via":
            self._emit_share()
        elif name == "mute_local":
            self._apply_mute()
        elif name == "share_enabled":
            if value:
                await self._start_share()
            elif self._share is not None:
                await self._mdns.stop()
                await self._share.stop()
                self._emit_share()
        elif name == "receive_dir":
            self._emit_share()

    async def _restart_streams(self) -> None:
        if self._reconcile_lock is None:
            self._reconcile_lock = asyncio.Lock()
        async with self._reconcile_lock:
            streams, self._streams = list(self._streams.values()), {}
            await asyncio.gather(*(s.stop() for s in streams), return_exceptions=True)
            self._stream_state.clear()
            await self._reconcile_locked()

    def _target_state(self, target: Target) -> tuple[str, str, float | None]:
        idle = self._idle.get(target.key)
        idle_volume = idle.volume if idle is not None else None
        if target.blocked:
            return ("error" if target.key in self._active else "idle"), target.blocked, None
        if target.key not in self._active:
            return "idle", "", idle_volume
        speakers = self._speakers_for(target)
        states = [self._stream_state.get(s.id, (airplay.STATE_CONNECTING, "")) for s in speakers]
        volume = idle_volume
        for speaker in speakers:
            stream = self._streams.get(speaker.id)
            if stream is not None and stream.volume is not None:
                volume = stream.volume
                break
        for state, message in states:
            if state == airplay.STATE_ERROR:
                return "error", message, volume
        if states and all(state == airplay.STATE_STREAMING for state, _ in states):
            return "streaming", "", volume
        return "connecting", "", volume

    def _emit_targets(self) -> None:
        rows = []
        known = set()
        for target in self._targets:
            known.add(target.key)
            state, message, volume = self._target_state(target)
            rows.append(
                {
                    "key": target.key,
                    "name": target.name,
                    "kind": target.kind,
                    "model": target.model,
                    "count": len(target.speakers),
                    "active": target.key in self._active,
                    "state": state,
                    "message": message,
                    "volume": volume,
                    "online": True,
                    "pairable": target.pairable is not None,
                }
            )
        for key in sorted(self._active - known):  # selected but not currently seen on the network
            rows.append(
                {
                    "key": key, "name": "Saved speaker", "kind": "single", "model": "",
                    "count": 1, "active": True, "state": "connecting",
                    "message": "Looking for it on the network…", "volume": None, "online": False,
                    "pairable": False,
                }
            )
        self._emit(
            "targets",
            {
                "targets": rows,
                "scanning": self._scanning,
                "capture": self._capture.device_name if self._capture.running else "",
                "capture_error": self._capture_message,
            },
        )

    # -- Audio capture -----------------------------------------------------
    def _capture_error(self, message: str) -> None:
        self._capture_message = message
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._emit_targets)

    async def _level_loop(self) -> None:
        was_running = False
        while True:
            await asyncio.sleep(0.08)
            running = self._capture.running and bool(self._streams)
            if running:
                self._emit("level", {"peak": self._capture.take_peak()})
            elif was_running:
                self._emit("level", {"peak": 0.0})
            was_running = running

    def _apply_mute(self) -> None:
        want = bool(self.config.get("mute_local")) and bool(self._streams)
        if want and self._muted_device is None:
            self._muted_device = audio.mute_default_output()
        elif not want:
            self._restore_mute()

    def _restore_mute(self) -> None:
        device, self._muted_device = self._muted_device, None
        if device is not None:
            audio.unmute_output(device)

    async def _play_test_tone(self) -> None:
        """Write short tones straight onto the line: left, right, then both (it does not play on the PC speakers)."""
        rate = audio.RATE
        amp = 0.12

        def tone(freq: float, seconds: float) -> np.ndarray:
            t = np.arange(int(rate * seconds)) / rate
            wave = np.sin(2 * np.pi * freq * t) * amp
            fade = int(rate * 0.02)
            envelope = np.ones_like(wave)
            envelope[:fade] = np.linspace(0, 1, fade)
            envelope[-fade:] = np.linspace(1, 0, fade)
            return wave * envelope

        gap = np.zeros(int(rate * 0.35))
        beep = tone(660.0, 0.5)
        left = np.concatenate((beep, gap, np.zeros_like(beep), gap, tone(880.0, 0.5)))
        right = np.concatenate((np.zeros_like(beep), gap, beep, gap, tone(880.0, 0.5)))
        pcm = (np.stack((left, right), axis=1) * 32767.0).astype(">i2")
        self._timeline.write(pcm, audio.now_pos() + len(pcm))

    # -- File sharing --------------------------------------------------
    def _receive_dir(self) -> Path:
        return Path(self.config.get("receive_dir"))

    async def _start_share(self) -> None:
        if self._share is None:
            self._share = ShareServer(
                self.config.get("share_token"),
                int(self.config.get("share_port")),
                self._receive_dir,
                self._emit,
            )
        if not self._share.running:
            try:
                await self._share.start()
            except Exception as exc:
                _LOG.error("Could not start the sharing server: %s", exc)
                self._emit("notice", {"text": f"File sharing could not be started: {exc}"})
        if self._share.running:
            await self._mdns.start(self._share.port)
            self._share.stable_host = self._mdns.host
        self._emit_share()

    def _share_urls(self) -> dict[str, str]:
        """Addresses the phone can use: local network and (if present) Tailscale."""
        share = self._share
        if share is None or not share.running:
            return {"lan": "", "tailscale": ""}
        near = next((s.address for s in self._speakers.values()), None)
        tailscale = net.tailscale_ip()
        return {
            "lan": share.url(net.primary_lan_ip(near)),
            "tailscale": share.url(tailscale) if tailscale else "",
        }

    def _current_share_url(self) -> str:
        urls = self._share_urls()
        via = self.config.get("share_via")
        return urls.get(via) or urls["lan"]

    def _emit_share(self) -> None:
        share = self._share
        running = share is not None and share.running
        if share is not None:
            share.stable_host = self._mdns.host
        urls = self._share_urls()
        via = self.config.get("share_via")
        if not urls.get(via):
            via = "lan"
        self._share_url = urls[via]
        self._emit(
            "share",
            {
                "running": running,
                "url": self._share_url,
                "via": via,
                "has_tailscale": bool(urls["tailscale"]),
                "items": [
                    {k: item[k] for k in ("id", "kind", "name", "size")}
                    for item in (share.items() if share else [])
                ],
                "receive_dir": str(self._receive_dir()),
            },
        )

    async def _share_files(self, paths: list[str]) -> None:
        if self._share is None:
            return
        for path in paths:
            self._share.add_file(path)
        self._emit_share()

    async def _share_text(self, text: str) -> None:
        if self._share is not None and self._share.add_text(text):
            self._emit_share()

    async def _remove_share_item(self, item_id: str) -> None:
        if self._share is not None:
            self._share.remove(item_id)
            self._emit_share()

    async def _clear_share_items(self) -> None:
        if self._share is not None:
            self._share.clear()
            self._emit_share()
