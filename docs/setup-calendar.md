# Google カレンダー（読み取り専用）の設定

calendar ツールは、認証情報が `%USERPROFILE%\.assistant\secrets\` にあるときだけ有効になる。権限は `calendar.readonly` のみ（予定の登録・変更・削除はできない）。

## 手順（一度だけ。ぴぐみさんが実行）
1. Google Cloud Console（https://console.cloud.google.com/）でプロジェクトを作り、「Google Calendar API」を有効にする。
2. 「APIとサービス → OAuth 同意画面」を作成する（ユーザータイプ: 外部。テストユーザーに自分の Gmail を追加）。スコープは追加不要（実行時に要求される）。
3. 「認証情報 → 認証情報を作成 → OAuth クライアント ID → アプリの種類: **デスクトップアプリ**」を作り、JSON をダウンロードする。
4. その JSON を次の名前で置く（リポジトリには置かない。`.gitignore` 済みのフォルダ）:
   `%USERPROFILE%\.assistant\secrets\google_client.json`
5. 認証を実行する（ブラウザが開くので、自分のアカウントで「許可」する）:
   ```powershell
   cd $HOME\Documents\AITool
   python -m tools.calendar.auth
   ```
   `google_token.json` が保存される（値は画面に出ない）。
6. `python -m api.server` を再起動すると `calendar_events` / `calendar_free_slots` が有効になる。

## 補足
- テストモード（未公開）のアプリは、refresh_token が7日で失効する。失効したら手順5をやり直す。長く使うなら、同意画面を「本番」に切り替える（個人利用なら審査不要の「未確認アプリ」の警告が出るだけ）。
- 作業時間帯（空き時間検索）は `config.toml` の `[calendar] work_start / work_end`。
- 予定のタイトル等は他人が書ける外部データとして扱い、指示としては解釈しない。
