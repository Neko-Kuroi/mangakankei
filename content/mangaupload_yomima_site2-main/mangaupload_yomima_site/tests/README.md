# テスト

これまで手作業で回していた実測を pytest 化したもの。

## 実行方法

```bash
pip install pytest
python -m pytest tests/
```

サイトのルート（このフォルダの1つ上）で実行する。
アプリは import 時にファイルを作るため、テストはサイト一式を
一時ディレクトリへコピーしてから読み込む（元のツリーは汚れない）。

## 中身

- `test_platform_auth.py` — パスワード規則、トークンの sub 形式不正、is_active（問題13-A）
- `test_viewer_session_ssrf.py` — セッション必須化、URL 宛先ポリシー、リダイレクト再検証、IP 照合（問題2・4）
- `test_internal_token.py` — 内部トークンの送信先制限（前方一致による漏洩の回帰防止）
- `test_cors.py` — CORS は既定で無効、環境変数 `MANGA_CORS_ORIGINS` を指定したときだけ許可
- `test_discord_escape.py` — `/discord/send` のエラー文を HTML エスケープする
