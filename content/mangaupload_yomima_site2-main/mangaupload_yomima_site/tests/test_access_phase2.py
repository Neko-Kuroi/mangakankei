"""段階2（プラットフォーム画面）の検証。

画面そのものは GET / の HTML と JS 構文で確認し、画面が依存する
API 側（マイ作品への停止注釈、年齢フラグの往復）を実エンドポイントで確認する。
"""
import json
import re
import shutil
import subprocess

import pytest


@pytest.fixture(autouse=True)
def _reset_limits(platform):
    platform.limiter.reset()


def _token(client, username, password="password123"):
    r = client.post("/api/auth/token", data={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def users(platform):
    import bcrypt as _bcrypt
    db = platform.SessionLocal()
    ids = {}
    for name in ("p2_author", "p2_admin"):
        u = platform.User(
            username=name, email=f"{name}@example.com",
            hashed_password=_bcrypt.hashpw(b"password123", _bcrypt.gensalt()).decode(),
            is_active=True)
        db.add(u)
        db.commit()
        db.refresh(u)
        ids[name] = u.id
    db.close()
    return ids


@pytest.fixture()
def author(users, platform_client):
    return users["p2_author"], _token(platform_client, "p2_author")


@pytest.fixture()
def admin(users, platform_client, monkeypatch):
    monkeypatch.setenv("MANGA_ADMIN_USERS", "p2_admin")
    return _token(platform_client, "p2_admin")


def _publish(platform, author_id, title_id=201):
    d = platform.FINAL_ZIP_DIR / f"user_{author_id}" / f"title_{title_id}"
    d.mkdir(parents=True, exist_ok=True)
    meta = {
        "status": "published", "access_level": "public", "age_restricted": False,
        "title_name": f"作品{title_id}", "episode_name": "第1話",
        "caption": "", "comment": "", "note": "",
        "scrambled": True, "tile_size": 16, "webhook_enabled": False,
        "author_name": "p2_author", "author_id": author_id,
        "title_id": title_id, "episode_id": 1,
    }
    (d / "episode_1_settings.json").write_text(json.dumps(meta, ensure_ascii=False))
    (d / "episode_1.cbz").write_bytes(b"PK\x03\x04dummy-cbz")
    return meta


# ---------------------------------------------------------------------------
# 画面の存在と JS 構文
# ---------------------------------------------------------------------------

def test_root_html_has_phase2_ui(platform, platform_client):
    r = platform_client.get("/")
    assert r.status_code == 200
    html = r.text
    for marker in (
        'id="admin-nav"', 'id="admin-page"', 'id="admin-works-list"',
        'id="age_restricted"', 'id="edit-age-restricted"',
        'id="grants-modal"', 'id="grants-username"',
        "loadAdminWorks", "openGrantsModal", "refreshMe",
        "管理者により公開停止中", "年齢確認が必要な作品にする",
    ):
        assert marker in html, marker


def test_embedded_script_is_valid_js(platform, tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node が無い環境では JS 構文検査をスキップ")
    scripts = re.findall(r"<script>(.*?)</script>", platform.index_html_content, re.S)
    assert len(scripts) == 1
    js_file = tmp_path / "ui.js"
    js_file.write_text(scripts[0], encoding="utf-8")
    r = subprocess.run([node, "--check", str(js_file)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# ---------------------------------------------------------------------------
# マイ作品への停止注釈
# ---------------------------------------------------------------------------

def test_works_suspension_annotation(platform, platform_client, author, admin):
    author_id, author_token = author
    _publish(platform, author_id, title_id=201)

    works = platform_client.get("/api/author/works", headers=_auth(author_token)).json()
    entry = [w for w in works if w["title_id"] == 201]
    assert entry and entry[0]["suspended"] is False
    assert entry[0]["suspend_reason"] is None

    platform_client.post(f"/api/admin/titles/{author_id}/201/suspend",
                         json={"reason": "確認のため"}, headers=_auth(admin))
    works = platform_client.get("/api/author/works", headers=_auth(author_token)).json()
    entry = [w for w in works if w["title_id"] == 201]
    assert entry[0]["suspended"] is True
    assert entry[0]["suspend_reason"] == "確認のため"

    platform_client.post(f"/api/admin/titles/{author_id}/201/resume", headers=_auth(admin))
    works = platform_client.get("/api/author/works", headers=_auth(author_token)).json()
    entry = [w for w in works if w["title_id"] == 201]
    assert entry[0]["suspended"] is False


# ---------------------------------------------------------------------------
# 年齢フラグの往復（編集画面の保存経路）
# ---------------------------------------------------------------------------

def test_age_flag_roundtrip(platform, platform_client, author):
    author_id, author_token = author
    _publish(platform, author_id, title_id=202)
    payload = {
        "status": "published", "access_level": "public",
        "title_name": "作品202", "episode_name": "第1話",
        "caption": "", "comment": "", "note": "",
        "scrambled": True, "tile_size": 16, "webhook_enabled": False,
        "age_restricted": True,
    }
    r = platform_client.patch("/api/author/titles/202/episodes/1/settings",
                              json=payload, headers=_auth(author_token))
    assert r.status_code == 200

    works = platform_client.get("/api/author/works", headers=_auth(author_token)).json()
    entry = [w for w in works if w["title_id"] == 202]
    assert entry[0]["age_restricted"] is True

    catalog = platform_client.get("/api/public/catalog").json()
    entry = [m for m in catalog if m["author_id"] == author_id and m["title_id"] == 202]
    assert entry and entry[0]["age_restricted"] is True
