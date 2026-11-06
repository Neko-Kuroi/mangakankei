"""問題13-A の検証: パスワード規則、sub の形式不正、is_active。

- パスワードは8文字未満と UTF-8 で72バイト超を登録時に拒否する
- トークンの sub が数値でない・空・欠落の場合は 500 でなく 401
- is_active=False のユーザーはログインもトークン検証も通らない
"""
import pytest


@pytest.fixture(autouse=True)
def _reset_limits(platform):
    platform.limiter.reset()


def signup(client, username, password):
    return client.post("/api/auth/signup", json={
        "username": username,
        "email": f"{username}@example.com",
        "password": password,
    })


def login(client, username, password="password123"):
    return client.post("/api/auth/token", data={"username": username, "password": password})


def works(client, token):
    return client.get("/api/author/works", headers={"Authorization": f"Bearer {token}"})


# ---------------------------------------------------------------------------
# パスワード規則
# ---------------------------------------------------------------------------

def test_signup_rejects_short_password(platform_client):
    r = signup(platform_client, "shorty", "a" * 7)
    assert r.status_code == 400
    assert "8文字以上" in r.json()["detail"]


def test_signup_rejects_over_72_bytes(platform_client):
    r = signup(platform_client, "longpw", "a" * 73)
    assert r.status_code == 400
    assert "72バイト" in r.json()["detail"]


def test_signup_rejects_multibyte_over_72_bytes(platform_client):
    r = signup(platform_client, "mb25", "あ" * 25)  # 25文字=75バイト
    assert r.status_code == 400


def test_signup_accepts_exactly_72_bytes(platform_client):
    r = signup(platform_client, "mb24", "あ" * 24)  # 24文字=72バイト
    assert r.status_code == 200
    assert login(platform_client, "mb24", "あ" * 24).status_code == 200


def test_signup_and_login_ok(platform_client):
    assert signup(platform_client, "okuser", "a" * 8).status_code == 200
    r = login(platform_client, "okuser", "a" * 8)
    assert r.status_code == 200
    assert r.json()["access_token"]


# ---------------------------------------------------------------------------
# is_active
# ---------------------------------------------------------------------------

def test_inactive_user_cannot_login(platform_client, make_user):
    make_user("dis", is_active=False)
    assert login(platform_client, "dis").status_code == 401


def test_inactive_user_token_rejected(platform_client, make_user, forge_token):
    uid = make_user("dis2", is_active=False)
    assert works(platform_client, forge_token(sub=str(uid))).status_code == 401


# ---------------------------------------------------------------------------
# sub の形式不正（旧実装では 500 だった）
# ---------------------------------------------------------------------------

def test_non_numeric_sub_is_401(platform_client, forge_token):
    assert works(platform_client, forge_token(sub="abc")).status_code == 401


def test_empty_sub_is_401(platform_client, forge_token):
    assert works(platform_client, forge_token(sub="")).status_code == 401


def test_missing_sub_is_401(platform_client, forge_token):
    assert works(platform_client, forge_token()).status_code == 401


def test_unknown_user_is_401(platform_client, forge_token):
    assert works(platform_client, forge_token(sub="99999")).status_code == 401


def test_valid_token_works(platform_client, make_user):
    make_user("act")
    token = login(platform_client, "act").json()["access_token"]
    assert works(platform_client, token).status_code == 200


# ---------------------------------------------------------------------------
# 手書きデコードだった3か所（preview / thumb / cover）
# ---------------------------------------------------------------------------

def test_preview_rejects_bad_sub(platform_client, forge_token):
    r = platform_client.get("/api/author/preview/1/1.cbz",
                            params={"token": forge_token(sub="abc")})
    assert r.status_code == 401


def test_preview_valid_token_passes_auth(platform_client, forge_token, make_user):
    uid = make_user("prev")
    r = platform_client.get("/api/author/preview/1/1.cbz",
                            params={"token": forge_token(sub=str(uid))})
    assert r.status_code == 404  # 認証は通過し、ファイルが無いだけ


def test_thumb_rejects_inactive_user(platform_client, forge_token, make_user):
    uid = make_user("dis3", is_active=False)
    r = platform_client.get("/api/author/thumb/1/1/0",
                            params={"token": forge_token(sub=str(uid))})
    assert r.status_code == 401


def test_thumb_valid_token_passes_auth(platform_client, forge_token, make_user):
    uid = make_user("th")
    r = platform_client.get("/api/author/thumb/1/1/0",
                            params={"token": forge_token(sub=str(uid))})
    assert r.status_code == 404


def test_cover_rejects_bad_sub(platform_client, forge_token):
    r = platform_client.get("/api/author/cover/1/1",
                            params={"token": forge_token(sub="abc")})
    assert r.status_code == 401
