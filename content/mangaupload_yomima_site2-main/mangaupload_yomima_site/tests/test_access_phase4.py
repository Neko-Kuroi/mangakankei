"""段階4（年齢確認）の検証。

プラットフォーム側：年齢対象の無料作品の CBZ／サムネイルを閉じる。
閲覧側：匿名はセッション申告ゲート、ログイン中はアカウント申告と連携し、
premium＋年齢の複合判定までを実フロー（実サーバー越し）で確認する。
"""
import io
import json
import socket
import threading
import time
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

JPG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 32


def _make_png():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (10, 10), (0, 0, 255)).save(buf, format="PNG")
    return buf.getvalue()


PNG_BYTES = _make_png()


@pytest.fixture(autouse=True)
def _reset(platform, viewer):
    platform.limiter.reset()
    viewer.limiter.reset()
    viewer._catalog_cache.update({"ts": 0.0, "data": None})
    viewer._grants_cache.clear()
    viewer._auth_store.clear()
    viewer._age_gate_store.clear()


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def live_platform(platform):
    import uvicorn
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
    monkeypatch.setenv("MANGA_INTERNAL_TOKEN", "p4-internal-secret")
    return live_platform


@pytest.fixture()
def vclient(viewer):
    yield TestClient(viewer.app)


def _make_user(platform, username, account_type="author"):
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


@pytest.fixture(scope="module")
def people(platform):
    return {
        "author": _make_user(platform, "p4_author"),
        "reader": _make_user(platform, "p4_reader", "reader"),
    }


def _publish(platform, author_id, title_id, access_level="public", age_restricted=True):
    d = platform.FINAL_ZIP_DIR / f"user_{author_id}" / f"title_{title_id}"
    d.mkdir(parents=True, exist_ok=True)
    meta = {
        "status": "published", "access_level": access_level,
        "age_restricted": age_restricted,
        "title_name": f"作品{title_id}", "episode_name": "第1話",
        "caption": "", "comment": "", "note": "",
        "scrambled": False, "tile_size": 16, "webhook_enabled": False,
        "author_name": "p4_author", "author_id": author_id,
        "title_id": title_id, "episode_id": 1,
    }
    (d / "episode_1_settings.json").write_text(json.dumps(meta, ensure_ascii=False))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("page1.png", PNG_BYTES)
    (d / "episode_1.cbz").write_bytes(buf.getvalue())
    thumbs = d / "episode_1_thumbs"
    thumbs.mkdir(exist_ok=True)
    (thumbs / "thumb_0000.jpg").write_bytes(JPG_BYTES)
    return f"/api/public/cbz/{author_id}/{title_id}/1.cbz"


def _grant(platform, author_id, title_id, user_id):
    db = platform.SessionLocal()
    db.add(platform.Grant(author_id=author_id, title_id=title_id, user_id=user_id))
    db.commit()
    db.close()


def _ptoken(platform_client, username):
    r = platform_client.post("/api/auth/token",
                             data={"username": username, "password": "password123"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _login(vclient, username):
    r = vclient.post("/auth/login", data={"username": username, "password": "password123"})
    assert r.status_code == 200, r.text
    return r


# ---------------------------------------------------------------------------
# プラットフォーム側：年齢対象の無料作品を閉じる
# ---------------------------------------------------------------------------

def test_platform_age_gate(platform, platform_client, people):
    author = people["author"]
    _publish(platform, author, 401)
    cbz = f"/api/public/cbz/{author}/401/1.cbz"
    thumb = f"/api/public/thumb/{author}/401/1/0"

    assert platform_client.get(cbz).status_code == 403
    assert platform_client.get(thumb).status_code == 403

    # 未申告の読者トークンでも開かない
    reader_token = _ptoken(platform_client, "p4_reader")
    assert platform_client.get(cbz, headers=_auth(reader_token)).status_code == 403

    # 作者本人は通る
    author_token = _ptoken(platform_client, "p4_author")
    assert platform_client.get(cbz, headers=_auth(author_token)).status_code == 200

    # 申告すれば読者も通る（CBZ・サムネイルとも）
    platform_client.post("/api/auth/declare-adult", headers=_auth(reader_token))
    assert platform_client.get(cbz, headers=_auth(reader_token)).status_code == 200
    assert platform_client.get(thumb, headers=_auth(reader_token)).status_code == 200


# ---------------------------------------------------------------------------
# 閲覧側：匿名のセッション申告ゲート
# ---------------------------------------------------------------------------

def test_viewer_age_gate_anonymous(platform, env, vclient, people):
    url = env + _publish(platform, people["author"], 402)
    r = vclient.get("/session", params={"url": url})
    assert r.status_code == 403
    assert "年齢" in r.json()["detail"]

    r = vclient.post("/age/declare")
    assert r.status_code == 200
    assert "age_gate" in r.headers.get("set-cookie", "")

    r = vclient.get("/session", params={"url": url})
    assert r.status_code == 200
    token = r.json()["session_token"]
    r = vclient.get("/download/start", params={"url": url, "session_token": token})
    assert "data: done" in r.text
    r = vclient.get("/images", params={"url": url, "session_token": token})
    assert r.status_code == 200
    assert "page1.png" in r.text


# ---------------------------------------------------------------------------
# 閲覧側：アカウント申告との連携
# ---------------------------------------------------------------------------

def test_viewer_age_account(platform, env, vclient, people):
    url = env + _publish(platform, people["author"], 403)
    # p4_reader はプラットフォーム側テストで申告済みになるため専用ユーザーを使う
    _make_user(platform, "p4_reader_acct", "reader")
    _login(vclient, "p4_reader_acct")
    r = vclient.get("/session", params={"url": url})
    assert r.status_code == 403

    r = vclient.post("/auth/declare-adult")
    assert r.status_code == 200, r.text

    r = vclient.get("/session", params={"url": url})
    assert r.status_code == 200
    token = r.json()["session_token"]
    r = vclient.get("/download/start", params={"url": url, "session_token": token})
    assert "data: done" in r.text
    r = vclient.get("/images", params={"url": url, "session_token": token})
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# 複合：premium ＋ 年齢（許可あり・申告なし → 申告で開く）
# ---------------------------------------------------------------------------

def test_viewer_premium_age_composite(platform, env, vclient, people):
    import os
    import manga as mg
    author = people["author"]
    url = env + _publish(platform, author, 404, access_level="premium")
    # p4_reader は他のテストで申告済みになり得るため、複合判定には専用ユーザーを使う
    rid = _make_user(platform, "p4_reader_age", "reader")
    _grant(platform, author, 404, rid)

    _login(vclient, "p4_reader_age")
    r = vclient.get("/session", params={"url": url})
    assert r.status_code == 200
    token = r.json()["session_token"]
    r = vclient.get("/download/start", params={"url": url, "session_token": token})
    assert "error:" in r.text
    assert not os.path.exists(mg.get_cache_path(url) + ".cbz")

    # 申告すれば、同じセッションのまま再試行で開く
    r = vclient.post("/auth/declare-adult")
    assert r.status_code == 200
    r = vclient.get("/download/start", params={"url": url, "session_token": token})
    assert "data: done" in r.text
    r = vclient.get("/images", params={"url": url, "session_token": token})
    assert r.status_code == 200
    assert "page1.png" in r.text


# ---------------------------------------------------------------------------
# リーダーページの年齢バナー
# ---------------------------------------------------------------------------

def test_reader_page_age_banner(platform, env, vclient, people):
    url = env + _publish(platform, people["author"], 405)
    r = vclient.get("/reader", params={"url": url})
    assert r.status_code == 200
    assert "年齢対象です" in r.text
    assert "declareAge" in r.text

    vclient.post("/age/declare")
    r = vclient.get("/reader", params={"url": url})
    assert "私は18歳以上です" not in r.text
