import asyncio
import json
from pathlib import Path

import aiohttp

from airpy import __version__
from airpy.localapi import LocalApi

EXTENSION = Path(__file__).resolve().parent.parent / "extension"


def test_local_api_is_reachable_only_the_intended_way():
    async def main():
        api = LocalApi(lambda: {"app": "AirPy", "streaming": True, "latency_ms": 255})
        await api.start()
        try:
            base = f"http://127.0.0.1:{api.port}"
            async with aiohttp.ClientSession() as session:
                ok = await session.get(base + "/airpy/sync")
                assert ok.status == 200
                assert (await ok.json())["latency_ms"] == 255
                assert "Access-Control-Allow-Origin" not in ok.headers  # web pages cannot read the response

                # the browser extension (chrome-extension://) is accepted
                ext = await session.get(base + "/airpy/sync", headers={"Origin": "chrome-extension://abc"})
                assert ext.status == 200
                # any web page is rejected
                evil = await session.get(base + "/airpy/sync", headers={"Origin": "https://evil.example"})
                assert evil.status == 404
                # DNS rebinding: the Host header does not match
                rebind = await session.get(base + "/airpy/sync", headers={"Host": f"evil.example:{api.port}"})
                assert rebind.status == 404

                page = await session.get(base + "/airpy/calibrate")
                assert page.status == 200
                assert "<video" in await page.text()
                assert (await session.get(base + "/")).status == 404
        finally:
            await api.stop()

    asyncio.run(main())


def test_extension_manifest_is_consistent():
    manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 3
    assert manifest["version"] == __version__
    assert manifest["host_permissions"] == ["http://127.0.0.1/*"]  # asks for no other site
    assert not {"tabs", "history", "cookies", "webRequest"} & set(manifest["permissions"])

    files = {manifest["background"]["service_worker"], manifest["action"]["default_popup"]}
    files |= {script for entry in manifest["content_scripts"] for script in entry["js"]}
    files |= set(manifest["icons"].values())
    files |= set(manifest["action"]["default_icon"].values())
    for name in files:
        assert (EXTENSION / name).is_file(), name


def test_popup_references_existing_files():
    html = (EXTENSION / "popup.html").read_text(encoding="utf-8")
    for name in ("popup.css", "popup.js"):
        assert name in html
        assert (EXTENSION / name).is_file()
