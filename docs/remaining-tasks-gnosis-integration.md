# local-llm 移行 残タスク指示書（Gnosis 疎通確認付き）

最終更新: 2026-05-11 19:33:30 JST

## 1. 目的

- `local-llm` を Gnosis から分離した実行基盤として完成させる。
- Gnosis は外部依存（OpenAI 互換 API / embedding API / MCP server）として `local-llm` を利用する形に寄せる。
- 削除・クリーニングは、起動と疎通が十分に確認できるまで実施しない。

## 2. 現時点で完了していること

- [x] `services/local-llm` 相当のランタイムを `local-llm` へ移植。
- [x] shared queue を `shared/daemon_queue.py` として移植し、参照 import を修正。
- [x] embedding daemon の import/root 解決を `local-llm` 側に合わせて修正。
- [x] 実行スクリプト整備:
  - `scripts/gemma4`
  - `scripts/bonsai`
  - `scripts/qwen`
  - `scripts/run_openai_api.sh`
  - `scripts/run_embedding_daemon.sh`
  - `scripts/verify_setup.sh`
- [x] 機能確認:
  - `scripts/test_tool_parsing.py` 成功
  - `embedding` テスト（12件）成功
  - LLM API `/health`, `/v1/models`, `/v1/chat/completions` 成功
  - embedding daemon `/health`, `/embed` 成功（384次元）

## 3. Gnosis 疎通確認（実行済み）

`local-llm` から `gnosis` MCP server へ接続し、ツール一覧取得と `doctor` 呼び出しを確認。

- 実行結果:
  - `connected: true`
  - `tool_count: 34`
  - `startup_errors: {}`
  - 必須 primary tools（`initial_instructions`, `agentic_search`, `search_knowledge`, `doctor`）の存在確認: `true`
- `doctor` 応答の要点:
  - `toolVisibility.status = ok`
  - `missingPrimaryTools = []`
  - `db.status = ok`

実行コマンド（再確認用）:

```bash
cd /Users/y.noguchi/Code/local-llm
./.venv/bin/python - <<'PY'
import asyncio
from vibe_mcp.client import VibeMcpClient

async def main():
    client = VibeMcpClient(
        root_dir='/Users/y.noguchi/Code/local-llm',
        gnosis_root_dir='/Users/y.noguchi/Code/gnosis',
    )
    await client.start()
    try:
        tools = await client.list_tools()
        doctor = await client.call_tool('doctor', {'format': 'json'})
        print('connected:', True)
        print('tool_count:', len(tools))
        print('startup_errors:', client.get_startup_errors())
        print('doctor_preview:', str(doctor)[:240])
    finally:
        await client.stop()

asyncio.run(main())
PY
```

## 4. 残タスク（優先度順）

### P0: local-llm 単体運用の完成

- [ ] `launchd/` 配下に `com.localLlm.llm.plist` / `com.localLlm.embedding.plist` を追加。
- [ ] `local-llm` 側で `doctor` / `status` 相当の自己診断コマンドを提供。
- [ ] API 認証の正式化（`LOCAL_LLM_REQUIRE_AUTH`, `LOCAL_LLM_ACCESS_TOKEN`）と embedding 側統一。
- [ ] ログ出力先を Gnosis 配下依存から切り離し（`services/local-llm/.debug` 参照の解消）。

### P1: Gnosis の外部依存化（参照先切替）

- [ ] `gnosis/src/constants.ts` の `services/local-llm` / `services/embedding` 既定パス依存を外す。
- [ ] `gnosis/src/scripts/local-llm-cli.ts` の repo 内 Python 実行前提を外部ランタイム呼び出しへ置換。
- [ ] `gnosis/package.json` の `local-llm:daemon` / `embedding:daemon` を削除または外部案内へ変更。
- [ ] `gnosis/scripts/setup-automation.sh` と `scripts/automation/com.gnosis.*` から local daemon 起動責務を除去。
- [ ] `gnosis/scripts/bootstrap-local-llm.ts` を外部 runtime 検出・案内モードへ縮小。
- [ ] `LOCAL_LLM_API_BASE_URL`, `LOCAL_LLM_API_KEY_ENV`, `GNOSIS_EMBED_DAEMON_URL`, `GNOSIS_EMBED_API_KEY_ENV` の運用ドキュメントを更新。

### P2: 削除フェーズ（起動確認後にのみ実施）

- [ ] `gnosis/services/local-llm` 削除。
- [ ] `gnosis/services/embedding` 削除。
- [ ] `gnosis` 側の不要 wrapper/script を削除し、README/運用手順を分離後仕様に同期。

## 5. 削除開始のゲート条件

以下を満たすまで、Gnosis 側の既存実装は削除しない。

1. `local-llm` 単体で LLM API と embedding daemon が常時起動できる。
2. Gnosis から OpenAI 互換 API 呼び出しが成功する（認証付き）。
3. Gnosis から embedding 呼び出しが成功する（認証付き）。
4. `GNOSIS_DOCTOR_REQUIRE_LOCAL_LLM=true bun run doctor` が期待ステータスを返す。
5. 主要フロー（例: review/agentic_search/knowflow の最小ケース）が degraded なしで通る。

## 6. 次の実装順（推奨）

1. P0 完了（local-llm 単体で運用できる状態に固定）
2. P1 切替（Gnosis から内包依存を外す）
3. Gnosis 側の verify/test 修正
4. ゲート条件を再実行
5. P2 削除

