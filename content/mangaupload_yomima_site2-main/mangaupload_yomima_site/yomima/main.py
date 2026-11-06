"""
viewer_main.py - FastAPI router

変更点:
- セッショントークン発行・検証
- /image/{session_token}/{filename} に変更
- /thumbnail を episode_thumbs/ から返すよう変更
- slowapi レート制限
- CF-Connecting-IP 対応
"""

import asyncio
import html
import concurrent.futures
import time
import hashlib
import secrets
import os
import httpx
from contextlib import asynccontextmanager
from typing import Annotated, Optional

import urllib.parse
from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.responses import Response as StarletteResponse

import manga as mg

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------
BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR    = os.path.join(BASE_DIR, "static")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

PLATFORM_BASE_URL = os.environ.get("PLATFORM_BASE_URL", "http://localhost:8000")

# ---------------------------------------------------------------------------
# プラットフォームのカタログ取得（短時間キャッシュ＋内部トークン）
# ---------------------------------------------------------------------------
CATALOG_CACHE_TTL = 60  # 秒
_catalog_cache: dict = {"ts": 0.0, "data": None}

def _internal_headers() -> dict:
    """プラットフォームへのサーバー間通信用のヘッダ（内部トークン）。"""
    token = os.environ.get("MANGA_INTERNAL_TOKEN", "")
    return {"X-Internal-Token": token} if token else {}

async def _fetch_platform_catalog(timeout: float = 10.0) -> list:
    """公開カタログを取得する。成功時は60秒キャッシュし、失敗時は例外を送出する。"""
    now = time.time()
    if _catalog_cache["data"] is not None and now - _catalog_cache["ts"] < CATALOG_CACHE_TTL:
        return _catalog_cache["data"]
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(
            f"{PLATFORM_BASE_URL}/api/public/catalog",
            headers=_internal_headers(),
        )
        resp.raise_for_status()
        data = resp.json()
    _catalog_cache.update({"ts": now, "data": data})
    return data

_executor       = concurrent.futures.ThreadPoolExecutor(max_workers=4)
_thumb_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)

# ---------------------------------------------------------------------------
# IP取得（Cloudflared対応）
# ---------------------------------------------------------------------------
def get_client_ip(request: Request) -> str:
    """レート制限用のクライアントIP取得（プラットフォーム側と同じ規則）。

    原則は接続元アドレス。CF-Connecting-IP は環境変数
    MANGA_TRUST_CF_HEADER=1 のときだけ採用する。
    """
    if os.environ.get("MANGA_TRUST_CF_HEADER") == "1":
        cf_ip = request.headers.get("CF-Connecting-IP")
        if cf_ip:
            return cf_ip
    return request.client.host if request.client else "unknown"

# ---------------------------------------------------------------------------
# レート制限
# ---------------------------------------------------------------------------
limiter = Limiter(key_func=get_client_ip)

# ---------------------------------------------------------------------------
# アプリ
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    mg.manage_cache_size()
    mg.cleanup_old_cache()
    yield
    _executor.shutdown(wait=False)
    _thumb_executor.shutdown(wait=False)

app = FastAPI(title="Manga Reader", lifespan=lifespan)
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)

templates = Jinja2Templates(directory=TEMPLATES_DIR)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return StarletteResponse("リクエストが多すぎます。しばらく待ってから再試行してください。", status_code=429)


# ---------------------------------------------------------------------------
# 読者アカウント（プラットフォームの JWT はサーバー側だけに保持）
# ---------------------------------------------------------------------------
AUTH_TTL_SECONDS = 3600  # プラットフォームのトークン有効期限（60分）に合わせる
_auth_store: dict = {}   # {cookie_token: {platform_token, user_id, username, expires_at}}
_grants_cache: dict = {} # {user_id: (取得時刻, {(author_id, title_id)})}


def _current_reader(request: Request):
    """ログイン中の読者を返す（未ログイン・期限切れは None）。"""
    token = request.cookies.get("viewer_auth", "")
    entry = _auth_store.get(token)
    if not entry:
        return None
    if time.time() > entry["expires_at"]:
        _auth_store.pop(token, None)
        return None
    return entry


async def _platform_login(username: str, password: str):
    """プラットフォームでログインし (platform_token, me) を返す。"""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{PLATFORM_BASE_URL}/api/auth/token",
            data={"username": username, "password": password},
        )
        if resp.status_code != 200:
            raise HTTPException(status_code=401, detail="ユーザー名またはパスワードが正しくありません")
        platform_token = resp.json()["access_token"]
        me_resp = await client.get(
            f"{PLATFORM_BASE_URL}/api/auth/me",
            headers={"Authorization": f"Bearer {platform_token}"},
        )
        if me_resp.status_code != 200:
            raise HTTPException(status_code=502, detail="プラットフォームとの通信に失敗しました")
        return platform_token, me_resp.json()


def _start_auth_session(platform_token: str, me: dict) -> JSONResponse:
    cookie_token = secrets.token_urlsafe(32)
    _auth_store[cookie_token] = {
        "platform_token": platform_token,
        "user_id":        me["id"],
        "username":       me["username"],
        "expires_at":     time.time() + AUTH_TTL_SECONDS,
    }
    resp = JSONResponse({"username": me["username"]})
    resp.set_cookie("viewer_auth", cookie_token, max_age=AUTH_TTL_SECONDS,
                    httponly=True, path="/", samesite="lax")
    return resp


@app.post("/auth/login")
async def auth_login(username: str = Form(...), password: str = Form(...)):
    platform_token, me = await _platform_login(username, password)
    return _start_auth_session(platform_token, me)


@app.post("/auth/register")
async def auth_register(username: str = Form(...), email: str = Form(...),
                        password: str = Form(...)):
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{PLATFORM_BASE_URL}/api/auth/signup",
            json={"username": username, "email": email, "password": password,
                  "account_type": "reader"},
        )
        if resp.status_code != 200:
            try:
                detail = resp.json().get("detail", "登録に失敗しました")
            except Exception:
                detail = "登録に失敗しました"
            raise HTTPException(status_code=400, detail=detail)
    platform_token, me = await _platform_login(username, password)
    return _start_auth_session(platform_token, me)


@app.post("/auth/logout")
async def auth_logout(request: Request):
    _auth_store.pop(request.cookies.get("viewer_auth", ""), None)
    resp = JSONResponse({"status": "logged_out"})
    resp.delete_cookie("viewer_auth", path="/")
    return resp


@app.get("/auth/me")
async def auth_me(request: Request):
    reader = _current_reader(request)
    if reader is None:
        raise HTTPException(status_code=401, detail="ログインしていません")
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{PLATFORM_BASE_URL}/api/auth/me",
            headers={"Authorization": f"Bearer {reader['platform_token']}"},
        )
    if resp.status_code == 401:
        _auth_store.pop(request.cookies.get("viewer_auth", ""), None)
        raise HTTPException(status_code=401, detail="ログインの有効期限が切れました")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="プラットフォームとの通信に失敗しました")
    return resp.json()


async def _fetch_my_grants(reader: dict) -> set:
    """ログイン中読者に許可が出ている (author_id, title_id) の集合（60秒キャッシュ）。"""
    now = time.time()
    cached = _grants_cache.get(reader["user_id"])
    if cached and now - cached[0] < CATALOG_CACHE_TTL:
        return cached[1]
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{PLATFORM_BASE_URL}/api/auth/grants",
            headers={"Authorization": f"Bearer {reader['platform_token']}"},
        )
        resp.raise_for_status()
        grants = {(g["author_id"], g["title_id"]) for g in resp.json()}
    _grants_cache[reader["user_id"]] = (now, grants)
    return grants


def _enforce_session_unlock(request: Request, session: dict) -> None:
    """許可制セッションは、unlock した読者本人だけが使える。"""
    uid = session.get("unlock_user_id")
    if uid is None:
        return
    reader = _current_reader(request)
    if reader is None or reader["user_id"] != uid:
        raise HTTPException(status_code=403, detail="この作品を読む権限がありません")


# ---------------------------------------------------------------------------
# 年齢確認（無料の年齢対象作品はセッション単位の自己申告ゲート、
# ログイン中はアカウントの年齢申告と連携する。どちらも自己申告）
# ---------------------------------------------------------------------------
AGE_GATE_TTL_SECONDS = 30 * 24 * 3600  # 30日
_age_gate_store: dict = {}  # {cookie_token: expires_at}


def _age_gate_ok(request: Request) -> bool:
    """匿名読者の年齢ゲート（自己申告）を通過済みか。"""
    token = request.cookies.get("age_gate", "")
    exp = _age_gate_store.get(token)
    if not exp:
        return False
    if time.time() > exp:
        _age_gate_store.pop(token, None)
        return False
    return True


@app.post("/age/declare")
@limiter.limit("10/minute")
async def age_declare(request: Request):
    """匿名読者の年齢申告（自己申告）。この端末では30日間有効。"""
    token = secrets.token_urlsafe(32)
    _age_gate_store[token] = time.time() + AGE_GATE_TTL_SECONDS
    resp = JSONResponse({"status": "declared"})
    resp.set_cookie("age_gate", token, max_age=AGE_GATE_TTL_SECONDS,
                    httponly=True, path="/", samesite="lax")
    return resp


async def _fetch_me(reader: dict):
    """プラットフォーム上の自分の情報を返す（失敗時は None）。"""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{PLATFORM_BASE_URL}/api/auth/me",
                headers={"Authorization": f"Bearer {reader['platform_token']}"},
            )
        return resp.json() if resp.status_code == 200 else None
    except Exception:
        return None


@app.post("/auth/declare-adult")
async def auth_declare_adult(request: Request):
    """ログイン中アカウントの年齢申告をプラットフォームに記録する。"""
    reader = _current_reader(request)
    if reader is None:
        raise HTTPException(status_code=401, detail="ログインしていません")
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{PLATFORM_BASE_URL}/api/auth/declare-adult",
            headers={"Authorization": f"Bearer {reader['platform_token']}"},
        )
    if resp.status_code == 401:
        _auth_store.pop(request.cookies.get("viewer_auth", ""), None)
        raise HTTPException(status_code=401, detail="ログインの有効期限が切れました")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="プラットフォームとの通信に失敗しました")
    return resp.json()


# ---------------------------------------------------------------------------
# List screen  GET /
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    share: Optional[str] = Query(default=None),
):
    manga_list:     list[dict] = []
    import_message: Optional[str] = None

    if share:
        manga_list, err = mg.decode_manga_list(share)
        if err:
            import_message = err

    return templates.TemplateResponse(request, "list.html", {
        "request":        request,
        "manga_list":     manga_list,
        "share_param":    share or "",
        "import_message": import_message,
        "max_count":      mg.MAX_MANGA_COUNT,
    })

# ---------------------------------------------------------------------------
# Public catalog screen  GET /public
# ---------------------------------------------------------------------------

@app.get("/public", response_class=HTMLResponse)
async def public_catalog(request: Request):
    catalog   = []
    error_msg = None
    try:
        catalog = await _fetch_platform_catalog()
    except Exception as e:
        error_msg = f"カタログの取得に失敗しました: {e}"

    reader   = _current_reader(request)
    entitled = set()
    if reader is not None:
        try:
            entitled = await _fetch_my_grants(reader)
        except Exception:
            entitled = set()

    return templates.TemplateResponse(request, "public.html", {
        "request":      request,
        "catalog":      catalog,
        "error_msg":    error_msg,
        "platform_url": PLATFORM_BASE_URL,
        "entitled":     entitled,
        "reader_user":  reader,
    })

# ---------------------------------------------------------------------------
# セッショントークン発行  GET /session
# ---------------------------------------------------------------------------

@app.get("/session")
@limiter.limit("30/minute")
async def create_session(
    request: Request,
    url: str = Query(...),
):
    """
    CBZ URLに対するセッショントークンを発行する。
    リーダーページを開くときに呼ばれる。
    """
    if not mg.validate_manga_url(url):
        raise HTTPException(status_code=400, detail="無効なURL")
    allowed, reason = mg.is_url_fetch_allowed(url)
    if not allowed:
        raise HTTPException(status_code=400, detail=reason)

    client_ip = get_client_ip(request)

    # 許可制（premium）のプラットフォーム作品は、ログイン中読者のセッションに
    # bind する（CBZ 取得にはその読者のトークンを併送し、ページ配信は本人だけ）
    if "/api/public/cbz/" in url:
        entry = None
        try:
            catalog = await _fetch_platform_catalog(timeout=5.0)
            entry = next((i for i in catalog if i.get("cbz_url") == url), None)
        except Exception:
            entry = None
        if entry is not None and entry.get("access_level") == "premium":
            reader = _current_reader(request)
            if reader is None:
                raise HTTPException(status_code=401, detail="この作品を読むにはログインが必要です")
            token = mg.create_session(url, client_ip,
                                      unlock_user_id=reader["user_id"],
                                      auth_token=reader["platform_token"])
            return {"session_token": token, "expires_in": mg.SESSION_TTL_SECONDS}
        if entry is not None and entry.get("age_restricted"):
            # 年齢対象の無料作品：ログイン中はアカウント申告、匿名は年齢ゲート
            reader = _current_reader(request)
            if reader is not None:
                me = await _fetch_me(reader)
                if me is not None and me.get("adult_declared_at"):
                    token = mg.create_session(url, client_ip, age_verified=True)
                    return {"session_token": token, "expires_in": mg.SESSION_TTL_SECONDS}
                raise HTTPException(status_code=403, detail="年齢対象の作品です。先に年齢申告をしてください")
            if _age_gate_ok(request):
                token = mg.create_session(url, client_ip, age_verified=True)
                return {"session_token": token, "expires_in": mg.SESSION_TTL_SECONDS}
            raise HTTPException(status_code=403, detail="年齢対象の作品です。年齢確認をしてください")

    token = mg.create_session(url, client_ip)
    return {"session_token": token, "expires_in": mg.SESSION_TTL_SECONDS}

# ---------------------------------------------------------------------------
# Add manga  POST /manga/add
# ---------------------------------------------------------------------------

@app.post("/manga/add", response_class=HTMLResponse)
async def manga_add(
    request: Request,
    url:   Annotated[str, Form()],
    title: Annotated[str, Form()] = "",
    share: Annotated[str, Form()] = "",
):
    manga_list, _ = mg.decode_manga_list(share) if share else ([], "")
    error: Optional[str] = None
    fetch_allowed, fetch_reason = mg.is_url_fetch_allowed(url)

    if not mg.validate_manga_url(url):
        error = "URLは .zip / .cbz / .rar / .cbr である必要があります"
    elif not fetch_allowed:
        error = fetch_reason
    elif len(manga_list) >= mg.MAX_MANGA_COUNT:
        error = f"最大 {mg.MAX_MANGA_COUNT} 件まで登録できます"
    elif url in {m['url'] for m in manga_list}:
        error = "このURLはすでに追加されています"
    else:
        manga_list.append(mg.build_manga_entry(url, title))

    new_share = mg.encode_manga_list(manga_list) if manga_list else ""

    return templates.TemplateResponse(request, "partials/manga_list.html", {
        "request":    request,
        "manga_list": manga_list,
        "share_param": new_share,
        "error":      error,
        "max_count":  mg.MAX_MANGA_COUNT,
    }, headers={"HX-Push-Url": f"/?share={new_share}" if new_share else "/"})

# ---------------------------------------------------------------------------
# Remove manga  POST /manga/remove
# ---------------------------------------------------------------------------

@app.post("/manga/remove", response_class=HTMLResponse)
async def manga_remove(
    request: Request,
    url:   Annotated[str, Form()],
    share: Annotated[str, Form()] = "",
):
    manga_list, _ = mg.decode_manga_list(share) if share else ([], "")
    manga_list    = [m for m in manga_list if m['url'] != url]
    new_share     = mg.encode_manga_list(manga_list) if manga_list else ""

    return templates.TemplateResponse(request, "partials/manga_list.html", {
        "request":    request,
        "manga_list": manga_list,
        "share_param": new_share,
        "error":      None,
        "max_count":  mg.MAX_MANGA_COUNT,
    }, headers={"HX-Push-Url": f"/?share={new_share}" if new_share else "/"})

# ---------------------------------------------------------------------------
# Reader screen  GET /reader
# ---------------------------------------------------------------------------

@app.get("/reader", response_class=HTMLResponse)
async def reader(
    request:  Request,
    url:      str = Query(...),
    share:    str = Query(default=""),
    webhook1: str = Query(default=""),
    webhook2: str = Query(default=""),
):
    if not mg.validate_manga_url(url):
        raise HTTPException(status_code=400, detail="無効なURL")
    allowed, reason = mg.is_url_fetch_allowed(url)
    if not allowed:
        raise HTTPException(status_code=400, detail=reason)

    manga_list, _ = mg.decode_manga_list(share) if share else ([], "")
    manga_title   = next(
        (m['title'] for m in manga_list if m['url'] == url),
        mg.get_filename_from_url(url)
    )

    # settings.json から scrambled / tile_size / タイトル / webhook_enabled を取得する
    scrambled           = False
    tile_size           = 16
    manga_title_display = manga_title

    # URLがプラットフォームのCBZ形式かどうかで許可判定を分ける。
    # 外部ZIP・マイリスト追加のURLは無条件で Discord 送信を許可する。
    # プラットフォーム公開作品（/api/public/cbz/）のみ作者の webhook_enabled 設定に従う。
    is_platform_url = "/api/public/cbz/" in url
    webhook_enabled = not is_platform_url   # 外部URL → True、プラットフォーム → カタログ確認後に決定
    is_premium      = False
    is_age          = False

    try:
        catalog = await _fetch_platform_catalog(timeout=5.0)
        for item in catalog:
            if item.get("cbz_url") == url:
                scrambled = item.get("scrambled", False)
                tile_size = item.get("tile_size", 16)
                # プラットフォーム作品のみ作者設定を反映。外部URLは上書きしない。
                if is_platform_url:
                    webhook_enabled = item.get("webhook_enabled", False)
                    is_premium      = item.get("access_level") == "premium"
                    is_age          = bool(item.get("age_restricted"))
                t = item.get("title_name", "").strip()
                e = item.get("episode_name", "").strip()
                if t and e:
                    manga_title_display = f"{t} — {e}"
                elif t:
                    manga_title_display = t
                elif e:
                    manga_title_display = e
                break
    except Exception:
        pass

    reader = _current_reader(request)
    age_gate_passed = _age_gate_ok(request)
    if is_age and not age_gate_passed and reader is not None:
        me = await _fetch_me(reader)
        age_gate_passed = bool(me and me.get("adult_declared_at"))

    return templates.TemplateResponse(request, "reader.html", {
        "request":         request,
        "manga_url":       url,
        "manga_title":     manga_title_display,
        "share_param":     share,
        "per_page":        mg.IMAGES_PER_LOAD,
        "webhook1":        webhook1,
        "webhook2":        webhook2,
        "scrambled":       scrambled,
        "tile_size":       tile_size,
        "webhook_enabled": webhook_enabled,
        "is_premium":      is_premium,
        "is_age_restricted": is_age,
        "age_gate_passed": age_gate_passed,
        "reader_logged_in": reader is not None,
    })

# ---------------------------------------------------------------------------
# Image list partial  GET /images
# セッショントークン必須
# ---------------------------------------------------------------------------

@app.get("/images", response_class=HTMLResponse)
@limiter.limit("60/minute")
async def images_partial(
    request:       Request,
    url:           str = Query(...),
    offset:        int = Query(default=0),
    limit:         int = Query(default=0),
    webhook1:      str = Query(default=""),
    webhook2:      str = Query(default=""),
    session_token: str = Query(default=""),
):
    # セッション検証（必須）
    session = mg.validate_session(session_token, get_client_ip(request))
    if not session:
        raise HTTPException(status_code=403, detail="セッションが無効または期限切れです")
    if session["cbz_url"] != url:
        raise HTTPException(status_code=403, detail="セッションのURLが一致しません")
    _enforce_session_unlock(request, session)

    allowed, reason = mg.is_url_fetch_allowed(url)
    if not allowed:
        raise HTTPException(status_code=400, detail=reason)

    url_hash     = hashlib.md5(url.encode()).hexdigest()
    file_ext     = os.path.splitext(url.split('?')[0])[-1].lower()
    archive_path = mg.get_cache_path(url) + file_ext
    extract_path = mg.get_cache_path(url) + "_extracted"

    if not os.path.exists(archive_path):
        return templates.TemplateResponse(request, "partials/download_pending.html", {
            "request":   request,
            "manga_url": url,
            "offset":    offset,
            "webhook1":  webhook1,
            "webhook2":  webhook2,
        })

    image_files, warnings = mg.extract_archive(archive_path, extract_path)
    total     = len(image_files)
    page_size = limit if limit > 0 else mg.IMAGES_PER_LOAD
    batch     = image_files[offset: offset + page_size]
    next_offset = offset + len(batch)
    has_more  = next_offset < total

    # セッションにpage_countを記録
    mg.set_session_page_count(session_token, total)

    image_items = []
    for i, path in enumerate(batch, start=offset):
        filename = os.path.basename(path)
        image_items.append({
            "src":      f"/image/{session_token}/{url_hash}/{filename}",
            "filename": filename,
            "index":    i,
            "total":    total,
        })

    return templates.TemplateResponse(request, "partials/image_batch.html", {
        "request":     request,
        "image_items": image_items,
        "next_offset": next_offset,
        "has_more":    has_more,
        "total":       total,
        "manga_url":   url,
        "webhook1":    webhook1,
        "webhook2":    webhook2,
        "warnings":    warnings,
    })

# ---------------------------------------------------------------------------
# Image binary  GET /image/{session_token}/{url_hash}/{filename}
# ---------------------------------------------------------------------------

def resolve_extracted_file(url_hash: str, filename: str) -> Optional[str]:
    """展開フォルダ直下のファイルだけ許可。外に出る名前は None"""
    base = os.path.realpath(os.path.join(mg.get_cache_dir(), f"{url_hash}_extracted"))
    name = os.path.basename(filename.replace("\\", "/"))
    if not name:
        return None
    path = os.path.realpath(os.path.join(base, name))
    return path if os.path.dirname(path) == base else None

@app.get("/image/{session_token}/{url_hash}/{filename}")
@limiter.limit("60/minute")
async def serve_image(
    request:       Request,
    session_token: str,
    url_hash:      str,
    filename:      str,
):
    # セッション検証
    session = mg.validate_session(session_token, get_client_ip(request))
    if not session:
        raise HTTPException(status_code=403, detail="セッションが無効または期限切れです")

    # url_hashがセッションのcbz_urlと一致するか確認
    expected_hash = hashlib.md5(session["cbz_url"].encode()).hexdigest()
    if url_hash != expected_hash:
        raise HTTPException(status_code=403, detail="不正なアクセスです")
    _enforce_session_unlock(request, session)

    path = resolve_extracted_file(url_hash, filename)
    if path is None or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="画像が見つかりません")

    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(_executor, mg.load_image_bytes, path)

    if data is None:
        raise HTTPException(status_code=500, detail="画像の読み込みに失敗しました")

    media_type = "image/png" if data[:8] == b'\x89PNG\r\n\x1a\n' else "image/jpeg"

    return Response(
        content=data,
        media_type=media_type,
        headers={
            "Cache-Control": "private, max-age=3600",
            "X-Content-Type-Options": "nosniff",
        },
    )

# ---------------------------------------------------------------------------
# Download + progress  GET /download/start  (SSE)
# ---------------------------------------------------------------------------

@app.get("/download/start")
@limiter.limit("3/minute")
async def download_start(request: Request, url: str = Query(...),
                         session_token: str = Query(default="")):
    # セッション検証（必須）＋ URL の形式・宛先ポリシー
    session = mg.validate_session(session_token, get_client_ip(request))
    if not session or session["cbz_url"] != url:
        raise HTTPException(status_code=403, detail="セッションが無効です")
    _enforce_session_unlock(request, session)
    allowed, reason = mg.is_url_fetch_allowed(url)
    if not allowed:
        raise HTTPException(status_code=400, detail=reason)
    file_ext     = os.path.splitext(url.split('?')[0])[-1].lower()
    archive_path = mg.get_cache_path(url) + file_ext

    async def event_stream():
        if os.path.exists(archive_path):
            yield "data: done\n\n"
            return

        loop:  asyncio.AbstractEventLoop = asyncio.get_running_loop()
        queue: asyncio.Queue             = asyncio.Queue()

        def progress_cb(p: float):
            loop.call_soon_threadsafe(queue.put_nowait, p)

        def run_download():
            success, err = mg.download_file(url, archive_path, progress_callback=progress_cb,
                                            auth_token=session.get("auth_token"),
                                            age_verified=bool(session.get("age_verified")))
            if success:
                loop.call_soon_threadsafe(queue.put_nowait, "done")
            else:
                loop.call_soon_threadsafe(queue.put_nowait, f"error:{err}")

        loop.run_in_executor(_executor, run_download)

        while True:
            item = await queue.get()
            if item == "done":
                yield "data: done\n\n"
                break
            elif isinstance(item, str) and item.startswith("error:"):
                yield f"data: {item}\n\n"
                break
            else:
                yield f"data: {item:.3f}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")

# ---------------------------------------------------------------------------
# Thumbnail  GET /thumbnail
# episode_thumbs/ から返す（非スクランブル・事前生成済み）
# ---------------------------------------------------------------------------

@app.get("/thumbnail")
@limiter.limit("30/minute")
async def serve_thumbnail(
    request:       Request,
    url:           str = Query(...),
    index:         int = Query(...),
    session_token: str = Query(default=""),
):
    # セッション検証（必須）
    session = mg.validate_session(session_token, get_client_ip(request))
    if not session or session["cbz_url"] != url:
        raise HTTPException(status_code=403, detail="セッションが無効です")
    _enforce_session_unlock(request, session)

    # 宛先ポリシー（組み立てる thumb URL は url と同一オリジンなので、これで足りる）
    allowed, reason = mg.is_url_fetch_allowed(url)
    if not allowed:
        raise HTTPException(status_code=400, detail=reason)

    # プラットフォームのCBZ URLから thumb URLを組み立てる
    # URL形式: {platform}/api/public/cbz/{uid}/{tid}/{eid}.cbz
    #       → {platform}/api/public/thumb/{uid}/{tid}/{eid}/{index}
    # プレビューURL: {platform}/api/author/preview/{tid}/{eid}.cbz?token=...
    #             → {platform}/api/author/thumb/{tid}/{eid}/{index}?token=...

    thumb_url = None

    if "/api/public/cbz/" in url:
        # 公開作品のサムネイル
        # /api/public/cbz/{uid}/{tid}/{eid}.cbz → /api/public/thumb/{uid}/{tid}/{eid}/{index}
        base    = url.split("/api/public/cbz/")[0]
        parts   = url.split("/api/public/cbz/")[1].replace(".cbz", "").split("/")
        if len(parts) == 3:
            thumb_url = f"{base}/api/public/thumb/{parts[0]}/{parts[1]}/{parts[2]}/{index}"

    elif "/api/author/preview/" in url:
        # プレビュー（作者のみ）
        # /api/author/preview/{tid}/{eid}.cbz?token=xxx → /api/author/thumb/{tid}/{eid}/{index}?token=xxx
        base_and_path = url.split("?")[0]
        token_part    = url.split("?")[1] if "?" in url else ""
        base   = base_and_path.split("/api/author/preview/")[0]
        parts  = base_and_path.split("/api/author/preview/")[1].replace(".cbz", "").split("/")
        if len(parts) == 2:
            thumb_url = f"{base}/api/author/thumb/{parts[0]}/{parts[1]}/{index}"
            if token_part:
                thumb_url += f"?{token_part}"

    if thumb_url:
        try:
            headers = _internal_headers()
            if session.get("auth_token") and "/api/public/thumb/" in thumb_url:
                # 許可制のサムネイルはプラットフォーム側でも読者トークンが要る
                headers["Authorization"] = f"Bearer {session['auth_token']}"
            if session.get("age_verified") and "/api/public/thumb/" in thumb_url:
                headers["X-Age-Verified"] = "1"
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(thumb_url, headers=headers)
                if resp.status_code == 200:
                    return Response(
                        content=resp.content,
                        media_type=resp.headers.get("content-type", "image/jpeg"),
                        headers={"Cache-Control": "private, max-age=86400"},
                    )
        except Exception:
            pass

    # フォールバック: キャッシュから生成（外部ZIP等）
    file_ext     = os.path.splitext(url.split('?')[0])[-1].lower()
    archive_path = mg.get_cache_path(url) + file_ext
    extract_path = mg.get_cache_path(url) + "_extracted"

    if not os.path.exists(archive_path):
        raise HTTPException(status_code=404, detail="アーカイブが見つかりません")

    image_files, _ = mg.extract_archive(archive_path, extract_path)
    if index < 0 or index >= len(image_files):
        raise HTTPException(status_code=404, detail="ページが見つかりません")

    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(
        _thumb_executor,
        lambda: mg.load_image_bytes(image_files[index], max_size=(80, 120)),
    )
    if data is None:
        raise HTTPException(status_code=500, detail="サムネイル生成失敗")

    media_type = "image/png" if data[:8] == b'\x89PNG\r\n\x1a\n' else "image/jpeg"
    return Response(
        content=data,
        media_type=media_type,
        headers={"Cache-Control": "private, max-age=86400"},
    )

# ---------------------------------------------------------------------------
# Discord send  POST /discord/send
# ---------------------------------------------------------------------------

async def _is_webhook_enabled(url: str) -> bool:
    """作者の Discord 送信許可をサーバー側でも確認する。

    外部URLは許可。プラットフォーム公開作品はカタログの webhook_enabled に従い、
    カタログに無い・取得できない場合は不許可（リーダー側の判定と一致させる）。
    """
    if "/api/public/cbz/" not in url:
        return True
    try:
        catalog = await _fetch_platform_catalog(timeout=5.0)
    except Exception:
        return False
    for item in catalog:
        if item.get("cbz_url") == url:
            return bool(item.get("webhook_enabled", False))
    return False

@app.post("/discord/send", response_class=HTMLResponse)
async def discord_send(
    request:  Request,
    url:      Annotated[str, Form()],
    filename: Annotated[str, Form()],
    webhook1: Annotated[str, Form()] = "",
    webhook2: Annotated[str, Form()] = "",
    session_token: Annotated[str, Form()] = "",
):
    # セッション検証（必須）
    session = mg.validate_session(session_token, get_client_ip(request))
    if not session or session["cbz_url"] != url:
        return HTMLResponse("<span class='status-error'>セッションが無効です</span>")

    # 作者の送信許可（プラットフォーム作品のみサーバー側でも確認する）
    if not await _is_webhook_enabled(url):
        return HTMLResponse("<span class='status-error'>この作品では Discord 送信が許可されていません</span>")

    url_hash   = hashlib.md5(url.encode()).hexdigest()
    image_path = resolve_extracted_file(url_hash, filename)
    if image_path is None:
        return HTMLResponse("<span class='status-error'>不正なファイル名です</span>")

    webhooks = [w for w in [webhook1, webhook2] if w]
    if not webhooks:
        return HTMLResponse("<span class='status-error'>Webhookが設定されていません</span>")

    errors = mg.send_image_to_discord(image_path, webhooks, os.path.basename(image_path))
    if errors:
        # エラー文には送信先URLや例外メッセージ（入力由来）が入るので、1件ずつエスケープする
        return HTMLResponse(
            "<span class='status-error'>" + "<br>".join(html.escape(e) for e in errors) + "</span>")
    return HTMLResponse("<span class='status-ok'>✓ 送信しました</span>")

# ---------------------------------------------------------------------------
# Share URL  GET /share/url
# ---------------------------------------------------------------------------

@app.get("/share/url")
async def share_url(
    request: Request,
    share:   str  = Query(default=""),
    shorten: bool = Query(default=True),
):
    base         = str(request.base_url).rstrip("/")
    encoded      = urllib.parse.quote(share, safe="")
    full_url     = f"{base}/?share={encoded}"
    result       = mg.shorten_url(full_url) if shorten else full_url
    return Response(content=result, media_type="text/plain")