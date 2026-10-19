# llama.cpp 接続とツール呼び出しの検証

## 接続情報（値は config/config.toml、APIキーは secrets）
- 接続先: LAN `http://192.168.0.13:8080/v1` → Tailscale `http://100.84.113.46:8080/v1`（この順に試す）
- 認証: `Authorization: Bearer <キー>`。キーは `%USERPROFILE%\.assistant\secrets\llama_api_key.txt` から読む（リポジトリ・config・ログには書かない）
- `/health` は認証なし。モデルは Qwen3.5-9B-UD-Q6_K_XL の1つだけ（`model` は何でもよい）
- スロット: 2本（1本あたり32,768トークン）。対話1・バッチ1（CLAUDE.md の「3本」から変更）

## ツール呼び出しの検証（未実施）
この開発環境は LAN / Tailscale に届かないため、実機検証はぴぐみさんのPCで行う。

1. 起動設定（ぴぐみさん提供、APIキーは除く）:
   - `llama-server.exe -m Qwen3.5-9B-UD-Q6_K_XL.gguf --device %DEV% -ngl 99 -fa on -c 65536 -np 2 --host 0.0.0.0 --port 8080 --jinja -t 6 -cram 2048 --api-key <キー>`
   - 環境変数 `GGML_VK_DISABLE_HOST_VISIBLE_VIDMEM=1`（Vulkan バックエンド）
   - `--jinja` あり。`-c 65536` を `-np 2` で分けるため 1 スロット 32,768 トークン
   - `%DEV%` は起動用バッチ側で設定される値。接続側（このアプリ）は参照しない
   - ビルド番号: b10540（ぴぐみさん確認）
2. `ASSISTANT_INTEGRATION=1 python -m pytest -m integration` で「GPUの状態は？」が host_status を呼ぶことを確認する
3. 呼ばれない・引数が壊れる場合は、`core/llm.py` の層を差し替えて GBNF 方式に切り替える（エージェントループは変更不要）

結果: **未記入**

## 実機テストの結果（2026-10-19、b10540）
- 接続・認証・ツール呼び出し（host_status）は動作した（`tool_calls=1`、状態 ok）。
- 失敗: ツール結果を渡した後の最終回答が空文字（`stopped_reason='done'` のまま `answer=''`）。
- **原因は未特定**。この環境から llama-server に届かず、生のレスポンスをまだ見ていない。候補:
  1. 本文が空で、思考（`reasoning_content`）だけが返っている
  2. `content` が null
  3. ツール結果メッセージの形式とチャットテンプレートの不整合
- 対応（フェーズ1、仮説に基づく）:
  - リクエストに `chat_template_kwargs: {enable_thinking: false}` を足す（config の `[llm.request]`。効果は実機で未確認）
  - 空の最終回答は、回答を促して1回だけ再試行し、それでも空なら `empty_response` として報告する（「完了」にしない）。空の応答の形（キー、finish_reason、思考の文字数）は監査ログに残る
  - 本文の `<think>…</think>` は取り除く
- 次の一手（ぴぐみさんのPCで実行）: `python -m core.diagnose "GPUの状態は？"` → `logs/diagnose-*.json` に生のやり取りが残る（APIキーは含まれない）。この内容でここを書き直す。
