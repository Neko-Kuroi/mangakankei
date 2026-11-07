"""レート制限キー分離 段階3: プラットフォームの 429 を閲覧側が正しく案内する。

プラットフォームの 429（slowapi のプレーンテキスト、および段階2の
ユーザー名カウンタの JSON）は、閲覧側では従来「パスワードが違う」
「登録に失敗しました」に化けていた。429 のときは混雑の案内を返すことを、
実プラットフォームを別スレッドで起動して HTTP 越しの実フローで確認する
（test_access_phase3 と同方式）。
"""
import socket
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

MESSAGE = "混み合っています。少し待ってから再試行してください"
PASSWORD_MESSAGE = "ユーザー名またはパスワードが正しくありません"


@pytest.fixture(autouse=True)
def _reset(platform, viewer):
    platform.limiter.reset()
    platform._login_failures.clear()
    viewer._auth_store.clear()
    yield
    platform.limiter.reset()
    platform._login_failures.clear()
    viewer._auth_store.clear()


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def live_platform(platform):
    import uvicorn
    # この検証環境には外向きプロキシの環境変数があり、httpx/requests が
    # localhost 宛てでも掴んで壊れるため、テスト中だけ外す（本番コードは不変）。
    import os
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        os.environ.pop(var, None)
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    config = uvicorn.Config(platform.app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            if httpx.get(f"{base}/api/public/catalog", timeout=1.0).status_code == 200:
                break
        except Exception:
            time.sleep(0.1)
    else:
        raise RuntimeError("platform server did not start")
    yield base
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture()
def env(viewer, live_platform, monkeypatch):
    monkeypatch.setattr(viewer, "PLATFORM_BASE_URL", live_platform)
    monkeypatch.setenv("PLATFORM_BASE_URL", live_platform)
    return live_platform


@pytest.fixture()
def vclient(viewer):
    # with で入ると lifespan の終了処理が共有 executor を止めてしまうため、
    # コンテキストなしで使う（TestClient は未入場でもリクエストできる）
    yield TestClient(viewer.app)


def _make_user(platform, username, account_type="reader"):
    import bcrypt as _bcrypt
    db = platform.SessionLocal()
    u = platform.User(
        username=username, email=f"{username}@example.com",
        hashed_password=_bcrypt.hashpw(b"password123", _bcrypt.gensalt()).decode(),
        is_active=True, account_type=account_type)
    db.add(u)
    db.commit()
    db.refresh(u)
    uid = u.id
    db.close()
    return uid


def test_login_wrong_password_keeps_password_message(env, vclient, platform):
    _make_user(platform, "rk3_wrong_user")
    r = vclient.post("/auth/login",
                     data={"username": "rk3_wrong_user", "password": "wrongpass999"})
    assert r.status_code == 401
    assert r.json()["detail"] == PASSWORD_MESSAGE


def test_login_rate_limited_shows_congestion_message(env, vclient, platform):
    _make_user(platform, "rk3_login_user")
    # 同じクライアントキーからの失敗を上限（5回/分）まで重ねる
    for _ in range(5):
        r = vclient.post("/auth/login",
                         data={"username": "rk3_login_user",
                               "password": "wrongpass999"})
        assert r.status_code == 401
        assert r.json()["detail"] == PASSWORD_MESSAGE
    # 枠が尽きた状態では、正しい資格情報でも 429 として混雑の案内になる
    # （従来は 401「パスワードが違う」に化けていた）
    r = vclient.post("/auth/login",
                     data={"username": "rk3_login_user", "password": "password123"})
    assert r.status_code == 429
    assert r.json()["detail"] == MESSAGE


def test_register_rate_limited_shows_congestion_message(env, vclient):
    # 登録は 3回/時（クライアントキー単位）。3人目までは通る
    for name in ("rk3_reg_a", "rk3_reg_b", "rk3_reg_c"):
        r = vclient.post("/auth/register",
                         data={"username": name, "email": f"{name}@example.com",
                               "password": "password123"})
        assert r.status_code == 200
    # 4人目はプラットフォームが 429 を返し、閲覧側は混雑の案内にする
    # （従来は 400「登録に失敗しました」に化けていた）
    r = vclient.post("/auth/register",
                     data={"username": "rk3_reg_d",
                           "email": "rk3_reg_d@example.com",
                           "password": "password123"})
    assert r.status_code == 429
    assert r.json()["detail"] == MESSAGE


def test_register_duplicate_keeps_platform_detail(env, vclient):
    r = vclient.post("/auth/register",
                     data={"username": "rk3_dup_user",
                           "email": "rk3_dup_user@example.com",
                           "password": "password123"})
    assert r.status_code == 200
    # 429 以外の失敗は従来どおりプラットフォームの detail をそのまま出す
    r = vclient.post("/auth/register",
                     data={"username": "rk3_dup_user",
                           "email": "rk3_dup_other@example.com",
                           "password": "password123"})
    assert r.status_code == 400
    assert r.json()["detail"] == "使用済みのユーザー名です"
