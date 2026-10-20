# Faraway
常駐 AI アシスタント（開発名 Faraway）。
仕様は CLAUDE.md。フェーズ1（権限ポリシー・監査ログ・host_status・network・エージェントループ）とチャット画面の最小版。

```
python -m pip install pytest
python -m pytest                      # モックのユニットテスト
python -m api.tokens add my-phone     # APIトークン発行（一度だけ表示）
python -m api.server                  # http://127.0.0.1:8765/ をブラウザで開き、トークンを入力
python -m core.cli "GPUの状態は？"     # CLI
ASSISTANT_INTEGRATION=1 python -m pytest -m integration   # 実機（llama.cpp）接続
```
## アプリ（Windows EXE / Android）
`powershell -File apps\windows\build.ps1` で `dist\Faraway.exe` を作れる。Android は PWA（ホーム画面に追加）と、Android Studio でビルドする APK プロジェクト（`apps/android`）。詳しくは `docs/app-setup.md`。

## Android など別端末のブラウザから使う
1. `config/config.toml` の `[api]` に待ち受けアドレスを書く（未設定なら `host` の 127.0.0.1 のみ）:
   `hosts = ["127.0.0.1", "192.168.0.13", "100.84.113.46"]`
2. サーバー用PCで `python -m api.tokens add my-android` を実行し、表示されたトークンを控える（一度だけ表示）。
3. 管理者 PowerShell でファイアウォールを許可する（LAN と Tailscale の中だけ）:
   `New-NetFirewallRule -DisplayName "Assistant API" -Direction Inbound -Protocol TCP -LocalPort 8765 -RemoteAddress LocalSubnet,100.64.0.0/10 -Action Allow`
4. `python -m api.server` を起動したままにする。
5. Android のブラウザで `http://192.168.0.13:8765/`（外出先は Tailscale を接続して `http://100.84.113.46:8765/`）を開き、トークンを入力する。

ルーターのポート開放はしない。HTTP のため LAN/Tailscale の外では使わない（Tailscale 内は暗号化される）。

APIキーは `%USERPROFILE%\.assistant\secrets\llama_api_key.txt` に置く。
