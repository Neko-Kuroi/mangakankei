"""段階2（ユーザー名単位の二重化）の検証。

ログインには IP/クライアントキー単位の制限に加えて、対象ユーザー名
単位の失敗カウンタ（5回/60秒）もかかる。送信元キーを次々に変えても
（閲覧側の秘密が差し替わってクライアントキーが全交換されても）、
同一アカウントへの失敗は数え続けられることを確認する。

実装メモ: slowapi の key_func は非同期関数に対応していない（コルーチン
オブジェクトがそのままキー文字列になり枠が貯まらないことを実測で確認）
ため、ユーザー名のカウントはハンドラ側で行う。成功でリセットされる。
"""
import time
from collections import deque

import pytest
from fastapi.testclient import TestClient

TOKEN = "ratelimit2-test-token"


@pytest.fixture(autouse=True)
def _env(platform, monkeypatch):
    monkeypatch.setenv("MANGA_INTERNAL_TOKEN", TOKEN)
    platform.limiter.reset()
    platform._login_failures.clear()
    yield
    platform.limiter.reset()
    platform._login_failures.clear()


class _LocalhostApp:
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


def _key(n):
    return format(n, "x").rjust(64, "0")


def _login(client, username, password, key=None):
    headers = {"X-Internal-Token": TOKEN}
    if key is not None:
        headers["X-Viewer-Client-Key"] = key
    return client.post("/api/auth/token",
                       data={"username": username, "password": password},
                       headers=headers)


def test_username_bucket_survives_client_key_rotation(internal_client, make_user):
    make_user("rk2_victim")
    # 送信元キーを毎回変えながら5回失敗する（IP側の枠は毎回新しい）
    for i in range(5):
        r = _login(internal_client, "rk2_victim", "wrong-password", key=_key(i + 1))
        assert r.status_code == 401
    # 新しいキーから正しい資格情報でも、ユーザー名の枠で止まる
    r = _login(internal_client, "rk2_victim", "password123", key=_key(99))
    assert r.status_code == 429
    # キーなし（直接相当）でも止まる
    r = _login(internal_client, "rk2_victim", "password123")
    assert r.status_code == 429


def test_username_bucket_is_per_user(internal_client, make_user):
    make_user("rk2_victim2")
    make_user("rk2_other")
    for i in range(5):
        r = _login(internal_client, "rk2_victim2", "wrong-password", key=_key(i + 1))
        assert r.status_code == 401
    # 別のユーザーは巻き込まれない
    r = _login(internal_client, "rk2_other", "password123", key=_key(50))
    assert r.status_code == 200


def test_username_limit_counts_direct_logins(platform_client, make_user):
    make_user("rk2_direct")
    for _ in range(5):
        r = platform_client.post(
            "/api/auth/token",
            data={"username": "rk2_direct", "password": "wrong-password"})
        assert r.status_code == 401
    r = platform_client.post(
        "/api/auth/token",
        data={"username": "rk2_direct", "password": "password123"})
    assert r.status_code == 429


def test_success_clears_failures(internal_client, make_user):
    make_user("rk2_reset")
    for i in range(4):
        r = _login(internal_client, "rk2_reset", "wrong-password", key=_key(i + 1))
        assert r.status_code == 401
    # 成功すると失敗カウントがリセットされる
    r = _login(internal_client, "rk2_reset", "password123", key=_key(10))
    assert r.status_code == 200
    for i in range(4):
        r = _login(internal_client, "rk2_reset", "wrong-password", key=_key(20 + i))
        assert r.status_code == 401
    r = _login(internal_client, "rk2_reset", "password123", key=_key(30))
    assert r.status_code == 200


def test_failure_counter_helpers(platform):
    assert platform._login_user_blocked("rk2_unit") is False
    for _ in range(5):
        platform._record_login_failure("rk2_unit")
    assert platform._login_user_blocked("rk2_unit") is True
    assert platform._login_user_blocked("rk2_someone_else") is False
    platform._clear_login_failures("rk2_unit")
    assert platform._login_user_blocked("rk2_unit") is False
    # ウィンドウを過ぎた失敗は数えない
    old = time.monotonic() - 120
    platform._login_failures["rk2_old"] = deque([old] * 5)
    assert platform._login_user_blocked("rk2_old") is False
