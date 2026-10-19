# ntfy（スマホ通知）の設定

通知先は、サーバー用PCで動かす ntfy。Raspberry Pi が未運用のため、当面はサーバー用PC上に置く。通知先は LAN/ローカル/Tailscale のアドレスに限る（`tools/notify` が公開アドレス宛てを拒否する）。

## 手順（ぴぐみさんが実行。私は第三者のバイナリをダウンロード・実行しない決まり）
1. ntfy の公式リリース（https://github.com/binwiederhier/ntfy/releases）から Windows 版（`ntfy_*_windows_amd64.zip`）を、自分でダウンロードして展開する。
2. サーバー用PCで起動する（例。ポート 2586、LAN/Tailscale から届くようにする）:
   ```powershell
   .\ntfy.exe serve --listen-http :2586
   ```
3. ファイアウォールで 2586 番を LAN と Tailscale だけに許可する（管理者 PowerShell）:
   ```powershell
   New-NetFirewallRule -DisplayName "ntfy" -Direction Inbound -Protocol TCP -LocalPort 2586 -RemoteAddress LocalSubnet,100.64.0.0/10 -Action Allow
   ```
4. Android に ntfy アプリを入れ、「サーバーを追加」で `http://100.84.113.46:2586`（Tailscale 経由）を指定し、推測されにくいトピック名（例: `assistant-xxxxxxxx`）を購読する。
5. `config\config.toml` の `[notify]` に `url = "http://192.168.0.13:2586"`、`topic = "（手順4のトピック名）"` を書き、`python -m api.server` を再起動する。

## 注意
- 認証なしの ntfy は、トピック名を知る人なら誰でも読み書きできる。トピック名は秘密として扱い、LAN/Tailscale の外に出さない。
- 通知本文にキー・トークン等の秘匿情報は含めない（ツールが送信前に拒否する）。
