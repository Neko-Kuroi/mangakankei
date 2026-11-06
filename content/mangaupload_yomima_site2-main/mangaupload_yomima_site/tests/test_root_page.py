"""問題12-B: 画面は埋め込み文字列を直接返す。

- GET / の本文が index_html_content と一致する（ファイル経由でない）
- import しても static/index.html が作られない（書き戻しの廃止）
"""


def test_root_serves_embedded_html(platform, platform_client):
    r = platform_client.get("/")
    assert r.status_code == 200
    assert r.text == platform.index_html_content


def test_no_static_index_written(site_copy, platform):
    assert not (site_copy / "static" / "index.html").exists()
