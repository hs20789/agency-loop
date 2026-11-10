# Agency Loop MVP — Architecture and Contracts

This is the developer contract. Product intent lives in `PROJECT.md` (Korean). When code and this file disagree, fix one of them in the same change.

## 1. Principles that code must enforce

1. Evidence first. Every model statement is tied to evidence IDs; app code, not the model, decides whether a quote is a verified record.
2. "Test ran" is not "test passed". Only the rule engine (`decisions/testruns.py`) turns a recorded run into pass/fail.
3. Later fixes are never ground truth and are never sent to any model.
4. Model agreement is never truth. Comparisons group by cited evidence IDs only.
5. No scores, accuracy rates, majority votes or ability verdicts anywhere (API or UI).
6. Session content is data. Models get no tools. Model output is rendered as text (`textContent`), never HTML.
7. Nothing leaves the server without a per-provider preview and consent (§5.1). Mock mode never sends anything.
8. Secrets and session content never reach logs. API keys are read from env by provider clients only.
9. Retrospective blinding is time based and server gated (§4.5).

## 2. Data model and module map

Shared types: `agency_loop/model.py`. Korean labels for every code: `agency_loop/labels.py`. Settings: `agency_loop/config.py` (`Settings.from_env()`).

| Package | Module | Responsibility |
|---|---|---|
| `ingest` | `parser.py` | JSONL bytes -> `Session` (allowlist extraction, turn numbering, path relativizing) |
| | `redact.py` | masking rules, placeholder numbering, `find_secrets()` final check |
| | `scope.py` | `build_scope(session, test_runs)` -> ScopeView dict (§7.3) |
| `decisions` | `evidence.py` | `build_evidence(session, excerpt_chars)`, diff rendering, `visible_evidence_ids(ws)` |
| | `testruns.py` | test command detection, runner output parsing, exit status |
| | `candidates.py` | request units, signals, scoring, top 3, evidence level, later-fix flag |
| | `retro.py` | retro eligibility, cutoff, pre-context, reveal, prediction label |
| `flow` | `consent.py` | previews, consent records, `check_send()` |
| | `bundle.py` | evidence bundle text for models |
| | `prompts.py`, `schemas.py` | prompts and JSON schemas for Claude, HCX-007, DeepSeek |
| | `report.py` | development-process report: section grading, gap summary for the What-if and feedback |
| | `citations.py` | statement grading |
| | `compare.py` | model comparison by evidence ID |
| | `selfassess.py` | tier, self-rating vs answer comparison, next difficulty |
| | `instruction.py` | rule check of the user's AI instruction against recorded tests |
| | `retrieval.py` | BGE-M3 in-memory similarity + reranker citation selection |
| | `caps.py` | per-flow and server-wide counters |
| | `orchestrator.py` | job pipelines: analysis, summary, review, whatif, feedback |
| `providers` | `base.py` | `ProviderError`, `LLMResult`, protocols, gate type |
| | `anthropic_claude.py` | Claude via `anthropic.AsyncAnthropic` (streaming, structured output) |
| | `clova.py` | HCX-007 review + reranker via `httpx` |
| | `mistral.py` | DeepSeek review via one raw `httpx` request + ledger |
| | `ollama_embed.py` | BGE-M3 `/api/embed` |
| | `local_llm_mcp.py` | MCP stdio client for the course `summarize_text` |
| | `mock.py` | deterministic mock providers |
| | `registry.py` | `build_providers(settings, ledger)` -> `Providers` |
| (root) | `ledger.py` | Mistral usage ledger + CLI |
| `web` | `app.py` | `create_app(settings=None, providers=None)` Starlette factory and routes |
| | `security.py` | Host/Origin checks, security headers, cookie helpers |
| | `upload.py` | streamed raw-body reading with the 32 MiB cap (33,554,432 bytes) |
| | `workspace.py` | in-memory store, TTL sweeper, per-IP upload rate |
| | `jobs.py` | background job runner and status |
| | `views.py` | allowlisted JSON serializers (the only place that builds API payloads) |
| `static` | `index.html`, `app.css`, `app.js` | one-page 3-step UI |

## 3. Ingest

### 3.1 Input format (Claude Code session JSONL)

There is no official schema; the parser follows the commonly observed structure and must tolerate unknown fields and record types. Each line is one JSON object.

- `type: "user"`, `message.role: "user"`:
  - `message.content` string -> human text.
  - `message.content` list -> `text` blocks (human text), `tool_result` blocks (`tool_use_id`, `content` = string or list of `{type: "text", text}`; `is_error`), `image` blocks (dropped, counted as `image`).
  - sibling `toolUseResult` (dict) -> only `interrupted` (bool) is read.
- `type: "assistant"`: `message.content` list -> `text` (assistant text), `tool_use` (`id`, `name`, `input`), `thinking` / `redacted_thinking` (dropped, counted as `thinking`). One API message may be split over several records; file order is kept.
- Skipped and counted in `ParseReport.skipped_record_types`: every other `type` (`summary`, `system`, `file-history-snapshot`, ...), records with `isSidechain: true` (`sidechain`), user records with `isMeta: true` (`meta`).
- Not human requests (skipped, counted as `command_wrapper`): user text whose stripped content starts with `<command-name>`, `<command-message>`, `<local-command-stdout>`, `<local-command-caveat>`, or that is exactly an interrupt marker like `[Request interrupted by user]` / `[Request interrupted by user for tool use]` (counted as `interrupt_marker`).
- `<system-reminder>...</system-reminder>` blocks are removed from user text (counted as `system_reminder`). A message that becomes empty is skipped.
- Dropped metadata (counted once per record as `meta_fields`): `uuid`, `parentUuid`, `sessionId`, `cwd`, `version`, `gitBranch`, `userType`, `requestId`, `message.id`, `message.model`, `message.usage`, `timestamp`, and any account/organization/user identifier key. The first `cwd` seen is used only to relativize paths, then discarded.
- Bad lines (invalid JSON, non-object): record the line number in `ParseReport.bad_lines`; never keep content.
- Upload must be UTF-8; invalid bytes are replaced.
- Implemented details (keep in sync with `parser.py`): `<local-command-stderr>`, `<bash-input>`, `<bash-stdout>`, `<bash-stderr>` wrappers also count as `command_wrapper`; user records with `isCompactSummary: true` are skipped as `summary`; a user record yields at most one `user_text` (text blocks joined by a blank line) and its tool results are emitted before that text, so they stay in the previous turn; blank lines are ignored; unknown record types are counted as `other` (session strings never become report keys) and unknown content blocks as `dropped["other"]`; tool names pass through the Redactor; outputs are redacted before the 200k cut; lone UTF-16 surrogates (from JSON escapes such as `\ud800`) become U+FFFD, like invalid UTF-8 bytes.

### 3.2 Events

`Event` fields are in `model.py`. Event kinds and allowlisted content:

| kind | from | `text` | `tool_input` (allowlist) |
|---|---|---|---|
| `user_text` | human text | redacted text | - |
| `assistant_text` | assistant `text` block | redacted text | - |
| `tool_use` | assistant `tool_use` | `""` | `Bash`: `command`, `description`; `Edit`: `file_path`, `old_string`, `new_string`, `replace_all`; `MultiEdit`: `file_path`, `edits[{old_string,new_string,replace_all}]`; `Write`: `file_path`, `content`; `NotebookEdit`: `notebook_path`, `new_source`; `Read`: `file_path`; `Grep`/`Glob`: `pattern`, `path`; other tools: `{}` (count `other_tool_input`) |
| `tool_result` | user `tool_result` block | joined text output, capped at 200,000 chars (keep the head 100k and tail 100k joined by `\n[... 출력 일부 생략 ...]\n`, which counts as truncation) | - |

`tool_use_id` is remapped to `tu1`, `tu2`, ... in order of first appearance; `tool_result.tool` is copied from the matching `tool_use`. `turn` increments on each human request (`user_text`); events before the first request have turn 0. All file paths in `tool_input` are relativized to the session `cwd` when they start with it, then redacted.

### 3.3 Redaction (`redact.py`)

API:
```python
class Redactor:                      # one instance per session; numbering is per session
    def redact(self, text: str, *, origin: str) -> str   # origin: "text" | "output"
    stats: RedactionStats
def find_secrets(text: str) -> list[str]   # categories still present; used right before any send
```
Order and placeholders (same value -> same placeholder within a session):

| category | detects | replacement |
|---|---|---|
| `private_key` | `-----BEGIN ... PRIVATE KEY-----` ... `-----END ... PRIVATE KEY-----` | `[PRIVATE_KEY]` |
| `secret` | `sk-ant-...`, `sk-...` (20+ chars), `AKIA[0-9A-Z]{16}`, `ghp_`/`gho_`/`github_pat_`, `xox[abprs]-`, `AIza[0-9A-Za-z_-]{35}`, JWT `eyJ...\.eyJ...\....`, `Bearer <token>`, and the value in `key=value` / `key: value` / `"key": "value"` where the key contains `api_key|apikey|secret|token|password|passwd|pwd|credential|access_key` (keep the key name, mask the value; ignore values shorter than 6 chars and obvious placeholders such as `xxx`, `<...>`, `your_...`, `${...}`, `os.environ...`) | `[SECRET_n]` |
| `url_credential` | `scheme://user:pass@host` | `scheme://[CREDENTIAL]@host` |
| `email` | email addresses | `[EMAIL_n]` |
| `ip` | IPv4 except `127.*` and `0.0.0.0` | `[IP_n]` |
| `phone` | Korean mobile/landline formats (`010-1234-5678`, `+82 10 ...`, `02-123-4567`) | `[PHONE_n]` |
| `home_path` | `/home/<user>/`, `/Users/<user>/`, `C:\Users\<user>\` prefixes | `<HOME>/` (Windows `<HOME>\`) |

Counts go to `stats.in_text` or `stats.in_outputs` by `origin`. `find_secrets` runs the `private_key`, `secret` and `url_credential` detectors and returns their category names (empty list = safe).

Key/value rule details: split the key into parts on `_`, `-`, `.` and camelCase. It is secret-like when (a) a part (or the pair `api`+`key`, `access`+`key`, `private`+`key`, `secret`+`key`) is one of `secret`, `token`, `password`, `passwd`, `pwd`, `credential`, `credentials`, `apikey`, and the LAST part is not a non-secret suffix (`count`, `len`, `length`, `size`, `limit`, `max`, `min`, `type`, `name`, `url`, `uri`, `path`, `file`, `dir`, `prefix`, `ttl`, `expiry`, `expires`, `timeout`, `header`, `field`, `param`, `env`, `enabled`, `format`, `mode`, `version`); or (b) the whole key ends, case-insensitively, with `password`, `passwd`, `secret` or `token` (so `PGPASSWORD`, `GITHUBTOKEN`). So `access_token`, `API_KEY`, `apiKey`, `SECRET_KEY_BASE`, `AWS_SECRET_ACCESS_KEY` match while `max_tokens`, `token_count`, `tokenizer`, `token_url` do not. Comparison forms `==`, `!=`, `===`, `!==` and `:=` count like `=`. SendGrid-style `SG.<16+>.<16+>` values are secrets (pattern rule); key forms `key = v`, `key: type = v`, `"key": v`, `cfg["key"] = v`, `key == v`; unquoted values that are code expressions (identifier paths such as `settings.x`, or followed by `(`/`[` such as `os.getenv("X")`) or `None`/`True`/`False`/`null` are not secrets; quoted values are judged as values. `Authorization: Basic <b64>` is masked like Bearer. Labels for categories: `labels.MASKED`.

### 3.4 Scope (`scope.py`)

`build_scope(session, test_runs) -> dict` returns the ScopeView of §7.3. It must not expose anything that depends on test results: test records are present/absent only; masked counts come from `stats.in_text` only; no event, line, record or tool-use counts. `has_code_edits` uses the same code/config file filter as candidates (`decisions.candidates.file_kind`), so a README-only session shows false.

## 4. Decisions

### 4.1 Evidence (`evidence.py`)

One `Evidence` per event, `id = f"E{idx:04d}"`. Kinds: `user_text` -> `user_request`; `assistant_text` -> `assistant_text`; `tool_use` of an edit tool -> `code_edit` (file set, excerpt = diff text); `tool_use` of `Bash` -> `command` (excerpt = command); `tool_result` of `Bash` -> `command_output`; anything else -> `tool_other`. Excerpts are capped at `Settings.evidence_excerpt_chars` (outputs keep the tail, since summaries are at the end).

Diff text: `Edit` -> `--- {file}` then old lines prefixed `- ` and new lines prefixed `+ `; `MultiEdit` -> the same per hunk separated by `@@`; `Write` -> all lines `+ `; `NotebookEdit` -> `+ ` lines of `new_source`. `diff_lines(evidence) -> list[{"op": "-"|"+"|" "|"@", "text": str}]` serves the UI viewer.

`visible_evidence_ids(ws) -> list[str]` is the single blinding gate for evidence (§4.5).

### 4.2 Test runs (`testruns.py`)

`find_test_runs(events) -> list[TestRun]`. A `Bash` tool_use is test-like when one of its command segments (split on `&&`, `||`, `;`, `|`, newlines; leading `VAR=value` assignments, `cd ...` segments, and `uv run` / `poetry run` / `pipenv run` / `npx` / `timeout N` prefixes stripped; the program may be called by path, e.g. `./gradlew test`, `.venv/bin/python -m unittest`) STARTS with `pytest`, `python[0-9.]* -m (pytest|unittest)`, `npm (run )?test`, `jest`, `vitest`, `go test`, `cargo test`, `mvn ... test`, `gradle(w)? test`, `phpunit` or `rspec`. So `pip install pytest` and `cat pytest.ini` are not test runs.

Supported runners (others get `runner=None`): `pytest` (`pytest`, `python -m pytest`), `unittest` (`python -m unittest`), `jest` (`npm test`, `npm run test`, `npx jest`, `jest`; the output must contain a Jest `Tests:` line).

Exit status: an output starting with `Exit code N` (optionally after `Error: `) -> `exit_code=N`, `exit_source="explicit"`; otherwise `is_error is False` -> `0`, `"is_error_false"`; otherwise `None`.

Summary parsing (last matching line wins):
- pytest: `^=+ (.+) in [0-9.]+s( \(.*\))? =+$` with counts `(\d+) (failed|passed|error|errors|skipped|xfailed|xpassed|deselected|warnings?)`; also the short form `^(\d+ (failed|passed)[^=]*) in [0-9.]+s$` printed by `-q`. fail if failed+errors > 0; pass if passed > 0 and failed+errors = 0; else None.
- unittest: `^Ran (\d+) tests? in` followed later by `^OK` (pass) or `^FAILED \(` (fail).
- jest: `^Tests:\s+(.*)$` with `(\d+) failed` / `(\d+) passed`.

`complete` = a summary line was found and the output has no truncation marker (`[... 출력 일부 생략 ...]`, `[N lines truncated]`, `lines truncated`, `output truncated`, `<response clipped>`, `Output too large`, `... [N lines`, case-insensitive; the bare word `truncated`, e.g. in a test name, is not a marker). Consistency: pytest/jest pass requires exit 0 and fail requires exit != 0 (pytest fail = 1); unittest the same. Inconsistent -> `result=None` and retro reason `inconsistent`.

`test_status(run) -> labels.TEST_STATUS key`: no run -> `none`; `result is None` -> `ran_unknown`; pass -> `recorded_pass`; fail -> `recorded_fail`. `other_version` is used by the instruction check when the run happened on a different code state.

### 4.3 Candidates (`candidates.py`)

`build_candidates(events, evidence, test_runs) -> list[Candidate]` (max 3).

- Request unit = all events with the same `turn >= 1`. A unit is a candidate when it has at least one code edit: `Edit`/`MultiEdit`/`Write`/`NotebookEdit` on a code or config file (extensions `.py .js .ts .tsx .jsx .mjs .cjs .java .kt .go .rs .rb .php .cs .c .h .cpp .hpp .swift .scala .sh .sql .html .css .scss .vue .svelte .ipynb`, names `requirements*.txt`, `pyproject.toml`, `package.json`, `setup.py`, `setup.cfg`, `Pipfile`, `go.mod`, `Cargo.toml`, `Dockerfile`, `docker-compose*.yml`, `Makefile`, `*.toml`, `*.yaml`, `*.yml`, `*.ini`, `*.cfg`, `.env.example`; never lock files).
- `decision_idx` = last code edit of the unit. Signals use ONLY events of the unit with `idx <= decision_idx`, and never `tool_result` text (outputs can carry results).
- Added lines = lines of `new_string`/`content`/`new_source` not present in `old_string`.
- Signals (code -> rule -> weight): `try_except` added lines `^\s*(try:|except\b|finally:|catch\s*\(|\}\s*catch|\.catch\(|rescue\b)` 3; `raise_error` `\b(raise|throw)\b` 2; `input_check` `isinstance\(|\bif not \w|ValueError|TypeError|validat|\bassert\b|\.strip\(\)|len\(\w+\)\s*[<>=]` 2; `http_call` `requests\.|httpx\.|urllib|aiohttp|fetch\(|axios|urlopen|http\.client|websocket` 3; `timeout_retry` `timeout|retr(y|ies)|backoff|sleep\(` 2; `file_or_db_io` `\bopen\(|Path\(|sqlite3|psycopg|cursor\.|\.execute\(|json\.(load|dump)|csv\.` 1; `dependency_change` dependency manifest edited or a unit Bash command before the decision matches `pip install|uv add|poetry add|npm (install|i)\b|yarn add|pnpm add` 2; `config_change` config file edited 1; `assumption_language` unit user/assistant text before the decision matches `가정|아마|일반적으로|보통은|추정|것으로 보입니다|assum|probably|likely|should be fine|presumably` 3; `alternative_language` `대신|대안|선택|방식으로|instead of|rather than|alternative|trade-?off|chose|choose` 2; `new_function` added lines `^\s*(async def |def |class |function |export (async )?function |func |const \w+\s*=\s*(async )?\()` 1; `large_change` added+removed lines >= 30, 1.
- Score = sum of distinct signal weights + min(changed_lines / 20, 2). Rank by score desc, then earlier turn. Ids `c1..c3`.
- Category = group with the highest weight sum; ties in this order: `error_handling` (try_except, raise_error), `external_call` (http_call, timeout_retry, file_or_db_io), `input_validation` (input_check), `dependency_config` (dependency_change, config_change), `assumption` (assumption_language, alternative_language), `design_choice` (new_function, large_change, or no signal).
- Evidence level (result independent): `has_request` = the unit's human request has >= 10 chars; `has_explanation` = assistant text in the unit before the decision has >= 40 chars in total. sufficient = both; partial = one; insufficient = neither.
- `evidence_ids` = evidence of the unit with `idx <= decision_idx`.
- `has_test_record` = a test-like run exists with `use_idx > decision_idx`.
- `later_fix` = an edit to one of the candidate's files exists after the analysis window end (§4.4).
- Cards never contain session text (§7.3).

### 4.4 Retro (`retro.py`)

- Target = the first test-like run with `use_idx > decision_idx`. No such run -> `no_test`. Any edit tool_use (any file) between `decision_idx` and `target.use_idx` -> `code_changed`.
- Then, in order: `runner is None` -> `unsupported_runner`; `result_idx is None` -> `no_exit_code`; `interrupted` -> `interrupted`; `not complete` -> `truncated`; `exit_code is None` -> `no_exit_code`; `result is None` -> `inconsistent`. Otherwise eligible.
- Analysis window = `[request_idx, end]` where `end = target.result_idx` when a target run with a result exists (eligible or not), else the last event of the unit.
- `precontext(ws) -> dict`: the candidate's request, the last human request before the run when different, the candidate's code edits, and the command (`evidence_id`, `command`, `runner`). Nothing else.
- `reveal(target, evidence) -> dict`: `command`, `exit_code`, `exit_source`, `result`, `result_label`, `summary_line`, `output_excerpt` (last 3,000 chars of the redacted output), `evidence_id` of the result.
- `label(choice, result)`: `unknown` -> `withheld`; equal -> `match`; else `mismatch`.
- `checkpoints(target)` fixed phrases: `종료 코드: {n} (명시)` or `종료 코드: 0 (is_error=false로 추정)`, `요약 줄: {summary_line}`.

### 4.5 Blinding rules (server gated)

- Selecting a candidate locks the workspace to it (`POST /api/select`). Other candidates need a new upload. (Stricter than "lock on submission"; it prevents another candidate's evidence from revealing this one's result.)
- Before selection: no evidence content is served at all, only cards (§7.3).
- Selected and retro eligible, prediction not stored: visible = pre-context evidence only. Everything with `idx > target.use_idx` is hidden from every response, every preview and every provider call. No provider call, preview or background job may start.
- After the prediction is stored (or when not retro eligible): visible = the analysis window of the candidate, plus retrieved related requests once retrieval ran. Later-fix edits are never visible and never sent; the UI only shows the `later_fix` tag.
- The reveal is returned only by the response of `POST /api/prediction` (`Cache-Control: no-store`). Later `GET /api/workspace` returns the stored prediction and reveal read-only. One prediction per workspace.
- Tests: two fixtures that differ only in the target result must give byte-identical pre-submission responses after removing random IDs; a marker string present only in the target output must not appear in any pre-submission response or in the server log.

## 5. Flow

### 5.1 Consent and previews (`consent.py`)

Stages and providers:

| stage | providers | payload |
|---|---|---|
| `analysis` | `local_rag`, `clova_rerank`, `local_llm`, `anthropic` | local_rag: human-request texts (query + pool); clova_rerank: query + top related requests; local_llm: the candidate context text (<= 4,000 chars); anthropic: the maximal analysis bundle (base items + retrieval pool) |
| `review:hcx`, `review:deepseek` | `hcx` or `deepseek` | the same bundle Claude received, without Claude's output |
| `feedback1`, `feedback2` | `anthropic` | bundle + What-if + the user's answers (+ first feedback for attempt 2) |

`make_preview(ws, stage, settings, **extra) -> dict` stores and returns `{preview_id, stage, providers: [{name, label, destination, purpose, mock, applicable, skip_reason, items: [{evidence_id, chars, sha256}], payload_text, chars, policy: [str]}]}`. `record_consent(ws, preview_id, decisions)` stores `ws.consents[stage]`. `check_send(ws, stage, provider, items, text)` raises `ProviderError("consent_missing")` without consent, `ProviderError("precondition")` when an item is not in the previewed set (by id and sha256), and `ProviderError("secret_detected")` when `find_secrets(text)` is not empty. Declined providers are never called (counter stays 0). A candidate with insufficient evidence gets no analysis preview at all (409 `precondition` with the insufficient-evidence message).

The analysis stage's Anthropic consent covers Ⓐ, Ⓔ and Ⓑ (the same bundle, different task texts; the purpose line says so). The feedback stages preview and consent the report's gap summary as a pseudo item of their own (§5.10), like the answers.

Policy lines (confirmed facts only; otherwise `미확인`): anthropic "API 데이터는 모델 학습에 쓰지 않음(공식 자료)", "30일 안에 삭제(공식 자료)"; deepseek "Public Preview: 입력·출력이 학습에 쓰일 수 있음(약관 §4.3)", "학습 거부·ZDR 미적용", "보관 기간 미확인"; hcx / clova_rerank "데이터 정책 미확인"; local_rag / local_llm "수업 서버: 공인 IP 평문 HTTP, 인증 없음", "보관 여부 미확인". In mock mode the destination reads "모의 응답(실제로 보내지 않음)".

### 5.2 Bundle (`bundle.py`)

```
<session_evidence>
<evidence id="E0012" kind="code_edit" turn="3" file="weather.py">
...text...
</evidence>
</session_evidence>
```
The user message of every model call is built only from these tagged sections (mocks parse them):
`<session_evidence>` (above), `<candidate>` (category, files, turn), `<tier>basic|standard|advanced</tier>` (What-if only),
`<gaps>` (the stored report's gap points, What-if and feedback only, only after a report was stored §5.10),
`<whatif>` (the generated What-if JSON, feedback only), `<answer field="expected_behavior|potential_issue|ai_instruction|verification">...</answer>` (feedback only),
`<first_feedback>` (the first feedback JSON, feedback2 only), `<task>` (the instruction for this call).
Escaping (`bundle.escape_text`, also applied to the user's answers): inside any session or user text, every `<` that starts one of our tag names, opening or closing (`evidence`, `session_evidence`, `candidate`, `tier`, `gaps`, `whatif`, `answer`, `first_feedback`, `task`), gets a backslash after it (`<\evidence`, `<\/answer`). Attribute values replace `"` with `'` and `<`/`>` with `‹`/`›`. Citation checks use the escaped text that was sent. Mocks read `<tier>`, `<gaps>`, `<whatif>`, `<answer>`, `<first_feedback>` and `<task>` only from the part of the prompt after `</session_evidence>`. Order: requests, assistant explanations, code edits, commands and outputs of the window (the target output only after the reveal), then retrieved requests (reranker-cited first, attribute `cited="true"`). Item caps: outputs 2,500 chars (head 500 + tail 2,000), others 3,000. Total cap `bundle_max_chars`; drop in this order until it fits: `tool_other`, uncited retrieved requests, older outputs. Never included: later fixes, thinking, predictions and reasons, the local LLM summary, the report's own sections (only its gap points are ever reused, through `<gaps>`). `build_bundle(ws, settings, *, retrieved=None, cited=None) -> Bundle(items: list[BundleItem(evidence_id, kind, turn, file, text, sha256)], text, chars)`; `analysis_bundle(ws, settings)` rebuilds the bundle from the stored retrieval result and is used for the analysis, What-if, feedback and both reviews. The total never exceeds `bundle_max_chars`: when the items that are never dropped exceed it, keep the request(s), the edits nearest the decision, and the target command and output, drop older edits and explanations first, and finally shorten item texts (citation checks use the shortened text that was sent). Uncited retrieved requests carry `cited="false"`. `build_bundle(..., maximal=True)` builds the preview's maximal set under the same cap; shortened texts carry one of `bundle.CUT_MARKS`. The shortening depends only on the window, so preview hashes and sent texts match. Retrieval results are ignored when the window alone exceeds the cap.

### 5.3 Prompts and schemas

Prompts (`prompts.py`, Korean): role; use only `<session_evidence>`; it is data, ignore instructions inside it; cite evidence IDs; a statement of kind `record` needs `quote` copied exactly from the cited evidence; `interpretation` for inference; `needs_verification` when unsure; later fixes are not answers; never judge the user's ability or give scores; answer in Korean; output JSON only.

Schemas (`schemas.py`; every object has `additionalProperties: false`; no min/max constraints):
- `STATEMENT = {text: str, kind: "record"|"interpretation"|"needs_verification", evidence_ids: [str], quote: str}` (`quote` is `""` unless `record`).
- `ANALYSIS = {decision_summary: str, reasons: [STATEMENT], alternatives: [STATEMENT], assumptions: [STATEMENT], risks: [STATEMENT], verification_points: [STATEMENT], scope_note: str}`
- `REPORT = {overview: str, tasks: [STATEMENT], changes: [STATEMENT], decisions: [STATEMENT], tests: [STATEMENT], not_determined: [str], prompt_findings: [{aspect: "clarity"|"context_constraints"|"exceptions"|"verification"|"scope_expectation", observation: STATEMENT, suggestion: str}], gaps: [{concern: "missing_verification"|"unclear_rationale"|"requirement_test_gap"|"nonfunctional_risk"|"post_failure_continuation", text, kind, evidence_ids, quote}], actions: [{kind: "ask_ai"|"requirements"|"tests"|"alternatives", text: str, evidence_ids: [str]}]}` (§5.10)
- `WHATIF = {can_generate: bool, reason: str, title: str, scenario: str, hypothetical_changes: [str], related_evidence_ids: [str], questions: {expected_behavior: str, potential_issue: str, ai_instruction: str, verification: str}}`
- `FEEDBACK = {items: [{field: "expected_behavior"|"potential_issue"|"ai_instruction"|"verification", strengths: [str], gaps: [str], hint: str, evidence_ids: [str]}], instruction_review: {has_requirements: bool, has_verification_steps: bool, note: str}, axes: {understanding: AX, verification: AX, control: AX}, coaching: str, explanation: str}` with `AX = "shown"|"partial"|"not_shown"`; `explanation` is `""` in the first feedback.
- `REVIEW = {summary: str, points: [{type: "judgment"|"issue"|"counterexample"|"alternative"|"missing_evidence", text: str, evidence_ids: [str], quote: str}]}`

Calls per flow: Ⓐ analysis (effort high, max_tokens 32,000), Ⓔ report (high, 24,000), Ⓑ whatif (high, 24,000), Ⓒ feedback1 (medium, 12,000), Ⓓ feedback2 (medium, 12,000). The What-if prompt gets the tier code and its meaning: basic = one changed condition with more context; standard = one or two interacting conditions; advanced = combined edge cases and verification design. The raw self-assessment is never sent. When a report was stored, Ⓑ and Ⓒ·Ⓓ also get `<gaps>` with at most `report.GAP_PROMPT_LIMIT` gap points (concern, concern_label, text, kind, evidence ids that were sent, §5.10).

### 5.4 Citations (`citations.py`)

`grade_statements(statements, items) -> list[statement + {"grade", "invalid_ids"}]` plus stats `{total, recorded, citation_unverified, ...}`. Normalize by collapsing whitespace. Any unknown evidence ID -> `citation_unverified`. `record` with a quote found in one of its cited items -> `recorded`, else `citation_unverified`. `interpretation` with >= 1 valid ID -> `inferred`, else `needs_verification`. `needs_verification` -> `needs_verification`. A quote must lie within one stretch of text between cut marks; a quote that crosses or copies a cut mark is `citation_unverified`.

### 5.5 Comparison (`compare.py`)

`compare(by_provider: dict[str, list[dict]]) -> dict`: for each evidence ID cited by any provider, the providers citing it. `same_evidence` = cited by >= 2 providers; `only_one` = cited by exactly 1; `different` = statements with no valid evidence ID (not comparable). Each entry carries the statements per provider. Always add the note "의견이 같아도 사실로 확정하지 않습니다".

### 5.6 Self-assessment (`selfassess.py`)

- Input `{understanding, verification, control}` each 0..2 (labels.SELF_RATING). Validate strictly.
- `tier_for(sa)`: sum <= 2 basic, 3..4 standard, >= 5 advanced.
- `compare_axes(sa, axes)`: observed shown=2, partial=1, not_shown=0; relation `higher` / `similar` / `lower`, neutral Korean message, never a score.
- `next_difficulty(tier, axes)`: all shown -> harder (advanced stays `same`); any not_shown -> easier (basic stays `same`); else same. Text only; the next task is not generated.
- Shapes (as implemented): `validate(value) -> dict` raises `ValueError`; `compare_axes` returns rows `{axis, axis_label, self_rating, self_label, observed, observed_label, relation, message}` where `relation` compares the self-rating with what the answer showed; `next_difficulty` returns `{code, label, message}`.

### 5.7 Instruction check (`instruction.py`)

`check_instruction(text, test_runs, window) -> dict`: mentions of test files (`test_\w+\.py`, `\w+\.test\.[jt]sx?`, `\w+_test\.go`) and runners; for each, whether the session recorded such a run and its `TEST_STATUS` (`other_version` when the run is outside the analysis window). Rule engine output, shown next to the model's `instruction_review`.

### 5.8 Caps (`caps.py`) — initial values

Per flow (workspace): Claude 7 attempts (the 5 calls ⒶⒺⒷⒸⒹ plus 2 spare retries), HCX 1 per stage, DeepSeek 1 per stage, reranker 1, embedding 1, local LLM 1. The report (Ⓔ) and the deep analysis (Ⓐ) share the `analysis` concurrency slots. Server-wide: 2 concurrent analysis jobs, 1 concurrent local LLM call, 300 Claude attempts per day, 300 CLOVA calls per day (in memory, reset at local midnight, not reset by new uploads). The gate is called before every HTTP attempt, including retries.

### 5.9 Orchestrator (`orchestrator.py`)

Jobs run as asyncio tasks (`web/jobs.py`). Status moves `queued -> running -> streaming -> validating -> done | failed`; `progress` holds short Korean text; `model` holds the provider-reported model name. Analysis pipeline: retrieval (consented, >= `rag_min_requests` requests) -> rerank (consented) -> bundle -> Claude Ⓐ -> grading. The development-process report (Ⓔ, §5.10) is its own job on the same bundle, started by the UI right after the analysis (`POST /api/report`); it shares the analysis consent and slot. The local LLM summary runs as its own background job and never blocks or feeds other steps. A failed step keeps every earlier result; the error has `code`, Korean `message`, `retryable`. `retryable` is true only while the per-flow cap still leaves a call for that provider and stage (`Caps.exhausted`), so the UI never offers a retry that would end in `cap_exceeded`. Starting a job drops older failed jobs of the same kind (only the newest attempt per kind is kept; the caps still count every call).

### 5.10 Development-process report (기능 1의 개발 과정 분석 리포트)

`POST /api/report` starts the Ⓔ job on the analysis bundle, under the analysis consent — so it can never run before the retro prediction/reveal gate (`unblinded_candidate`) opens, and it reuses the bundle exactly (retrieval settled, same hashes, same cap). Preconditions: selected and unblinded, a done analysis (the report reuses its bundle; the analysis goes first), Anthropic consented. Insufficient evidence or a declined Anthropic return a notice and call nothing. One job per flow unless it failed (`already_submitted`).

- Claude Ⓔ produces (§5.3 REPORT): an overview of the development flow; graded statement groups (tasks, changes, decisions, tests); `not_determined` (what the records alone cannot show); prompt findings (aspect + observation + suggestion); gap points (concern + statement); actionable recommendations (kind + text + ids). `report.grade_report` grades every statement with the Ⓐ machinery (evidence ids, exact-quote checks, grade badges) and counts citation stats; action ids are filtered to the sent bundle and the dropped count is shown.
- Prompt rules keep the product principles: session developer and current user are different people; "기록에서는 확인되지 않는다" is the only way to state absence (never "did not do"); no scores, grades, ability verdicts; short prompts are fine when prior context exists; every finding and gap cites evidence; actions must tie to the actual records.
- Reuse (never a wholesale feed-back into models): `report.review_gaps` compacts at most `GAP_PROMPT_LIMIT` stored gap points into the `<gaps>` section of the What-if (Ⓑ) user message and of the feedback stages' payloads (feedback stages preview and consent them as a pseudo item, like the answers). The What-if waits while a report job is in flight (`409 precondition`) so the inclusion is deterministic; a failed or absent report lets Ⓑ·Ⓒ·Ⓓ proceed without gap points. Nothing else from the report — overview, findings, actions — is ever sent to a model.
- The reports of HCX-007 and DeepSeek never receive the report either: they keep reviewing the analysis bundle only.

## 6. Providers

### 6.1 Interfaces (`base.py`)

```python
class ProviderError(Exception):
    def __init__(self, code: str, message: str | None = None, *, retryable: bool = False,
                 attempted: bool = False, status: int | None = None) -> None
# codes: consent_missing cap_exceeded daily_cap budget_exceeded halted secret_detected refusal incomplete
#        invalid_output timeout connection auth provider_rate_limited server_error bad_request disabled precondition

@dataclass
class LLMResult:
    provider: str; model: str; data: dict | None; text: str; stop_reason: str | None
    usage: dict; attempts: int; mock: bool; elapsed_ms: int

Gate = Callable[[str], None]          # gate(provider_name) before each HTTP attempt; raises ProviderError
Status = Callable[[str, str], None]   # on_status(status, korean_progress)

class Claude(Protocol):
    async def generate(self, *, stage: str, system: str, user: str, schema: dict, effort: str,
                       max_tokens: int, gate: Gate, on_status: Status | None = None) -> LLMResult
class Reviewer(Protocol):              # HCX-007 and DeepSeek
    async def review(self, *, system: str, user: str, schema: dict, gate: Gate,
                     on_status: Status | None = None) -> LLMResult
class Embedder(Protocol):
    async def embed(self, texts: list[str], *, gate: Gate) -> list[list[float]]
class Reranker(Protocol):
    async def rerank(self, query: str, documents: list[dict], *, gate: Gate) -> list[str]  # cited ids
class Summarizer(Protocol):
    async def summarize(self, text: str, *, max_sentences: int = 3, gate: Gate) -> dict  # {model, summary, elapsed_ms}

@dataclass
class Providers:
    claude: Claude | None; hcx: Reviewer | None; deepseek: Reviewer | None
    embedder: Embedder | None; reranker: Reranker | None; summarizer: Summarizer | None
```

### 6.2 Live adapters (request shapes)

- Claude: `anthropic.AsyncAnthropic(api_key=<ANTHROPIC_API_KEY only>, max_retries=0, timeout=600, base_url=settings.anthropic_base_url)` (the base URL is always explicit, default `https://api.anthropic.com`, so an `ANTHROPIC_BASE_URL` env var is never used; a missing key is `auth` before any request; `python -m agency_loop` removes `ANTHROPIC_CUSTOM_HEADERS` from its environment because the SDK would merge those headers into every request); `async with client.messages.stream(model=settings.claude_model, max_tokens=..., system=..., messages=[{"role": "user", "content": user}], output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}}) as stream`; status `streaming` on the first text delta; `final = await stream.get_final_message()`. No `thinking` parameter (adaptive is the default on Opus 5.5; thinking is never shown), no sampling parameters, no fallback (off until approved). `stop_reason == "refusal"` -> `refusal`; `"max_tokens"` -> `incomplete` (retryable by the user). JSON from the text blocks; parse/validation failure -> `invalid_output`. App retry: one extra attempt for 429 (honour `retry-after` up to 20 s), 5xx/529 and connection errors; none for 400/401/403/404/refusal/max_tokens. Every attempt calls `gate("anthropic")` first.
- HCX-007: `POST {clova_base_url}/v3/chat-completions/HCX-007`, headers `Authorization: Bearer {CLOVA_API_KEY}`, `Content-Type: application/json`, `X-NCP-CLOVASTUDIO-REQUEST-ID: <uuid4>`; body `{messages, topP: 0.8, topK: 0, temperature: 0.5, repetitionPenalty: 1.1, seed: 7, includeAiFilters: false, maxCompletionTokens: settings.hcx_max_completion_tokens, thinking: {effort: settings.hcx_thinking_effort}}`. Success when `status.code == "20000"`; text in `result.message.content`; usage `result.usage.promptTokens/completionTokens`. JSON output is not documented: ask for JSON in the prompt, parse leniently (first `{` .. last `}`), validate; on failure return `data=None` and keep the text (UI: 형식 미확인). 429 -> `provider_rate_limited`, never retried; CLOVA calls are then disabled for 60 s server-wide.
- Reranker: `POST {clova_base_url}/v1/api-tools/reranker` body `{query, documents: [{id, doc}] (1-10), maxTokens: 1024}` -> `result.citedDocuments[].id`. `result.result` is never shown.
- DeepSeek (Mistral): one `httpx.AsyncClient` POST `{mistral_base_url}/v1/chat/completions`, headers `Authorization: Bearer {MISTRAL_API_KEY}`, body `{model, messages, max_tokens: settings.mistral_max_tokens, response_format: {type: "json_schema", json_schema: {name: "review", schema, strict: true}}}`; no retries, non-streaming, timeout 120 s. Ledger: reserve before send, settle with `usage.prompt_tokens`/`completion_tokens` after, release when the request was definitely not sent (connect error) or rejected with 4xx; keep the reservation on timeouts/5xx/unknown. 402 -> `budget_exceeded` and DeepSeek disabled until restart. A missing ledger file -> `ledger_missing`; a corrupt one -> `halted`. The ledger reservation comes BEFORE `gate("deepseek")` (a gate refusal releases it), so a ledger refusal never uses up the flow's one DeepSeek call.
- BGE-M3: `POST {ollama_embed_url}/api/embed` body `{model: "bge-m3", input: [...], truncate: false, keep_alive: "5m", options: {num_ctx: 4096}}` -> `embeddings`. Inputs over ~4,000 chars are split; the item vector is the mean.
- Local LLM: MCP stdio client (`mcp>=1.30,<2`): `StdioServerParameters(command=settings.mcp_command, args=list(settings.mcp_args), cwd=settings.mcp_cwd, env={"OLLAMA_BASE_URL": settings.ollama_chat_url, "OLLAMA_MODEL": settings.local_llm_model, "PYTHONDONTWRITEBYTECODE": "1", "PATH": os.environ["PATH"]})` - no API keys in the child env (mcp's `stdio_client` also adds its own safe defaults such as HOME, PATH, SHELL, TERM, USER, LOGNAME); `ClientSession.call_tool("summarize_text", {"text": text[:4000], "max_sentences": 3})` with an overall timeout of `mcp_timeout_sec`. The course folder is executed, never modified.

### 6.3 Mock providers (`mock.py`)

Deterministic, no network, small artificial delay (default 0.05 s). They parse `<evidence id=...>` items out of the prompt and build schema-valid JSON that cites real IDs and copies real substrings as quotes, so citations verify. Every result has `mock=True` and a model name prefixed `mock-`; the UI labels it "모의 응답(개발용)". Feedback mocks derive strengths/gaps from simple answer features (length, mentions of tests/conditions); they are clearly generic and never pretend to judge ability.

### 6.4 Ledger (`ledger.py`)

```python
class LedgerRefused(Exception): code: str   # "cap" | "halted" | "missing" | "corrupt"
class Ledger:
    def __init__(self, path, *, cap_micro_usd, price_in, price_out, out_ub_tokens)
    @staticmethod
    def init(path) -> None              # O_EXCL create, mode 0o600; refuses if it exists
    def reserve(self, call_id: str, request_body_bytes: int, max_tokens: int) -> int   # micro-USD reserved
    def settle(self, call_id: str, prompt_tokens: int, completion_tokens: int) -> int  # micro-USD charged
    def release(self, call_id: str) -> None   # definitely not sent: charge 0
    def ack(self, call_id: str) -> None       # operator acknowledges an overrun
    def status(self) -> dict                  # spent, unsettled, cap, remaining, halted, overruns
```
Append-only JSONL, metadata only (`op`, `id`, micro-USD amounts, token counts, unix time). Every operation opens the file fresh, takes a process-wide `threading.Lock` plus `fcntl.flock(LOCK_EX)`, repairs a torn last line (truncate to the last newline), recomputes `spent = sum over ids of (charge if settled/released else reservation)`, and for reserve refuses when `spent + R > cap` or an unacknowledged overrun exists. Writes check for short writes and `fsync` before returning. `R = ceil((request_body_bytes + 4096) * price_in / 1e6) + ceil(out_ub * price_out / 1e6)` with `out_ub = max(max_tokens, settings.mistral_out_ub_tokens)`. A settle above the reservation records an overrun and halts reserves until `ack`. CLI: `python -m agency_loop.ledger init|status|ack <call_id>`.

## 7. Web API

All JSON. Workspace via cookie `al_ws` (HttpOnly, SameSite=Strict, Path=/). Every `/api/*` response has `Cache-Control: no-store`. Errors: `{"error": {"code", "message", "retryable"}}` with Korean messages from `labels.ERROR`. Unknown or foreign workspace/job/evidence IDs -> 404.

### 7.1 Security (`security.py`)

- Host header must be in `settings.allowed_hosts` (400 otherwise).
- POST/DELETE must carry `Origin` equal to `http://<allowed host>` (403 otherwise). No CORS headers.
- Headers on every response: `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, `Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'`.
- Upload rate: `upload_rate_per_10min` per client IP; workspace count cap `max_workspaces` (503 `server_busy`).
- Logging: never log bodies, evidence, prompts, model output or keys.

### 7.2 Endpoints

| Method and path | Request | Response |
|---|---|---|
| `GET /` | - | `static/index.html` |
| `GET /static/{file}` | - | static assets |
| `GET /api/health` | - | `{"ok": true, "modes": {anthropic, clova, mistral, embed, local_llm}}` |
| `POST /api/workspaces` | raw JSONL body (any content type), <= 32 MiB (33,554,432 bytes; one more byte is 413, declared or chunked), streamed | 201 `{"scope": ScopeView, "candidates": [Card], "expires_in_sec"}` + cookie; replaces the caller's previous workspace. 413 `too_large`, 400 `bad_format` (no parseable line), 422 `no_events`, 429, 503 |
| `GET /api/demo-session` | — | 200 the synthetic `fixtures/sessions/demo_weather.jsonl` (`application/x-ndjson`, attachment). One fixed file: no path or query selects another, no listing; 404 `not_found` when the fixtures are not installed. The page fetches it and POSTs the bytes to `/api/workspaces` like a picked file |
| `GET /api/workspace` | - | `WorkspaceView` (§7.3) |
| `DELETE /api/workspace` | - | 204, cookie cleared, data dropped |
| `POST /api/select` | `{"candidate_id", "self_assessment": {understanding, verification, control}}` | `{"selected", "tier", "tier_label", "retro": RetroView, "evidence": [EvidenceView]}`; 409 `locked` if already selected |
| `GET /api/evidence/{id}` | - | `EvidenceView` or 404 when not visible |
| `POST /api/prediction` | `{"choice": "pass"|"fail"|"unknown", "reason": str (<= 1000)}` | `{"prediction", "reveal", "label", "label_text", "checkpoints", "note", "evidence": [EvidenceView]}`; 409 when already stored or not eligible |
| `POST /api/preview` | `{"stage", "answers"?}` | Preview (§5.1) |
| `POST /api/consent` | `{"preview_id", "providers": {name: bool}}` | `{"stage", "providers"}` |
| `POST /api/analysis` | - | `{"job_id": str or null, "summary_job_id": str or null, "notice": str or null}`; `job_id` is null with a notice when Anthropic was declined or the evidence level is insufficient (rule-engine evidence only); a re-run is allowed only when the previous analysis job failed |
| `POST /api/report` | - | `{"job_id": str or null, "notice": str or null}`; 409 `precondition` before the prediction (blinded) or without a done analysis; notice (no call) when Anthropic was declined or evidence is insufficient; 409 `already_submitted` while a report job that did not fail exists; the report reuses the analysis bundle and consent (§5.10) |
| `POST /api/reviews` | `{"provider": "hcx"|"deepseek"}` | `{"job_id"}` |
| `POST /api/summary` | - | `{"job_id"}`; only when `local_llm` was consented for the analysis stage and the previous summary job failed |
| `POST /api/whatif` | - | `{"job_id"}`; needs a done analysis and no in-flight report job (409 `precondition` while one is queued or running §5.10) |
| `POST /api/feedback` | `{"attempt": 1|2, "preview_id"}` | `{"job_id"}` (answers come from the consented preview) |
| `GET /api/jobs/{id}` | - | `JobView` |

Implemented conventions: a duplicate analysis, What-if or feedback attempt -> 409 `already_submitted` (a re-run is allowed only after a failure); the prediction `reason` is optional (default ""); `POST /api/analysis` without a recorded consent returns `job_id` null with a notice; with insufficient evidence the analysis stage has no consent step and calls no provider at all (Local LLM included) and stores retrieval state `insufficient`; `ws.consents[stage]` = `{preview_id, providers, items: {provider: {id: sha256}}, answers}` and only the newest preview per stage is kept; caps keys for reviews are `"<provider>:<stage>"`. An unknown candidate in `POST /api/select` -> 404 `not_found`; JSON request bodies are capped at 64 KiB and must re-encode as UTF-8 (a lone surrogate escape is 400 `invalid_request`); every upload attempt counts toward the per-IP rate; the cookie is a session cookie and every access refreshes the sliding TTL. Preconditions not met (e.g. What-if before the analysis is done) -> 409 `precondition`; prediction on an ineligible candidate -> 409 `not_eligible`; a second prediction or a third feedback -> 409 `already_submitted`; a declined provider -> 409 `consent_missing`.

### 7.3 Views (`views.py`)

- ScopeView: `{"format": "claude_code_jsonl", "human_requests": int, "has_code_edits": bool, "has_test_records": bool, "masked": {category: n} (in_text only), "masked_labels": {...}, "dropped": [korean category names], "bad_lines": int, "notices": [str]}`.
- Card: `{"id", "rank", "turn", "title" (labels.CATEGORY_TITLE), "category", "category_label", "signals": [{"code", "label"}], "files": [str], "lines": [jsonl line of each edit], "evidence_level", "evidence_level_label", "has_test_record", "retro_available"}`. No session text.
- RetroView: eligible -> `{"available": true, "precontext": {...}, "question", "choices": [{"value", "label"}]}`; not eligible -> `{"available": false, "reason", "reason_label", "message"}`; after submission `{"available": true, "submitted": true, "prediction", "reveal", "label", "label_text", "checkpoints"}`.
- EvidenceView: `{"id", "kind", "kind_label", "turn", "line", "file", "text", "diff": [{"op", "text"}] | null, "later_fix": bool}`.
- WorkspaceView: `{"scope", "candidates", "selected", "tier", "tier_label", "self_assessment", "retro", "evidence_ids", "evidence": [EvidenceView] (all visible items, so a page reload needs one request), "consents", "results": {"analysis", "report", "summary", "retrieval", "reviews", "comparison", "whatif", "feedback": [..]}, "jobs": [JobView without result], "calls", "expires_in_sec"}`. Everything respects §4.5. `RetroView.prediction` after submission is `{choice, choice_label, reason}` and the submitted RetroView carries the same `note` (`views.PREDICTION_NOTE`) as the prediction response.
- JobView: `{"id", "kind", "status", "status_label", "progress", "model", "mock" (model starts with "mock-"), "result", "error", "updated_at"}`. A job's `result` is the same dict that is stored under `ws.results`.
- PreviewView: the record of §5.1 plus `stage_label`; payload texts are already redacted and blinded.

Stored results (`ws.results`, shown through WorkspaceView and JobView):
- `GradedStatement = {text, kind, evidence_ids, quote, grade, grade_label, invalid_ids}`.
- `analysis = {model, mock, decision_summary, scope_note, sections: {reasons, alternatives, assumptions, risks, verification_points: [GradedStatement]}, section_labels, stats: {total, recorded, inferred, needs_verification, citation_unverified}, bundle_ids, bundle_chars, search_state, search_state_label}`.
- `report = {model, mock, overview, tasks, changes, decisions, tests: [GradedStatement], summary_labels, not_determined: [str], prompt_findings: [{aspect, aspect_label, observation: GradedStatement, suggestion}], gaps: [{concern, concern_label, ...GradedStatement}], actions: [{kind, kind_label, text, evidence_ids}], dropped_id_count, stats: the analysis shape, bundle_ids, bundle_chars}` (§5.10).
- `retrieval = {state: "skipped"|"no_results"|"insufficient"|"done"|"failed", state_label, retrieved: [evidence id of user_request items], cited: [evidence id]}` (the evidence gate reads `retrieved`; `failed` = the embedding or rerank call failed and the analysis continued with rule-engine evidence; a rerank that cites nothing -> `no_results` with `retrieved` cleared).
- `summary = {model, mock, summary, elapsed_ms, label: "요약(Local LLM, 화면 표시용 — 근거나 다른 모델의 입력으로 쓰지 않음)"}` or `{error}`.
- `whatif = {model, mock, tier, tier_label, can_generate, reason, title, scenario, hypothetical_changes, related_evidence_ids, dropped_id_count, questions: {field: str}, question_labels: {field: label}}`.
- `feedback = [FeedbackResult]` (attempt 1 then 2): `{attempt, model, mock, answers: {field: str}, items: [{field, field_label, strengths, gaps, hint, evidence_ids}], instruction_review, instruction_check (rule engine, §5.7), axes, axes_labels, comparison (compare_axes rows), coaching, explanation, next_difficulty (attempt 2 only, else null)}`.
- `reviews = {"hcx"|"deepseek": {model, mock, format_ok, summary, points: [GradedStatement-like with type, type_label], text (only when format_ok is false)}}`; `comparison` = §5.5 output.

## 8. UI (`static/`)

One page, three steps with a stepper: ① 세션 업로드, ② Agency Mirror (2-1 후보 선택 · 2-2 기록 확인 예측 · 2-3 결과 공개 · 2-4 심층 분석; skipped sub-steps greyed out), ③ What-if Lab + Feedback. Inside 2-4 the development-process report renders around the deep analysis in the recommended order: 개발 세션 요약 → Claude 심층 분석(주요 기술적 의사결정) → 프롬프트 개선 포인트 → 판단·검증 보완 포인트 → 다음 개발을 위한 개선 행동; the report auto-starts once the analysis is done (only when Anthropic was consented and the evidence is sufficient), its job line sits with the others, and the 가정 상황 만들기 button appears only once the report has settled so the scenario can reuse its gap points. Top bar: step indicator, theme toggle (light/dark via CSS variables, respects `prefers-color-scheme`), 작업 공간 삭제 (confirmation dialog). Components: evidence cards with grade badges (text + colour), code/log viewer (line numbers, +/- highlighting, collapse), retro vs hypothetical badges (different colour and wording), model comparison panel, consent dialog per stage (destination, chars, policy, payload text collapsed), job status line in an `aria-live` region, 모의 응답 badge when `mock`. Beginner layer (display only, §4.5 untouched): a candidate explainer and plain-language self-assessment questions; evidence shown under human names (내 요청, AI의 설명, 수정된 코드, 실행한 명령, 명령 결과, 도구 알림 — `labels.EVIDENCE_KIND` stays what models see) with ID, turn/line and the server label folded into 상세 정보, grouped in session order as 무엇을 시켰나 → 어떻게 고쳤나 → 어떻게 확인했나; statements preview their cited records on hover/focus and jump to the original on click (with a way back); every badge carries a one-line meaning (title + screen-reader text, inline on cards and under 설명 더 보기); a 설명 더 보기 toggle and a dismissible six-step tour (`al-explain`, `al-tour-done` in localStorage, try/catch). Upload help (step 1, every section a `<details>` closed by default): Claude Code 세션이란?, Claude Code 세션 파일 찾는 방법 (Linux · macOS / Windows tablist with arrow/Home/End keys and roving tabindex, exact commands with copy buttons via the Clipboard API that say so when it is unavailable, the official docs as a plain link with `target=_blank rel="noopener noreferrer"`, never fetched), 파일을 찾지 못했다면 (demo file paths), 파일 크기와 보안 안내; a 시연 파일로 바로 해 보기 button (`GET /api/demo-session`, then the normal upload). The client pre-check `MAX_UPLOAD_BYTES` equals `max_upload_bytes` (a unit test compares them). Vanilla JS, `fetch` with `credentials: "same-origin"`, every model or session string set via `textContent`. Keyboard operable, visible focus, WCAG AA contrast, `prefers-reduced-motion` respected, system Korean fonts only (no external requests).

## 9. Tests and commands

- Unit and integration tests: `.venv/bin/python -m unittest discover -s tests -t . -v` (stdlib `unittest`; async with `IsolatedAsyncioTestCase`; HTTP with `starlette.testclient.TestClient`).
- Lint: `.venv/bin/ruff check .`; JS syntax: `node --check agency_loop/static/app.js`; UI smoke: `node tests/ui/ui_smoke.mjs http://127.0.0.1:<port>` against a mock-mode server; opt-in course MCP test: `AGENCY_TEST_COURSE_MCP=1 .venv/bin/python -m unittest tests.test_course_mcp_optin`.
- Fixtures: `fixtures/sessions/*.jsonl`, generated by `.venv/bin/python fixtures/make_fixtures.py`. All synthetic.
- No test may call a real external service. Live adapters are tested against local mock HTTP servers (`tests/mock_servers.py`) and a fake MCP server (`tests/fake_mcp_server.py`).
