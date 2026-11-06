"""CORS 設定の検証（プラットフォーム側）。

- 既定では CORS ヘッダーを付けない（同梱の画面は同一オリジンでしか通信しない）
- 環境変数 MANGA_CORS_ORIGINS を指定したときだけ、そのオリジンを許可する
- credentials は許可しない（認証は Bearer）
"""
import importlib.util
import sys

import pytest
from fastapi.testclient import TestClient

ORIGIN = "https://app.example.org"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _client(site_copy, monkeypatch, origins):
    if origins is None:
        monkeypatch.delenv("MANGA_CORS_ORIGINS", raising=False)
    else:
        monkeypatch.setenv("MANGA_CORS_ORIGINS", origins)
    mod = _load(f"platform_cors_{abs(hash(origins))}", site_copy / "main.py")
    return TestClient(mod.app, raise_server_exceptions=False)


def test_no_cors_headers_by_default(platform, site_copy, monkeypatch):
    client = _client(site_copy, monkeypatch, None)
    r = client.get("/api/config", headers={"Origin": ORIGIN})
    assert r.status_code == 200
    assert "access-control-allow-origin" not in r.headers
    assert "access-control-allow-credentials" not in r.headers


def test_preflight_is_not_granted_by_default(platform, site_copy, monkeypatch):
    client = _client(site_copy, monkeypatch, None)
    r = client.options("/api/auth/token", headers={
        "Origin": ORIGIN, "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in r.headers


def test_listed_origin_is_allowed(platform, site_copy, monkeypatch):
    client = _client(site_copy, monkeypatch, f"{ORIGIN}, https://other.example.org")
    r = client.get("/api/config", headers={"Origin": ORIGIN})
    assert r.headers.get("access-control-allow-origin") == ORIGIN
    assert "access-control-allow-credentials" not in r.headers


def test_unlisted_origin_is_not_allowed(platform, site_copy, monkeypatch):
    client = _client(site_copy, monkeypatch, ORIGIN)
    r = client.get("/api/config", headers={"Origin": "https://evil.example.net"})
    assert r.headers.get("access-control-allow-origin") != "https://evil.example.net"
