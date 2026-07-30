# Coding Agent Provider Compatibility Plan

作成日: 2026-06-16

最終更新: 2026-06-16

## 実装状況

初回バッチに続き、未対応項目も実装済み。

- Provider profile: `core/provider_profiles.py` で `gemma` / `qwen` / `bonsai` を定義。
- thinking / special token: profile instruction と parser 前の reasoning block 除去、profile sanitize を追加。
- generation params: `top_p` / `stop` を API から model manager まで伝播。
- finish reason: generation stats から `length` を返す経路を追加。
- schema constrained 相当: 真の constrained decoding ではなく、schema 不適合時の 1 回限定 JSON-only retry を実装。
- tool result flow: assistant tool call id と tool result id の保持をテストで固定。
- live smoke: `scripts/smoke_provider_contract.py` を追加。
- model selection docs: README に provider profile と選定基準を追加。

## 目的

`local-llm` を通常チャット用の OpenAI 互換 API ではなく、コーディングエージェントの provider として安定して使える状態へ寄せる。

この計画では、既存の `/v1/chat/completions`、`/v1/responses`、tool call parsing、context budget、streaming 実装を前提に、互換性を壊さず段階的に強化する。

## 現状の前提

- API server は provider 責務に集中し、tool 実行や agent loop はクライアント側の責務とする。
- `/v1/chat/completions` は `tool_calls` を返せる。
- `/v1/responses` は最小限の Responses API 互換形を返せる。
- モデル出力の tool call は、生成後に `core/tool_calling.py` で復元して OpenAI 互換形へ変換している。
- `core/daemon.py` は tools を system prompt に注入している。
- `core/model.py` は tokenizer の `apply_chat_template` に依存して prompt を組み立てている。
- context overflow は `core/context_budget.py` と route 層の structured error で扱う。

## 非目標

- サーバー側で `read_file`、`edit_file`、`run_shell` などの tool を実行しない。
- agent loop をこの repository に実装しない。
- vLLM、llama.cpp、Ollama へのランタイム移行はこの計画に含めない。
- 大規模な API 再設計や既存 route の破壊的変更は行わない。
- parallel tool calls は、明示的に対応するまで未対応として扱う。

## 実装フェーズ

### Phase 1: Provider Contract Tests

最初に互換性テストを追加し、現状の期待契約と不足契約を固定する。

対象:

- `tests/test_chat_route.py`
- `tests/test_responses_route.py`
- `tests/test_tool_calling.py` または新規 `tests/test_provider_contract.py`

追加する確認:

- `/v1/chat/completions` が通常回答で `finish_reason: "stop"` を返す。
- tools 有効時に JSON tool payload を `message.tool_calls` に変換する。
- nested arguments を JSON string として保持する。
- `tool_choice: "none"` では tool parsing を無効化する。
- forced function が tools に存在しない場合は `400` を返す。
- `tool_choice: "required"` の未対応挙動を明示する。
- streaming + tool call で `delta.tool_calls` chunk と final `finish_reason: "tool_calls"` を返す。
- context budget exceeded が structured error を返す。
- thinking / tool special token が通常回答に漏れない。

成功条件:

- fake daemon ベースの route tests で OpenAI 互換 shape を固定できる。
- 既存テストが regress しない。

検証:

```bash
./.venv/bin/python -m pytest tests/test_chat_route.py tests/test_responses_route.py tests/test_tool_calling.py tests/test_context_budget.py
```

### Phase 2: Tool Schema Validation

tool call 復元後の arguments を、request の `tools[].function.parameters` に対して検証する。

対象:

- `core/tool_calling.py`
- `api/routes/chat.py`
- `api/routes/responses.py`
- `api/schemas.py`

実装方針:

- `parse_tool_call` は復元処理に集中させる。
- schema validation は route 層、または新規 helper に分離する。
- validation 失敗時に `{}` へ黙って落とさない。
- invalid arguments の場合は debug log に tool name、error、raw prefix を残す。
- API response は既存互換を優先しつつ、必要なら `400` または diagnostic metadata を選ぶ。

推奨する最初の挙動:

- forced tool call で arguments が schema 不適合なら `400`。
- `tool_choice: "auto"` で schema 不適合なら通常回答へ落とさず、diagnostic つき error にする。
- schema が未指定または `{"type":"object"}` の場合は現状通り受け入れる。

成功条件:

- invalid JSON / invalid schema が tool call として実行側へ渡らない。
- valid nested arguments は壊さず通る。

検証:

```bash
./.venv/bin/python -m pytest tests/test_chat_route.py tests/test_responses_route.py
```

### Phase 3: Tool Choice Semantics

`tool_choice` の扱いを OpenAI 互換 provider として明示する。

対象:

- `api/routes/chat.py`
- `api/routes/responses.py`
- `api/schemas.py`

実装方針:

- `none`: tools を daemon に渡さず、tool parsing も行わない。
- `auto`: tools を渡し、出力が allowed tool に一致した場合だけ `tool_calls` に変換する。
- forced function: 指定 tool のみ daemon に渡し、存在しなければ `400`。
- `required`: tools がなければ `400`。tools があり、モデルが tool call を返さなければ `400` または structured failure。
- parallel tool calls: 未対応として request で無効化するか、README に制約として明記する。

成功条件:

- `tool_choice` の各値が route tests で固定される。
- `required` が曖昧に `stop` へ落ちない。

検証:

```bash
./.venv/bin/python -m pytest tests/test_chat_route.py tests/test_responses_route.py
```

### Phase 4: Streaming Contract

streaming の互換性を明文化し、テストで固定する。

対象:

- `api/routes/chat.py`
- `tests/test_chat_route.py`
- `README.md`

現状方針:

- tools なし: daemon の生成 chunk を live SSE として返す。
- tools あり: 一度 buffered generation してから synthetic `delta.tool_calls` を返す。

実装方針:

- この挙動を provider contract として README に書く。
- tool call streaming の SSE shape を test に追加する。
- stream 中の error は OpenAI 互換 client が解釈しやすい形に寄せる。

成功条件:

- tool あり streaming が `delta.tool_calls` を返す。
- final chunk が `finish_reason: "tool_calls"` を返す。
- `[DONE]` が最後に返る。

検証:

```bash
./.venv/bin/python -m pytest tests/test_chat_route.py
```

### Phase 5: Finish Reason Accuracy

`finish_reason` をより正確に返す。

対象:

- `core/model.py`
- `core/daemon.py`
- `api/routes/chat.py`
- `api/routes/responses.py`

実装方針:

- generation stats から `completion_tokens >= max_tokens` を検出できる場合は `length` とみなす。
- context overflow は既存通り request error として扱う。
- tool call 復元時は `tool_calls` を優先する。
- 通常完了は `stop`。

成功条件:

- max token 到達時に `stop` と誤表示しない。
- tool call の場合は `length` より `tool_calls` を優先する。

検証:

```bash
./.venv/bin/python -m pytest tests/test_chat_route.py tests/test_responses_route.py tests/test_daemon_preload.py
```

### Phase 6: Model Provider Profiles

モデルごとの prompt / tool / thinking / stop token 方針を分離する。

対象:

- `core/model.py`
- `core/daemon.py`
- 新規候補: `core/provider_profiles.py`
- `.env.example`
- `README.md`

実装方針:

- `Gemma`、`Qwen`、`Bonsai` の profile を用意する。
- profile は最低限以下を持つ。
  - model id / path matching
  - tool instruction style
  - thinking suppression instruction
  - stop token / special token sanitization policy
  - default temperature / top_p 相当の推奨値
- まずは prompt construction の補助に留め、モデル loader の大改造は避ける。

成功条件:

- 汎用 tool instruction が profile 経由で組み立てられる。
- thinking を持つモデルで `<think>` や thought channel が通常回答に漏れにくくなる。
- 既存の `apply_chat_template` 利用を壊さない。

検証:

```bash
./.venv/bin/python -m pytest tests/test_daemon_normalize_messages.py tests/test_tool_calling.py tests/test_chat_route.py
```

### Phase 7: Live API Smoke Test

fake daemon ではなく、起動中の local API を直接叩く smoke script を追加する。

対象:

- 新規候補: `scripts/smoke_provider_contract.py`
- `README.md`

確認項目:

- system prompt が効く。
- JSON-only 出力ができる。
- tool call を 1 個返せる。
- tool result 後に次の回答ができる。
- streaming が SSE として成立する。
- tool streaming が documented contract 通りに返る。
- special token / thinking が表に出ない。
- max token 到達時の `finish_reason` が正しい。
- context overflow が structured error になる。

成功条件:

- API server 起動済みなら 1 command で provider contract を確認できる。
- smoke test は重いので通常 unit test には含めず、手動または CI optional にする。

検証:

```bash
./scripts/run_openai_api.sh
./.venv/bin/python scripts/smoke_provider_contract.py --base-url http://127.0.0.1:44448/v1 --model gemma-4-e4b-it
```

### Phase 8: Documentation

コーディングエージェント provider としての制約と推奨設定を README に反映する。

対象:

- `README.md`
- `.env.example`

記載する内容:

- provider は tool call を返すが、tool 実行はクライアント責務。
- tool あり streaming は buffered tool-call stream。
- parallel tool calls は未対応。
- 推奨 temperature は `0.0` から `0.2`。
- `LOCAL_LLM_MAX_OUTPUT_TOKENS` と `LOCAL_LLM_MAX_TOOL_CALL_TOKENS` の意味。
- context overflow 時は system/tool 定義を落とさず structured error を返す方針。
- model profile による差分。

成功条件:

- README を読めば、通常チャット用途と coding agent provider 用途の違いが分かる。
- 既知制約が明示され、クライアント側が誤った期待を持ちにくい。

## 推奨実装順

1. Phase 1: Provider Contract Tests
2. Phase 2: Tool Schema Validation
3. Phase 3: Tool Choice Semantics
4. Phase 4: Streaming Contract
5. Phase 5: Finish Reason Accuracy
6. Phase 6: Model Provider Profiles
7. Phase 7: Live API Smoke Test
8. Phase 8: Documentation

## 最小完了ライン

最初の実装バッチでは、Phase 1 から Phase 4 までを完了ラインにする。

理由:

- tool call の構造化と `tool_choice` は coding agent の破綻に直結する。
- streaming contract を固定すれば、クライアント側の期待値を合わせやすい。
- model profile は重要だが、先に API 契約を固めないと検証軸が曖昧になる。

## 完了判定

以下を満たしたら、coding agent provider としての第一段階は完了とする。

- `tool_choice` の各モードがテストで固定されている。
- valid tool call は OpenAI 互換 `tool_calls` として返る。
- invalid tool call は曖昧に通常回答へ落ちない。
- tool streaming の SSE shape がテストで固定されている。
- context overflow が structured error として維持される。
- README に provider 制約が明記されている。
- 関連 pytest が通る。
