# Faraway アプリ（Windows EXE / Android）

Faraway の画面（チャット・管理）はサーバーが配信する。アプリは、それを専用ウィンドウ／アプリとして開く薄い入口。サーバーを更新すれば、アプリの画面も新しくなる。

| 端末 | 提供形態 | 状態 |
|---|---|---|
| Windows | `Faraway.exe`（apps/windows。`build.ps1` でビルド） | ビルド・動作確認済み |
| Android | ① PWA（ホーム画面に追加。ビルド不要） | サーバー側は実装・テスト済み。HTTPS（Tailscale 証明書）の設定が必要 |
| Android | ② APK（apps/android の Android Studio プロジェクト） | **ソースのみ。この PC には Java / Android SDK が無く、ビルド・動作確認はできていない** |

## Windows: Faraway.exe

### ビルド（Windows 標準の C# コンパイラを使う。追加のインストールは不要）
```powershell
cd $HOME\Documents\AITool
powershell -File apps\windows\build.ps1        # → dist\Faraway.exe
```

### 使い方
- 初回: `dist\Faraway.exe` を起動 → サーバーのアドレス（例 `http://192.168.0.13:8765/`）を入力。
  サーバー（`python -m api.server`）が表示した「開くだけで設定される URL（…/#token=…）」を入力すると、ログインも自動で済む。
- 以降: 起動するだけ。Edge（無ければ Chrome）の専用ウィンドウで Faraway が開く。
- `Faraway.exe --admin` 管理画面 / `Faraway.exe --reset` 保存した接続先とログイン情報を消す。
- 保存先: アドレスは `%APPDATA%\Faraway\url.txt`、ログイン情報は専用プロファイル `%APPDATA%\Faraway\profile`（トークンは設定ファイルには書かない）。
- 接続できないときは、再試行／アドレス変更を選べる。

### 仕組みと制約
- Edge のアプリ表示（`--app`）で画面を開く起動アプリ。独自の画面は持たない。Edge / Chrome が必要（Windows 11 には標準で入っている）。
- 起動の瞬間だけ、ペアリング URL（トークン入り）が Edge のコマンドラインに出る。常用は「アドレスのみ」で起動するので、初回だけの扱い。
- 署名していないため、他の PC に配ると SmartScreen の警告が出ることがある（自分の PC では問題ない）。

## Android ①: PWA（おすすめ。ビルド不要）

Chrome の「アプリをインストール」で、ホーム画面にアイコンができ、アドレスバーのない専用ウィンドウで開く。**HTTPS が必要**（HTTP の LAN アドレスでは「ショートカットの追加」止まり）。Tailscale の証明書を使うと、HTTPS を無料で用意できる。

1. Tailscale の管理画面（https://login.tailscale.com/admin/dns）で、MagicDNS と「HTTPS Certificates」を有効にする。
2. サーバー用 PC で証明書を作る（`<名前>.<tailnet>.ts.net` は管理画面に表示されるこの PC の名前）:
   ```powershell
   cd $HOME\.assistant\secrets
   tailscale cert <名前>.<tailnet>.ts.net
   ```
   `<名前>.<tailnet>.ts.net.crt` と `.key` ができる。
3. `config\config.toml` の `[api]` に書く（パスは上のファイル）:
   ```toml
   tls_cert = "C:\\Users\\user\\.assistant\\secrets\\<名前>.<tailnet>.ts.net.crt"
   tls_key  = "C:\\Users\\user\\.assistant\\secrets\\<名前>.<tailnet>.ts.net.key"
   tls_hosts = ["100.84.113.46"]     # HTTPS にするアドレス。LAN（192.168.0.13）は HTTP のまま残せる
   ```
4. サーバーを再起動する（`python -m api.server`）。`listening on 100.84.113.46:8765 (HTTPS)` と出る。
5. Android で Tailscale を接続し、Chrome で `https://<名前>.<tailnet>.ts.net:8765/#token=…` を開く（トークン付き URL は `python -m api.tokens add my-android` で表示される）。
6. Chrome のメニュー →「アプリをインストール」（または「ホーム画面に追加」）。

注意: Tailscale の証明書は約90日で期限が切れる。切れたら手順2をやり直してサーバーを再起動する。

## Android ②: APK（Android Studio でビルド）

`apps/android` は、Faraway の画面を WebView で開く最小の Android アプリ（Java、依存ライブラリなし）。サーバーのアドレスは初回に入力（トークン付き URL も可）。設定したサーバー以外のページは外部ブラウザで開く。

**このソースは、私（Claude）の環境に Java・Android SDK が無いため、ビルドも実機での動作確認もできていない。** 標準的な構成で書いたが、初回のビルドで修正が要るかもしれない。

### ビルド手順
1. Android Studio（https://developer.android.com/studio）をインストールする。
2. 「Open」で `apps\android` フォルダを開く（Gradle の同期が走る。JDK 17 は Android Studio に同梱）。
3. メニュー「Build → Build Bundle(s) / APK(s) → Build APK(s)」。
4. 完了の通知の「locate」で `app\build\outputs\apk\debug\app-debug.apk` が見つかる。
5. 端末に転送して開く（「提供元不明のアプリ」の許可が要る）。または USB デバッグで Android Studio の ▶ から直接インストールする。

### 補足
- デバッグ署名の APK は、自分の端末に入れる用。Play ストアに出す場合は、リリース署名が別途要る。
- HTTP（暗号化なし）を許可している（`network_security_config.xml`）。通信先は設定したサーバーに限る。Tailscale 経由なら WireGuard で暗号化される。
