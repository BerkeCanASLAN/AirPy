import asyncio
import json

import aiohttp
import pytest

from airpy.share import ShareServer, safe_name, unique_path


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("photo.jpg", "photo.jpg"),
        ("..\\..\\evil.txt", "evil.txt"),
        ("../../etc/passwd", "passwd"),
        ("a/b/c.txt", "c.txt"),
        ('a<b>:"c|d?.txt', "a_b___c_d_.txt"),
        ("CON.txt", "_CON.txt"),
        ("com1", "_com1"),
        ("file. ", "file"),
        ("...", "file"),
        ("", "file"),
        ("fotoğraf ğüşiöç.png", "fotoğraf ğüşiöç.png"),
    ],
)
def test_safe_name(raw, expected):
    assert safe_name(raw) == expected


def test_safe_name_limits_length_and_keeps_extension():
    name = safe_name("x" * 400 + ".mp4")
    assert len(name) <= 180
    assert name.endswith(".mp4")


def test_unique_path_never_overwrites(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"1")
    (tmp_path / "a (2).txt").write_bytes(b"2")
    assert unique_path(tmp_path, "a.txt").name == "a (3).txt"
    (tmp_path / "noext").write_bytes(b"1")
    assert unique_path(tmp_path, "noext").name == "noext (2)"
    assert unique_path(tmp_path, "free.txt").name == "free.txt"


def run(tmp_path, scenario):
    events = []

    async def main():
        server = ShareServer("secret-token", 54310, lambda: tmp_path / "incoming", lambda e, p: events.append((e, p)))
        await server.start()
        try:
            async with aiohttp.ClientSession() as session:
                await scenario(server, session, f"http://127.0.0.1:{server.port}/secret-token/", events)
        finally:
            await server.stop()

    asyncio.run(main())


def test_requires_the_secret_token(tmp_path):
    async def scenario(server, session, base, events):
        root = base.rsplit("/", 2)[0]
        assert (await session.get(root + "/wrong-token/")).status == 404
        assert (await session.get(root + "/")).status == 404
        assert (await session.post(root + "/wrong-token/api/text", json={"text": "x"})).status == 404
        page = await session.get(base)
        assert page.status == 200
        assert "AirPy" in await page.text()
        assert page.headers["Cache-Control"] == "no-store"

    run(tmp_path, scenario)


def test_upload_is_confined_to_the_receive_folder(tmp_path):
    async def scenario(server, session, base, events):
        # Raw body: browsers send the file name as is (the aiohttp client would encode it)
        boundary = "----airpytest"
        body = b""
        for name, content in (
            ("..\\..\\escape.txt", b"hello"),
            ("big.bin", b"x" * 300_000),
            ("big.bin", b"same"),
        ):
            body += (
                f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{name}"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n"
            ).encode() + content + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        response = await session.post(
            base + "api/upload", data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
        )
        assert response.status == 200
        assert (await response.json())["saved"] == ["escape.txt", "big.bin", "big (2).bin"]

    run(tmp_path, scenario)
    received = tmp_path / "incoming"
    assert sorted(p.name for p in received.iterdir()) == ["big (2).bin", "big.bin", "escape.txt"]
    assert (received / "big.bin").stat().st_size == 300_000
    assert not (tmp_path / "escape.txt").exists()  # nothing was written outside the folder
    assert not list(received.glob("*.part"))


def test_text_endpoint(tmp_path):
    async def scenario(server, session, base, events):
        ok = await session.post(base + "api/text", json={"text": "  merhaba  "})
        assert ok.status == 200
        assert (await session.post(base + "api/text", json={"text": "   "})).status == 400
        assert (await session.post(base + "api/text", data=b"not json")).status == 400
        assert ("text", {"text": "merhaba"}) in events

    run(tmp_path, scenario)


def test_pc_to_phone_items_and_download(tmp_path):
    source = tmp_path / "gönder ö.bin"
    source.write_bytes(bytes(range(256)) * 10)

    async def scenario(server, session, base, events):
        item = server.add_file(str(source))
        assert server.add_file(str(source)) is item  # the same file is not added twice
        assert server.add_file(str(tmp_path / "missing.bin")) is None
        server.add_text("PC metni")
        state = await (await session.get(base + "api/state")).json()
        kinds = sorted(i["kind"] for i in state["items"])
        assert kinds == ["file", "text"]
        assert "path" not in json.dumps(state)  # local paths do not leak to the phone

        download = await session.get(base + "dl/" + item["id"])
        assert download.status == 200
        assert await download.read() == source.read_bytes()
        assert "attachment" in download.headers["Content-Disposition"]
        assert (await session.get(base + "dl/missing")).status == 404

        server.remove(item["id"])
        assert (await session.get(base + "dl/" + item["id"])).status == 404

    run(tmp_path, scenario)
