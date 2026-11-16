"""A small API reachable only from this computer (127.0.0.1), for the browser extension.

``/airpy/sync``     : streaming state and total latency (the extension delays the picture by this much)
``/airpy/calibrate``: light/beep sync test page for fine-tuning the extension

It cannot be reached from outside (it only listens on the loopback interface). So that web pages
cannot read the response no CORS header is added, requests whose Origin is http(s) are rejected
and the Host header is validated (against DNS-rebinding attacks).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from aiohttp import web

_LOG = logging.getLogger(__name__)
_WEB_DIR = Path(__file__).parent / "web"

PORT_FIRST = 53307  # separate from the sharing server's range (53318+)
PORT_TRIES = 10
_EXTENSION_ORIGINS = ("chrome-extension://", "moz-extension://", "extension://")


class LocalApi:
    def __init__(self, info: Callable[[], dict]) -> None:
        self._info = info
        self._runner: web.AppRunner | None = None
        self.port = 0

    async def start(self) -> None:
        app = web.Application(middlewares=[self._guard])
        app.router.add_get("/airpy/sync", self._sync)
        app.router.add_get("/airpy/calibrate", self._calibrate)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        last_error: Exception | None = None
        for port in range(PORT_FIRST, PORT_FIRST + PORT_TRIES):
            try:
                await web.TCPSite(runner, "127.0.0.1", port).start()
            except OSError as exc:
                last_error = exc
                continue
            self.port = port
            self._runner = runner
            _LOG.info("Local API on 127.0.0.1:%d", port)
            return
        await runner.cleanup()
        raise RuntimeError(f"could not start the local API: {last_error}")

    async def stop(self) -> None:
        runner, self._runner = self._runner, None
        self.port = 0
        if runner is not None:
            await runner.cleanup()

    @web.middleware
    async def _guard(self, request: web.Request, handler):
        host = request.headers.get("Host", "")
        origin = request.headers.get("Origin")
        local = request.remote in ("127.0.0.1", "::1")
        host_ok = host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}")
        origin_ok = origin is None or origin.startswith(_EXTENSION_ORIGINS)
        if not (local and host_ok and origin_ok):
            raise web.HTTPNotFound()
        response = await handler(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    async def _sync(self, request: web.Request) -> web.Response:
        return web.json_response(self._info())

    async def _calibrate(self, request: web.Request) -> web.Response:
        html = (_WEB_DIR / "calibrate.html").read_text(encoding="utf-8")
        return web.Response(text=html, content_type="text/html", charset="utf-8")
