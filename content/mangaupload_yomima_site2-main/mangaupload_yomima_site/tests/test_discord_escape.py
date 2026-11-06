"""/discord/send のエラー文のエスケープ検証（viewer 側）。

エラー文には例外メッセージ（送信先URLなど入力由来の文字列）が入るので、
HTML としてそのまま返さない。
"""
import hashlib

import pytest

PLAT = "http://localhost:8000/api/public/cbz/1/2/3.cbz"
PAYLOAD = "<script>alert(1)</script>"
# 1x1 PNG
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000001e221bc330000000049454e44ae426082")


@pytest.fixture(autouse=True)
def _reset_limits(viewer):
    viewer.limiter.reset()


@pytest.fixture()
def session_and_file(viewer_client, manga, viewer, monkeypatch):
    # 問題3Cのサーバー側確認を通すため、この作品は作者が送信を許可していることにする
    async def fake_catalog(timeout=5.0):
        return [{"cbz_url": PLAT, "webhook_enabled": True}]
    monkeypatch.setattr(viewer, "_fetch_platform_catalog", fake_catalog)
    r = viewer_client.get("/session", params={"url": PLAT})
    assert r.status_code == 200
    import os
    url_hash = hashlib.md5(PLAT.encode()).hexdigest()
    folder = os.path.join(manga.get_cache_dir(), f"{url_hash}_extracted")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "a.png"), "wb") as f:
        f.write(PNG)
    return r.json()["session_token"]


def test_error_text_is_html_escaped(viewer_client, manga, session_and_file, monkeypatch):
    def boom(*args, **kwargs):
        raise manga.requests.exceptions.ConnectionError(PAYLOAD)
    monkeypatch.setattr(manga.requests, "post", boom)

    r = viewer_client.post("/discord/send", data={
        "url": PLAT, "filename": "a.png",
        "webhook1": "https://discord.com/api/webhooks/1/x",
        "session_token": session_and_file,
    })
    assert r.status_code == 200
    assert PAYLOAD not in r.text
    assert "&lt;script&gt;" in r.text


def test_multiple_errors_keep_br_separator(viewer_client, manga, session_and_file, monkeypatch):
    def boom(*args, **kwargs):
        raise manga.requests.exceptions.ConnectionError("fail")
    monkeypatch.setattr(manga.requests, "post", boom)

    r = viewer_client.post("/discord/send", data={
        "url": PLAT, "filename": "a.png",
        "webhook1": "https://discord.com/api/webhooks/1/x",
        "webhook2": "https://discord.com/api/webhooks/2/y",
        "session_token": session_and_file,
    })
    assert "<br>" in r.text
