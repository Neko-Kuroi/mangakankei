"""問題2・問題4 の検証: セッション必須化と SSRF 対策。

- /images・/thumbnail・/discord/send・/download/start はトークン必須
- URL は http/https のみ、内部アドレスは原則拒否
  （プラットフォームのオリジンと環境変数の許可リストは例外）
- リダイレクトはホップごとに宛先を再検証する
- セッションの IP 照合は環境変数 MANGA_SESSION_BIND_IP=1 のときだけ
"""
import io
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

PLAT = "http://localhost:8000/api/public/cbz/1/2/3.cbz"


@pytest.fixture(autouse=True)
def _reset_limits(viewer):
    viewer.limiter.reset()


@pytest.fixture()
def token(viewer_client):
    r = viewer_client.get("/session", params={"url": PLAT})
    assert r.status_code == 200
    return r.json()["session_token"]


# ---------------------------------------------------------------------------
# 宛先ポリシー（単体）
# ---------------------------------------------------------------------------

def test_policy_allows_platform_url(manga):
    assert manga.is_url_fetch_allowed(PLAT)[0]


def test_policy_rejects_loopback(manga):
    assert not manga.is_url_fetch_allowed("http://127.0.0.1:9999/x.cbz")[0]


def test_policy_rejects_link_local(manga):
    assert not manga.is_url_fetch_allowed("http://169.254.169.254/x.cbz")[0]


def test_policy_rejects_rfc1918(manga):
    assert not manga.is_url_fetch_allowed("http://192.168.1.5/book.cbz")[0]


def test_policy_rejects_fake_platform_host(manga):
    assert not manga.is_url_fetch_allowed("http://localhost:8000.evil.example/x.cbz")[0]


def test_policy_rejects_file_scheme(manga):
    assert not manga.validate_manga_url("file:///etc/passwd.zip")


def test_policy_allows_public_host(manga):
    assert manga.is_url_fetch_allowed("https://example.com/book.cbz")[0]


def test_policy_env_allowlist(manga, monkeypatch):
    monkeypatch.setenv("MANGA_URL_ALLOW_HOSTS", "192.168.1.5")
    assert manga.is_url_fetch_allowed("http://192.168.1.5/book.cbz")[0]


# ---------------------------------------------------------------------------
# リダイレクトの再検証
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def jump_server():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("1.jpg", b"\xff\xd8\xff")
    payload = buf.getvalue()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/jump-internal.cbz":
                self.send_response(302)
                self.send_header("Location", "http://169.254.169.254/x.cbz")
                self.end_headers()
            elif self.path == "/jump-ok.cbz":
                self.send_response(302)
                self.send_header("Location", "/real.cbz")
                self.end_headers()
            elif self.path == "/real.cbz":
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 8901), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


def test_redirect_to_internal_is_rejected(manga, jump_server, monkeypatch, tmp_path):
    monkeypatch.setenv("MANGA_URL_ALLOW_HOSTS", "127.0.0.1")
    ok, err = manga.download_file(
        "http://127.0.0.1:8901/jump-internal.cbz", str(tmp_path / "a.cbz"))
    assert not ok
    assert "内部" in err


def test_redirect_to_public_path_succeeds(manga, jump_server, monkeypatch, tmp_path):
    monkeypatch.setenv("MANGA_URL_ALLOW_HOSTS", "127.0.0.1")
    target = tmp_path / "b.cbz"
    ok, err = manga.download_file(
        "http://127.0.0.1:8901/jump-ok.cbz", str(target))
    assert ok, err
    assert target.exists()


# ---------------------------------------------------------------------------
# セッション必須化（HTTP）
# ---------------------------------------------------------------------------

def test_session_rejects_internal_url(viewer_client):
    r = viewer_client.get("/session", params={"url": "http://127.0.0.1:9999/x.cbz"})
    assert r.status_code == 400


def test_images_requires_token(viewer_client):
    r = viewer_client.get("/images", params={"url": PLAT})
    assert r.status_code == 403


def test_images_rejects_other_works_token(viewer_client, token):
    r = viewer_client.get("/session", params={
        "url": "http://localhost:8000/api/public/cbz/9/9/9.cbz"})
    other = r.json()["session_token"]
    r = viewer_client.get("/images", params={"url": PLAT, "session_token": other})
    assert r.status_code == 403


def test_images_accepts_valid_token(viewer_client, token):
    r = viewer_client.get("/images", params={"url": PLAT, "session_token": token})
    assert r.status_code == 200


def test_thumbnail_requires_token(viewer_client):
    r = viewer_client.get("/thumbnail", params={"url": PLAT, "index": 0})
    assert r.status_code == 403


def test_thumbnail_accepts_valid_token(viewer_client, token):
    r = viewer_client.get("/thumbnail", params={
        "url": PLAT, "index": 0, "session_token": token})
    assert r.status_code == 404  # 認証は通過し、未キャッシュなだけ


def test_download_requires_token(viewer_client):
    r = viewer_client.get("/download/start", params={"url": PLAT})
    assert r.status_code == 403


def test_download_accepts_valid_token(viewer_client, token):
    r = viewer_client.get("/download/start", params={
        "url": PLAT, "session_token": token})
    # プラットフォーム未起動のため error イベントが返る（=認証とポリシーは通過）
    assert r.status_code == 200
    assert "error:" in r.text


def test_discord_send_requires_token(viewer_client):
    r = viewer_client.post("/discord/send", data={
        "url": PLAT, "filename": "1.jpg",
        "webhook1": "https://discord.com/api/webhooks/1/a"})
    assert "セッションが無効です" in r.text


def test_reader_allows_platform_url(viewer_client):
    r = viewer_client.get("/reader", params={"url": PLAT})
    assert r.status_code == 200


def test_reader_rejects_internal_url(viewer_client):
    r = viewer_client.get("/reader", params={"url": "http://127.0.0.1:9/x.cbz"})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# セッションの IP 照合（環境変数で有効化したときだけ）
# ---------------------------------------------------------------------------

def test_session_ip_binding_off_by_default(manga):
    tok = manga.create_session(PLAT, "10.0.0.1")
    assert manga.validate_session(tok, "10.0.0.2") is not None


def test_session_ip_binding_when_enabled(manga, monkeypatch):
    tok = manga.create_session(PLAT, "10.0.0.1")
    monkeypatch.setenv("MANGA_SESSION_BIND_IP", "1")
    assert manga.validate_session(tok, "10.0.0.2") is None
    assert manga.validate_session(tok, "10.0.0.1") is not None
