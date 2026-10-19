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

## 実機テストの結果（2026-10-20、b10540）
- 統合テスト（`python -m pytest -m integration`）は通った。`core.cli` / `core.diagnose` でも最終回答が返る。
- 診断の記録（`logs/diagnose-*.json`）: 1 回目は `finish_reason=tool_calls`、2 回目は `finish_reason=stop` で `content` 58 文字、`reasoning_content` なし。
- 前回（2026-10-19）の空回答は、`[llm.request]` の `chat_template_kwargs: {enable_thinking: false}` と空回答の再試行を入れた後は再現していない。
  変更前の生レスポンスは取っていないため、どれが決め手かは未確定（思考だけが返っていた説が有力だが推測）。
- 残している保険: 空の最終回答は1回だけ再試行し、それでも空なら `empty_response` で報告する。`<think>…</think>` は取り除く。
- 再発したら `python -m core.diagnose "GPUの状態は？"` で生のやり取りを記録して確認する。
