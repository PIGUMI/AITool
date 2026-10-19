# llama.cpp 接続とツール呼び出しの検証

## 接続情報（値は config/config.toml、APIキーは secrets）
- 接続先: LAN `http://192.168.0.13:8080/v1` → Tailscale `http://100.84.113.46:8080/v1`（この順に試す）
- 認証: `Authorization: Bearer <キー>`。キーは `%USERPROFILE%\.assistant\secrets\llama_api_key.txt` から読む（リポジトリ・config・ログには書かない）
- `/health` は認証なし。モデルは Qwen3.5-9B-UD-Q6_K_XL の1つだけ（`model` は何でもよい）
- スロット: 2本（1本あたり32,768トークン）。対話1・バッチ1（CLAUDE.md の「3本」から変更）

## ツール呼び出しの検証（未実施）
この開発環境は LAN / Tailscale に届かないため、実機検証はぴぐみさんのPCで行う。

1. llama.cpp のビルド番号と起動コマンド（`--jinja` の有無）を記録する: **未記入**
2. `ASSISTANT_INTEGRATION=1 python -m pytest -m integration` で「GPUの状態は？」が host_status を呼ぶことを確認する
3. 呼ばれない・引数が壊れる場合は、`core/llm.py` の層を差し替えて GBNF 方式に切り替える（エージェントループは変更不要）

結果: **未記入**
