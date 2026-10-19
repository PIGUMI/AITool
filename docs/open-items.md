# 未対応・未決事項（フェーズ1時点）
- GPU状態の取得: AMD RX 9060 XT を Windows から読む方法が未決。config の `host_status.gpu_command`（JSONを出すコマンド）が空の間は「取得不可」と答える。値は推測しない
- CPU・メモリ: `psutil` を使う設計だが、依存追加の確認待ちのため未導入（未導入なら「取得不可」）
- 依存パッケージ: 実行時は標準ライブラリのみ。開発時は pytest のみ
- ストリーミング: SSEでツール実行の進捗と最終回答を配信。回答のトークン単位の逐次表示は未対応
- R2の承認フロー、dev、calendar、search、notify はフェーズ2以降
- network: Raspberry Pi・TL-SG105E・開発者PCのアドレス未設定（無効）
- ダッシュボード・承認ダイアログ・監査ログ検索のUIは未実装（チャット画面のみ）
- PWA化とHTTPS（VPN経由）はフェーズ4の前に確認
- 実機統合テスト（tests/test_integration.py）は未実行
