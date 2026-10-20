# GitHub 連携（push と PR の作成）の設定

Faraway の `dev_submit` は、1 回の承認で、feature ブランチの **push** と、下書き **PR の作成**を行う。**マージは、ぴぐみさんが GitHub で行う**（Faraway にはできない）。

## しくみ
- **push**: この PC の Git の資格情報（Windows の資格情報マネージャー。すでにログイン済み）をそのまま使う。新しいログインは要らない。
- **PR の作成**: GitHub API（`api.github.com` だけ）を、**権限を絞ったアクセストークン**で呼ぶ。トークンは `%USERPROFILE%\.assistant\secrets\github_token.txt` に置く（コード・ログ・チャット・モデルへの入力には出ない。監査ログでもマスクされる）。
- **トークンが無くても使える**: push までは行い、PR の作成は「GitHub の PR 作成ページへのリンク（compare_url）」を返す。

## トークンの作り方（Fine-grained personal access token）
**OAuth のログインや classic トークンは使わない**（権限が広く、マージや削除までできてしまうため）。
1. GitHub → 右上のアイコン → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → Generate new token。
2. 入力:
   - Token name: `faraway-pr`
   - Expiration: 90 日など（切れたら作り直す）
   - **Repository access: Only select repositories** → `PIGUMI/Faraway` だけ
   - **Permissions（Repository permissions）**: **Pull requests: Read and write**、Metadata: Read-only（自動）。**ほかは付けない。特に Contents は付けない**（付けるとマージできてしまう）。
3. 生成されたトークン（`github_pat_…`）を、**画面にもチャットにも出さずに**保存する（PowerShell）:
   ```powershell
   New-Item -ItemType Directory -Force "$HOME\.assistant\secrets" | Out-Null
   $k = Read-Host "トークンを貼り付けて Enter"
   Set-Content -NoNewline -Path "$HOME\.assistant\secrets\github_token.txt" -Value $k
   Remove-Variable k
   ```
4. サーバーを再起動する（再起動しなくても、PR 作成時に読み込まれる。マスク対象への登録のために再起動を勧める）。

## 使い方
1. Faraway に、コードの変更を頼む（例: 「`current_time` に UTC の表示を足して。テストも追加して」）。
2. Faraway が、`dev_start` → 編集 → `dev_run`（テスト）→ `dev_commit` と進める。
3. 変更を出すとき、`dev_submit` の**承認要求**が出る。管理画面の「承認」で、**差分の要約（変更ファイル・コミット数）・PR のタイトルと本文・コミット ID** を確認して、承認する。
4. push と下書き PR の作成が行われる。PR の URL が返る。
5. GitHub で PR を確認し、問題なければ「Ready for review」→「Merge pull request」（**ここは人の手**）。
6. Faraway の「更新」タブで、更新の確認 → 準備 → 適用（承認）。

## 安全のしくみ
- **承認のあとに変更されたコミットは、出ていかない**: 承認要求を作った時点の HEAD（コミット ID）が、承認の対象（ハッシュ）に含まれる。承認の前後でコミットが増えたら、実行されず、「もう一度依頼してください」と返る。
- 送るのは feature ブランチだけ。`main` への push・force push・ブランチ削除は、そもそも実装していない。
- PR のタイトル・本文に、トークンやパスワードらしい文字列があれば送らない。
- 通信先は `api.github.com` だけ。対象リポジトリは設定で固定。
- 端末のファイルなど私的なデータを読んだ依頼では、`dev_submit` を含む外部へ出ていくツールが止まる。
- PR は既定で**下書き**。`[dev] pr_draft = false` で通常の PR になる。

## 設定（`config.toml` の `[dev]`）
```toml
pr_draft = true                       # 下書き PR にする（既定）
github_token_file = "github_token.txt"  # secrets の中のファイル名

[[dev.repos]]
name = "faraway"
remote_url = "https://github.com/PIGUMI/Faraway.git"
# github_repo = "PIGUMI/Faraway"     # PR 作成先を明示したいとき（省略時は remote_url から読み取る）
```

## 制約
- トークンの期限が切れると、PR の作成だけが失敗する（push は成功し、PR 作成ページのリンクが返る）。
- `gh` コマンドが必要な `dev_pr_draft` は、`gh` が無ければ使えない。通常は `dev_submit` を使う。
