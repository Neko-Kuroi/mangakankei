"""段階1（レート制限キーの分離）の検証。

閲覧側経由の認証が全読者でプラットフォームの IP 枠を分け合う問題への対処:
- 内部リクエスト（localhost＋内部トークン）のときだけ、閲覧側が付けた
  不透明なクライアントキー（X-Viewer-Client-Key、HMAC hex 64文字）を
  レート制限のキーにする。生IPはプラットフォームに届かない。
- 閲覧側はキーを読者の実IPから HMAC-SHA256 で計算し、秘密は閲覧側だけが持つ。
"""
import hashlib
import hmac
import os
import stat

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

TOKEN = "ratelimit-test-token"
KEY_A = "a" * 64
KEY_B = "b" * 64


@pytest.fixture(autouse=True)
def _env(platform, monkeypatch):
    monkeypatch.setenv("MANGA_INTERNAL_TOKEN", TOKEN)
    platform.limiter.reset()
    platform._login_failures.clear()
    yield
    platform.limiter.reset()
    platform._login_failures.clear()


class _LocalhostApp:
    """ASGI ラッパ: 接続元を 127.0.0.1 に見せる（TestClient の既定は testclient で
    内部判定を通らないため。実運用の同一マシン構成を再現する）。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            scope = dict(scope, client=("127.0.0.1", 50000))
        await self.app(scope, receive, send)


@pytest.fixture()
def internal_client(platform):
    return TestClient(_LocalhostApp(platform.app),
                      raise_server_exceptions=False)


def _internal(key=None):
    headers = {"X-Internal-Token": TOKEN}
    if key:
        headers["X-Viewer-Client-Key"] = key
    return headers


def _signup(client, n, headers):
    return client.post("/api/auth/signup", json={
        "username": f"rk_{n}", "email": f"rk_{n}@example.com",
        "password": "password123", "account_type": "reader",
    }, headers=headers)


# ---------------------------------------------------------------------------
# プラットフォーム側: 枠の分離
# ---------------------------------------------------------------------------

def test_signup_buckets_separated_by_key(internal_client):
    for i in range(3):
        assert _signup(internal_client, f"a{i}", _internal(KEY_A)).status_code == 200
    # 同じキーの4人目は詰まる
    assert _signup(internal_client, "a3", _internal(KEY_A)).status_code == 429
    # 違うキーなら別の枠で通る
    assert _signup(internal_client, "b0", _internal(KEY_B)).status_code == 200


def test_signup_shared_bucket_without_key(internal_client):
    for i in range(3):
        assert _signup(internal_client, f"n{i}", _internal()).status_code == 200
    assert _signup(internal_client, "n3", _internal()).status_code == 429


def test_key_header_without_token_is_ignored(platform_client):
    # 内部トークンが無ければクライアントキーは信頼されない（偽装で枠を逃げられない）
    for i in range(3):
        r = _signup(platform_client, f"f{i}", {"X-Viewer-Client-Key": KEY_A})
        assert r.status_code == 200
    r = _signup(platform_client, "f3", {"X-Viewer-Client-Key": KEY_B})
    assert r.status_code == 429


def test_malformed_key_is_ignored(internal_client):
    bad = _internal("not-a-valid-key")
    for i in range(3):
        assert _signup(internal_client, f"m{i}", bad).status_code == 200
    assert _signup(internal_client, "m3", bad).status_code == 429


def test_login_buckets_separated_by_key(internal_client, make_user):
    make_user("rk_login")
    make_user("rk_login_other")
    for _ in range(5):
        r = internal_client.post(
            "/api/auth/token",
            data={"username": "rk_login", "password": "wrong-password"},
            headers=_internal(KEY_A))
        assert r.status_code == 401
    # 同じキーでは正しい資格情報でも詰まる
    r = internal_client.post(
        "/api/auth/token",
        data={"username": "rk_login", "password": "password123"},
        headers=_internal(KEY_A))
    assert r.status_code == 429
    # 違うキー・違うユーザー（別の読者）なら巻き込まれない
    # （同一ユーザーがキーだけ変えた場合は段階2のユーザー名枠で止まる。
    #  そちらは test_ratelimit_key2.py で検証する）
    r = internal_client.post(
        "/api/auth/token",
        data={"username": "rk_login_other", "password": "password123"},
        headers=_internal(KEY_B))
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# プラットフォーム側: キー決定関数の単体確認
# ---------------------------------------------------------------------------

def _fake_request(host, headers):
    scope = {
        "type": "http", "method": "GET", "path": "/",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": (host, 12345), "server": ("x", 80),
        "scheme": "http", "query_string": b"",
    }
    return Request(scope)


def test_get_ip_for_limit_branches(platform):
    assert platform.get_ip_for_limit(
        _fake_request("127.0.0.1", _internal(KEY_A))) == "viewer:" + KEY_A
    assert platform.get_ip_for_limit(
        _fake_request("127.0.0.1", _internal())) == "127.0.0.1"
    # 内部トークンがあっても localhost 以外からは信頼しない
    assert platform.get_ip_for_limit(
        _fake_request("203.0.113.9", _internal(KEY_A))) == "203.0.113.9"


# ---------------------------------------------------------------------------
# 閲覧側: キー計算と秘密の解決
# ---------------------------------------------------------------------------

def _viewer_request(viewer, host, headers=None):
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http", "method": "POST", "path": "/auth/login",
        "headers": raw, "client": (host, 12345), "server": ("x", 8001),
        "scheme": "http", "query_string": b"",
    }
    return Request(scope)


@pytest.fixture()
def viewer_secret(viewer, monkeypatch):
    monkeypatch.setattr(viewer, "_client_key_secret_cache", None)
    return viewer


def test_client_key_is_hmac_of_ip(viewer_secret, monkeypatch):
    monkeypatch.setenv("MANGA_CLIENT_KEY_SECRET", "unit-test-secret")
    req = _viewer_request(viewer_secret, "203.0.113.7")
    expected = hmac.new(b"unit-test-secret", b"203.0.113.7",
                        hashlib.sha256).hexdigest()
    assert viewer_secret._client_key_for(req) == expected


def test_client_key_uses_cf_ip_when_trusted(viewer_secret, monkeypatch):
    monkeypatch.setenv("MANGA_CLIENT_KEY_SECRET", "unit-test-secret")
    monkeypatch.setenv("MANGA_TRUST_CF_HEADER", "1")
    req = _viewer_request(viewer_secret, "10.0.0.1",
                          {"CF-Connecting-IP": "198.51.100.9"})
    expected = hmac.new(b"unit-test-secret", b"198.51.100.9",
                        hashlib.sha256).hexdigest()
    assert viewer_secret._client_key_for(req) == expected


def test_auth_proxy_headers_carry_key(viewer_secret, monkeypatch):
    monkeypatch.setenv("MANGA_CLIENT_KEY_SECRET", "unit-test-secret")
    monkeypatch.setenv("MANGA_INTERNAL_TOKEN", TOKEN)
    req = _viewer_request(viewer_secret, "203.0.113.7")
    headers = viewer_secret._auth_proxy_headers(req)
    assert headers["X-Internal-Token"] == TOKEN
    assert headers["X-Viewer-Client-Key"] == hmac.new(
        b"unit-test-secret", b"203.0.113.7", hashlib.sha256).hexdigest()


def test_secret_file_generated_with_0600(viewer_secret, monkeypatch, tmp_path):
    monkeypatch.delenv("MANGA_CLIENT_KEY_SECRET", raising=False)
    target = tmp_path / "instance" / "client_key_secret"
    monkeypatch.setattr(viewer_secret, "_client_key_secret_file",
                        lambda: str(target))
    value = viewer_secret._client_key_secret()
    assert len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    assert target.read_text().strip() == value
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
    # 2回目はファイルの値を返す（再生成しない）
    monkeypatch.setattr(viewer_secret, "_client_key_secret_cache", None)
    assert viewer_secret._client_key_secret() == value


def test_secret_env_takes_priority(viewer_secret, monkeypatch, tmp_path):
    target = tmp_path / "client_key_secret"
    target.write_text("file-value")
    monkeypatch.setattr(viewer_secret, "_client_key_secret_file",
                        lambda: str(target))
    monkeypatch.setenv("MANGA_CLIENT_KEY_SECRET", "env-value")
    assert viewer_secret._client_key_secret() == "env-value"


def test_login_endpoint_passes_request_to_proxy(viewer, viewer_client, monkeypatch):
    captured = {}

    async def fake_login(username, password, request=None):
        captured["request"] = request
        return "token", {"id": 1, "username": username}

    monkeypatch.setattr(viewer, "_platform_login", fake_login)
    r = viewer_client.post("/auth/login",
                           data={"username": "rk_ep", "password": "password123"})
    assert r.status_code == 200
    assert captured["request"] is not None
