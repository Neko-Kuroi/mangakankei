"""問題3C の検証: 作者の webhook_enabled を /discord/send がサーバー側でも確認する。

- プラットフォーム作品で作者が不許可なら、直接叩いても送信されない
- カタログに無いプラットフォーム作品も不許可（リーダー側の判定と一致）
- 作者が許可していれば送信できる
- 外部URLはカタログ確認なしで送信できる
"""
import hashlib
import os

import pytest

PLAT = "http://localhost:8000/api/public/cbz/1/2/3.cbz"
EXT = "https://example.com/book.cbz"
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000001e221bc330000000049454e44ae426082")


@pytest.fixture(autouse=True)
def _reset_limits(viewer):
    viewer.limiter.reset()


@pytest.fixture()
def send_ready(viewer_client, manga):
    """セッション発行＋展開済みファイルを置き、トークンを返す。"""
    def _prepare(url):
        r = viewer_client.get("/session", params={"url": url})
        assert r.status_code == 200
        url_hash = hashlib.md5(url.encode()).hexdigest()
        folder = os.path.join(manga.get_cache_dir(), f"{url_hash}_extracted")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "a.png"), "wb") as f:
            f.write(PNG)
        return r.json()["session_token"]
    return _prepare


@pytest.fixture()
def post_calls(manga, monkeypatch):
    calls = []

    class _Resp:
        def raise_for_status(self):
            pass

    def fake_post(url, **kwargs):
        calls.append(url)
        return _Resp()

    monkeypatch.setattr(manga.requests, "post", fake_post)
    return calls


def _set_catalog(monkeypatch, viewer, items):
    async def fake_catalog(timeout=5.0):
        return items
    monkeypatch.setattr(viewer, "_fetch_platform_catalog", fake_catalog)


def _send(client, url, token):
    return client.post("/discord/send", data={
        "url": url, "filename": "a.png",
        "webhook1": "https://discord.com/api/webhooks/1/x",
        "session_token": token,
    })


def test_author_disabled_is_rejected(viewer_client, viewer, send_ready, post_calls, monkeypatch):
    token = send_ready(PLAT)
    _set_catalog(monkeypatch, viewer, [{"cbz_url": PLAT, "webhook_enabled": False}])
    r = _send(viewer_client, PLAT, token)
    assert "許可されていません" in r.text
    assert post_calls == []


def test_platform_work_not_in_catalog_is_rejected(viewer_client, viewer, send_ready, post_calls, monkeypatch):
    token = send_ready(PLAT)
    _set_catalog(monkeypatch, viewer, [])
    r = _send(viewer_client, PLAT, token)
    assert "許可されていません" in r.text
    assert post_calls == []


def test_author_enabled_sends(viewer_client, viewer, send_ready, post_calls, monkeypatch):
    token = send_ready(PLAT)
    _set_catalog(monkeypatch, viewer, [{"cbz_url": PLAT, "webhook_enabled": True}])
    r = _send(viewer_client, PLAT, token)
    assert "送信しました" in r.text
    assert len(post_calls) == 1


def test_external_url_needs_no_permission(viewer_client, viewer, send_ready, post_calls, monkeypatch):
    token = send_ready(EXT)
    _set_catalog(monkeypatch, viewer, [])
    r = _send(viewer_client, EXT, token)
    assert "送信しました" in r.text
    assert len(post_calls) == 1
