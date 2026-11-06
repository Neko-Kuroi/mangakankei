"""passlib 脱却の互換性検証。

- passlib の CryptContext が作った既存ハッシュ（$2b$）でログインできる
- 問題13-A 以前に登録できた72バイト超のパスワードが移行後も通る
  （passlib は先頭72バイトで切り詰めてハッシュ化していた）
- プラットフォームが passlib を読み込まない

フィクスチャのハッシュは移行前の実装（passlib 1.7.4 + bcrypt 4.0.1 の
CryptContext(schemes=["bcrypt"])）で生成した実物。
"""
import sys

import pytest

LEGACY_HASH = "$2b$12$7zwR/NxPVbx5SdEOh5EfzOgMEtgIlFoZE0x1RpNuAe8DEzIt7dw0a"  # "legacy-pass-123"
LEGACY_LONG_HASH = "$2b$12$iVXd8CZQdvH9cX.Q06jlzenSm7OHDm8NmoQiMat3t2G75ThKLb2jm"  # "a" * 80


@pytest.fixture(autouse=True)
def _reset_limits(platform):
    platform.limiter.reset()


def _insert_user(platform, username, hashed):
    db = platform.SessionLocal()
    try:
        db.add(platform.User(
            username=username,
            email=f"{username}@example.com",
            hashed_password=hashed,
            is_active=True,
        ))
        db.commit()
    finally:
        db.close()


def _login(client, username, password):
    return client.post("/api/auth/token", data={"username": username, "password": password})


def test_platform_no_longer_uses_passlib(platform):
    assert "passlib" not in sys.modules
    assert not hasattr(platform, "pwd_context")
    assert platform._hash_password("x" * 8).startswith("$2b$")


def test_legacy_passlib_hash_can_login(platform, platform_client):
    _insert_user(platform, "legacy_user", LEGACY_HASH)
    r = _login(platform_client, "legacy_user", "legacy-pass-123")
    assert r.status_code == 200
    r = _login(platform_client, "legacy_user", "wrong-password")
    assert r.status_code == 401


def test_legacy_long_password_can_login(platform, platform_client):
    _insert_user(platform, "legacy_long", LEGACY_LONG_HASH)
    r = _login(platform_client, "legacy_long", "a" * 80)
    assert r.status_code == 200
