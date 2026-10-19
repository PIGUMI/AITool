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
