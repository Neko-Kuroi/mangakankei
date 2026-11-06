"""段階1（基盤と執行）の検証。

- アカウント種別（author/reader）と年齢申告
- premium エピソードの CBZ／サムネイルはグラント必須
- 管理者による公開停止が全配信面に効き、作家の操作では外せない
- 年齢対象の公開作品はカタログで表紙が出ず、公開表紙も返らない
  （CBZ は閲覧側ゲート担当なのでプラットフォームでは開いたまま）
"""
import json

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


def _publish(platform, author_id, author_name, title_id, episode_id=1,
             access_level="public", age_restricted=False, status="published"):
    d = platform.FINAL_ZIP_DIR / f"user_{author_id}" / f"title_{title_id}"
    d.mkdir(parents=True, exist_ok=True)
    meta = {
        "status": status, "access_level": access_level,
        "age_restricted": age_restricted,
        "title_name": f"作品{title_id}", "episode_name": "第1話",
        "caption": "", "comment": "", "note": "内部メモ",
        "scrambled": True, "tile_size": 16, "webhook_enabled": False,
        "author_name": author_name, "author_id": author_id,
        "title_id": title_id, "episode_id": episode_id,
    }
    (d / f"episode_{episode_id}_settings.json").write_text(
        json.dumps(meta, ensure_ascii=False))
    (d / f"episode_{episode_id}.cbz").write_bytes(b"PK\x03\x04dummy-cbz")
    (d / f"episode_{episode_id}_cover.jpg").write_bytes(b"\xff\xd8\xffcover")
    thumbs = d / f"episode_{episode_id}_thumbs"
    thumbs.mkdir(exist_ok=True)
    (thumbs / "thumb_0000.jpg").write_bytes(b"\xff\xd8\xffthumb")
    return meta


def _cbz(client, author_id, title_id, token=None):
    return client.get(f"/api/public/cbz/{author_id}/{title_id}/1.cbz",
                      headers=_auth(token) if token else {})


@pytest.fixture(scope="module")
def users(platform, platform_client):
    """DB 直作りのユーザー（author 群）と、API 登録の reader。"""
    import bcrypt as _bcrypt
    db = platform.SessionLocal()
    ids = {}
    for name in ("p1_author", "p1_other", "p1_young", "p1_admin"):
        u = platform.User(
            username=name, email=f"{name}@example.com",
            hashed_password=_bcrypt.hashpw(b"password123", _bcrypt.gensalt()).decode(),
            is_active=True)
        db.add(u)
        db.commit()
        db.refresh(u)
        ids[name] = u.id
    db.close()
    r = platform_client.post("/api/auth/signup", json={
        "username": "p1_reader", "email": "p1_reader@example.com",
        "password": "password123", "account_type": "reader",
    })
    assert r.status_code == 200, r.text
    return ids


@pytest.fixture()
def author(users, platform_client):
    return users["p1_author"], _token(platform_client, "p1_author")


@pytest.fixture()
def reader(users, platform_client):
    return _token(platform_client, "p1_reader")


@pytest.fixture()
def admin(users, platform_client, monkeypatch):
    monkeypatch.setenv("MANGA_ADMIN_USERS", "p1_admin")
    return _token(platform_client, "p1_admin")


# ---------------------------------------------------------------------------
# アカウント種別・年齢申告
# ---------------------------------------------------------------------------

def test_signup_defaults_to_author(platform_client):
    r = platform_client.post("/api/auth/signup", json={
        "username": "p1_default", "email": "p1_default@example.com",
        "password": "password123",
    })
    assert r.status_code == 200
    token = _token(platform_client, "p1_default")
    me = platform_client.get("/api/auth/me", headers=_auth(token)).json()
    assert me["account_type"] == "author"
    assert me["is_admin"] is False
    assert me["adult_declared_at"] is None


def test_signup_rejects_bad_account_type(platform_client):
    r = platform_client.post("/api/auth/signup", json={
        "username": "p1_bad", "email": "p1_bad@example.com",
        "password": "password123", "account_type": "superuser",
    })
    assert r.status_code == 400


def test_reader_cannot_use_author_endpoints(platform_client, reader):
    me = platform_client.get("/api/auth/me", headers=_auth(reader)).json()
    assert me["account_type"] == "reader"
    r = platform_client.get("/api/author/works", headers=_auth(reader))
    assert r.status_code == 403


def test_declare_adult_is_sticky(platform_client, reader):
    r = platform_client.post("/api/auth/declare-adult", headers=_auth(reader))
    assert r.status_code == 200
    first = r.json()["adult_declared_at"]
    assert first
    r = platform_client.post("/api/auth/declare-adult", headers=_auth(reader))
    assert r.json()["adult_declared_at"] == first
    me = platform_client.get("/api/auth/me", headers=_auth(reader)).json()
    assert me["adult_declared_at"] == first


# ---------------------------------------------------------------------------
# グラントと premium 執行
# ---------------------------------------------------------------------------

def test_premium_cbz_flow(platform, platform_client, author, reader):
    author_id, author_token = author
    _publish(platform, author_id, "p1_author", title_id=101, access_level="premium")

    assert _cbz(platform_client, author_id, 101).status_code == 401          # 匿名
    assert _cbz(platform_client, author_id, 101, reader).status_code == 403  # 許可なし
    assert _cbz(platform_client, author_id, 101, author_token).status_code == 200  # 本人

    # サムネイルも同じ門
    r = platform_client.get(f"/api/public/thumb/{author_id}/101/1/0")
    assert r.status_code == 401

    # 許可を出す
    r = platform_client.post("/api/author/titles/101/grants",
                             json={"username": "p1_reader"}, headers=_auth(author_token))
    assert r.status_code == 200 and r.json()["already"] is False
    r = platform_client.post("/api/author/titles/101/grants",
                             json={"username": "p1_reader"}, headers=_auth(author_token))
    assert r.json()["already"] is True  # 冪等

    grants = platform_client.get("/api/author/titles/101/grants",
                                 headers=_auth(author_token)).json()
    assert [g["username"] for g in grants] == ["p1_reader"]

    assert _cbz(platform_client, author_id, 101, reader).status_code == 200
    r = platform_client.get(f"/api/public/thumb/{author_id}/101/1/0", headers=_auth(reader))
    assert r.status_code == 200

    # 取り消すとまた読めない
    reader_id = grants[0]["user_id"]
    r = platform_client.delete(f"/api/author/titles/101/grants/{reader_id}",
                               headers=_auth(author_token))
    assert r.status_code == 200
    assert _cbz(platform_client, author_id, 101, reader).status_code == 403
    r = platform_client.delete(f"/api/author/titles/101/grants/{reader_id}",
                               headers=_auth(author_token))
    assert r.status_code == 404


def test_grant_api_rules(platform, platform_client, author):
    author_id, author_token = author
    _publish(platform, author_id, "p1_author", title_id=102, access_level="premium")
    other_token = _token(platform_client, "p1_other")

    # 存在しないユーザー
    r = platform_client.post("/api/author/titles/102/grants",
                             json={"username": "no_such_user"}, headers=_auth(author_token))
    assert r.status_code == 404
    # 存在しない作品
    r = platform_client.post("/api/author/titles/999/grants",
                             json={"username": "p1_reader"}, headers=_auth(author_token))
    assert r.status_code == 404
    # 他人の作品には出せない（自分のフォルダに無いので 404）
    r = platform_client.post("/api/author/titles/102/grants",
                             json={"username": "p1_reader"}, headers=_auth(other_token))
    assert r.status_code == 404
    # 自分自身には出せない
    r = platform_client.post("/api/author/titles/102/grants",
                             json={"username": "p1_author"}, headers=_auth(author_token))
    assert r.status_code == 400


def test_premium_age_combination(platform, platform_client, author, reader):
    author_id, author_token = author
    _publish(platform, author_id, "p1_author", title_id=103,
             access_level="premium", age_restricted=True)
    platform_client.post("/api/auth/declare-adult", headers=_auth(reader))
    platform_client.post("/api/author/titles/103/grants",
                         json={"username": "p1_reader"}, headers=_auth(author_token))
    assert _cbz(platform_client, author_id, 103, reader).status_code == 200


def test_premium_age_blocks_undeclared(platform, platform_client, author):
    author_id, author_token = author
    _publish(platform, author_id, "p1_author", title_id=104,
             access_level="premium", age_restricted=True)
    young = _token(platform_client, "p1_young")
    platform_client.post("/api/author/titles/104/grants",
                         json={"username": "p1_young"}, headers=_auth(author_token))
    r = _cbz(platform_client, author_id, 104, young)
    assert r.status_code == 403
    assert "年齢確認" in r.json()["detail"]


# ---------------------------------------------------------------------------
# 管理者による公開停止
# ---------------------------------------------------------------------------

def test_admin_endpoints_require_admin(platform_client, author):
    _, author_token = author
    r = platform_client.get("/api/admin/works", headers=_auth(author_token))
    assert r.status_code == 403


def test_suspension_flow(platform, platform_client, author, reader, admin):
    author_id, author_token = author
    _publish(platform, author_id, "p1_author", title_id=105, access_level="premium")
    platform_client.post("/api/author/titles/105/grants",
                         json={"username": "p1_reader"}, headers=_auth(author_token))
    assert _cbz(platform_client, author_id, 105, reader).status_code == 200

    r = platform_client.post(f"/api/admin/titles/{author_id}/105/suspend",
                             json={"reason": "権利者からの申告"}, headers=_auth(admin))
    assert r.status_code == 200 and r.json()["status"] == "suspended"
    r = platform_client.post(f"/api/admin/titles/{author_id}/105/suspend",
                             json={"reason": "重ねて"}, headers=_auth(admin))
    assert r.json()["status"] == "already_suspended"

    # 全配信面が止まる
    assert _cbz(platform_client, author_id, 105, reader).status_code == 403
    assert platform_client.get(f"/api/public/cover/{author_id}/105/1").status_code == 403
    assert platform_client.get(f"/api/public/thumb/{author_id}/105/1/0",
                               headers=_auth(reader)).status_code == 403
    catalog = platform_client.get("/api/public/catalog").json()
    assert not [m for m in catalog if m["author_id"] == author_id and m["title_id"] == 105]

    # 管理一覧では停止が見える
    works = platform_client.get("/api/admin/works", headers=_auth(admin)).json()
    entry = [w for w in works if w["author_id"] == author_id and w["title_id"] == 105]
    assert entry and entry[0]["suspended"] is True
    assert entry[0]["suspend_reason"] == "権利者からの申告"

    # 作家が設定を保存し直しても停止は外れない
    r = platform_client.patch("/api/author/titles/105/episodes/1/settings", json={
        "status": "published", "access_level": "premium", "title_name": "作品105",
        "episode_name": "第1話", "caption": "", "comment": "", "note": "",
        "scrambled": True, "tile_size": 16, "webhook_enabled": False,
        "age_restricted": False,
    }, headers=_auth(author_token))
    assert r.status_code == 200
    assert _cbz(platform_client, author_id, 105, reader).status_code == 403

    # 解除で元に戻る
    r = platform_client.post(f"/api/admin/titles/{author_id}/105/resume",
                             headers=_auth(admin))
    assert r.status_code == 200
    assert _cbz(platform_client, author_id, 105, reader).status_code == 200
    r = platform_client.post(f"/api/admin/titles/{author_id}/105/resume",
                             headers=_auth(admin))
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 年齢対象の公開作品と、通常作品の回帰
# ---------------------------------------------------------------------------

def test_age_public_cover_hidden(platform, platform_client, author, reader):
    author_id, author_token = author
    _publish(platform, author_id, "p1_author", title_id=106, age_restricted=True)
    catalog = platform_client.get("/api/public/catalog").json()
    entry = [m for m in catalog if m["author_id"] == author_id and m["title_id"] == 106]
    assert entry and entry[0]["cover_url"] is None
    assert entry[0]["age_restricted"] is True
    assert platform_client.get(f"/api/public/cover/{author_id}/106/1").status_code == 403
    # 段階4で CBZ も閉じた：匿名は 403、作者本人と申告済み読者は取得できる
    assert _cbz(platform_client, author_id, 106).status_code == 403
    assert _cbz(platform_client, author_id, 106, author_token).status_code == 200
    platform_client.post("/api/auth/declare-adult", headers=_auth(reader))
    assert _cbz(platform_client, author_id, 106, reader).status_code == 200


def test_plain_public_unchanged(platform, platform_client, author):
    author_id, _ = author
    _publish(platform, author_id, "p1_author", title_id=107)
    catalog = platform_client.get("/api/public/catalog").json()
    entry = [m for m in catalog if m["author_id"] == author_id and m["title_id"] == 107]
    assert entry and entry[0]["cover_url"]
    assert _cbz(platform_client, author_id, 107).status_code == 200
    assert platform_client.get(f"/api/public/cover/{author_id}/107/1").status_code == 200
    assert platform_client.get(f"/api/public/thumb/{author_id}/107/1/0").status_code == 200
