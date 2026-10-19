# アシスタント
仕様は CLAUDE.md。フェーズ1（権限ポリシー・監査ログ・host_status・network・エージェントループ）とチャット画面の最小版。

```
python -m pip install pytest
python -m pytest                      # モックのユニットテスト
python -m api.tokens add my-phone     # APIトークン発行（一度だけ表示）
python -m api.server                  # http://127.0.0.1:8765/ をブラウザで開き、トークンを入力
python -m core.cli "GPUの状態は？"     # CLI
ASSISTANT_INTEGRATION=1 python -m pytest -m integration   # 実機（llama.cpp）接続
```
APIキーは `%USERPROFILE%\.assistant\secrets\llama_api_key.txt` に置く。
