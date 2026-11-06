"""
manga.py - Core business logic

変更点:
- load_image_bytes() にスクランブル画像フラグを追加（リサイズスキップ）
- セッショントークン管理を追加
- /thumbnail はepisode_thumbs/から返すよう変更
"""

import os
import zipfile
import rarfile
import hashlib
import ipaddress
import socket
import tempfile
import requests
import shutil
import time
import json
import base64
import secrets
import urllib.parse
from io import BytesIO
from typing import Optional

from PIL import Image
from natsort import natsorted

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MAX_MANGA_COUNT     = 12
CACHE_SIZE_LIMIT_MB = 270
IMAGES_PER_LOAD     = 1          # 1枚ずつfetch（reader.htmlのbuffer方式に合わせる）
MAX_DOWNLOAD_SIZE_MB = 240
MAX_EXTRACT_ENTRY_MB = 50     # 解凍: 1エントリの上限
MAX_EXTRACT_TOTAL_MB = 480    # 解凍: 展開合計の上限（ダウンロード上限の2倍）
MAX_EXTRACT_FILES    = 2500   # 解凍: エントリ数の上限
IMAGE_EXTENSIONS    = ('.jpg', '.jpeg', '.png', '.webp', '.gif')
ARCHIVE_EXTENSIONS  = ('.zip', '.cbz', '.rar', '.cbr')
CACHE_TTL_SECONDS   = 86400 / 48  # ~30分
SESSION_TTL_SECONDS = 3600        # セッション有効期限1時間

# ---------------------------------------------------------------------------
# セッショントークン管理
# ---------------------------------------------------------------------------

# { session_token: { cbz_url, created_at, expires_at, ip, page_count } }
_session_store: dict[str, dict] = {}

def create_session(cbz_url: str, client_ip: str,
                   unlock_user_id=None, auth_token=None,
                   age_verified: bool = False) -> str:
    """セッショントークンを発行する。

    unlock_user_id / auth_token は許可制（premium）作品用：このセッションで
    ページを返してよい読者のユーザーIDと、CBZ 取得に使うその読者の
    プラットフォームトークン（サーバー側だけに保持し、ブラウザには渡さない）。
    """
    token = secrets.token_urlsafe(32)   # 推測不可能な43文字
    now   = time.time()
    _session_store[token] = {
        "cbz_url":    cbz_url,
        "created_at": now,
        "expires_at": now + SESSION_TTL_SECONDS,
        "ip":         client_ip,
        "page_count": None,   # 初回アクセス時に確定
        "unlock_user_id": unlock_user_id,
        "auth_token":     auth_token,
        "age_verified":   age_verified,
    }
    _cleanup_expired_sessions()
    return token

def validate_session(token: str, client_ip: Optional[str] = None) -> Optional[dict]:
    """
    セッションを検証して情報を返す。
    無効・期限切れの場合は None を返す。

    IP 照合は環境変数 MANGA_SESSION_BIND_IP=1 のときだけ行う
    （トンネル越し等で読者の IP が変わる環境では有効化しないこと）。
    """
    session = _session_store.get(token)
    if not session:
        return None
    if time.time() > session["expires_at"]:
        _session_store.pop(token, None)
        return None
    if client_ip is not None and os.environ.get("MANGA_SESSION_BIND_IP") == "1":
        if session.get("ip") != client_ip:
            return None
    return session

def set_session_page_count(token: str, page_count: int):
    if token in _session_store:
        _session_store[token]["page_count"] = page_count

def _cleanup_expired_sessions():
    """期限切れセッションを掃除する"""
    now     = time.time()
    expired = [t for t, s in _session_store.items() if now > s["expires_at"]]
    for t in expired:
        _session_store.pop(t, None)

# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def get_cache_dir() -> str:
    cache_dir = os.path.join(tempfile.gettempdir(), "manga_cache")
    os.makedirs(cache_dir, exist_ok=True)
    return cache_dir

def get_cache_path(url: str) -> str:
    file_hash = hashlib.md5(url.encode()).hexdigest()
    return os.path.join(get_cache_dir(), file_hash)

def get_dir_size(path: str) -> int:
    total = 0
    if os.path.exists(path):
        for dirpath, _, filenames in os.walk(path):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                if not os.path.islink(fp):
                    total += os.path.getsize(fp)
    return total

def manage_cache_size(current_reading_url: Optional[str] = None) -> None:
    cache_dir    = get_cache_dir()
    current_hash = (
        hashlib.md5(current_reading_url.encode()).hexdigest()
        if current_reading_url else None
    )
    items: dict[str, dict] = {}

    for name in os.listdir(cache_dir):
        path = os.path.join(cache_dir, name)
        ext  = os.path.splitext(name)[-1].lower()

        if os.path.isfile(path) and ext in ARCHIVE_EXTENSIONS:
            url_hash = os.path.splitext(name)[0]
            items.setdefault(url_hash, {
                'archive_path': None, 'extracted_path': None,
                'total_size': 0, 'mtime': 0,
                'is_currently_reading': (url_hash == current_hash)
            })
            items[url_hash]['archive_path'] = path
            items[url_hash]['mtime'] = max(items[url_hash]['mtime'], os.path.getmtime(path))

        elif os.path.isdir(path) and name.endswith("_extracted"):
            url_hash = name.replace("_extracted", "")
            items.setdefault(url_hash, {
                'archive_path': None, 'extracted_path': None,
                'total_size': 0, 'mtime': 0,
                'is_currently_reading': (url_hash == current_hash)
            })
            items[url_hash]['extracted_path'] = path
            items[url_hash]['mtime'] = max(items[url_hash]['mtime'], os.path.getmtime(path))

    total_bytes  = 0
    for info in items.values():
        size = 0
        if info.get('archive_path') and os.path.exists(info['archive_path']):
            size += os.path.getsize(info['archive_path'])
        if info.get('extracted_path') and os.path.exists(info['extracted_path']):
            size += get_dir_size(info['extracted_path'])
        info['total_size'] = size
        total_bytes += size

    limit_bytes  = CACHE_SIZE_LIMIT_MB * 1024 * 1024
    if total_bytes <= limit_bytes:
        return

    sorted_items = sorted(items.values(), key=lambda x: (x['is_currently_reading'], x['mtime']))
    for info in sorted_items:
        if total_bytes <= limit_bytes:
            break
        if info['is_currently_reading']:
            continue
        try:
            if info.get('archive_path') and os.path.exists(info['archive_path']):
                os.remove(info['archive_path'])
            if info.get('extracted_path') and os.path.exists(info['extracted_path']):
                shutil.rmtree(info['extracted_path'])
            total_bytes -= info['total_size']
        except Exception:
            pass

def cleanup_old_cache() -> None:
    cache_dir = get_cache_dir()
    now       = time.time()
    try:
        for name in os.listdir(cache_dir):
            path = os.path.join(cache_dir, name)
            if os.path.isfile(path) and now - os.path.getmtime(path) > CACHE_TTL_SECONDS:
                os.remove(path)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def download_file(
    url: str,
    save_path: str,
    max_size_mb: int = MAX_DOWNLOAD_SIZE_MB,
    progress_callback=None,
    auth_token: str = None,
    age_verified: bool = False,
) -> tuple[bool, str]:
    if os.path.exists(save_path):
        return True, ""

    max_bytes = max_size_mb * 1024 * 1024
    internal_token = os.environ.get("MANGA_INTERNAL_TOKEN", "")
    try:
        # リダイレクトは手動でたどり、ホップごとに宛先ポリシーを再検証する。
        # 内部トークンはプラットフォーム宛てのときにだけ付ける（外部へ漏らさない）
        current_url = url
        response    = None
        for _ in range(4):  # 初回＋リダイレクト3回まで
            allowed, reason = fetch_policy_check(current_url)
            if not allowed:
                return False, reason
            headers = {}
            if internal_token and is_platform_origin(current_url):
                headers["X-Internal-Token"] = internal_token
            if auth_token and is_platform_origin(current_url):
                # 許可制作品の取得用：読者本人のトークン（プラットフォーム宛てのみ）
                headers["Authorization"] = f"Bearer {auth_token}"
            if age_verified and is_platform_origin(current_url):
                # 年齢ゲート通過済みの取得であることをプラットフォームへ伝える
                headers["X-Age-Verified"] = "1"
            response = requests.get(current_url, stream=True, timeout=(10, 60),
                                    headers=headers, allow_redirects=False)
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                response.close()
                if not location:
                    return False, "リダイレクト先がありません"
                current_url = urllib.parse.urljoin(current_url, location)
                continue
            break
        response.raise_for_status()
        total_size = int(response.headers.get('content-length', 0))

        if total_size > 0 and total_size > max_bytes:
            return False, f"ファイルが {max_size_mb}MB を超えています ({total_size/1024/1024:.1f}MB)"

        bytes_downloaded = 0
        tmp_path         = save_path + ".tmp"

        with open(tmp_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                bytes_downloaded += len(chunk)
                if bytes_downloaded > max_bytes:
                    os.remove(tmp_path)
                    return False, f"ダウンロードが {max_size_mb}MB を超えたため中止しました"
                f.write(chunk)
                if progress_callback:
                    progress = (bytes_downloaded / total_size
                                if total_size > 0
                                else min(bytes_downloaded / max_bytes, 1.0))
                    progress_callback(progress)

        os.rename(tmp_path, save_path)
        return True, ""

    except requests.exceptions.RequestException as e:
        return False, f"ダウンロードエラー: {e}"

# ---------------------------------------------------------------------------
# Archive extraction
# ---------------------------------------------------------------------------

def is_valid_image(filename: str) -> bool:
    return os.path.basename(filename).lower().endswith(IMAGE_EXTENSIONS)

def guess_ext_from_bytes(data: bytes) -> str:
    if data[:3] == b'\xff\xd8\xff':      return '.jpg'
    if data[:8] == b'\x89PNG\r\n\x1a\n': return '.png'
    if data[:6] in (b'GIF87a', b'GIF89a'): return '.gif'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP': return '.webp'
    return ''

def is_rar_archive(path: str) -> bool:
    return path.lower().endswith(('.rar', '.cbr'))

def get_safe_filename(original: str, index: int, ext_override: str = "") -> str:
    basename = original.replace('\\', '/').split('/')[-1]
    ext      = ext_override if ext_override else os.path.splitext(basename)[1].lower()
    stem     = os.path.splitext(basename)[0]

    # スクランブル命名規則（stem=20文字: 4文字Base62 + 16文字hex）はそのまま保持
    # プレフィックスを付けるとJS側のseed抽出位置がズレて復号できなくなる
    if len(stem) == 20:
        try:
            int(stem[4:], 16)
            # スクランブルファイル名と判定 → そのまま返す
            return stem + ext
        except ValueError:
            pass

    # 通常ファイルは従来通りプレフィックス付きで返す
    for ch in ('#', '?', '&', ' ', '+', '%', '=', ':', ';', '@', '!', '*', "'", '(', ')'):
        stem = stem.replace(ch, '_')
    stem = stem[:40] if len(stem) > 40 else stem
    return f"{index:05d}_{stem}{ext}"

_extract_cache: dict[str, tuple[list[str], list[str]]] = {}

def extract_archive(archive_path: str, extract_to: str) -> tuple[list[str], list[str]]:
    if archive_path in _extract_cache:
        return _extract_cache[archive_path]

    os.makedirs(extract_to, exist_ok=True)
    image_files: list[str] = []
    warnings:    list[str] = []
    is_rar = is_rar_archive(archive_path)

    entry_max = MAX_EXTRACT_ENTRY_MB * 1024 * 1024
    total_max = MAX_EXTRACT_TOTAL_MB * 1024 * 1024

    try:
        archive = rarfile.RarFile(archive_path) if is_rar else zipfile.ZipFile(archive_path)
        with archive:
            total_written = 0
            for index, file_info in enumerate(archive.infolist()):
                if index >= MAX_EXTRACT_FILES:
                    warnings.append(f"エントリ数が上限 ({MAX_EXTRACT_FILES}) に達したため打ち切りました")
                    break
                filename = file_info.filename
                if filename.endswith('/') or filename.endswith('\\'):
                    continue
                if file_info.file_size > entry_max:
                    warnings.append(
                        f"サイズ上限のためスキップ: {os.path.basename(filename)}"
                        f" ({file_info.file_size / 1024 / 1024:.1f}MB)"
                    )
                    continue

                try:
                    with archive.open(file_info) as src:
                        head = src.read(16)

                        if is_valid_image(filename):
                            ext = os.path.splitext(
                                os.path.basename(filename).replace('\\', '/').split('/')[-1]
                            )[1].lower()
                        else:
                            ext = guess_ext_from_bytes(head)
                            if not ext:
                                continue

                        safe_name = get_safe_filename(filename, index, ext_override=ext)
                        dest      = os.path.join(extract_to, safe_name)
                        if os.path.exists(dest):
                            image_files.append(dest)
                            continue

                        written = 0
                        chunk   = head
                        with open(dest, 'wb') as f:
                            while chunk:
                                written += len(chunk)
                                if written > entry_max or total_written + written > total_max:
                                    break
                                f.write(chunk)
                                chunk = src.read(1024 * 1024)

                        if total_written + written > total_max:
                            os.remove(dest)
                            warnings.append(f"展開合計が上限 ({MAX_EXTRACT_TOTAL_MB}MB) に達したため打ち切りました")
                            break
                        if written > entry_max:
                            os.remove(dest)
                            warnings.append(f"サイズ上限のためスキップ: {os.path.basename(filename)}")
                            continue

                        total_written += written
                        image_files.append(dest)
                except Exception as e:
                    warnings.append(f"スキップ: {os.path.basename(filename)} ({e})")

    except (zipfile.BadZipFile, rarfile.BadRarFile) as e:
        warnings.append(f"無効なアーカイブ: {e}")
    except Exception as e:
        warnings.append(f"解凍エラー: {e}")

    result = (natsorted(image_files), warnings)
    _extract_cache[archive_path] = result
    return result

# ---------------------------------------------------------------------------
# Image loading
# ---------------------------------------------------------------------------

def is_scrambled_filename(filename: str) -> bool:
    """
    ファイル名がスクランブル命名規則かどうか判定。
    規則: {4桁Base62}{16文字hex}.png
    例:   0001a1b2c3d4e5f6g7h8.png
    """
    name = os.path.basename(filename)
    stem = os.path.splitext(name)[0]
    # 全長20文字かつ先頭4文字がBase62、残り16文字がhex
    if len(stem) != 20:
        return False
    hex_part = stem[4:]
    try:
        int(hex_part, 16)
        return True
    except ValueError:
        return False

def load_image_bytes(
    image_path: str,
    max_size: tuple[int, int] = (1200, 1800),
    force_no_resize: bool = False,
) -> Optional[bytes]:
    """
    画像バイナリを返す。

    スクランブル済み画像（または force_no_resize=True）の場合は
    リサイズしない。アップロード時にすでに適切なサイズになっているため。

    非スクランブル画像（外部ZIP等）は従来通りmax_sizeでリサイズする。
    """
    if not os.path.exists(image_path):
        return None

    # スクランブル画像かどうかをファイル名で判定
    scrambled = force_no_resize or is_scrambled_filename(image_path)

    try:
        thumb_cache_dir = os.path.join(get_cache_dir(), "thumbs")
        os.makedirs(thumb_cache_dir, exist_ok=True)

        stat      = os.stat(image_path)
        cache_key = hashlib.md5(
            f"{image_path}:{stat.st_mtime}:{max_size}:{scrambled}".encode()
        ).hexdigest()
        jpg_cache = os.path.join(thumb_cache_dir, cache_key + ".jpg")
        png_cache = os.path.join(thumb_cache_dir, cache_key + ".png")

        if os.path.exists(jpg_cache):
            with open(jpg_cache, "rb") as f: return f.read()
        if os.path.exists(png_cache):
            with open(png_cache, "rb") as f: return f.read()

        with Image.open(image_path) as img:
            # スクランブル済み画像はリサイズしない
            if not scrambled:
                img.thumbnail(max_size, Image.LANCZOS)

            buf = BytesIO()
            if img.mode in ("RGBA", "LA"):
                img.save(buf, format="PNG")
                data = buf.getvalue()
                with open(png_cache, "wb") as f: f.write(data)
            else:
                if img.mode != "RGB":
                    img = img.convert("RGB")
                img.save(buf, format="JPEG", quality=90)
                data = buf.getvalue()
                with open(jpg_cache, "wb") as f: f.write(data)
            return data

    except Exception:
        return None

# ---------------------------------------------------------------------------
# Manga list serialization
# ---------------------------------------------------------------------------

def encode_manga_list(manga_list: list[dict]) -> str:
    payload    = {'manga_urls': manga_list, 'export_time': time.time()}
    json_bytes = json.dumps(payload, ensure_ascii=True).encode('ascii')
    return base64.urlsafe_b64encode(json_bytes).decode('ascii')

def decode_manga_list(encoded: str) -> tuple[list[dict], str]:
    try:
        encoded = encoded.strip().replace(' ', '+')
        padding = 4 - len(encoded) % 4
        if padding != 4:
            encoded += '=' * padding
        try:
            raw = base64.urlsafe_b64decode(encoded).decode('utf-8')
        except Exception:
            raw = base64.b64decode(encoded).decode('utf-8')
        data = json.loads(raw)
        return data.get('manga_urls', []), ""
    except Exception as e:
        return [], f"デコードエラー: {e}"

def validate_manga_url(url: str) -> bool:
    try:
        scheme = urllib.parse.urlparse(url).scheme.lower()
    except Exception:
        return False
    if scheme not in ("http", "https"):
        return False
    ext = os.path.splitext(url.split('?')[0])[-1].lower()
    return ext in ARCHIVE_EXTENSIONS

# ---------------------------------------------------------------------------
# URL 取得ポリシー（SSRF対策）
# ---------------------------------------------------------------------------

# 「内部アドレス」の定義: ループバック／RFC1918／リンクローカル／ULA／0.0.0.0/8。
# ipaddress の is_private は使わない（ベンチマーク用 198.18.0.0/15 等も
# private 扱いになり、fake-ip 方式の DNS 環境で外部ホスト全てが弾かれるため）
_BLOCKED_NETWORKS = [ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "169.254.0.0/16", "0.0.0.0/8", "::1/128", "fc00::/7", "fe80::/10",
)]

def _is_dangerous_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    if ip.is_multicast or ip.is_unspecified:
        return True
    return any(ip in net for net in _BLOCKED_NETWORKS if ip.version == net.version)

def is_platform_origin(url: str) -> bool:
    """url が PLATFORM_BASE_URL と同じオリジン（スキーム・ホスト・ポート）か。

    文字列の前方一致ではなく URL を解析して比べる。前方一致だと
    plat.example.com.evil.com や plat.example.com@evil.com も一致してしまう。
    省略されたポートは既定値（http=80 / https=443）として扱い、userinfo 付きは認めない。
    """
    default_ports = {"http": 80, "https": 443}
    try:
        p = urllib.parse.urlparse(url)
        b = urllib.parse.urlparse(
            os.environ.get("PLATFORM_BASE_URL", "http://localhost:8000"))
        if p.username or p.password:
            return False
        scheme, base_scheme = p.scheme.lower(), b.scheme.lower()
        return (scheme, (p.hostname or "").lower(), p.port or default_ports.get(scheme)) == \
               (base_scheme, (b.hostname or "").lower(), b.port or default_ports.get(base_scheme))
    except ValueError:
        return False


def fetch_policy_check(url: str) -> tuple[bool, str]:
    """サーバー側で取得してよい宛先かどうか（スキーム＋宛先ポリシー）。

    内部アドレスは原則禁止。例外は次の2つだけ:
    - プラットフォームのオリジン（PLATFORM_BASE_URL と一致）… 正規の連携先
    - 環境変数 MANGA_URL_ALLOW_HOSTS に列挙したホスト名（LAN の NAS 等）
    拡張子の判定は含まない（リダイレクト先の署名付きURL等を弾かないため）。
    """
    try:
        p = urllib.parse.urlparse(url)
        scheme = p.scheme.lower()
        host   = (p.hostname or "").lower()
        port   = p.port
    except Exception:
        return False, "無効なURLです"
    if scheme not in ("http", "https"):
        return False, "http/https のURLだけ取得できます"
    if not host:
        return False, "ホストがありません"

    if is_platform_origin(url):
        return True, ""

    allow_hosts = {h.strip().lower()
                   for h in os.environ.get("MANGA_URL_ALLOW_HOSTS", "").split(",")
                   if h.strip()}
    if host in allow_hosts:
        return True, ""

    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return False, "ホストを解決できません"
    if not infos:
        return False, "ホストを解決できません"
    for info in infos:
        if _is_dangerous_ip(info[4][0]):
            return False, "内部アドレスへの取得は許可されていません"
    return True, ""

def is_url_fetch_allowed(url: str) -> tuple[bool, str]:
    """入口でのURL検証（形式＋宛先ポリシー）。"""
    if not validate_manga_url(url):
        return False, "無効なURLです"
    return fetch_policy_check(url)

def get_filename_from_url(url: str) -> str:
    filename = os.path.basename(url).split('?')[0]
    try:
        return urllib.parse.unquote(filename, encoding='utf-8')
    except Exception:
        return filename

def build_manga_entry(url: str, title: str = "") -> dict:
    return {
        'url':        url,
        'title':      title or get_filename_from_url(url),
        'added_time': time.time(),
    }

# ---------------------------------------------------------------------------
# Discord Webhook
# ---------------------------------------------------------------------------

_DISCORD_WEBHOOK_HOSTS = {"discord.com", "ptb.discord.com", "canary.discord.com", "discordapp.com"}

def is_discord_webhook_url(url: str) -> bool:
    """Discord の Webhook URL（https・Discord のホスト・/api/webhooks/ 配下）だけを許可する"""
    try:
        p = urllib.parse.urlsplit(url)
        port = p.port
    except ValueError:
        return False
    return (
        p.scheme == "https"
        and (p.hostname or "") in _DISCORD_WEBHOOK_HOSTS
        and port in (None, 443)
        and not p.username and not p.password   # user@host 形式の偽装を拒否
        and p.path.startswith("/api/webhooks/")
    )

def send_image_to_discord(
    image_path:   str,
    webhook_urls: list[str],
    filename:     Optional[str] = None,
) -> list[str]:
    if not os.path.exists(image_path):
        return ["ファイルが見つかりません"]   # サーバー上のパスは返さない

    if filename is None:
        filename = os.path.basename(image_path)

    ext      = os.path.splitext(image_path)[-1].lower()
    mime_map = {'.png': 'image/png', '.jpg': 'image/jpeg',
                '.jpeg': 'image/jpeg', '.webp': 'image/webp'}
    mime     = mime_map.get(ext, 'image/png')

    # Discord に送るファイル名：元のstemの先頭5文字 + 拡張子
    stem     = os.path.splitext(filename)[0]
    filename = stem[:5] + ext

    errors: list[str] = []
    for webhook_url in webhook_urls:
        if not webhook_url:
            continue
        if not is_discord_webhook_url(webhook_url):
            errors.append("Webhook URL は Discord のものだけ使えます")
            continue
        try:
            with open(image_path, 'rb') as f:
                resp = requests.post(
                    webhook_url,
                    files={'file': (filename, f, mime)},
                    timeout=30,
                    allow_redirects=False
                )
                resp.raise_for_status()
        except requests.exceptions.RequestException as e:
            errors.append(f"Discord送信エラー ({webhook_url[:40]}...): {e}")

    return errors

# ---------------------------------------------------------------------------
# TinyURL
# ---------------------------------------------------------------------------

def shorten_url(long_url: str) -> str:
    try:
        resp = requests.get(
            "https://tinyurl.com/api-create.php",
            params={"url": long_url},
            timeout=10
        )
        resp.raise_for_status()
        return resp.text.strip()
    except Exception:
        return long_url