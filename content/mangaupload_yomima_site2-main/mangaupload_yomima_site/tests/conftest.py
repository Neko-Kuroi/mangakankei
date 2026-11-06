"""検証用 fixture。

両アプリとも import 時にファイルを作る（プラットフォームは
static/index.html の上書きと instance/secret_key の生成、DB は
カレントディレクトリ相対）ため、サイト一式を一時ディレクトリへ
コピーしてから別名で読み込む。

実行方法は tests/README.md を参照。
"""
import importlib.util
import os
import shutil
import sys
from datetime import datetime, timedelta, UTC
from pathlib import Path

import pytest

SITE_ROOT = Path(__file__).resolve().parent.parent


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def site_copy(tmp_path_factory) -> Path:
    dst = tmp_path_factory.mktemp("site") / "site"
    shutil.copytree(
        SITE_ROOT, dst,
        ignore=shutil.ignore_patterns(
            "storage", "instance", "__pycache__", "tests", "*.patch"),
    )
    return dst


@pytest.fixture(scope="session")
def platform(site_copy):
    # プラットフォームは ./storage 等をカレントディレクトリ相対で解決する
    os.chdir(site_copy)
    return _load_module("platform_main", site_copy / "main.py")


@pytest.fixture(scope="session")
def platform_client(platform):
    from fastapi.testclient import TestClient
    return TestClient(platform.app, raise_server_exceptions=False)


@pytest.fixture(scope="session")
def manga(site_copy):
    return _load_module("manga", site_copy / "yomima" / "manga.py")


@pytest.fixture(scope="session")
def viewer(site_copy, manga):
    return _load_module("viewer_main", site_copy / "yomima" / "main.py")


@pytest.fixture(scope="session")
def viewer_client(viewer):
    from fastapi.testclient import TestClient
    return TestClient(viewer.app, raise_server_exceptions=False)


@pytest.fixture()
def make_user(platform):
    """signup エンドポイントを使わず DB へ直接ユーザーを作る。"""
    from passlib.context import CryptContext
    ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

    def _make(username, password="password123", is_active=True):
        db = platform.SessionLocal()
        user = platform.User(
            username=username,
            email=f"{username}@example.com",
            hashed_password=ctx.hash(password),
            is_active=is_active,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        db.close()
        return user.id

    return _make


@pytest.fixture()
def forge_token(platform):
    """アプリ自身の秘密鍵で任意の payload のトークンを作る。"""
    from jose import jwt as jose_jwt

    def _forge(**payload):
        body = {"exp": datetime.now(UTC) + timedelta(minutes=30)}
        body.update(payload)
        return jose_jwt.encode(body, platform.SECRET_KEY, algorithm=platform.ALGORITHM)

    return _forge
