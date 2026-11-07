import os
import re
import uuid
import json
import time
import shutil
import zipfile
import logging
import tempfile
import secrets
import asyncio
import threading
from collections import deque
from io import BytesIO
from pathlib import Path
from datetime import datetime, timedelta, UTC
from typing import List, Generator, Optional

from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr
import bcrypt
from jose import jwt, JWTError
from PIL import Image
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, UniqueConstraint
from sqlalchemy.orm import sessionmaker, declarative_base, Session
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

# ---------------------------------------------------------------------------
# ログ設定
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ディレクトリ準備
# ---------------------------------------------------------------------------
TMP_DIR       = Path("./storage/tmp")
FINAL_ZIP_DIR = Path("./storage/zips")
for d in [TMP_DIR, FINAL_ZIP_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------
INSTANCE_DIR = Path("./instance")

def _load_secret_key() -> str:
    """環境変数 MANGA_SECRET_KEY > instance/secret_key > 新規生成（0600）"""
    env_key = os.environ.get("MANGA_SECRET_KEY", "").strip()
    if env_key:
        return env_key
    key_path = INSTANCE_DIR / "secret_key"
    if not key_path.exists():
        INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
        # 一時ファイルに書き切ってから link で公開（公開時点で常に完成済み）
        fd, tmp_name = tempfile.mkstemp(dir=INSTANCE_DIR, prefix=".secret_key.")  # 権限 0600
        try:
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_urlsafe(48))
            try:
                os.link(tmp_name, key_path)
            except FileExistsError:
                pass  # 先着の鍵を使う
        finally:
            os.unlink(tmp_name)
    key = key_path.read_text().strip()
    if not key:
        raise RuntimeError(f"{key_path} が空です。削除して再起動するか MANGA_SECRET_KEY を設定してください")
    return key

SECRET_KEY                 = _load_secret_key()
ALGORITHM                  = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60
MAX_PAGE_SIZE_MB            = 10
MAX_TOTAL_SIZE_MB           = 100
MAX_COVER_SIZE_MB           = 5
MAX_PAGE_SIZE_BYTES         = MAX_PAGE_SIZE_MB  * 1024 * 1024
MAX_TOTAL_SIZE_BYTES        = MAX_TOTAL_SIZE_MB * 1024 * 1024
MAX_COVER_SIZE_BYTES        = MAX_COVER_SIZE_MB * 1024 * 1024
DISPLAY_MAX_SIZE            = (1200, 1800)   # アップロード時リサイズ上限
THUMB_SIZE                  = (150, 220)     # サムネイルサイズ
TILE_SIZE_DEFAULT           = 16             # スクランブルタイルサイズ
ALLOWED_TILE_SIZES          = (8, 16, 32)    # UI の選択肢と同じ（閲覧側の復号もこの前提）
CHARSET_62                  = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

def validate_tile_size(tile_size: int) -> None:
    """tile_size が許可値でなければ 400（画面が理由を表示できるよう HTTPException で返す）"""
    if tile_size not in ALLOWED_TILE_SIZES:
        raise HTTPException(
            status_code=400,
            detail=f"tile_size は {ALLOWED_TILE_SIZES} のいずれかにしてください（受信値: {tile_size}）",
        )

# ---------------------------------------------------------------------------
# データベース
# ---------------------------------------------------------------------------
DATABASE_URL = "sqlite:///./storage/manga_platform.db"
engine       = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base         = declarative_base()

class User(Base):
    __tablename__ = "users"
    id              = Column(Integer, primary_key=True, index=True)
    username        = Column(String,  unique=True, index=True, nullable=False)
    email           = Column(String,  unique=True, index=True, nullable=False)
    hashed_password = Column(String,  nullable=False)
    is_active       = Column(Boolean, default=True)
    # author=作家用 / reader=読むためだけのアカウント（アップロード不可）
    account_type    = Column(String,  default="author")
    # 年齢確認の自己申告をした日時（NULL=未申告）
    adult_declared_at = Column(DateTime, nullable=True)

class Grant(Base):
    """作家が読者へ出す作品（title）単位の閲覧許可。"""
    __tablename__ = "grants"
    id         = Column(Integer, primary_key=True, index=True)
    author_id  = Column(Integer, index=True, nullable=False)
    title_id   = Column(Integer, index=True, nullable=False)
    user_id    = Column(Integer, index=True, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(UTC))
    __table_args__ = (UniqueConstraint("author_id", "title_id", "user_id"),)

class Suspension(Base):
    """管理者による作品（title）単位の公開停止。lifted_at が NULL の行が有効。"""
    __tablename__ = "suspensions"
    id           = Column(Integer, primary_key=True, index=True)
    author_id    = Column(Integer, index=True, nullable=False)
    title_id     = Column(Integer, index=True, nullable=False)
    reason       = Column(String,  default="")
    suspended_by = Column(Integer, nullable=False)
    created_at   = Column(DateTime, default=lambda: datetime.now(UTC))
    lifted_at    = Column(DateTime, nullable=True)

Base.metadata.create_all(bind=engine)


def _migrate_users_columns() -> None:
    """既存 DB の users テーブルに後付けの列が無ければ足す（create_all は
    既存テーブルを変更しないため）。新規 DB では何もしない。"""
    from sqlalchemy import inspect, text
    cols = {c["name"] for c in inspect(engine).get_columns("users")}
    with engine.begin() as conn:
        if "account_type" not in cols:
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN account_type VARCHAR DEFAULT 'author'"))
        if "adult_declared_at" not in cols:
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN adult_declared_at DATETIME"))


_migrate_users_columns()

def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ---------------------------------------------------------------------------
# 認証
# ---------------------------------------------------------------------------
# パスワードハッシュは bcrypt を直接使う（passlib は使わない）。
# passlib 経由で作られた既存の $2b$ ハッシュもそのまま検証できる。
# 72バイト超は passlib と同じく先頭72バイトで切り詰める（問題13-A 以前は
# 72バイト超のパスワードも登録できたため。新しい bcrypt は切り詰めずに
# エラーになるので、ここで明示する）。
def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8")[:72], bcrypt.gensalt()).decode("utf-8")


def _verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8")[:72], hashed.encode("utf-8"))
    except ValueError:
        return False
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/token")

def user_from_token(token: str, db: Session) -> User:
    """トークンからユーザーを引く共通処理。

    デコード失敗・sub の形式不正・ユーザー不在・停止中（is_active=False）は
    すべて 401 にする。手書きデコードの重複をここに集約する。
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = int(payload.get("sub"))
    except (JWTError, TypeError, ValueError):
        raise HTTPException(status_code=401, detail="トークンが無効または期限切れです")
    user = db.query(User).filter(User.id == user_id).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="ユーザーが見つかりません")
    return user

def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db)
) -> User:
    return user_from_token(token, db)


def get_current_author(current_user: User = Depends(get_current_user)) -> User:
    """作者系エンドポイント用。reader アカウントは使えない。"""
    if current_user.account_type == "reader":
        raise HTTPException(status_code=403, detail="読者アカウントでは利用できません")
    return current_user


def is_admin(user: User) -> bool:
    """管理者は環境変数 MANGA_ADMIN_USERS（ユーザー名のカンマ区切り）で指定する。"""
    names = {n.strip() for n in os.environ.get("MANGA_ADMIN_USERS", "").split(",") if n.strip()}
    return user.username in names


def get_current_admin(current_user: User = Depends(get_current_user)) -> User:
    if not is_admin(current_user):
        raise HTTPException(status_code=403, detail="管理者権限が必要です")
    return current_user


def _active_suspension(db: Session, author_id: int, title_id: int):
    """有効な公開停止があればその行を返す。"""
    return (
        db.query(Suspension)
        .filter(Suspension.author_id == author_id,
                Suspension.title_id == title_id,
                Suspension.lifted_at.is_(None))
        .first()
    )


def _has_grant(db: Session, author_id: int, title_id: int, user_id: int) -> bool:
    return (
        db.query(Grant)
        .filter(Grant.author_id == author_id,
                Grant.title_id == title_id,
                Grant.user_id == user_id)
        .first()
        is not None
    )


def _optional_reader(request: Request, db: Session):
    """Authorization ヘッダがあれば検証してユーザーを返し、無ければ None。
    ヘッダがあるのに無効なら 401（黙って匿名扱いに落とさない）。"""
    auth = request.headers.get("Authorization", "")
    if not auth:
        return None
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="トークンが無効または期限切れです")
    return user_from_token(auth[len("Bearer "):], db)


def _enforce_not_suspended(db: Session, author_id: int, title_id: int) -> None:
    if _active_suspension(db, author_id, title_id):
        raise HTTPException(status_code=403, detail="この作品は公開が停止されています")


def _enforce_premium_access(meta: dict, author_id: int, title_id: int,
                            request: Request, db: Session) -> None:
    """premium エピソードの配信前チェック（CBZ・サムネイル共通）。
    public なら何もしない。拒否は HTTPException。"""
    if meta.get("access_level") != "premium":
        return
    reader = _optional_reader(request, db)
    if reader is None:
        raise HTTPException(status_code=401, detail="この作品を読むにはログインが必要です")
    if reader.id == author_id:
        return
    if not _has_grant(db, author_id, title_id, reader.id):
        raise HTTPException(status_code=403, detail="この作品を読む許可がありません")
    if meta.get("age_restricted") and reader.adult_declared_at is None:
        raise HTTPException(status_code=403, detail="この作品を読むには年齢確認が必要です")


def _enforce_age_access(meta: dict, author_id: int, request: Request, db: Session) -> None:
    """年齢対象の無料作品の配信前チェック（CBZ・サムネイル共通）。

    通れるのは：作者本人、年齢申告済みのログインユーザー、そして年齢ゲートを
    通過した閲覧側からの取得（内部通信＋ X-Age-Verified ヘッダ）。
    premium の年齢判定は _enforce_premium_access 側で行う。
    """
    if not meta.get("age_restricted") or meta.get("access_level") == "premium":
        return
    reader = _optional_reader(request, db)
    if reader is not None and (reader.id == author_id or reader.adult_declared_at is not None):
        return
    if _is_internal_request(request) and request.headers.get("X-Age-Verified") == "1":
        return
    raise HTTPException(status_code=403, detail="年齢確認が必要です")

# ---------------------------------------------------------------------------
# Pydantic スキーマ
# ---------------------------------------------------------------------------
class UserRegister(BaseModel):
    username: str
    email: EmailStr
    password: str
    account_type: str = "author"   # author / reader（閲覧側の登録は reader）


class GrantRequest(BaseModel):
    username: str


class AdminSuspendRequest(BaseModel):
    reason: str = ""

class EpisodePublishSettings(BaseModel):
    status:          str
    access_level:    str
    title_name:      str  = ""
    episode_name:    str  = ""
    caption:         str  = ""   # 公開：作品紹介文
    comment:         str  = ""   # 公開：作者コメント
    note:            str  = ""   # 非公開：作者備考
    scrambled:       bool = True
    tile_size:       int  = TILE_SIZE_DEFAULT
    webhook_enabled: bool = False
    age_restricted:  bool = False

# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------

def get_client_ip(request: Request) -> str:
    """レート制限用のクライアントIP取得。

    原則は接続元アドレスを使う。CF-Connecting-IP はクライアントが自由に
    書けるヘッダなので無条件には信頼せず、cloudflared の裏で動かす場合に
    限り、環境変数 MANGA_TRUST_CF_HEADER=1 のときだけ採用する。
    """
    if os.environ.get("MANGA_TRUST_CF_HEADER") == "1":
        cf_ip = request.headers.get("CF-Connecting-IP")
        if cf_ip:
            return cf_ip
    return request.client.host if request.client else "unknown"

def natural_keys(text: str):
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', text)]

def to_base62(n: int, width: int = 4) -> str:
    if n == 0:
        return "0".zfill(width)
    res = ""
    while n > 0:
        n, r = divmod(n, 62)
        res = CHARSET_62[r] + res
    return res.zfill(width)

def generate_page_filename(index: int) -> str:
    """スクランブルあり・なし共通のランダムファイル名生成"""
    prefix      = to_base62(index, width=4)
    random_part = secrets.token_hex(8)   # 16文字hex = 64bit
    return f"{prefix}{random_part}.png"

class Xorshift32:
    def __init__(self, seed: int):
        self.state = seed & 0xFFFFFFFF
        if self.state == 0:
            self.state = 1

    def next(self) -> int:
        x = self.state
        x ^= (x << 13) & 0xFFFFFFFF
        x ^= (x >> 17) & 0xFFFFFFFF
        x ^= (x << 5)  & 0xFFFFFFFF
        self.state = x & 0xFFFFFFFF
        return self.state

def xorshift_shuffle(items: list, seed: int) -> list:
    rng = Xorshift32(seed)
    res = items[:]
    for i in range(len(res) - 1, 0, -1):
        j = rng.next() % (i + 1)
        res[i], res[j] = res[j], res[i]
    return res

def scramble_image(img: Image.Image, seed: int, tile_size: int) -> Image.Image:
    """画像をタイル単位でスクランブルする"""
    width, height  = img.size
    cols           = width  // tile_size
    rows           = height // tile_size
    num_tiles      = cols * rows
    shuffled       = xorshift_shuffle(list(range(num_tiles)), seed)
    scrambled_img  = img.copy()

    for i in range(num_tiles):
        dest_idx = shuffled[i]
        sx, sy   = (i        % cols) * tile_size, (i        // cols) * tile_size
        dx, dy   = (dest_idx % cols) * tile_size, (dest_idx // cols) * tile_size
        tile     = img.crop((sx, sy, sx + tile_size, sy + tile_size))
        scrambled_img.paste(tile, (dx, dy))

    # 端数領域はそのままコピー
    if width % tile_size > 0:
        ex = cols * tile_size
        scrambled_img.paste(img.crop((ex, 0, width, height)), (ex, 0))
    if height % tile_size > 0:
        ey = rows * tile_size
        scrambled_img.paste(img.crop((0, ey, width, height)), (0, ey))

    return scrambled_img

def generate_thumbnail(img_path: Path, thumb_dir: Path, index: int) -> Path:
    """サムネイル生成（非スクランブル・小サイズ）"""
    thumb_path = thumb_dir / f"thumb_{index:04d}.jpg"
    with Image.open(img_path) as img:
        img = img.convert("RGB")
        img.thumbnail(THUMB_SIZE, Image.LANCZOS)
        img.save(thumb_path, "JPEG", quality=80)
    return thumb_path

# ---------------------------------------------------------------------------
# アップロード進捗ストア（メモリ内）
# ---------------------------------------------------------------------------
upload_progress: dict[str, dict] = {}

# ---------------------------------------------------------------------------
# ビューカウント用IPクールダウン（メモリ内）
# key: "{user_id}:{title_id}:{episode_id}:{ip}" → 最終カウント時刻
# ---------------------------------------------------------------------------
_view_cooldown: dict[str, float] = {}
VIEW_COOLDOWN_SECONDS = 3600  # 同一IPは1時間に1カウント

# ---------------------------------------------------------------------------
# settings.json の書き込み（ロック＋原子的差し替え）
# ---------------------------------------------------------------------------
_settings_lock = threading.Lock()

def _save_settings(settings_path: Path, meta: dict) -> None:
    """settings.json を一時ファイル経由で原子的に書き換える。

    書き手（設定保存・閲覧数カウント）は _settings_lock の下で
    読み→更新→この関数、の順で使うこと。読み手は rename のため
    常に完全なファイルだけを見る。
    """
    tmp_path = settings_path.with_suffix(".json.tmp")
    with open(tmp_path, "w") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, settings_path)

def increment_view_count(settings_path: Path, client_ip: str) -> None:
    """settings.jsonのview_countを1増やす（IPクールダウン付き）"""
    cooldown_key = f"{settings_path}:{client_ip}"
    now = time.time()
    last = _view_cooldown.get(cooldown_key, 0)
    if now - last < VIEW_COOLDOWN_SECONDS:
        return  # クールダウン中はカウントしない
    try:
        with _settings_lock:
            with open(settings_path, "r") as f:
                meta = json.load(f)
            meta["view_count"] = meta.get("view_count", 0) + 1
            _save_settings(settings_path, meta)
        _view_cooldown[cooldown_key] = now
    except Exception as e:
        logger.warning(f"view_count更新失敗: {e}")

def update_progress(job_id: str, phase: str, current: int, total: int, **kwargs):
    upload_progress[job_id] = {
        "phase":   phase,
        "current": current,
        "total":   total,
        **kwargs,
    }

# ---------------------------------------------------------------------------
# レート制限
# ---------------------------------------------------------------------------
def get_ip_for_limit(request: Request) -> str:
    """レート制限のキー。

    閲覧側経由の認証では、閲覧側が読者の実IPから計算した不透明な
    クライアントキー（X-Viewer-Client-Key）をキーに使うことで、
    全読者が閲覧側サーバーの IP 枠を分け合わないようにする。
    生IPはプラットフォームに届かない。ヘッダを信頼するのは内部
    リクエスト（localhost＋内部トークン一致）のときだけ。
    """
    if _is_internal_request(request):
        key = request.headers.get("X-Viewer-Client-Key", "")
        if re.fullmatch(r"[0-9a-f]{64}", key):
            return "viewer:" + key
    return get_client_ip(request)


# ---------------------------------------------------------------------------
# ログインのユーザー名単位カウンタ（IP/クライアントキー単位と併用）
#
# slowapi の key_func は非同期関数に対応しておらず（コルーチンがそのまま
# キー文字列化されて枠が貯まらないことを実測で確認）、フォームの
# ユーザー名をキーにできないため、ハンドラ側で失敗回数を数える。
# 送信元を変えても、閲覧側の秘密の差し替えでクライアントキーが全交換
# されても、同一アカウントへの失敗は数え続けられる。成功でリセット。
# ---------------------------------------------------------------------------
_LOGIN_USER_LIMIT = 5
_LOGIN_USER_WINDOW = 60.0
_login_failures: dict = {}
_login_failures_lock = threading.Lock()


def _login_user_blocked(username: str) -> bool:
    """直近ウィンドウ内の失敗が上限に達していれば True。"""
    now = time.monotonic()
    with _login_failures_lock:
        q = _login_failures.get(username)
        if not q:
            return False
        cutoff = now - _LOGIN_USER_WINDOW
        while q and q[0] < cutoff:
            q.popleft()
        return len(q) >= _LOGIN_USER_LIMIT


def _record_login_failure(username: str) -> None:
    now = time.monotonic()
    with _login_failures_lock:
        q = _login_failures.setdefault(username, deque())
        cutoff = now - _LOGIN_USER_WINDOW
        while q and q[0] < cutoff:
            q.popleft()
        q.append(now)
        if len(_login_failures) > 4096:
            stale = [n for n, dq in _login_failures.items()
                     if not dq or dq[-1] < cutoff]
            for n in stale:
                _login_failures.pop(n, None)


def _clear_login_failures(username: str) -> None:
    with _login_failures_lock:
        _login_failures.pop(username, None)

def _is_internal_request(request: Request) -> bool:
    """同一マシン上の内部クライアント（閲覧側など）からのリクエストかどうか。

    localhost からの接続で、環境変数 MANGA_INTERNAL_TOKEN と一致する
    X-Internal-Token ヘッダがある場合のみ内部扱いにする。
    内部リクエストは公開カタログ・CBZ のレート制限の対象外
    （読者全員分を代表して取得するため、IP 枠を共有させない）。
    """
    token = os.environ.get("MANGA_INTERNAL_TOKEN", "")
    if not token:
        return False
    if not request.client or request.client.host not in ("127.0.0.1", "::1"):
        return False
    return secrets.compare_digest(request.headers.get("X-Internal-Token", ""), token)

limiter = Limiter(key_func=get_ip_for_limit)

# ---------------------------------------------------------------------------
# リクエストボディサイズ制限ミドルウェア
# ---------------------------------------------------------------------------
class LimitUploadSizeMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if "upload-and-zip" in str(request.url.path):
            content_length = request.headers.get("content-length")
            if content_length and int(content_length) > MAX_TOTAL_SIZE_BYTES + 5 * 1024 * 1024:
                return Response("リクエストサイズが上限を超えています", status_code=413)
        return await call_next(request)

# ---------------------------------------------------------------------------
# FastAPI アプリ
# ---------------------------------------------------------------------------
app = FastAPI()
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)
app.add_middleware(LimitUploadSizeMiddleware)
# CORS: 同梱の画面はすべて同一オリジンで通信するので、既定では無効。
# 別オリジンのクライアントを許すときだけ、環境変数 MANGA_CORS_ORIGINS に
# 許可するオリジンをカンマ区切りで指定する（認証は Bearer なので credentials は不要）。
_cors_origins = [o.strip() for o in os.environ.get("MANGA_CORS_ORIGINS", "").split(",") if o.strip()]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return Response("リクエストが多すぎます。しばらく待ってから再試行してください。", status_code=429)

# ---------------------------------------------------------------------------
# 認証エンドポイント
# ---------------------------------------------------------------------------

@app.post("/api/auth/signup")
@limiter.limit("3/hour")
async def signup(request: Request, user_data: UserRegister, db: Session = Depends(get_db)):
    if db.query(User).filter(User.email == user_data.email).first():
        raise HTTPException(400, "登録済みのメールアドレスです")
    if db.query(User).filter(User.username == user_data.username).first():
        raise HTTPException(400, "使用済みのユーザー名です")
    if user_data.account_type not in ("author", "reader"):
        raise HTTPException(400, "アカウント種別が不正です")
    if len(user_data.password) < 8:
        raise HTTPException(400, "パスワードは8文字以上で入力してください")
    if len(user_data.password.encode("utf-8")) > 72:
        raise HTTPException(400, "パスワードが長すぎます（72バイトまで）")
    hashed_pw = _hash_password(user_data.password)
    db.add(User(username=user_data.username, email=user_data.email,
                hashed_password=hashed_pw, account_type=user_data.account_type))
    db.commit()
    return {"message": "ユーザー登録が完了しました。ログインしてください。"}

@app.post("/api/auth/token")
@limiter.limit("5/minute")
async def login(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db)
):
    if _login_user_blocked(form_data.username):
        raise HTTPException(429, "リクエストが多すぎます。しばらく待ってから再試行してください。")
    user = db.query(User).filter(User.username == form_data.username).first()
    if not user or not _verify_password(form_data.password, user.hashed_password):
        _record_login_failure(form_data.username)
        raise HTTPException(401, "ユーザー名またはパスワードが正しくありません")
    if not user.is_active:
        _record_login_failure(form_data.username)
        raise HTTPException(401, "ユーザー名またはパスワードが正しくありません")
    _clear_login_failures(form_data.username)
    access_token = jwt.encode(
        {"sub": str(user.id), "exp": datetime.now(UTC) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)},
        SECRET_KEY, algorithm=ALGORITHM
    )
    return {"access_token": access_token, "token_type": "bearer", "username": user.username}

@app.get("/api/auth/me")
async def auth_me(current_user: User = Depends(get_current_user)):
    return {
        "id":                current_user.id,
        "username":          current_user.username,
        "email":             current_user.email,
        "account_type":      current_user.account_type or "author",
        "is_admin":          is_admin(current_user),
        "adult_declared_at": (current_user.adult_declared_at.isoformat()
                              if current_user.adult_declared_at else None),
    }


@app.get("/api/auth/grants")
async def my_grants(
    current_user: User = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """自分に許可が出ている作品（author_id, title_id）の一覧。閲覧側の表示用。"""
    rows = db.query(Grant).filter(Grant.user_id == current_user.id).all()
    return [{"author_id": g.author_id, "title_id": g.title_id} for g in rows]


@app.post("/api/auth/declare-adult")
async def declare_adult(
    current_user: User = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """年齢確認の自己申告を記録する（取り消しはできない）。"""
    if current_user.adult_declared_at is None:
        current_user.adult_declared_at = datetime.now(UTC)
        db.commit()
    return {"adult_declared_at": current_user.adult_declared_at.isoformat()}


# ---------------------------------------------------------------------------
# 作者：自分の作品一覧
# ---------------------------------------------------------------------------

@app.get("/api/author/works")
async def get_my_works(
    current_user: User = Depends(get_current_author),
    db:           Session = Depends(get_db),
):
    works    = []
    user_dir = FINAL_ZIP_DIR / f"user_{current_user.id}"
    if not user_dir.exists():
        return []
    susp_cache = {}
    for settings_path in sorted(user_dir.glob("title_*/episode_*_settings.json")):
        try:
            with open(settings_path, "r") as f:
                meta = json.load(f)
            cbz_path = settings_path.parent / f"episode_{meta['episode_id']}.cbz"
            cover_path = settings_path.parent / f"episode_{meta['episode_id']}_cover.jpg"
            meta["has_cbz"]   = cbz_path.exists()
            meta["has_cover"] = cover_path.exists()
            tid = meta.get("title_id")
            if tid not in susp_cache:
                susp_cache[tid] = _active_suspension(db, current_user.id, tid)
            susp = susp_cache[tid]
            meta["suspended"]      = susp is not None
            meta["suspend_reason"] = susp.reason if susp else None
            works.append(meta)
        except Exception:
            continue
    return works

# ---------------------------------------------------------------------------
# 作者：アップロード開始（job_idを返してバックグラウンドで処理）
# ---------------------------------------------------------------------------

# 同時に処理するアップロードジョブの上限（メモリ使用量を抑える）
_upload_semaphore = threading.Semaphore(2)

@app.post("/api/author/titles/{title_id}/episodes/{episode_id}/upload-and-zip")
async def upload_manga(
    title_id:         int,
    episode_id:       int,
    background_tasks: BackgroundTasks,
    files:            List[UploadFile] = File(...),
    cover:            Optional[UploadFile] = File(default=None),
    do_scramble:      bool = True,
    tile_size:        int  = TILE_SIZE_DEFAULT,
    current_user:     User = Depends(get_current_author),
):
    validate_tile_size(tile_size)

    # ── job_id 発行 ──
    job_id = uuid.uuid4().hex
    update_progress(job_id, "queued", 0, len(files))

    # ── バリデーションしながら一時ファイルへ書き出す ──
    # ファイル内容をメモリに保持せず、背景処理へはパスを渡す
    spool_dir = TMP_DIR / f"spool_{job_id}"
    spool_dir.mkdir(parents=True, exist_ok=True)
    file_paths: list = []
    cover_spool_path = None
    cover_filename   = None
    try:
        total_size = 0
        for i, f in enumerate(files):
            spool_path = spool_dir / f"upload_{i:04d}"
            size = 0
            with open(spool_path, "wb") as out:
                while chunk := await f.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_PAGE_SIZE_BYTES:
                        raise HTTPException(400, f"{f.filename} が {MAX_PAGE_SIZE_MB}MB を超えています")
                    total_size += len(chunk)
                    if total_size > MAX_TOTAL_SIZE_BYTES:
                        raise HTTPException(400, f"合計サイズが {MAX_TOTAL_SIZE_MB}MB を超えています")
                    out.write(chunk)
            file_paths.append((f.filename, spool_path))

        if cover:
            cover_filename   = cover.filename
            cover_spool_path = spool_dir / "cover_upload"
            cover_size = 0
            with open(cover_spool_path, "wb") as out:
                while chunk := await cover.read(1024 * 1024):
                    cover_size += len(chunk)
                    if cover_size > MAX_COVER_SIZE_BYTES:
                        raise HTTPException(400, f"バンプ画像が {MAX_COVER_SIZE_MB}MB を超えています")
                    out.write(chunk)
    except HTTPException:
        shutil.rmtree(spool_dir, ignore_errors=True)
        upload_progress.pop(job_id, None)
        raise

    background_tasks.add_task(
        _process_upload,
        job_id, title_id, episode_id,
        file_paths, cover_spool_path, cover_filename,
        do_scramble, tile_size, current_user.id, spool_dir,
    )

    return {"job_id": job_id, "total_files": len(files)}

def _process_upload(
    job_id:           str,
    title_id:         int,
    episode_id:       int,
    file_paths:       list,
    cover_spool_path: Optional[Path],
    cover_filename:   Optional[str],
    do_scramble:      bool,
    tile_size:        int,
    user_id:          int,
    spool_dir:        Path,
):
    """バックグラウンドで実行されるアップロード処理"""
    user_dir  = FINAL_ZIP_DIR / f"user_{user_id}" / f"title_{title_id}"
    user_dir.mkdir(parents=True, exist_ok=True)
    thumb_dir = user_dir / f"episode_{episode_id}_thumbs"
    final_zip_path = user_dir / f"episode_{episode_id}.cbz"
    work_dir = TMP_DIR / f"u{user_id}_t{title_id}_e{episode_id}_{uuid.uuid4().hex[:6]}"
    work_dir.mkdir(parents=True, exist_ok=True)
    # 成果物は作業領域で完成させてから差し替える（失敗時に既存の公開物を壊さない）
    work_thumb_dir = work_dir / "thumbs"
    work_thumb_dir.mkdir(exist_ok=True)
    tmp_zip_path   = work_dir / f"episode_{episode_id}.cbz"
    tmp_cover_path = work_dir / "cover.jpg"

    _upload_semaphore.acquire()
    try:
        # ── ファイル名でソート ──
        file_paths.sort(key=lambda x: natural_keys(x[0]))
        total = len(file_paths)
        saved_pages = []

        for index, (filename, spool_path) in enumerate(file_paths):
            suffix = Path(filename).suffix.lower()
            if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
                continue

            update_progress(job_id, "resize", index + 1, total)

            raw_path = spool_path  # 受け取り時に書き出した一時ファイルをそのまま使う

            # リサイズ（スクランブル前）
            with Image.open(raw_path) as img:
                img = img.convert("RGB")
                img.thumbnail(DISPLAY_MAX_SIZE, Image.LANCZOS)
                resized_path = work_dir / f"resized_{index:04d}.png"
                img.save(resized_path, "PNG")

            # サムネイル生成（非スクランブル）
            generate_thumbnail(resized_path, work_thumb_dir, index)

            update_progress(job_id, "scramble", index + 1, total)

            # スクランブル or そのまま保存
            page_filename = generate_page_filename(index)
            page_path     = work_dir / page_filename

            with Image.open(resized_path) as img:
                img = img.convert("RGB")
                if do_scramble:
                    seed_hex = page_filename[4:12]
                    seed     = int(seed_hex, 16)
                    img      = scramble_image(img, seed, tile_size)
                img.save(page_path, "PNG")

            saved_pages.append(page_path)
            raw_path.unlink()
            resized_path.unlink()

        if not saved_pages:
            raise ValueError("有効な画像ファイルがありませんでした")

        # ── CBZ作成（ZIP_STORED: 画像は既に圧縮済みのため再圧縮不要）──
        update_progress(job_id, "zip", 0, len(saved_pages))
        with zipfile.ZipFile(tmp_zip_path, 'w', zipfile.ZIP_STORED) as comic_zip:
            for i, p in enumerate(saved_pages):
                update_progress(job_id, "zip", i + 1, len(saved_pages))
                comic_zip.write(p, arcname=p.name)

        # ── バンプ画像保存（作業領域へ）──
        if cover_spool_path and cover_filename:
            with Image.open(cover_spool_path) as img:
                img = img.convert("RGB")
                img.thumbnail((600, 900), Image.LANCZOS)
                img.save(tmp_cover_path, "JPEG", quality=85)

        # ── 成果物の差し替え（ここまでは既存の公開物に触れていない）──
        os.replace(tmp_zip_path, final_zip_path)
        if thumb_dir.exists():
            shutil.rmtree(thumb_dir)
        os.replace(work_thumb_dir, thumb_dir)
        if tmp_cover_path.exists():
            os.replace(tmp_cover_path, user_dir / f"episode_{episode_id}_cover.jpg")

        update_progress(
            job_id, "done", len(saved_pages), len(saved_pages),
            page_count=len(saved_pages),
            scrambled=do_scramble,
            tile_size=tile_size,
        )

    except Exception as e:
        update_progress(job_id, "error", 0, 0, message=str(e))
        logger.error(f"Upload error [{job_id}]: {e}")
    finally:
        _upload_semaphore.release()
        if work_dir.exists():
            shutil.rmtree(work_dir)
        if spool_dir.exists():
            shutil.rmtree(spool_dir)

# ---------------------------------------------------------------------------
# 作者：アップロード進捗（ポーリング用GET）
# SSEはEventSourceがカスタムヘッダーを送れないため、
# 通常のGETポーリングで実装する
# ---------------------------------------------------------------------------

@app.get("/api/author/upload-progress/{job_id}")
async def get_upload_progress(
    job_id: str,
    current_user: User = Depends(get_current_author)
):
    progress = upload_progress.get(job_id)
    if not progress:
        raise HTTPException(status_code=404, detail="job not found")

    # done/error の場合はストアから削除
    if progress["phase"] in ("done", "error"):
        upload_progress.pop(job_id, None)

    return progress

# ---------------------------------------------------------------------------
# 作者：設定保存
# ---------------------------------------------------------------------------

@app.patch("/api/author/titles/{title_id}/episodes/{episode_id}/settings")
async def update_settings(
    title_id:     int,
    episode_id:   int,
    settings:     EpisodePublishSettings,
    current_user: User = Depends(get_current_author)
):
    validate_tile_size(settings.tile_size)

    user_dir = FINAL_ZIP_DIR / f"user_{current_user.id}" / f"title_{title_id}"
    user_dir.mkdir(parents=True, exist_ok=True)
    settings_path = user_dir / f"episode_{episode_id}_settings.json"

    # CBZ が無いエピソードは公開できない（カタログに並ぶのに開けない作品を防ぐ）
    if settings.status == "published" and not (user_dir / f"episode_{episode_id}.cbz").exists():
        raise HTTPException(status_code=400, detail="先に原稿をアップロードしてください")

    with _settings_lock:
        existing = {}
        if settings_path.exists():
            with open(settings_path, "r") as f:
                existing = json.load(f)

        existing.update({
            "status":          settings.status,
            "access_level":    settings.access_level,
            "title_name":      settings.title_name,
            "episode_name":    settings.episode_name,
            "caption":         settings.caption,
            "comment":         settings.comment,
            "note":            settings.note,
            "scrambled":       settings.scrambled,
            "tile_size":       settings.tile_size,
            "webhook_enabled": settings.webhook_enabled,
            "age_restricted":  settings.age_restricted,
            "author_name":     current_user.username,
            "author_id":       current_user.id,
            "title_id":        title_id,
            "episode_id":      episode_id,
            "updated_at":      time.time(),
        })
        if "created_at" not in existing:
            existing["created_at"] = time.time()

        _save_settings(settings_path, existing)

    return {"status": "success"}

# ---------------------------------------------------------------------------
# 作者：エピソード削除
# ---------------------------------------------------------------------------

@app.delete("/api/author/titles/{title_id}/episodes/{episode_id}")
async def delete_episode(
    title_id:     int,
    episode_id:   int,
    current_user: User = Depends(get_current_author)
):
    user_dir      = FINAL_ZIP_DIR / f"user_{current_user.id}" / f"title_{title_id}"
    cbz_path      = user_dir / f"episode_{episode_id}.cbz"
    settings_path = user_dir / f"episode_{episode_id}_settings.json"
    cover_path    = user_dir / f"episode_{episode_id}_cover.jpg"
    thumb_dir     = user_dir / f"episode_{episode_id}_thumbs"

    deleted = []
    for path in [cbz_path, settings_path, cover_path]:
        if path.exists():
            path.unlink()
            deleted.append(path.name)
    if thumb_dir.exists():
        shutil.rmtree(thumb_dir)
        deleted.append("thumbs/")

    if not deleted:
        raise HTTPException(404, "削除対象のファイルが見つかりません")

    if user_dir.exists() and not any(user_dir.iterdir()):
        user_dir.rmdir()

    return {"status": "success", "deleted": deleted}

# ---------------------------------------------------------------------------
# 作者：プレビュー用CBZ
# ---------------------------------------------------------------------------

@app.get("/api/author/preview/{title_id}/{episode_id}.cbz")
async def preview_cbz(
    title_id:   int,
    episode_id: int,
    token:      str,
    db:         Session = Depends(get_db)
):
    _token_user = user_from_token(token, db)
    if _token_user.account_type == "reader":
        raise HTTPException(status_code=403, detail="読者アカウントでは利用できません")
    user_id = _token_user.id

    cbz_path = FINAL_ZIP_DIR / f"user_{user_id}" / f"title_{title_id}" / f"episode_{episode_id}.cbz"
    if not cbz_path.exists():
        raise HTTPException(status_code=404, detail="CBZファイルが見つかりません")

    return FileResponse(
        path=str(cbz_path),
        media_type="application/zip",
        filename=f"preview_t{title_id}_ep{episode_id}.cbz",
        headers={"Cache-Control": "no-store"},
    )

# ---------------------------------------------------------------------------
# 作者：サムネイル配信
# ---------------------------------------------------------------------------

@app.get("/api/author/thumb/{title_id}/{episode_id}/{index}")
async def author_thumb(
    title_id:   int,
    episode_id: int,
    index:      int,
    token:      str,
    db:         Session = Depends(get_db)
):
    _token_user = user_from_token(token, db)
    if _token_user.account_type == "reader":
        raise HTTPException(status_code=403, detail="読者アカウントでは利用できません")
    user_id = _token_user.id

    thumb_path = (
        FINAL_ZIP_DIR / f"user_{user_id}" / f"title_{title_id}"
        / f"episode_{episode_id}_thumbs" / f"thumb_{index:04d}.jpg"
    )
    if not thumb_path.exists():
        raise HTTPException(status_code=404, detail="サムネイルが見つかりません")

    return FileResponse(str(thumb_path), media_type="image/jpeg",
                        headers={"Cache-Control": "max-age=86400"})

# ---------------------------------------------------------------------------
# 作者：カバー画像配信（下書き・未保存でも本人のみ）
# ---------------------------------------------------------------------------

@app.get("/api/author/cover/{title_id}/{episode_id}")
async def author_cover(
    title_id:   int,
    episode_id: int,
    token:      str,
    db:         Session = Depends(get_db)
):
    _token_user = user_from_token(token, db)
    if _token_user.account_type == "reader":
        raise HTTPException(status_code=403, detail="読者アカウントでは利用できません")
    user_id = _token_user.id

    cover_path = (
        FINAL_ZIP_DIR / f"user_{user_id}" / f"title_{title_id}"
        / f"episode_{episode_id}_cover.jpg"
    )
    if not cover_path.exists():
        raise HTTPException(status_code=404, detail="カバー画像が見つかりません")

    return FileResponse(str(cover_path), media_type="image/jpeg",
                        headers={"Cache-Control": "max-age=86400"})

# ---------------------------------------------------------------------------
# 作者：閲覧許可（グラント）の管理
# ---------------------------------------------------------------------------

def _author_title_dir(current_user: User, title_id: int) -> Path:
    title_dir = FINAL_ZIP_DIR / f"user_{current_user.id}" / f"title_{title_id}"
    if not any(title_dir.glob("episode_*_settings.json")):
        raise HTTPException(status_code=404, detail="作品が見つかりません")
    return title_dir


@app.get("/api/author/titles/{title_id}/grants")
async def list_grants(
    title_id:     int,
    current_user: User = Depends(get_current_author),
    db:           Session = Depends(get_db),
):
    _author_title_dir(current_user, title_id)
    result = []
    grants = (
        db.query(Grant)
        .filter(Grant.author_id == current_user.id, Grant.title_id == title_id)
        .order_by(Grant.created_at)
        .all()
    )
    for g in grants:
        user = db.query(User).filter(User.id == g.user_id).first()
        if user:
            result.append({
                "user_id":    user.id,
                "username":   user.username,
                "created_at": g.created_at.isoformat() if g.created_at else None,
            })
    return result


@app.post("/api/author/titles/{title_id}/grants")
async def create_grant(
    title_id:     int,
    body:         GrantRequest,
    current_user: User = Depends(get_current_author),
    db:           Session = Depends(get_db),
):
    _author_title_dir(current_user, title_id)
    target = db.query(User).filter(User.username == body.username).first()
    if not target:
        raise HTTPException(status_code=404, detail="ユーザーが見つかりません")
    if target.id == current_user.id:
        raise HTTPException(status_code=400, detail="自分自身には許可できません")
    existing = (
        db.query(Grant)
        .filter(Grant.author_id == current_user.id,
                Grant.title_id == title_id,
                Grant.user_id == target.id)
        .first()
    )
    if existing:
        return {"user_id": target.id, "username": target.username, "already": True}
    db.add(Grant(author_id=current_user.id, title_id=title_id, user_id=target.id))
    db.commit()
    return {"user_id": target.id, "username": target.username, "already": False}


@app.delete("/api/author/titles/{title_id}/grants/{user_id}")
async def delete_grant(
    title_id:     int,
    user_id:      int,
    current_user: User = Depends(get_current_author),
    db:           Session = Depends(get_db),
):
    _author_title_dir(current_user, title_id)
    grant = (
        db.query(Grant)
        .filter(Grant.author_id == current_user.id,
                Grant.title_id == title_id,
                Grant.user_id == user_id)
        .first()
    )
    if not grant:
        raise HTTPException(status_code=404, detail="許可が見つかりません")
    db.delete(grant)
    db.commit()
    return {"status": "success"}


# ---------------------------------------------------------------------------
# 管理者：公開停止
# ---------------------------------------------------------------------------

@app.get("/api/admin/works")
async def admin_works(
    admin: User = Depends(get_current_admin),
    db:    Session = Depends(get_db),
):
    works = []
    for settings_path in sorted(FINAL_ZIP_DIR.glob("user_*/title_*/episode_*_settings.json")):
        try:
            with open(settings_path, "r") as f:
                meta = json.load(f)
            susp = _active_suspension(db, meta["author_id"], meta["title_id"])
            meta["suspended"]      = susp is not None
            meta["suspend_reason"] = susp.reason if susp else None
            meta.pop("note", None)
            works.append(meta)
        except Exception:
            continue
    return works


@app.post("/api/admin/titles/{author_id}/{title_id}/suspend")
async def admin_suspend(
    author_id: int,
    title_id:  int,
    body:      AdminSuspendRequest,
    admin:     User = Depends(get_current_admin),
    db:        Session = Depends(get_db),
):
    title_dir = FINAL_ZIP_DIR / f"user_{author_id}" / f"title_{title_id}"
    if not any(title_dir.glob("episode_*_settings.json")):
        raise HTTPException(status_code=404, detail="作品が見つかりません")
    active = _active_suspension(db, author_id, title_id)
    if active:
        return {"status": "already_suspended", "reason": active.reason}
    db.add(Suspension(author_id=author_id, title_id=title_id,
                      reason=body.reason, suspended_by=admin.id))
    db.commit()
    return {"status": "suspended", "reason": body.reason}


@app.post("/api/admin/titles/{author_id}/{title_id}/resume")
async def admin_resume(
    author_id: int,
    title_id:  int,
    admin:     User = Depends(get_current_admin),
    db:        Session = Depends(get_db),
):
    active = _active_suspension(db, author_id, title_id)
    if not active:
        raise HTTPException(status_code=404, detail="公開停止されていません")
    active.lifted_at = datetime.now(UTC)
    db.commit()
    return {"status": "resumed"}


# ---------------------------------------------------------------------------
# 公開：CBZ配信
# ---------------------------------------------------------------------------

@app.get("/api/public/cbz/{user_id}/{title_id}/{episode_id}.cbz")
@limiter.limit("10/minute", exempt_when=_is_internal_request)
async def serve_public_cbz(
    request:    Request,
    user_id:    int,
    title_id:   int,
    episode_id: int,
    db:         Session = Depends(get_db),
):
    settings_path = (
        FINAL_ZIP_DIR / f"user_{user_id}" / f"title_{title_id}"
        / f"episode_{episode_id}_settings.json"
    )
    if not settings_path.exists():
        raise HTTPException(status_code=404, detail="作品が見つかりません")

    with open(settings_path, "r") as f:
        meta = json.load(f)

    if meta.get("status") != "published":
        raise HTTPException(status_code=403, detail="この作品は非公開です")

    _enforce_not_suspended(db, user_id, title_id)
    _enforce_premium_access(meta, user_id, title_id, request, db)
    _enforce_age_access(meta, user_id, request, db)

    cbz_path = FINAL_ZIP_DIR / f"user_{user_id}" / f"title_{title_id}" / f"episode_{episode_id}.cbz"
    if not cbz_path.exists():
        raise HTTPException(status_code=404, detail="CBZファイルが見つかりません")

    # ビューカウント（IPクールダウン付き、バックグラウンドで処理）
    client_ip = get_client_ip(request)
    increment_view_count(settings_path, client_ip)

    return FileResponse(
        path=str(cbz_path),
        media_type="application/zip",
        filename=f"title{title_id}_ep{episode_id}.cbz",
        headers={"Cache-Control": "public, max-age=3600"},
    )

# ---------------------------------------------------------------------------
# 公開：サムネイル配信
# ---------------------------------------------------------------------------

@app.get("/api/public/thumb/{user_id}/{title_id}/{episode_id}/{index}")
async def public_thumb(user_id: int, title_id: int, episode_id: int, index: int,
                       request: Request, db: Session = Depends(get_db)):
    settings_path = (
        FINAL_ZIP_DIR / f"user_{user_id}" / f"title_{title_id}"
        / f"episode_{episode_id}_settings.json"
    )
    if not settings_path.exists():
        raise HTTPException(status_code=404, detail="作品が見つかりません")
    with open(settings_path, "r") as f:
        meta = json.load(f)
    if meta.get("status") != "published":
        raise HTTPException(status_code=403, detail="非公開です")

    _enforce_not_suspended(db, user_id, title_id)
    _enforce_premium_access(meta, user_id, title_id, request, db)
    _enforce_age_access(meta, user_id, request, db)

    thumb_path = (
        FINAL_ZIP_DIR / f"user_{user_id}" / f"title_{title_id}"
        / f"episode_{episode_id}_thumbs" / f"thumb_{index:04d}.jpg"
    )
    if not thumb_path.exists():
        raise HTTPException(status_code=404, detail="サムネイルが見つかりません")

    return FileResponse(str(thumb_path), media_type="image/jpeg",
                        headers={"Cache-Control": "max-age=86400"})

# ---------------------------------------------------------------------------
# 公開：カタログ（cbz_url・cover_url付き、noteは除外）
# ---------------------------------------------------------------------------

@app.get("/api/public/catalog")
@limiter.limit("5/minute", exempt_when=_is_internal_request)
async def public_catalog(request: Request, db: Session = Depends(get_db)):
    base_url       = str(request.base_url).rstrip("/")
    published_list = []
    for settings_path in FINAL_ZIP_DIR.glob("user_*/title_*/episode_*_settings.json"):
        try:
            with open(settings_path, "r") as f:
                meta = json.load(f)
            if meta.get("status") != "published":
                continue
            uid = meta["author_id"]
            tid = meta["title_id"]
            eid = meta["episode_id"]
            if _active_suspension(db, uid, tid):
                continue
            meta["cbz_url"]   = f"{base_url}/api/public/cbz/{uid}/{tid}/{eid}.cbz"
            # カバー画像URL
            cover_path = settings_path.parent / f"episode_{eid}_cover.jpg"
            meta["cover_url"] = (
                f"{base_url}/api/public/cover/{uid}/{tid}/{eid}"
                if cover_path.exists() and not meta.get("age_restricted") else None
            )
            # noteは公開APIから除外
            meta.pop("note", None)
            published_list.append(meta)
        except Exception:
            continue
    return published_list

# ---------------------------------------------------------------------------
# 公開：カバー画像配信
# ---------------------------------------------------------------------------

@app.get("/api/public/cover/{user_id}/{title_id}/{episode_id}")
async def public_cover(user_id: int, title_id: int, episode_id: int,
                       db: Session = Depends(get_db)):
    settings_path = (
        FINAL_ZIP_DIR / f"user_{user_id}" / f"title_{title_id}"
        / f"episode_{episode_id}_settings.json"
    )
    if not settings_path.exists():
        raise HTTPException(status_code=404, detail="作品が見つかりません")
    with open(settings_path, "r") as f:
        meta = json.load(f)
    if meta.get("status") != "published":
        raise HTTPException(status_code=403, detail="非公開です")

    _enforce_not_suspended(db, user_id, title_id)
    if meta.get("age_restricted"):
        raise HTTPException(status_code=403, detail="年齢確認が必要な作品の表紙は表示できません")

    cover_path = (
        FINAL_ZIP_DIR / f"user_{user_id}" / f"title_{title_id}"
        / f"episode_{episode_id}_cover.jpg"
    )
    if not cover_path.exists():
        raise HTTPException(status_code=404, detail="カバー画像が見つかりません")
    return FileResponse(str(cover_path), media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})

# ---------------------------------------------------------------------------
# 設定情報（JSがプラットフォームURLを知るため）
# ---------------------------------------------------------------------------

@app.get("/api/config")
async def get_config(request: Request):
    base_url = str(request.base_url).rstrip("/")
    return {"platform_base_url": base_url}

# ---------------------------------------------------------------------------
# 画面配信
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def read_root():
    # 画面は直下の埋め込み文字列が正本（単一ファイル配布のため）。
    # ファイルは経由しない。
    return index_html_content

# ---------------------------------------------------------------------------
# UI HTML
# ---------------------------------------------------------------------------

index_html_content = r'''<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>マンガプラットフォーム</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:"Noto Sans JP",sans-serif;background:#f3f4f6;color:#333;min-height:100vh}
    nav{background:#1e293b;padding:14px 20px;display:flex;align-items:center;gap:16px;flex-wrap:wrap}
    nav .logo{color:#fff;font-weight:700;font-size:1.1rem;margin-right:8px}
    nav a{color:#94a3b8;font-size:.85rem;text-decoration:none;cursor:pointer;padding:4px 10px;border-radius:4px;transition:background .15s}
    nav a:hover{background:#334155;color:#fff}
    #login-status{margin-left:auto;color:#38bdf8;font-size:.8rem}
    .container{max-width:860px;margin:28px auto;background:#fff;padding:28px;border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,.07)}
    .page{display:none}.page.active{display:block}
    h2{font-size:1.15rem;margin-bottom:18px;color:#1e293b}
    h3{font-size:.95rem;margin-bottom:12px;color:#334155}
    .form-group{margin-bottom:14px}
    .form-group label{display:block;font-size:.82rem;font-weight:600;margin-bottom:4px;color:#475569}
    .form-group input,.form-group select,.form-group textarea{width:100%;padding:8px 10px;border:1px solid #cbd5e1;border-radius:4px;font-size:.88rem;font-family:inherit}
    .form-group textarea{resize:vertical;min-height:60px}
    .btn{display:inline-flex;align-items:center;gap:.3em;padding:8px 18px;border:none;border-radius:4px;font-size:.84rem;font-weight:600;cursor:pointer;transition:background .15s}
    .btn-primary{background:#2563eb;color:#fff}.btn-primary:hover{background:#1d4ed8}
    .btn-success{background:#10b981;color:#fff}.btn-success:hover{background:#059669}
    .btn-danger{background:#ef4444;color:#fff}.btn-danger:hover{background:#dc2626}
    .btn-ghost{background:#f1f5f9;color:#475569;border:1px solid #cbd5e1}.btn-ghost:hover{background:#e2e8f0}
    .btn:disabled{background:#9ca3af;cursor:not-allowed}
    .btn-block{width:100%;justify-content:center}
    .drop-zone{border:2px dashed #cbd5e1;padding:24px;text-align:center;border-radius:6px;cursor:pointer;background:#f8fafc;font-size:.88rem;color:#64748b;transition:background .15s}
    .drop-zone:hover{background:#f1f5f9}
    .badge{display:inline-block;padding:2px 8px;font-size:.72rem;font-weight:700;border-radius:9999px;color:#fff}
    .badge-published{background:#10b981}.badge-draft{background:#94a3b8}
    .badge-free{background:#3b82f6}.badge-premium{background:#f59e0b}
    .work-card{border:1px solid #e2e8f0;border-radius:6px;padding:16px;margin-bottom:12px;background:#fafafa}
    .work-card-header{display:flex;align-items:flex-start;gap:12px}
    .work-card-cover{width:60px;height:90px;object-fit:cover;border-radius:4px;background:#e2e8f0;flex-shrink:0}
    .work-card-cover-placeholder{width:60px;height:90px;background:#e2e8f0;border-radius:4px;display:flex;align-items:center;justify-content:center;font-size:1.5rem;flex-shrink:0}
    .work-card-info{flex:1;min-width:0}
    .work-card-title{font-size:.95rem;font-weight:700;color:#1e293b;margin-bottom:3px}
    .work-card-sub{font-size:.77rem;color:#64748b;margin-top:4px}
    .work-card-caption{font-size:.8rem;color:#475569;margin-top:6px;line-height:1.5}
    .work-card-note{font-size:.78rem;color:#92400e;background:#fef3c7;border:1px solid #fde68a;border-radius:4px;padding:4px 8px;margin-top:6px}
    .work-card-actions{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}
    .modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:1000;align-items:center;justify-content:center;overflow-y:auto;padding:20px}
    .modal-overlay.open{display:flex}
    .modal{background:#fff;border-radius:8px;padding:24px;width:min(560px,92vw);box-shadow:0 8px 32px rgba(0,0,0,.18)}
    .modal h3{margin-bottom:16px}
    .modal-footer{display:flex;gap:8px;justify-content:flex-end;margin-top:20px}
    .msg{font-size:.82rem;padding:8px 12px;border-radius:4px;margin-top:10px;display:block}
    .msg-error{background:#fef2f2;color:#dc2626;border:1px solid #fecaca}
    .msg-success{background:#f0fdf4;color:#16a34a;border:1px solid #bbf7d0}
    .msg-info{color:#64748b;font-size:.8rem;margin-top:6px}
    .divider{border:none;border-top:1px solid #e2e8f0;margin:20px 0}
    .empty-state{text-align:center;padding:40px;color:#94a3b8;font-size:.88rem;border:1px dashed #e2e8f0;border-radius:6px}
    .viewer-url-bar{background:#1e293b;color:#94a3b8;font-size:.78rem;padding:8px 20px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
    .viewer-url-bar input{flex:1;min-width:200px;padding:4px 8px;border-radius:4px;border:1px solid #334155;background:#0f172a;color:#e2e8f0;font-size:.78rem}
    /* プログレスバー */
    .progress-wrap{background:#e2e8f0;border-radius:4px;height:8px;overflow:hidden;margin-top:8px}
    .progress-bar{height:100%;background:#2563eb;border-radius:4px;transition:width .2s ease;width:0%}
    .progress-label{font-size:.78rem;color:#475569;margin-top:4px}
    /* 公開作品カード */
    .catalog-card{border:1px solid #e2e8f0;border-radius:8px;padding:16px;margin-bottom:12px;background:#fff;display:flex;gap:14px;align-items:flex-start}
    .catalog-cover{width:80px;height:120px;object-fit:cover;border-radius:4px;background:#e2e8f0;flex-shrink:0}
    .catalog-cover-placeholder{width:80px;height:120px;background:#e2e8f0;border-radius:4px;display:flex;align-items:center;justify-content:center;font-size:2rem;flex-shrink:0}
    .catalog-info{flex:1;min-width:0}
    .catalog-title{font-size:1rem;font-weight:700;color:#1e293b;margin-bottom:4px}
    .catalog-sub{font-size:.78rem;color:#64748b;margin-bottom:6px}
    .catalog-caption{font-size:.82rem;color:#475569;line-height:1.6}
    /* スクランブル設定 */
    .scramble-option{display:flex;align-items:center;gap:8px;padding:10px 12px;border:1px solid #cbd5e1;border-radius:4px;cursor:pointer;transition:background .15s}
    .scramble-option:hover{background:#f8fafc}
    .scramble-option input{width:auto}
  </style>
</head>
<body>

<nav>
  <span class="logo">📚 マンガプラットフォーム</span>
  <a onclick="showPage('search-page'); loadPublishedManga();">🔍 公開作品</a>
  <a onclick="requireLogin('works-page'); loadMyWorks();">📋 マイ作品</a>
  <a onclick="showPage('upload-page')">📤 アップロード</a>
  <a onclick="showPage('auth-page')">🔐 アカウント</a>
  <a id="admin-nav" style="display:none" onclick="requireLogin('admin-page'); loadAdminWorks();">🛡 管理</a>
  <span id="login-status">未ログイン</span>
</nav>

<div class="viewer-url-bar">
  <span>👁 ビューワーURL:</span>
  <input type="url" id="viewer-url-input" placeholder="https://xxxx.trycloudflare.com">
  <button class="btn btn-ghost" onclick="applyViewerUrl()" style="color:#e2e8f0;border-color:#475569">設定</button>
  <span id="viewer-url-status" style="color:#38bdf8"></span>
</div>

<!-- 公開作品一覧 -->
<div id="search-page" class="container page active">
  <h2>🔍 公開中の作品</h2>
  <button class="btn btn-ghost" onclick="loadPublishedManga()" style="margin-bottom:16px">🔄 更新</button>
  <div id="manga-list"><div class="empty-state">読み込み中…</div></div>
</div>

<!-- マイ作品管理 -->
<div id="works-page" class="container page">
  <h2>📋 マイ作品管理</h2>
  <button class="btn btn-ghost" onclick="loadMyWorks()" style="margin-bottom:16px">🔄 更新</button>
  <div id="works-list"><div class="empty-state">読み込み中…</div></div>
</div>

<!-- 管理者：全作品と公開停止 -->
<div id="admin-page" class="container page">
  <h2>🛡 管理者：全作品</h2>
  <button class="btn btn-ghost" onclick="loadAdminWorks()" style="margin-bottom:16px">🔄 更新</button>
  <div id="admin-works-list"><div class="empty-state">読み込み中…</div></div>
</div>

<!-- アップロード -->
<div id="upload-page" class="container page">
  <h2>📤 原稿アップロード</h2>
  <p id="upload-warn" class="msg msg-error" style="display:none">⚠️ 先にログインしてください</p>

  <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
    <div class="form-group">
      <label>作品タイトル名</label>
      <input type="text" id="title_name" placeholder="例: 鬼の城">
    </div>
    <div class="form-group">
      <label>エピソード名</label>
      <input type="text" id="episode_name" placeholder="例: 第1話">
    </div>
  </div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
    <div class="form-group">
      <label>作品ID</label>
      <input type="number" id="title_id" value="1" min="1">
    </div>
    <div class="form-group">
      <label>エピソードID</label>
      <input type="number" id="episode_id" value="1" min="1">
    </div>
  </div>
  <div class="form-group">
    <label>キャプション <span style="font-weight:400;color:#94a3b8">（任意・公開）</span></label>
    <textarea id="caption" placeholder="作品の紹介文"></textarea>
  </div>
  <div class="form-group">
    <label>作者コメント <span style="font-weight:400;color:#94a3b8">（任意・公開）</span></label>
    <textarea id="comment" placeholder="読者へのコメント"></textarea>
  </div>
  <div class="form-group">
    <label>備考 <span style="font-weight:400;color:#92400e">（非公開・自分のみ）</span></label>
    <textarea id="note" placeholder="管理メモ、素材フォルダパスなど"></textarea>
  </div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
    <div class="form-group">
      <label>公開状態</label>
      <select id="status_select">
        <option value="draft">下書き（非公開）</option>
        <option value="published">公開</option>
      </select>
    </div>
    <div class="form-group">
      <label>アクセス権限</label>
      <select id="access_level_select">
        <option value="public">無料公開</option>
        <option value="premium">許可した人だけ読める</option>
      </select>
    </div>
  </div>

  <!-- スクランブル設定 -->
  <div class="form-group">
    <label>🔒 画像スクランブル</label>
    <div style="display:flex;gap:8px;margin-top:4px">
      <label class="scramble-option" style="flex:1">
        <input type="radio" name="scramble" value="1" checked> する（推奨）
      </label>
      <label class="scramble-option" style="flex:1">
        <input type="radio" name="scramble" value="0"> しない
      </label>
    </div>
    <div style="margin-top:8px;display:flex;align-items:center;gap:8px">
      <label style="font-size:.82rem;color:#475569;font-weight:600">タイルサイズ:</label>
      <select id="tile_size" style="width:auto;padding:4px 8px;border:1px solid #cbd5e1;border-radius:4px;font-size:.82rem">
        <option value="16" selected>16px（推奨）</option>
        <option value="8">8px（高強度）</option>
        <option value="32">32px（軽量）</option>
      </select>
    </div>
  </div>

  <div class="form-group">
    <label>
      <input type="checkbox" id="age_restricted" style="width:auto;margin-right:6px">
      年齢確認が必要な作品にする（18歳未満は閲覧できません）
    </label>
  </div>

  <!-- Webhook設定 -->
  <div class="form-group">
    <label>
      <input type="checkbox" id="webhook_enabled" style="width:auto;margin-right:6px">
      Discord Webhook送信を許可する
    </label>
  </div>

  <!-- バンプ画像 -->
  <div class="form-group">
    <label>🖼 バンプ画像（表紙）<span style="font-weight:400;color:#94a3b8"> 任意・5MBまで</span></label>
    <div class="drop-zone" id="cover-drop-zone">
      <p>表紙画像をドロップ、またはクリックして選択</p>
      <input type="file" id="cover-input" accept="image/*" style="display:none">
    </div>
    <div id="cover-preview" style="margin-top:8px"></div>
  </div>

  <!-- 原稿画像 -->
  <div class="form-group">
    <label>📄 原稿画像（複数可）<span style="font-weight:400;color:#94a3b8"> 1枚10MBまで・合計100MBまで</span></label>
    <div class="drop-zone" id="drop-zone">
      <p>原稿画像をドロップ、またはクリックして選択</p>
      <input type="file" id="file-input" multiple accept="image/*" style="display:none">
    </div>
    <div id="file-list" class="msg-info"></div>
  </div>

  <button id="upload-btn" class="btn btn-primary btn-block" onclick="startUpload()" disabled style="margin-top:14px">
    アップロード開始
  </button>

  <!-- プログレス表示 -->
  <div id="progress-area" style="display:none;margin-top:16px">
    <div id="progress-phase" style="font-size:.84rem;font-weight:600;color:#1e293b;margin-bottom:4px"></div>
    <div class="progress-wrap"><div class="progress-bar" id="progress-bar"></div></div>
    <div class="progress-label" id="progress-label"></div>
  </div>
  <div id="upload-status" style="margin-top:10px"></div>
</div>

<!-- アカウント -->
<div id="auth-page" class="container page">
  <h2>🔐 アカウント管理</h2>
  <div style="max-width:400px">
    <h3>ログイン</h3>
    <div class="form-group"><label>ユーザー名</label><input type="text" id="login-user"></div>
    <div class="form-group"><label>パスワード</label><input type="password" id="login-pass"></div>
    <button class="btn btn-primary btn-block" onclick="handleLogin()">ログインする</button>
    <div id="login-msg" style="margin-top:8px"></div>
    <hr class="divider">
    <h3>新規アカウント作成</h3>
    <div class="form-group"><label>ユーザー名</label><input type="text" id="reg-user" placeholder="taro_manga"></div>
    <div class="form-group"><label>メールアドレス</label><input type="email" id="reg-email" placeholder="test@example.com"></div>
    <div class="form-group"><label>パスワード</label><input type="password" id="reg-pass" placeholder="8文字以上"></div>
    <button class="btn btn-success btn-block" onclick="handleSignup()">アカウントを登録</button>
    <div id="signup-msg" style="margin-top:8px"></div>
  </div>
</div>

<!-- 編集モーダル -->
<div id="edit-modal" class="modal-overlay">
  <div class="modal">
    <h3>✏️ エピソード編集</h3>
    <input type="hidden" id="edit-title-id">
    <input type="hidden" id="edit-episode-id">
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
      <div class="form-group"><label>作品タイトル名</label><input type="text" id="edit-title-name"></div>
      <div class="form-group"><label>エピソード名</label><input type="text" id="edit-episode-name"></div>
    </div>
    <div class="form-group"><label>キャプション</label><textarea id="edit-caption"></textarea></div>
    <div class="form-group"><label>作者コメント</label><textarea id="edit-comment"></textarea></div>
    <div class="form-group"><label>備考（非公開）</label><textarea id="edit-note"></textarea></div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
      <div class="form-group">
        <label>公開状態</label>
        <select id="edit-status">
          <option value="draft">下書き（非公開）</option>
          <option value="published">公開</option>
        </select>
      </div>
      <div class="form-group">
        <label>アクセス権限</label>
        <select id="edit-access">
          <option value="public">無料公開</option>
          <option value="premium">許可した人だけ読める</option>
        </select>
      </div>
    </div>
    <div class="form-group">
      <label><input type="checkbox" id="edit-webhook" style="width:auto;margin-right:6px">Discord Webhook送信を許可する</label>
    </div>
    <div class="form-group">
      <label><input type="checkbox" id="edit-age-restricted" style="width:auto;margin-right:6px">年齢確認が必要な作品にする</label>
    </div>
    <div id="edit-msg" style="margin-top:8px"></div>
    <div class="modal-footer">
      <button class="btn btn-ghost" onclick="closeEditModal()">キャンセル</button>
      <button class="btn btn-primary" onclick="saveEdit()">💾 保存</button>
    </div>
  </div>
</div>

<!-- 削除確認モーダル -->
<div id="delete-modal" class="modal-overlay">
  <div class="modal">
    <h3>🗑️ 削除の確認</h3>
    <p style="font-size:.88rem;color:#475569;margin-bottom:4px">以下のエピソードを削除します。この操作は取り消せません。</p>
    <p id="delete-target-name" style="font-weight:700;margin:12px 0;color:#1e293b"></p>
    <input type="hidden" id="delete-title-id">
    <input type="hidden" id="delete-episode-id">
    <div class="modal-footer">
      <button class="btn btn-ghost" onclick="closeDeleteModal()">キャンセル</button>
      <button class="btn btn-danger" onclick="confirmDelete()">削除する</button>
    </div>
  </div>
</div>

<!-- 閲覧許可モーダル -->
<div id="grants-modal" class="modal-overlay">
  <div class="modal">
    <h3>🔑 閲覧許可</h3>
    <p id="grants-target-name" style="font-weight:700;margin:12px 0;color:#1e293b"></p>
    <input type="hidden" id="grants-title-id">
    <div id="grants-list" style="margin-bottom:12px"></div>
    <div style="display:flex;gap:8px">
      <input type="text" id="grants-username" placeholder="許可するユーザー名" style="flex:1">
      <button class="btn btn-primary" onclick="addGrant()">追加</button>
    </div>
    <div id="grants-msg" style="margin-top:8px"></div>
    <div class="modal-footer">
      <button class="btn btn-ghost" onclick="closeGrantsModal()">閉じる</button>
    </div>
  </div>
</div>

<script>
let token = "";
let selectedFiles = [];
let selectedCover = null;
let platformBaseUrl = "";
let viewerBaseUrl   = "";
let editScrambled = true;   // 編集中エピソードの実際のスクランブル設定（openEditModal で控える）
let editTileSize  = 16;     // 同タイルサイズ

/* ── 起動時設定取得 ── */
async function initConfig() {
  try {
    const res = await fetch("/api/config");
    const cfg = await res.json();
    platformBaseUrl = cfg.platform_base_url.replace(/\/$/, "");
  } catch(e) {
    platformBaseUrl = window.location.origin;
  }
}

/* ── ビューワーURL設定 ── */
function applyViewerUrl() {
  const val = document.getElementById("viewer-url-input").value.trim().replace(/\/$/, "");
  if (!val) return;
  viewerBaseUrl = val;
  document.getElementById("viewer-url-status").textContent = "✅ 設定済み";
  loadPublishedManga();
  if (token) loadMyWorks();
}

/* ── ページ切替 ── */
function showPage(pageId) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.getElementById(pageId).classList.add('active');
}
function requireLogin(pageId) {
  if (!token) { showPage('auth-page'); showMsg('login-msg','error','ログインが必要です'); return; }
  showPage(pageId);
}

/* ── メッセージ ── */
function showMsg(id, type, text) {
  const el = document.getElementById(id);
  if (!el) return;
  el.className = `msg msg-${type}`;
  el.textContent = text;
  el.style.display = 'block';
}

/* ── 認証 ── */
async function handleLogin() {
  const fd = new FormData();
  fd.append("username", document.getElementById("login-user").value);
  fd.append("password", document.getElementById("login-pass").value);
  try {
    const res  = await fetch("/api/auth/token", { method: "POST", body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "ログイン失敗");
    token = data.access_token;
    document.getElementById("login-status").textContent = `👤 ${data.username}`;
    refreshMe();
    document.getElementById("upload-warn").style.display = "none";
    showMsg('login-msg','success', `${data.username} としてログインしました`);
    showPage('works-page');
    loadMyWorks();
  } catch(e) { showMsg('login-msg','error', e.message); }
}

async function handleSignup() {
  const payload = {
    username: document.getElementById("reg-user").value,
    email:    document.getElementById("reg-email").value,
    password: document.getElementById("reg-pass").value,
  };
  try {
    const res  = await fetch("/api/auth/signup", {
      method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload)
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "登録エラー");
    showMsg('signup-msg','success', data.message);
  } catch(e) { showMsg('signup-msg','error', e.message); }
}

/* ── 公開作品一覧 ── */
async function loadPublishedManga() {
  const el = document.getElementById("manga-list");
  el.innerHTML = '<div class="empty-state">読み込み中…</div>';
  try {
    const res   = await fetch("/api/public/catalog");
    const items = await res.json();
    if (!items.length) { el.innerHTML = '<div class="empty-state">公開中の作品はありません</div>'; return; }
    el.innerHTML = items.map(m => {
      const title   = esc(m.title_name   || `作品${m.title_id}`);
      const episode = esc(m.episode_name || `エピソード${m.episode_id}`);
      const canRead = viewerBaseUrl && m.cbz_url;
      const coverHtml = m.cover_url
        ? `<img class="catalog-cover" src="${esc(m.cover_url)}" alt="cover">`
        : `<div class="catalog-cover-placeholder">📖</div>`;
      return `
        <div class="catalog-card">
          ${coverHtml}
          <div class="catalog-info">
            <div class="catalog-title">${title} — ${episode}</div>
            <div class="catalog-sub">
              👤 ${esc(m.author_name)} &nbsp;
              <span class="badge ${m.access_level==='public'?'badge-free':'badge-premium'}">
                ${m.access_level==='public'?'無料':'許可制'}
              </span>
            </div>
            ${m.caption ? `<div class="catalog-caption">${esc(m.caption)}</div>` : ''}
            ${canRead ? `<button class="btn btn-primary" style="margin-top:10px" onclick="openViewer('${esc(m.cbz_url)}','${esc(JSON.stringify(m))}')">📖 読む</button>`
                      : `<span style="font-size:.75rem;color:#94a3b8;margin-top:8px;display:block">ビューワーURL未設定</span>`}
          </div>
        </div>`;
    }).join('');
  } catch(e) { el.innerHTML = `<div class="empty-state">読み込みエラー: ${e.message}</div>`; }
}

/* ── マイ作品一覧 ── */
async function loadMyWorks() {
  if (!token) return;
  const el = document.getElementById("works-list");
  el.innerHTML = '<div class="empty-state">読み込み中…</div>';
  try {
    const res   = await fetch("/api/author/works", { headers: {"Authorization":`Bearer ${token}`} });
    if (!res.ok) throw new Error("取得失敗");
    const works = await res.json();
    if (!works.length) {
      el.innerHTML = '<div class="empty-state">まだ作品がありません。アップロードページから投稿してください。</div>';
      return;
    }
    el.innerHTML = works.map(w => {
      const titleLabel   = esc(w.title_name   || `作品${w.title_id}`);
      const episodeLabel = esc(w.episode_name || `エピソード${w.episode_id}`);
      const isPublished  = w.status === 'published';
      const previewUrl   = `${platformBaseUrl}/api/author/preview/${w.title_id}/${w.episode_id}.cbz?token=${encodeURIComponent(token)}`;
      const canPreview   = viewerBaseUrl && w.has_cbz;
      const coverHtml    = w.has_cover
        ? `<img class="work-card-cover" src="${platformBaseUrl}/api/author/cover/${w.title_id}/${w.episode_id}?token=${encodeURIComponent(token)}" alt="cover">`
        : `<div class="work-card-cover-placeholder">📖</div>`;
      return `
        <div class="work-card">
          <div class="work-card-header">
            ${coverHtml}
            <div class="work-card-info">
              <div class="work-card-title">${titleLabel} — ${episodeLabel}</div>
              <div class="work-card-sub">
                <span class="badge ${isPublished?'badge-published':'badge-draft'}">${isPublished?'公開中':'下書き'}</span>
                &nbsp;
                <span class="badge ${w.access_level==='public'?'badge-free':'badge-premium'}">${w.access_level==='public'?'無料':'許可制'}</span>
                ${w.age_restricted ? ' &nbsp;<span style="font-size:.72rem;color:#dc2626">🔞 年齢確認</span>' : ''}
                ${w.scrambled ? ' &nbsp;<span style="font-size:.72rem;color:#7c3aed">🔒 スクランブル済</span>' : ''}
                ${!w.has_cbz  ? ' &nbsp;<span style="color:#ef4444;font-size:.75rem">⚠ CBZ未生成</span>' : ''}
                ${isPublished ? ` &nbsp;<span style="font-size:.78rem;color:#64748b">👁 ${(w.view_count||0).toLocaleString()} views</span>` : ''}
              </div>
              ${w.suspended ? `<div style="color:#dc2626;font-weight:700;font-size:.82rem;margin-top:4px">⛔ 管理者により公開停止中${w.suspend_reason ? `（理由：${esc(w.suspend_reason)}）` : ''}</div>` : ''}
              ${w.caption ? `<div class="work-card-caption">${esc(w.caption)}</div>` : ''}
              ${w.note    ? `<div class="work-card-note">📝 ${esc(w.note)}</div>` : ''}
            </div>
          </div>
          <div class="work-card-actions">
            <button class="btn btn-ghost" onclick='openEditModal(${w.title_id},${w.episode_id},${JSON.stringify(w)})'>✏️ 編集</button>
            ${canPreview ? `<button class="btn btn-ghost" onclick="openPreview('${previewUrl}')">👁 プレビュー</button>` : ''}
            <button class="btn btn-ghost" onclick="openGrantsModal(${w.title_id},'${titleLabel}')">🔑 閲覧許可</button>
            <button class="btn btn-danger" style="margin-left:auto" onclick="openDeleteModal(${w.title_id},${w.episode_id},'${titleLabel} — ${episodeLabel}')">🗑 削除</button>
          </div>
        </div>`;
    }).join('');
  } catch(e) { el.innerHTML = `<div class="empty-state">エラー: ${e.message}</div>`; }
}

/* ── ビューワー連携 ── */
function openViewer(cbzUrl) {
  if (!viewerBaseUrl) { alert("ビューワーURLを設定してください"); return; }
  window.open(`${viewerBaseUrl}/reader?${new URLSearchParams({url:cbzUrl})}`, '_blank');
}
function openPreview(previewUrl) {
  if (!viewerBaseUrl) { alert("ビューワーURLを設定してください"); return; }
  window.open(`${viewerBaseUrl}/reader?${new URLSearchParams({url:previewUrl})}`, '_blank');
}

/* ── 編集モーダル ── */
function openEditModal(titleId, episodeId, w) {
  document.getElementById('edit-title-id').value     = titleId;
  document.getElementById('edit-episode-id').value   = episodeId;
  document.getElementById('edit-title-name').value   = w.title_name   || '';
  document.getElementById('edit-episode-name').value = w.episode_name || '';
  document.getElementById('edit-caption').value      = w.caption      || '';
  document.getElementById('edit-comment').value      = w.comment      || '';
  document.getElementById('edit-note').value         = w.note         || '';
  document.getElementById('edit-status').value       = w.status;
  document.getElementById('edit-access').value       = w.access_level;
  document.getElementById('edit-webhook').checked    = !!w.webhook_enabled;
  document.getElementById('edit-age-restricted').checked = !!w.age_restricted;
  editScrambled = w.scrambled !== false;
  editTileSize  = w.tile_size || 16;
  document.getElementById('edit-msg').textContent    = '';
  document.getElementById('edit-modal').classList.add('open');
}
function closeEditModal() { document.getElementById('edit-modal').classList.remove('open'); }

async function saveEdit() {
  const titleId   = document.getElementById('edit-title-id').value;
  const episodeId = document.getElementById('edit-episode-id').value;
  const payload = {
    title_name:      document.getElementById('edit-title-name').value,
    episode_name:    document.getElementById('edit-episode-name').value,
    caption:         document.getElementById('edit-caption').value,
    comment:         document.getElementById('edit-comment').value,
    note:            document.getElementById('edit-note').value,
    status:          document.getElementById('edit-status').value,
    access_level:    document.getElementById('edit-access').value,
    webhook_enabled: document.getElementById('edit-webhook').checked,
    age_restricted:  document.getElementById('edit-age-restricted').checked,
    scrambled: editScrambled, tile_size: editTileSize, // 編集では既存値を維持（変更不可）
  };
  try {
    const res  = await fetch(`/api/author/titles/${titleId}/episodes/${episodeId}/settings`, {
      method:'PATCH', headers:{'Content-Type':'application/json','Authorization':`Bearer ${token}`},
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || '保存失敗');
    showMsg('edit-msg','success','保存しました');
    setTimeout(() => { closeEditModal(); loadMyWorks(); }, 800);
  } catch(e) { showMsg('edit-msg','error', e.message); }
}

/* ── 削除モーダル ── */
function openDeleteModal(titleId, episodeId, label) {
  document.getElementById('delete-title-id').value   = titleId;
  document.getElementById('delete-episode-id').value = episodeId;
  document.getElementById('delete-target-name').textContent = label;
  document.getElementById('delete-modal').classList.add('open');
}
function closeDeleteModal() { document.getElementById('delete-modal').classList.remove('open'); }

async function confirmDelete() {
  const titleId   = document.getElementById('delete-title-id').value;
  const episodeId = document.getElementById('delete-episode-id').value;
  try {
    const res  = await fetch(`/api/author/titles/${titleId}/episodes/${episodeId}`, {
      method:'DELETE', headers:{'Authorization':`Bearer ${token}`}
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || '削除失敗');
    closeDeleteModal();
    loadMyWorks();
  } catch(e) { alert('削除エラー: ' + e.message); }
}

/* ── ファイル選択（原稿） ── */
const dropZone  = document.getElementById('drop-zone');
const fileInput = document.getElementById('file-input');
dropZone.addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', e => handleFiles(e.target.files));
dropZone.addEventListener('dragover',  e => { e.preventDefault(); dropZone.style.background='#e2e8f0'; });
dropZone.addEventListener('dragleave', () => dropZone.style.background='#f8fafc');
dropZone.addEventListener('drop', e => { e.preventDefault(); dropZone.style.background='#f8fafc'; handleFiles(e.dataTransfer.files); });

const MAX_PAGE_MB  = 10;
const MAX_TOTAL_MB = 100;

function handleFiles(files) {
  const imgs = Array.from(files).filter(f => f.type.startsWith('image/'));
  let totalSize = 0;
  const errors  = [];

  for (const f of imgs) {
    if (f.size > MAX_PAGE_MB * 1024 * 1024) {
      errors.push(`${f.name} が ${MAX_PAGE_MB}MB を超えています`);
    }
    totalSize += f.size;
  }
  if (totalSize > MAX_TOTAL_MB * 1024 * 1024) {
    errors.push(`合計サイズが ${MAX_TOTAL_MB}MB を超えています`);
  }

  if (errors.length) {
    document.getElementById('file-list').innerHTML =
      `<span style="color:#dc2626">${errors.join('<br>')}</span>`;
    selectedFiles = [];
    document.getElementById('upload-btn').disabled = true;
    return;
  }

  selectedFiles = imgs;
  document.getElementById('file-list').textContent =
    `${imgs.length} 枚 / 合計 ${(totalSize/1024/1024).toFixed(1)}MB`;
  document.getElementById('upload-btn').disabled = imgs.length === 0 || !token;
}

/* ── ファイル選択（バンプ画像） ── */
const coverDropZone = document.getElementById('cover-drop-zone');
const coverInput    = document.getElementById('cover-input');
coverDropZone.addEventListener('click', () => coverInput.click());
coverInput.addEventListener('change', e => handleCover(e.target.files[0]));
coverDropZone.addEventListener('dragover',  e => { e.preventDefault(); coverDropZone.style.background='#e2e8f0'; });
coverDropZone.addEventListener('dragleave', () => coverDropZone.style.background='#f8fafc');
coverDropZone.addEventListener('drop', e => { e.preventDefault(); coverDropZone.style.background='#f8fafc'; handleCover(e.dataTransfer.files[0]); });

function handleCover(file) {
  if (!file || !file.type.startsWith('image/')) return;
  if (file.size > 5 * 1024 * 1024) {
    document.getElementById('cover-preview').innerHTML =
      '<span style="color:#dc2626;font-size:.82rem">バンプ画像が5MBを超えています</span>';
    selectedCover = null;
    return;
  }
  selectedCover = file;
  const url = URL.createObjectURL(file);
  document.getElementById('cover-preview').innerHTML =
    `<img src="${url}" style="height:90px;border-radius:4px;border:1px solid #e2e8f0">
     <span style="font-size:.78rem;color:#475569;margin-left:8px">${file.name}</span>`;
}

/* ── アップロード（ポーリング版） ── */
async function startUpload() {
  if (!token) { showPage('auth-page'); return; }
  const tId      = document.getElementById('title_id').value;
  const eId      = document.getElementById('episode_id').value;
  const scramble = document.querySelector('input[name="scramble"]:checked').value;
  const tileSize = document.getElementById('tile_size').value;
  const statusEl = document.getElementById('upload-status');
  const progArea = document.getElementById('progress-area');

  statusEl.textContent = '';
  statusEl.className   = '';
  progArea.style.display = 'block';
  document.getElementById('upload-btn').disabled = true;
  updateProgressUI({phase:'queued', current:0, total:selectedFiles.length});

  try {
    const fd = new FormData();
    selectedFiles.forEach(f => fd.append('files', f));
    if (selectedCover) fd.append('cover', selectedCover);

    const uploadUrl = `/api/author/titles/${tId}/episodes/${eId}/upload-and-zip`
                    + `?do_scramble=${scramble==='1'}&tile_size=${tileSize}`;

    const upRes  = await fetch(uploadUrl, {
      method: 'POST',
      headers: {"Authorization": `Bearer ${token}`},
      body: fd
    });
    const upData = await upRes.json();
    if (!upRes.ok) throw new Error(upData.detail || 'アップロード失敗');

    // ポーリングで進捗監視
    await pollProgress(upData.job_id);

    // 設定保存
    const setRes = await fetch(`/api/author/titles/${tId}/episodes/${eId}/settings`, {
      method: 'PATCH',
      headers: {'Content-Type':'application/json', 'Authorization':`Bearer ${token}`},
      body: JSON.stringify({
        status:          document.getElementById('status_select').value,
        access_level:    document.getElementById('access_level_select').value,
        title_name:      document.getElementById('title_name').value,
        episode_name:    document.getElementById('episode_name').value,
        caption:         document.getElementById('caption').value,
        comment:         document.getElementById('comment').value,
        note:            document.getElementById('note').value,
        webhook_enabled: document.getElementById('webhook_enabled').checked,
        age_restricted:  document.getElementById('age_restricted').checked,
        scrambled:       scramble === '1',
        tile_size:       parseInt(tileSize),
      })
    });
    if (!setRes.ok) throw new Error('設定保存失敗');

    showMsg('upload-status', 'success', '✅ アップロード完了！');
    selectedFiles = [];
    selectedCover = null;
    document.getElementById('file-list').textContent    = '';
    document.getElementById('cover-preview').innerHTML  = '';

  } catch(e) {
    showMsg('upload-status', 'error', '❌ ' + e.message);
  } finally {
    document.getElementById('upload-btn').disabled = selectedFiles.length === 0;
  }
}

/* ── 自分の情報（管理者ナビの表示判定） ── */
async function refreshMe() {
  if (!token) return;
  try {
    const res = await fetch("/api/auth/me", { headers: {"Authorization":`Bearer ${token}`} });
    if (!res.ok) return;
    const me = await res.json();
    document.getElementById('admin-nav').style.display = me.is_admin ? '' : 'none';
  } catch(e) {}
}

/* ── 閲覧許可モーダル ── */
function openGrantsModal(titleId, titleLabel) {
  document.getElementById('grants-title-id').value = titleId;
  document.getElementById('grants-target-name').textContent = titleLabel;
  document.getElementById('grants-username').value = '';
  document.getElementById('grants-msg').textContent = '';
  document.getElementById('grants-modal').classList.add('open');
  loadGrants();
}
function closeGrantsModal() { document.getElementById('grants-modal').classList.remove('open'); }

async function loadGrants() {
  const titleId = document.getElementById('grants-title-id').value;
  const el = document.getElementById('grants-list');
  el.innerHTML = '<div style="font-size:.85rem;color:#64748b">読み込み中…</div>';
  try {
    const res  = await fetch(`/api/author/titles/${titleId}/grants`, { headers: {"Authorization":`Bearer ${token}`} });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || '取得失敗');
    if (!data.length) { el.innerHTML = '<div style="font-size:.85rem;color:#64748b">許可しているユーザーはいません</div>'; return; }
    el.innerHTML = data.map(g => `
      <div style="display:flex;align-items:center;gap:8px;padding:4px 0">
        <span style="flex:1">👤 ${esc(g.username)}</span>
        <button class="btn btn-danger" onclick="removeGrant(${g.user_id})">取り消し</button>
      </div>`).join('');
  } catch(e) { el.innerHTML = `<div style="font-size:.85rem;color:#dc2626">${esc(e.message)}</div>`; }
}

async function addGrant() {
  const titleId  = document.getElementById('grants-title-id').value;
  const username = document.getElementById('grants-username').value.trim();
  if (!username) return;
  try {
    const res  = await fetch(`/api/author/titles/${titleId}/grants`, {
      method:'POST', headers:{'Content-Type':'application/json','Authorization':`Bearer ${token}`},
      body: JSON.stringify({username})
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || '追加失敗');
    showMsg('grants-msg','success', data.already ? 'すでに許可済みです' : '許可を追加しました');
    document.getElementById('grants-username').value = '';
    loadGrants();
  } catch(e) { showMsg('grants-msg','error', e.message); }
}

async function removeGrant(userId) {
  const titleId = document.getElementById('grants-title-id').value;
  try {
    const res  = await fetch(`/api/author/titles/${titleId}/grants/${userId}`, {
      method:'DELETE', headers:{"Authorization":`Bearer ${token}`}
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || '取り消し失敗');
    loadGrants();
  } catch(e) { showMsg('grants-msg','error', e.message); }
}

/* ── 管理者：全作品と公開停止 ── */
async function loadAdminWorks() {
  if (!token) return;
  const el = document.getElementById('admin-works-list');
  el.innerHTML = '<div class="empty-state">読み込み中…</div>';
  try {
    const res   = await fetch('/api/admin/works', { headers: {"Authorization":`Bearer ${token}`} });
    const works = await res.json();
    if (!res.ok) throw new Error(works.detail || '取得失敗');
    if (!works.length) { el.innerHTML = '<div class="empty-state">作品がありません</div>'; return; }
    const groups = new Map();
    for (const w of works) {
      const key = `${w.author_id}/${w.title_id}`;
      if (!groups.has(key)) groups.set(key, {
        author_id: w.author_id, title_id: w.title_id,
        author_name: w.author_name || '', title_name: w.title_name || `作品${w.title_id}`,
        suspended: !!w.suspended, suspend_reason: w.suspend_reason || '', episodes: [],
      });
      groups.get(key).episodes.push(w);
    }
    el.innerHTML = [...groups.values()].map(g => {
      const eps = g.episodes.map(w =>
        `${esc(w.episode_name || `エピソード${w.episode_id}`)}（${w.status==='published'?'公開中':'下書き'}・${w.access_level==='public'?'無料':'許可制'}）`
      ).join('、');
      const controls = g.suspended
        ? `<span style="color:#dc2626;font-weight:700">⛔ 公開停止中</span>
           <button class="btn btn-primary" onclick="adminResume(${g.author_id},${g.title_id})">解除する</button>`
        : `<input type="text" id="suspend-reason-${g.author_id}-${g.title_id}" placeholder="停止の理由（任意）" style="flex:1;min-width:160px">
           <button class="btn btn-danger" onclick="adminSuspend(${g.author_id},${g.title_id})">公開を停止</button>`;
      return `
        <div class="work-card">
          <div class="work-card-title">${esc(g.title_name)} <span style="font-weight:400;color:#64748b;font-size:.8rem">👤 ${esc(g.author_name)}</span></div>
          <div class="work-card-sub" style="margin:6px 0">${eps}</div>
          ${g.suspended && g.suspend_reason ? `<div style="font-size:.8rem;color:#475569">理由：${esc(g.suspend_reason)}</div>` : ''}
          <div class="work-card-actions">${controls}</div>
        </div>`;
    }).join('');
  } catch(e) { el.innerHTML = `<div class="empty-state">エラー: ${esc(e.message)}</div>`; }
}

async function adminSuspend(authorId, titleId) {
  const reason = document.getElementById(`suspend-reason-${authorId}-${titleId}`).value;
  try {
    const res  = await fetch(`/api/admin/titles/${authorId}/${titleId}/suspend`, {
      method:'POST', headers:{'Content-Type':'application/json','Authorization':`Bearer ${token}`},
      body: JSON.stringify({reason})
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || '停止失敗');
    loadAdminWorks();
  } catch(e) { alert(e.message); }
}

async function adminResume(authorId, titleId) {
  try {
    const res  = await fetch(`/api/admin/titles/${authorId}/${titleId}/resume`, {
      method:'POST', headers:{"Authorization":`Bearer ${token}`}
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || '解除失敗');
    loadAdminWorks();
  } catch(e) { alert(e.message); }
}

/* ── ユーティリティ ── */
function esc(str) {
  return String(str||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
                        .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

/* ── 進捗ポーリング ── */
async function pollProgress(jobId) {
  return new Promise((resolve, reject) => {
    const interval = setInterval(async () => {
      try {
        const res = await fetch(`/api/author/upload-progress/${jobId}`, {
          headers: {"Authorization": `Bearer ${token}`}
        });
        if (!res.ok) { clearInterval(interval); reject(new Error('進捗取得失敗')); return; }
        const p = await res.json();
        updateProgressUI(p);
        if (p.phase === 'done')  { clearInterval(interval); resolve(p); }
        if (p.phase === 'error') { clearInterval(interval); reject(new Error(p.message)); }
      } catch(e) { clearInterval(interval); reject(e); }
    }, 400);
  });
}

const PHASE_LABELS = {
  queued:   '待機中…',
  resize:   'リサイズ処理中',
  scramble: 'スクランブル処理中',
  zip:      'CBZ作成中',
  done:     '完了',
  error:    'エラー',
};

function updateProgressUI(p) {
  document.getElementById('progress-phase').textContent = PHASE_LABELS[p.phase] || p.phase;
  const pct = p.total > 0 ? Math.round(p.current / p.total * 100) : 0;
  document.getElementById('progress-bar').style.width  = pct + '%';
  document.getElementById('progress-label').textContent =
    p.total > 0 ? `${p.current} / ${p.total} (${pct}%)` : '';
}

/* 起動 */
initConfig();
loadPublishedManga();
</script>
</body>
</html>
'''
