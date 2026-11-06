"""段階3（閲覧側：読者ログイン・施錠表示・unlock）の検証。

プラットフォームは実サーバー（uvicorn スレッド）で起動し、閲覧側は
そちらを向くよう PLATFORM_BASE_URL を差し替えて、HTTP 越しの実フローで確認する。
"""
import hashlib
import io
import json
import socket
import threading
import time
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

def _make_png():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (10, 10), (255, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


PNG_BYTES = _make_png()


@pytest.fixture(autouse=True)
def _reset(platform, viewer):
    platform.limiter.reset()
    viewer.limiter.reset()
    viewer._catalog_cache.update({"ts": 0.0, "data": None})
    viewer._grants_cache.clear()
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
        "author":  _make_user(platform, "p3_author"),
        "reader1": _make_user(platform, "p3_reader1", "reader"),
        "reader2": _make_user(platform, "p3_reader2", "reader"),
    }


def _publish(platform, author_id, title_id, access_level):
    d = platform.FINAL_ZIP_DIR / f"user_{author_id}" / f"title_{title_id}"
    d.mkdir(parents=True, exist_ok=True)
    meta = {
        "status": "published", "access_level": access_level, "age_restricted": False,
        "title_name": f"作品{title_id}", "episode_name": "第1話",
        "caption": "", "comment": "", "note": "",
        "scrambled": False, "tile_size": 16, "webhook_enabled": False,
        "author_name": "p3_author", "author_id": author_id,
        "title_id": title_id, "episode_id": 1,
    }
    (d / "episode_1_settings.json").write_text(json.dumps(meta, ensure_ascii=False))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("page1.png", PNG_BYTES)
    (d / "episode_1.cbz").write_bytes(buf.getvalue())
    return f"/api/public/cbz/{author_id}/{title_id}/1.cbz"


def _grant(platform, author_id, title_id, user_id):
    db = platform.SessionLocal()
    db.add(platform.Grant(author_id=author_id, title_id=title_id, user_id=user_id))
    db.commit()
    db.close()


def _login(vclient, username):
    r = vclient.post("/auth/login", data={"username": username, "password": "password123"})
    assert r.status_code == 200, r.text
    return r


def _cbz_url(base, path):
    return base + path


# ---------------------------------------------------------------------------
# 登録・ログイン・ログアウト
# ---------------------------------------------------------------------------

def test_register_login_logout(platform, viewer, env, vclient):
    r = vclient.get("/auth/me")
    assert r.status_code == 401

    r = vclient.post("/auth/register", data={
        "username": "p3_newbie", "email": "p3_newbie@example.com",
        "password": "password123"})
    assert r.status_code == 200, r.text
    assert "viewer_auth" in r.headers.get("set-cookie", "")

    db = platform.SessionLocal()
    u = db.query(platform.User).filter_by(username="p3_newbie").one()
    assert u.account_type == "reader"
    db.close()

    r = vclient.get("/auth/me")
    assert r.status_code == 200
    assert r.json()["username"] == "p3_newbie"
    assert r.json()["account_type"] == "reader"

    r = vclient.post("/auth/logout")
    assert r.status_code == 200
    assert vclient.get("/auth/me").status_code == 401

    r = vclient.post("/auth/login", data={"username": "p3_newbie", "password": "wrong-pass"})
    assert r.status_code == 401
    r = _login(vclient, "p3_newbie")
    assert r.json()["username"] == "p3_newbie"


def test_my_grants_endpoint(platform, platform_client, people):
    author, reader = people["author"], people["reader1"]
    r = platform_client.post("/api/auth/token",
                             data={"username": "p3_reader1", "password": "password123"})
    token = r.json()["access_token"]
    h = {"Authorization": f"Bearer {token}"}
    got = platform_client.get("/api/auth/grants", headers=h).json()
    assert all(not (g["author_id"] == author and g["title_id"] == 399) for g in got)
    _grant(platform, author, 399, reader)
    got = platform_client.get("/api/auth/grants", headers=h).json()
    assert {"author_id": author, "title_id": 399} in got


# ---------------------------------------------------------------------------
# 施錠表示（カタログ）
# ---------------------------------------------------------------------------

def test_catalog_lock_marks(platform, env, vclient, people):
    base = env
    _publish(platform, people["author"], 301, "premium")
    _grant(platform, people["author"], 301, people["reader1"])

    r = vclient.get("/public")
    assert r.status_code == 200
    assert "🔒 許可制" in r.text
    assert "許可制・読めます" not in r.text

    _login(vclient, "p3_reader1")
    r = vclient.get("/public")
    assert "許可制・読めます" in r.text


# ---------------------------------------------------------------------------
# unlock の実フロー
# ---------------------------------------------------------------------------

def test_premium_unlock_flow(platform, viewer, env, vclient, people):
    import manga as mg
    base = env
    author = people["author"]
    url = _cbz_url(base, _publish(platform, author, 302, "premium"))

    # 匿名ではセッション自体がもらえない
    r = vclient.get("/session", params={"url": url})
    assert r.status_code == 401

    # 許可のない読者：セッションは出るが、ダウンロードでプラットフォームに拒否される
    _login(vclient, "p3_reader2")
    r = vclient.get("/session", params={"url": url})
    assert r.status_code == 200
    token2 = r.json()["session_token"]
    r = vclient.get("/download/start", params={"url": url, "session_token": token2})
    assert "error:" in r.text
    assert not __import__("os").path.exists(mg.get_cache_path(url) + ".cbz")

    # 許可のある読者：ダウンロードでき、ページが読める
    _grant(platform, author, 302, people["reader1"])
    if True:
        c1 = TestClient(viewer.app)
        _login(c1, "p3_reader1")
        r = c1.get("/session", params={"url": url})
        assert r.status_code == 200
        token1 = r.json()["session_token"]
        r = c1.get("/download/start", params={"url": url, "session_token": token1})
        assert "data: done" in r.text

        r = c1.get("/images", params={"url": url, "session_token": token1})
        assert r.status_code == 200
        assert "page1.png" in r.text

        # 展開時に連番が付くため、/images が返した実URLで画像を取りにいく
        import re as _re
        m = _re.search(r"/image/[^\"']+", r.text)
        assert m, r.text[:300]
        image_path = m.group(0)
        r = c1.get(image_path)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/")
        assert len(r.content) > 0

        # 別ユーザー（許可なし）の cookie では他人のセッションを使えない
        if True:
            c2 = TestClient(viewer.app)
            _login(c2, "p3_reader2")
            r = c2.get("/images", params={"url": url, "session_token": token1})
            assert r.status_code == 403
            r = c2.get(image_path)
            assert r.status_code == 403

        # cookie が無ければ同じセッションでも使えない
        if True:
            c3 = TestClient(viewer.app)
            r = c3.get("/images", params={"url": url, "session_token": token1})
            assert r.status_code == 403


def test_public_work_stays_open(platform, env, vclient, people):
    base = env
    url = _cbz_url(base, _publish(platform, people["author"], 303, "public"))
    r = vclient.get("/session", params={"url": url})
    assert r.status_code == 200
    token = r.json()["session_token"]
    r = vclient.get("/download/start", params={"url": url, "session_token": token})
    assert "data: done" in r.text
    r = vclient.get("/images", params={"url": url, "session_token": token})
    assert r.status_code == 200
    assert "page1.png" in r.text


def test_reader_page_lock_banner(platform, env, vclient, people):
    base = env
    url = _cbz_url(base, _publish(platform, people["author"], 305, "premium"))
    r = vclient.get("/reader", params={"url": url})
    assert r.status_code == 200
    assert 'id="lock-msg"' in r.text
    assert "この作品は許可制です" in r.text  # 未ログイン向けの案内が初期表示される
    assert "showLock" in r.text

    r = vclient.get("/public")
    assert 'id="auth-forms"' in r.text  # base のログインUI


def test_download_file_auth_header(platform, env, tmp_path, people):
    import manga as mg
    base = env
    author = people["author"]
    url = _cbz_url(base, _publish(platform, author, 304, "premium"))
    _grant(platform, author, 304, people["reader1"])

    # トークンなしではプラットフォームが拒否する
    dest = str(tmp_path / "a.cbz")
    ok, err = mg.download_file(url, dest)
    assert ok is False

    # 読者トークンを併送すれば取得できる
    from fastapi.testclient import TestClient as TC
    with TC(platform.app) as pc:
        r = pc.post("/api/auth/token",
                    data={"username": "p3_reader1", "password": "password123"})
        reader_token = r.json()["access_token"]
    dest = str(tmp_path / "b.cbz")
    ok, err = mg.download_file(url, dest, auth_token=reader_token)
    assert ok is True, err
