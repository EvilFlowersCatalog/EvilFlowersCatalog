---
draft: true
date: 2026-11-09
authors:
  - jdubec
categories:
  - Feature
tags:
  - assistant
  - llm
  - ollama
  - mcp
  - search
---

# IP-022: Assistant Intelligence on a Self-Hosted CPU Model

<!-- more -->

## Status

**Draft.** Phases 1–3 are implemented on the working tree together with the client half of #104, with the
recommended answers to Q1–Q7 applied (pending confirmation), and tested locally (scripted model, no real LLM).
Not yet run against `elvira-assistant` in the cluster. The deployment steps are posted on knowledge#11.
Phases 4–5 need the cluster.

Issues: EvilFlowersCatalog#104 (model switch, client), EvilFlowersCatalog#105 (tools and prompts),
EvilFlowersCatalog/knowledge#11 (deployment).

## Problem Statement

The assistant (`apps/assistant`, IP-015) moves from Ollama Cloud (`gemma4:31b-cloud`) to a self-hosted
model. The `knowledge` repository (commit `499f951`, 2026-11-09) deployed **Ollama 0.40.15 on the
control node** (6 CPU threads, no GPU). It serves **`elvira-assistant`**: Qwen3.6-35B-A3B Q4_K_M, a
35B/3B-active MoE, plus a Modelfile with `num_thread 6` and `num_ctx 32768`. Only `evilflowers-dev` may call it,
and it has no HTTPRoute yet. That changes two things.

1. **Latency profile.** On CPU, generation is usable (~10 tok/s), but prompt evaluation runs at only tens to
   about 100 tok/s. The agent loop re-sends the conversation every tool round. Every token of the system prompt,
   the tool list and the earlier tool results is paid on the first round, and again whenever the prompt cache misses.
2. **A reasoning model.** Qwen3.6 thinks before it answers unless the request says `"think": false`. On CPU
   that is minutes of hidden tokens per round.

At the same time the assistant is weaker than it needs to be:

- It offers only `search_entries`, `get_entry` and `displayBooks`. That limit was set to protect small models
  from long tool lists. But the MCP description of `search_entries` alone is 3.4 K characters, so the list was
  long anyway.
- It cannot resolve subjects. STU files every entry under one MDT term, and the model has to guess
  `category_term`.
- It cannot answer "what do I have borrowed", "something like this" or questions about publication contents.
  The catalog already serves these over REST: shelf and loans in MCP, similar entries, and passage search across
  the library in `apps/extraction/discovery.py`.
- Tool results are replayed in full: catalog ids, URIs, counters and 600-character summaries, on every later
  round and turn.
- A rejected argument ("Unknown argument(s): category") reached the model only as "The tool failed.", so it
  could not correct itself.
- The system prompt was written without reviewing real conversations.
- Nothing measures whether a model change makes the assistant better or worse.

## Proposed Solution

### 1. Client for a local reasoning model (#104)

`OllamaClient.payload()` builds the request from deployment settings only, so two requests with the same messages
render the same prompt:

| Setting | Default | Sent as |
|---|---|---|
| `EVILFLOWERS_ASSISTANT_OLLAMA_THINK` | `0` | `"think": false` (`1` → `true`, empty → not sent) |
| `EVILFLOWERS_ASSISTANT_OLLAMA_NUM_CTX` | `32768` | `options.num_ctx` (0 → not sent). Equal to the server's `OLLAMA_CONTEXT_LENGTH` and the Modelfile, so the model is never reloaded |
| `EVILFLOWERS_ASSISTANT_OLLAMA_KEEP_ALIVE` | `-1` | `keep_alive` (number or duration; empty → not sent) |
| `EVILFLOWERS_ASSISTANT_OLLAMA_TEMPERATURE` | empty | `options.temperature` |
| `EVILFLOWERS_ASSISTANT_OLLAMA_HOST` | empty | `Host` header, for Compose → internal Gateway (`llm.elvira.internal`), as `EVILFLOWERS_EMBEDDING_HOST` does |
| `EVILFLOWERS_ASSISTANT_OLLAMA_CONNECT_TIMEOUT` | `10` | `requests` timeout becomes `(connect, read)`; the read timeout (300) must cover prompt evaluation |

Blank values reproduce the pre-#104 request exactly, so Ollama Cloud keeps working and rollback is a change of
environment variables. `message.thinking` is never shown or stored; the span counts it
(`evilflowers.assistant.thinking_chunks`) so a model that thinks anyway shows up in Tempo. Ollama's own
`load/prompt_eval/eval` durations go on the `chat` span, because on CPU prompt evaluation is the number to watch.

Callers that expect only JSON pass a JSON Schema as Ollama's `format` on the rounds that offer no tools. These
are semantic highlight selection (every call), the agentic highlight retry after prose or after running out of
rounds, and explain's answer after `MAX_TOOL_ROUNDS`. The answer is then constrained to the schema instead of
being parsed out of prose. A request that offers tools never sets `format`, so the model stays free to call a
tool. That means the **common** exit of explain and agentic highlight is still unconstrained: the model answers
without a tool call inside the tool-offering loop. Phase 4 must check that path.

### 2. Tool surface: more tools in the same budget

`apps/assistant/tools.py` advertises its own short description and slim schema for each tool. The MCP
registry's descriptions are unchanged, because they serve desktop clients. The handlers still accept the full
MCP argument set.

| Tool | Source | Offered to |
|---|---|---|
| `search_entries` | MCP (hybrid search, IP-021) | everyone. Advertised arguments: query, author, category_term, language_codes, published_from/to, lcp_states, order_by, limit (default 6, max 15) |
| `get_entry` | MCP | everyone |
| `list_categories` | MCP | everyone; returns the whole vocabulary (every page, up to 4 × 50) as term + label |
| `get_my_shelf`, `list_my_loans` | MCP (`ToolAccess.USER`) | authenticated sessions only |
| `find_similar` | new, wraps `discovery.similar_entries` (entry vector, else PDF vector) | everyone |
| `search_library_text` | new, wraps `discovery.search_passages` (paragraphs across the library) | everyone; LCP text withheld, page only. Not named `search_passages`, because a bound chat also offers `search_paragraphs` (the reader's own copy) |
| `displayBooks` | local UI directive | everyone |

`ALLOWED_TOOLS` stays the security boundary. The test `test_every_allowed_registry_tool_is_read_only` asserts
`writes == False` for each one. The two local tools are read-only by construction and go through
`EntryFilter` access control. `find_similar` refuses an entry the reader cannot see. `search_library_text`
inherits `_may_read_text`, so text from Readium LCP publications never reaches the model (IP-018 Q5).

The tool list is fixed for a session: it depends only on whether the reader is authenticated. It is part of the
cached prompt prefix and never changes inside a conversation.

Measured first-round prefix (system prompt + tool JSON, ≈ characters ÷ 4; Qwen's chat template adds a few
hundred tokens on top):

| | Before | After |
|---|---|---|
| Plain chat, signed in | 3 tools, ~1.45 K tokens | 8 tools, ~1.42 K tokens |
| Chat bound to a reader's copy | 9 tools, ~2.2 K tokens | 14 tools, ~2.17 K tokens |

A test keeps the plain-chat prefix under 2 K tokens (#104 target: ≤ 3 K).

### 3. Slim tool results

`tools.run()` returns `(payload, text)`. The full payload is stored in `ChatMessage.tool_result` for clients
and audit. The model sees `text`, a slim projection built per tool:

- Search hits keep id, title, authors, year, language, categories, a summary cut at 280 characters, rating,
  availability (state, slots, next available, queue position) and `on_my_shelf`. They drop `catalog_id`,
  `resource_uri`, publisher, page count and popularity. One sample hit shrank by more than half.
- An empty search carries a `note` that says what to try next: other or English words, fewer filters, or
  `search_library_text`.
- `ToolError` messages from the MCP layer (validation, not found, unknown arguments) go to the model as
  `{"error": …}`, so it can correct the call.
- Null, empty-string and empty-list arguments are dropped before the handler runs. Small models fill optional
  arguments with them.

### 4. Turn loop

- **`status` SSE frames.** One goes out before each model round (`{"state": "thinking", "round": n}`) and one
  before each tool (`{"state": "tool", "tool": name}`), so the UI can show activity during a 20–40 s prompt
  evaluation. The portal's stream reader (`useSendChatMessage.tsx`) switches on the event name and ignores
  unknown events. Displaying these frames is a portal follow-up.
- **Repeated-call guard.** A second call to the same tool with the same (cleaned) arguments within a turn is not
  run. The model gets "you already called this… change the arguments or answer". `MAX_TOOL_ROUNDS` stays as the
  runaway guard.

### 5. System prompt

`SYSTEM_PROMPT` is restructured into fixed sections: **role**, **tools** (when each one applies, search again
in English once, don't repeat a call), **grounding** (only what tool results say, ids verbatim), **answer**
(at most 5 cards; why they fit and whether they can be borrowed, no title lists), **language** (Slovak never
Czech) and **limits**. It contains nothing per request. The reader's UI language is still pinned by the last
message of every request (`language_instruction`), after the history, so it never changes the cached prefix.

### 6. Evaluation harness

`python manage.py assistant_eval run --username <reader>` runs each case in
`apps/assistant/evaluation_cases.json` as a real turn in a throw-away chat. `--model`, `--endpoint`,
`--api-key` and `--think` override the settings for the run. Each case is scored on:

- the tools called (`tools_any`, `tools_all`, `tools_none`);
- whether cards were shown;
- the answer's language (a Slovak vs Czech heuristic);
- model rounds (more than 5 counts as a loop);
- errors;
- time to first token and total time (p50/p95 in the summary).

`--output` writes the full JSON report. `assistant_eval export --limit 50` prints recent real user questions
from dev chats as case skeletons, so the set can be grounded in real conversations (#105 §1). The 20 seed
cases cover topic search (SK/EN), availability, author, subjects, year, loans, shelf, passages, similar
publications, empty results, off-topic questions, "borrow it for me", an English question in the Slovak UI and a
Czech question in the Slovak UI.

## Implementation Plan

### Phase 1: Client (#104) ✅ on the working tree

- [x] `think`, `options.num_ctx`, `keep_alive`, `temperature`, `Host`, `(connect, read)` timeout; settings in
  `settings/base.py` and `.env.example`
- [x] `format` (JSON Schema) on the no-tools rounds of the JSON-only callers: `semantic.py`, the highlight
  retry, explain after `MAX_TOOL_ROUNDS`
- [x] Thinking chunks and Ollama durations on the `chat` span
- [x] Payload, header and stream tests (`tests/test_ollama_payload.py`)

### Phase 2: Tools, results, loop, prompt ✅ on the working tree

- [x] Assistant-side descriptions and slim schemas; `list_categories`, `get_my_shelf`, `list_my_loans`
- [x] `discovery.similar_entries` and `discovery.search_passages` extracted from the views; `find_similar`,
  `search_library_text` tools; `list_categories` walks every page (STU's 59 terms exceed one MCP page of 50)
- [x] Slim results, `ToolError` passthrough, argument cleaning
- [x] `status` frames, repeated-call guard
- [x] Restructured `SYSTEM_PROMPT`
- [x] Tests: `tests/test_tool_surface.py` (offered tools, read-only boundary, prefix budget, slim results,
  repeat guard) and `tests/test_turn_tools.py` (whole turn against the DB with a scripted model, access checks)

### Phase 3: Evaluation harness ✅ on the working tree

- [x] `apps/assistant/evaluation.py`, `manage.py assistant_eval run|export`, 20 seed cases

### Phase 4: Dev deployment and measurement (knowledge#11)

Ansible steps are posted on knowledge#11 (comment of 2026-11-09, "Update after `499f951`").

- [ ] Catalog env in `roles/evilflowers_k8s` for `evilflowers-dev`:
  - `EVILFLOWERS_ASSISTANT_OLLAMA_ENDPOINT=http://ollama.shared.svc.cluster.local/api/chat`
  - `EVILFLOWERS_ASSISTANT_OLLAMA_MODEL=elvira-assistant`
  - `EVILFLOWERS_ASSISTANT_OLLAMA_API_KEY=` (empty)
  - `EVILFLOWERS_ASSISTANT_OLLAMA_THINK=0`
  - `EVILFLOWERS_ASSISTANT_OLLAMA_NUM_CTX=32768`
  - `EVILFLOWERS_ASSISTANT_OLLAMA_KEEP_ALIVE=-1`
  - `EVILFLOWERS_ASSISTANT_OLLAMA_TIMEOUT` per Q4
- [ ] Warm-up loads `elvira-assistant` (Cookbooks/Shared-Inference)
- [ ] `assistant_eval export` from dev, then add `expect` to about 30 real questions → set of about 50
- [ ] `assistant_eval run` against `gemma4:31b-cloud` (baseline) and `elvira-assistant`; record both summaries
  here
- [ ] Run explain, highlight (agentic and semantic) once each against `elvira-assistant`; check the JSON
  contracts hold with `think: false`
- [ ] Check Tempo: `gen_ai.usage.input_tokens` of the first `chat` span per turn,
  `evilflowers.ollama.prompt_eval_duration_s`, `thinking_chunks` absent
- [ ] Q1: does `prompt_eval_count` include prefix-cache hits? Compare round 2's input tokens with round 1's
- [ ] Give the evaluation account an `AssistantUserPolicy` with a raised `daily_token_limit` before running

### Phase 5: Prompt iteration and prod

- [ ] Iterate the prompt and descriptions against the failures the evaluation finds; one or two worked
  examples only if they measurably help (they cost prefix tokens)
- [ ] Portal: show `status` frames ("searching the library…"), EvilFlowersCatalog/elvira-portal#36
- [ ] Prod (Compose): HTTPRoute `llm.elvira.internal` + `EVILFLOWERS_ASSISTANT_OLLAMA_HOST` once Known-Issues 7
  is proven; only after the evaluation passes

## Technical Details

### Files

| File | Change |
|---|---|
| `apps/assistant/llm.py` | `payload()`, new settings, `format`, timings, thinking count |
| `apps/assistant/tools.py` | `SPECIFICATIONS`, `specifications(authenticated)`, `run()`, `slim()`, local tools |
| `apps/assistant/turn.py` | session tool list, `status` frames, `_arguments`, `_repeated`, `tools.run` |
| `apps/assistant/services.py` | `SYSTEM_PROMPT` |
| `apps/assistant/explain.py`, `highlight.py`, `semantic.py` | answer schemas for `format` |
| `apps/extraction/discovery.py` | `similar_entries()`, `search_passages()` extracted; views unchanged in behaviour |
| `apps/assistant/evaluation.py`, `management/commands/assistant_eval.py`, `evaluation_cases.json` | harness |
| `evil_flowers_catalog/settings/base.py`, `.env.example` | settings |

### Prompt cache

Ollama reuses the KV cache for the longest common prefix of the rendered prompt in a slot. Everything before the
history (system prompt, the opening note of a bound chat, tool list) must be byte-identical across rounds and
turns. The guarantees:

- The tool list depends only on authentication.
- `payload()` has nothing per request.
- The language instruction is the last message.
- Tool results are stored once, as `text`, and replayed verbatim.

With `OLLAMA_NUM_PARALLEL=2`, two concurrent chats each hold a slot. A third evicts one, and that chat's next
round evaluates its whole prompt again.

## Alternatives Considered

### Stage tools (resolution tools only after an empty search)

This keeps the first round smallest, but it changes the tool list inside a conversation, which breaks the prompt
cache on exactly the round that follows a disappointing search. Slimming the descriptions made room for every
tool in the old budget, so staging is not needed.

### Edit the MCP descriptions instead of overriding them

One source of truth, but MCP clients (Claude Desktop and others) benefit from the long descriptions, and the
assistant needs a different trade-off. The override map is small, and a test checks that every advertised
argument is accepted by the handler.

### A second, smaller model for routing

For example a 4B model to pick the tool and the 35B model to answer. That doubles memory on a node with none to
spare, and Qwen3.6-35B-A3B already reads only 3B parameters per token. Not now.

### Thinking on for hard questions only

This needs a classifier for "hard", and every thinking round costs minutes on CPU. Turning it on is a single
environment variable if a GPU arrives.

## Trade-offs and Risks

- **Slim results hide fields.** A question about the publisher, page count or popularity needs `get_entry`.
  The prompt does not mention this. The evaluation shows whether the model figures it out.
- **More tools, more wrong choices.** Mitigated by short, disjoint descriptions and the "Tools" section of the
  prompt. To be measured.
- **The Slovak vs Czech heuristic is crude.** It catches the typical drift (ř/ů/ě, "jsou", "který") but can
  misjudge short answers; under 12 characters it returns no verdict.
- **The evaluation spends quota and model time.** Each case is a real turn counted against the user's daily
  limit. Use a dedicated evaluation account with a raised `AssistantUserPolicy`.
- **`format` with Qwen3.6 on Ollama 0.40** is unverified on the real model (Phase 4).
- **Timeout.** On 6 threads, a bound chat's first round (system prompt + tools + a 6,000-character page) could
  take more than 100 s before the first byte. See Q4.

## Success Criteria

- [ ] First-round input tokens of a plain chat ≤ 3 K on dev (Tempo)
- [ ] Evaluation on `elvira-assistant`: ≥ 90 % of cases pass, 0 Czech answers, 0 errors
- [ ] Median time to first token ≤ 30 s for a plain chat on control
- [ ] Explain and highlight return valid JSON on `elvira-assistant` in every Phase 4 run
- [ ] No tool outside `ALLOWED_TOOLS` + `LOCAL_TOOLS` is ever called (enforced by code, checked by test)

## Future Considerations

- Expose the search fallback level (`strict`/`relaxed`/`fuzzy`) and `did_you_mean` from `EntryFilter.search`
  in the `search_entries` result, so the model can tell an approximate match from an exact one.
- Vision: Qwen3.6 accepts images. It could caption figures in image-service (`CAPTION_OLLAMA_ENDPOINT`) without
  a second model (#104, separate decision).
- Conversation summaries instead of trimming cold chats (`EVILFLOWERS_ASSISTANT_TRIM_MESSAGES`).
- MTP variant `qwen3.6:35b-a3b-mtp-q4_K_M`, if Ollama uses it for speculative decoding on CPU.

## References

- EvilFlowersCatalog#104, #105; EvilFlowersCatalog/knowledge#11; knowledge commit `499f951` (Ollama on control)
- IP-012 (direction), IP-014 (MCP registry), IP-015 (assistant), IP-018 (viewer, passages), IP-019 (gen_ai
  telemetry), IP-021 (hybrid search, entry vectors)
- Ollama `/api/chat`: `think`, `format`, `keep_alive`, `options`

## Discussion

None yet.

## Review Questions

**Status**: ⏳ Awaiting Answers. Each question is implemented with its recommended option, pending confirmation.
**Review Date**: 2026-11-09
**Reviewer**: Claude AI

The following questions must be answered before implementation:

---

### Q1: Daily token quota vs. multi-round turns 🔴 Critical

**Issue**: `usage()` sums `tokens_used`, which `turn.py` records per round as `prompt_eval_count + eval_count`.
A 3-round turn with a ~2 K-token prompt books about 6 K tokens. With
`EVILFLOWERS_ASSISTANT_DAILY_LIMIT_TOKENS=50000`, a reader runs out after roughly 8 questions. Whether Ollama's
`prompt_eval_count` excludes prefix-cache hits on 0.40 is unverified. If it excludes them, the real charge is
lower but varies with cache luck.

**Context**: Under Ollama Cloud tokens were money. Self-hosted, the scarce resource is CPU time, not tokens.

**Question**: What should the daily quota limit?

**Options**:
- [ ] **A**: Keep counting tokens, raise the default to 200 K, and measure `prompt_eval_count` cache behaviour
  in Phase 4 (recommended)
- [ ] **B**: Count only `eval_count` (output tokens) toward the quota
- [ ] **C**: Drop the token limit and rely on the message limit (100/day)

**Answer**:
```
```

**Resolution**:
```
Implemented as A (pending confirmation): EVILFLOWERS_ASSISTANT_DAILY_LIMIT_TOKENS default 50 000 → 200 000
(settings/base.py, .env.example). Phase 4 checks whether Ollama 0.40 counts prefix-cache hits in
prompt_eval_count: compare gen_ai.usage.input_tokens of round 2 with round 1 of the same turn (≈ equal =
cached tokens are counted; much smaller = only new tokens are).
```

---

### Q2: Which reader tools, and is `list_authors` / `list_feeds` needed? ⚠️ Medium

**Issue**: Implemented: `list_categories`, `get_my_shelf`, `list_my_loans`. Left out: `list_authors`,
because `search_entries.author` already matches name substrings and accents, and `list_feeds` / `get_feed`,
because curated feeds are not something readers ask for by name.

**Context**: Each tool costs ~50–150 prefix tokens, and the more tools there are, the more often the model picks
the wrong one.

**Question**: Is the chosen set right?

**Options**:
- [ ] **A**: Keep it as implemented; add a tool only when the evaluation shows a need (recommended)
- [ ] **B**: Add `list_feeds` (e.g. "what's in the new acquisitions feed")
- [ ] **C**: Add `list_authors` as well

**Answer**:
```
```

**Resolution**:
```
Implemented as A (pending confirmation): search_entries, get_entry, list_categories, get_my_shelf,
list_my_loans, find_similar, search_library_text, displayBooks. A tool is added only when an evaluation
case fails for lack of it.
```

---

### Q3: Library-wide passage text to the model ⚠️ Medium

**Issue**: `search_library_text` returns up to 600 characters of each matching paragraph from open-access
publications the reader may see, across the whole library. LCP publications and IP-blocked ones return a page
without text. That is the same rule as `GET /api/v1/entries/passages`.

**Context**: IP-018 Q5 forbade giving LCP text to the model. Open-access text was already allowed in bound chats
for the reader's own copy, and the passages endpoint already shows it to the same reader in the portal.

**Question**: Is open-access passage text across the library acceptable in chat?

**Options**:
- [ ] **A**: Yes, same rule as the REST endpoint (implemented, recommended)
- [ ] **B**: Only titles and pages, never text
- [ ] **C**: Only for publications in catalogs marked public

**Answer**:
```
```

**Resolution**:
```
Implemented as A (pending confirmation): search_library_text uses discovery.search_passages, i.e. the
REST endpoint's rule (_may_read_text): text for open-access entries the reader may see, page only for
Readium LCP and IP-blocked entries.
```

---

### Q4: Read timeout on control ⚠️ Medium

**Issue**: The read timeout (300 s) applies between bytes. On 6 threads at roughly 50 tok/s prompt evaluation,
a bound chat's first round (~2.2 K prefix + a 6,000-character page + history) takes about 60–90 s before the
first byte. A cold cache after a slot eviction can double that.

**Context**: knowledge#11 assumed 16–32 vCPU on app-01; the deployment gave Ollama 6 threads on control.

**Question**: How should the timeout be set?

**Options**:
- [ ] **A**: Keep 300 s and decide from the Phase 4 p95 of `prompt_eval_duration_s` (recommended)
- [ ] **B**: Lower to 120 s so a stuck request frees the gevent worker sooner
- [ ] **C**: Raise to 600 s

**Answer**:
```
```

**Resolution**:
```
Implemented as A (pending confirmation): read timeout stays 300 s, connect timeout 10 s
(EVILFLOWERS_ASSISTANT_OLLAMA_CONNECT_TIMEOUT). Revisit from the Phase 4 p95 of
evilflowers.ollama.prompt_eval_duration_s on bound chats.
```

---

### Q5: `num_ctx` sent by the client vs. the Modelfile ℹ️ Low

**Issue**: `elvira-assistant`'s Modelfile already sets `num_ctx 32768`, and the server sets
`OLLAMA_CONTEXT_LENGTH=32768`. The client sends `options.num_ctx=32768` too. If the three ever disagree,
Ollama reloads the model (~30–60 s on CPU) on the first request with the other value.

**Question**: Should the client send `num_ctx` at all?

**Options**:
- [ ] **A**: Send it (default 32768), so the value is visible in the catalog's config; keep the three equal
  in the knowledge defaults (implemented)
- [ ] **B**: Default `0` (not sent) and let the Modelfile decide (recommended if the Modelfile stays the single
  source)

**Answer**:
```
```

**Resolution**:
```
Implemented as A (pending confirmation): the client sends options.num_ctx=32768 by default. The
knowledge#11 comment derives the catalog value from inference_llm.num_ctx, so the three values have
one source. Set EVILFLOWERS_ASSISTANT_OLLAMA_NUM_CTX=0 to switch to B.
```

---

### Q6: Seed cases vs. real questions ℹ️ Low

**Issue**: The 20 seed cases were written without access to dev chats (Mimir records 0 assistant calls in the
last 7 days on dev). `assistant_eval export` exists to replace them with real questions.

**Question**: Should the shipped `evaluation_cases.json` stay synthetic?

**Options**:
- [ ] **A**: Keep the seeds in the repository and add anonymised real questions after review (recommended)
- [ ] **B**: Keep real questions outside the repository (they may contain personal data)

**Answer**:
```
```

**Resolution**:
```
Implemented as A (pending confirmation): apps/assistant/evaluation_cases.json holds the 20 synthetic
seeds. `assistant_eval export` prints to stdout only, never into the repository; real questions are added
to the file only after a human removes personal details.
```

---

### Q7: Status frames and the portal ℹ️ Low

**Issue**: The new `status` events are ignored by the portal's stream reader today. Showing them is a portal
change.

**Question**: Open a portal issue now, or wait for the Phase 4 latency numbers?

**Options**:
- [ ] **A**: Open it now; a 20–40 s silent spinner is likely (recommended)
- [ ] **B**: Wait for measurements

**Answer**:
```
```

**Resolution**:
```
A: the catalog emits `status` frames; the portal side is EvilFlowersCatalog/elvira-portal#36 (opened
2026-11-09): onStatus handler in useSendChatMessage.tsx, a one-line indicator per state and tool.
```

---

## Changelog

| Date | Author | Changes |
|------|--------|---------|
| 2026-11-09 | jdubec | Initial draft from #104/#105 and the knowledge Ollama deployment (`elvira-assistant` on control); Phases 1–3 implemented and tested locally |
| 2026-11-09 | jdubec | Added Review Questions section |
| 2026-11-09 | jdubec | Read-back fixes: `search_passages` tool renamed `search_library_text` (clash with `search_paragraphs`); `list_categories` pages through the whole vocabulary; `format` scope stated precisely (no-tools rounds only) |
| 2026-11-09 | jdubec | Recommended answers to Q1–Q7 implemented (pending confirmation): daily token default 200 000 (Q1); Phase 4 steps posted on knowledge#11; Phase 4 checklist gains the Q1 cache check and the evaluation account's quota |
| 2026-11-09 | jdubec | Q7: portal issue opened (EvilFlowersCatalog/elvira-portal#36) |
