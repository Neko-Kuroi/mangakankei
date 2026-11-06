"""内部トークン（MANGA_INTERNAL_TOKEN）の送信先制限の検証。

プラットフォームのオリジンにだけ送り、前方一致で偽装できるホスト
（plat.example.com.evil.com や plat.example.com@evil.com）には送らない。
宛先ポリシーはこのテストの対象外なので素通しにし、送信ヘッダだけを見る。
"""
import pytest


class _FakeResponse:
    status_code = 200
    headers = {"content-length": "3"}

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=8192):
        yield b"abc"

    def close(self):
        pass


@pytest.fixture()
def captured(manga, monkeypatch):
    sent = []

    def fake_get(url, **kwargs):
        sent.append((url, kwargs.get("headers", {})))
        return _FakeResponse()

    monkeypatch.setenv("PLATFORM_BASE_URL", "https://plat.example.com")
    monkeypatch.setenv("MANGA_INTERNAL_TOKEN", "SECRET123")
    monkeypatch.setattr(manga, "fetch_policy_check", lambda url: (True, ""))
    monkeypatch.setattr(manga.requests, "get", fake_get)
    return sent


def _token_for(manga, captured, tmp_path, name, url):
    captured.clear()
    target = tmp_path / name
    ok, err = manga.download_file(url, str(target))
    assert ok, err
    assert captured, "リクエストが発生していません"
    return captured[0][1].get("X-Internal-Token")


def test_token_sent_to_platform(manga, captured, tmp_path):
    tok = _token_for(manga, captured, tmp_path, "a.zip",
                     "https://plat.example.com/a.zip")
    assert tok == "SECRET123"


def test_token_sent_to_platform_with_default_port(manga, captured, tmp_path):
    tok = _token_for(manga, captured, tmp_path, "b.zip",
                     "https://plat.example.com:443/a.zip")
    assert tok == "SECRET123"


def test_token_not_sent_to_other_host(manga, captured, tmp_path):
    tok = _token_for(manga, captured, tmp_path, "c.zip",
                     "https://other.example.org/a.zip")
    assert tok is None


def test_token_not_sent_to_suffix_spoof(manga, captured, tmp_path):
    tok = _token_for(manga, captured, tmp_path, "d.zip",
                     "https://plat.example.com.evil.com/a.zip")
    assert tok is None


def test_token_not_sent_to_userinfo_spoof(manga, captured, tmp_path):
    tok = _token_for(manga, captured, tmp_path, "e.zip",
                     "https://plat.example.com@evil.com/a.zip")
    assert tok is None


def test_token_not_sent_on_scheme_mismatch(manga, captured, tmp_path):
    tok = _token_for(manga, captured, tmp_path, "f.zip",
                     "http://plat.example.com/a.zip")
    assert tok is None
