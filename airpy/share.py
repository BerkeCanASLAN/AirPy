"""AirDrop-like local file/text sharing (phone browser <-> PC).

Real AirDrop needs the AWDL wireless layer, which Windows does not have. Instead a small web
server is started on the local network; the phone scans a QR code and sends/receives files from
Safari. Access is protected by a secret token inside the address.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
import secrets
import socket
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

from aiohttp import web

_LOG = logging.getLogger(__name__)

_WEB_DIR = Path(__file__).parent / "web"
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {
    f"LPT{i}" for i in range(1, 10)
}
_MAX_TEXT = 200_000


def safe_name(name: str) -> str:
    """Make an incoming file name safe for Windows (no directory traversal)."""
    name = os.path.basename(str(name).replace("\\", "/"))
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    if not name:
        name = "file"
    if name.split(".")[0].upper() in _RESERVED:
        name = "_" + name
    if len(name) > 180:
        stem, dot, ext = name.rpartition(".")
        name = (stem[: 170 - len(ext)] + dot + ext) if dot else name[:180]
    return name


def unique_path(directory: Path, name: str) -> Path:
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    for index in range(2, 10_000):
        candidate = directory / (f"{stem} ({index}).{ext}" if dot else f"{stem} ({index})")
        if not candidate.exists():
            return candidate
    return directory / f"{secrets.token_hex(4)}-{name}"


class ShareServer:
    """HTTP server for exchanging files with the phone."""

    def __init__(
        self,
        token: str,
        port: int,
        receive_dir: Callable[[], Path],
        emit: Callable[[str, dict], None],
    ) -> None:
        self._token = token
        self._want_port = port
        self._receive_dir = receive_dir
        self._emit = emit
        self._runner: web.AppRunner | None = None
        self.port = 0
        self.stable_host = ""  # a fixed name such as "airpy.local" when it is published
        self._items: dict[str, dict] = {}  # items the PC offers to the phone

    # -- lifecycle --------------------------------------------------
    async def start(self) -> None:
        app = web.Application(middlewares=[self._guard])
        prefix = "/{token}"
        app.router.add_get(prefix, self._redirect)
        app.router.add_get(prefix + "/", self._page)
        app.router.add_get(prefix + "/api/state", self._state)
        app.router.add_post(prefix + "/api/upload", self._upload)
        app.router.add_post(prefix + "/api/text", self._text)
        app.router.add_get(prefix + "/dl/{item}", self._download)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        last_error: Exception | None = None
        for port in range(self._want_port, self._want_port + 10):
            try:
                site = web.TCPSite(self._runner, "0.0.0.0", port)
                await site.start()
                self.port = port
                _LOG.info("Sharing server on port %d", port)
                return
            except OSError as exc:
                last_error = exc
        await self._runner.cleanup()
        self._runner = None
        raise RuntimeError(f"could not start the sharing server: {last_error}")

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self.port = 0

    @property
    def running(self) -> bool:
        return self._runner is not None

    def url(self, host: str) -> str:
        return f"http://{host}:{self.port}/{self._token}/"

    # -- PC -> phone items -------------------------------------------
    def add_file(self, path: str) -> dict | None:
        file = Path(path)
        try:
            if not file.is_file():
                return None
            size = file.stat().st_size
        except OSError:
            return None
        for item in self._items.values():
            if item["kind"] == "file" and item["path"] == str(file):
                return item
        item = {
            "id": secrets.token_urlsafe(8),
            "kind": "file",
            "name": file.name,
            "size": size,
            "path": str(file),
            "added": time.time(),
        }
        self._items[item["id"]] = item
        return item

    def add_text(self, text: str) -> dict | None:
        text = text.strip()
        if not text:
            return None
        item = {
            "id": secrets.token_urlsafe(8),
            "kind": "text",
            "name": text[:60],
            "size": len(text),
            "text": text[:_MAX_TEXT],
            "added": time.time(),
        }
        self._items[item["id"]] = item
        return item

    def remove(self, item_id: str) -> None:
        self._items.pop(item_id, None)

    def clear(self) -> None:
        self._items.clear()

    def items(self) -> list[dict]:
        return sorted(self._items.values(), key=lambda i: i["added"], reverse=True)

    # -- HTTP -------------------------------------------------------------
    @web.middleware
    async def _guard(self, request: web.Request, handler):
        token = request.match_info.get("token", "")
        if not hmac.compare_digest(token.encode(), self._token.encode()):
            await asyncio.sleep(0.3)  # slow down token guessing
            raise web.HTTPNotFound()
        response = await handler(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    async def _redirect(self, request: web.Request) -> web.Response:
        raise web.HTTPFound(f"/{self._token}/")

    async def _page(self, request: web.Request) -> web.Response:
        html = (_WEB_DIR / "index.html").read_text(encoding="utf-8")
        return web.Response(text=html, content_type="text/html", charset="utf-8")

    async def _state(self, request: web.Request) -> web.Response:
        public = []
        for item in self.items():
            entry = {k: item[k] for k in ("id", "kind", "name", "size")}
            if item["kind"] == "text":
                entry["text"] = item["text"]
            public.append(entry)
        stable = self.url(self.stable_host) if self.stable_host else ""
        return web.json_response(
            {"host": socket.gethostname(), "items": public, "stable": stable}
        )

    async def _upload(self, request: web.Request) -> web.Response:
        if not request.content_type.startswith("multipart/"):
            raise web.HTTPBadRequest(text="multipart expected")
        directory = self._receive_dir()
        directory.mkdir(parents=True, exist_ok=True)
        reader = await request.multipart()
        saved = []
        while True:
            part = await reader.next()
            if part is None:
                break
            if not part.filename:
                await part.release()
                continue
            target = unique_path(directory, safe_name(part.filename))
            temp = target.with_name(target.name + ".part")
            size = 0
            try:
                with temp.open("wb") as handle:
                    while True:
                        chunk = await part.read_chunk(256 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        handle.write(chunk)
                temp.replace(target)
            except BaseException:
                temp.unlink(missing_ok=True)  # do not leave a half-finished transfer behind
                raise
            info = {"name": target.name, "path": str(target), "size": size}
            saved.append(info)
            _LOG.info("File received: %s (%d bytes)", target.name, size)
            self._emit("received", info)
        return web.json_response({"saved": [s["name"] for s in saved]})

    async def _text(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            text = str(data.get("text", ""))[:_MAX_TEXT].strip()
        except Exception as exc:
            raise web.HTTPBadRequest(text="invalid request") from exc
        if not text:
            raise web.HTTPBadRequest(text="empty text")
        self._emit("text", {"text": text})
        return web.json_response({"ok": True})

    async def _download(self, request: web.Request) -> web.StreamResponse:
        item = self._items.get(request.match_info["item"])
        if item is None or item["kind"] != "file":
            raise web.HTTPNotFound()
        path = Path(item["path"])
        if not path.is_file():
            self._items.pop(item["id"], None)
            raise web.HTTPNotFound()
        ascii_name = re.sub(r'[^A-Za-z0-9._-]', "_", item["name"]) or "file"
        return web.FileResponse(
            path,
            headers={
                "Content-Disposition": (
                    f"attachment; filename=\"{ascii_name}\"; "
                    f"filename*=UTF-8''{quote(item['name'])}"
                )
            },
        )
