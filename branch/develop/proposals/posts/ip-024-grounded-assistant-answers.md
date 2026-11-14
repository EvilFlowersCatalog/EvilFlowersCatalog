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
  - citations
  - evaluation
  - prompts
---

# IP-024: Grounded Assistant Answers — Evidence Labels, Citation Checks and Robust Structured Output

<!-- more -->

## Status

**Superseded by [IP-030](ip-030-assistant-evidence-first.md) (2026-11-13).** The evidence ledger, structured output,
citation scoring, prompt registry and evaluation tooling stay and are extended there. Chapter summaries become section
summaries, and bound chats move to labels (IP-030 Q1). Open items and Q1–Q7 moved to IP-030 "Carried over".

**Draft (until 2026-11-13).** Phases 1–5 are implemented on the working tree and tested locally with a scripted model (no real
LLM). Prompts are byte-identical to before (Phase 2 test). What remains needs people or the cluster: a run on
`elvira-assistant` (IP-022 Phase 4), ~200 human-labelled support pairs (Phase 3), ~50 gold questions with
pages (Phase 4), and a first batch of chapter summaries (Phase 5) — all tracked in EvilFlowersCatalog#121;
deployment steps in EvilFlowersCatalog/knowledge#24. This builds on IP-022: it does not add a tool
to the prompt prefix (#114: ≤ 1.5K tokens, enforced by test).

Issues: EvilFlowersCatalog#115 (tracking, Phase 1), #116 (Phase 2, prompt registry), #117 (Phase 3, support
check), #118 (Phase 4, gold set), #119 (Phase 5, chapter summaries), EvilFlowersCatalog/elvira-portal#37
(Phase 1b, citation rendering).

## Problem Statement

IP-022 gave the assistant library-wide passage search (`search_library_text`). The answer that follows has no
reliable way to say *which* passage a sentence comes from.

- **Citations are prose.** The system prompt says "Cite a passage as the publication's title and page". The
  reader gets text such as "(Mechanika tekutín, s. 47)". The portal cannot link it, and nothing checks that the
  title and page were in a tool result rather than made up.
- **Bound chats have `[[page:N]]`, library-wide chats have nothing.** A chat about the reader's own copy can cite
  a page, because there is only one publication. Across the library, a page number means nothing without the
  publication.
- **No measure of grounding.** `assistant_eval` checks tools, cards, language, loops and latency. It cannot tell
  an answer that cites what it read from one that invents a page.
- **Three copies of JSON parsing.** `explain.py`, `highlight.py` and `semantic.py` each extract the model's JSON
  with the same greedy `\{.*\}` regular expression. None of them handles a fenced block, a trailing comma or
  typographic quotes. Explain's common exit — the model answers without a tool call inside the tool-offering
  loop, so Ollama's `format` is not applied (IP-022 §1) — falls back to showing raw prose when the JSON is
  malformed.

### Where the ideas come from

We reviewed `Sibyx/monad-knowledge` (sha `e8860d38`). It is a research toolkit on Anthropic models, not a
library assistant. It has no local-model or Slovak support, so everything below about Qwen on CPU is our
adaptation, not something that repository shows. The parts that transfer:

| monad-knowledge | What it does | Adopted here as |
|---|---|---|
| Evidence packs (`domain/evidence.py`, `render_pack`) | Retrieved passages carry labels `[E1]`, `[E2]`; the model cites labels; labels are resolved after generation and unknown labels are reported, never silently dropped | §1 Evidence labels (Phase 1) |
| Support check (`pipeline/support.py`) | One verifier call per (cited sentence, passage) pair: supported / partial / not supported, with a score calibrated on labelled pairs before it gates anything | §4, Phase 3 (issue), shadow mode only |
| `run_json` + `parse_json_response` (`agents/query.py`) | Structured output: fenced block → outermost object → repair, then one "JSON only" retry | §2 Structured output (Phase 1) |
| Pre-registered evaluation studies, retrieval benchmark | Thresholds fixed before measuring; nDCG/Recall on a curated gold set; a change is better when the benchmark says so, "never argued" | §3 Citation scoring (Phase 1), §5 gold set (Phase 4, issue) |
| Vault-hosted prompts (`agents/prompts.py`) | Prompts as versioned files with a code fallback, shared preamble, live enumerations | §6 Prompt registry (Phase 2, issue) |
| Chapter summaries (`agents/chapter_summary.py`) | 150–200 words per chapter, for embedding and routing | §7 (Phase 5, issue; overlaps IP-012) |
| Prompt rules | "Say why an error is costly", "an empty result is scoped to the query", "never fill a value that is not stated", "honest scope instead of hedging" | Candidates for IP-022 Phase 5 prompt iteration, applied only with an evaluation run before and after |

## Proposed Solution

### 1. Evidence labels on library passages

`search_library_text` labels every passage whose text the model may read: `E1`, `E2`, … A passage whose text is
withheld (Readium LCP, IP-018 Q5) gets no label, because the model has nothing to cite from it.

```json
{"items": [{"entry_id": "…", "title": "Fluid Mechanics",
            "passages": [{"label": "E3", "page": 47, "section": "2.4 Bernoulli", "text": "…"}]}],
 "searched_publications": 412}
```

**Labels are unique per chat, not per turn.** Tool results are replayed verbatim on every later round and turn.
If every search restarted at `E1`, the model would see two different `E1` passages in its history. The next label
continues after the highest one already issued in the chat. The same paragraph found again keeps its first label.

The **ledger** (`apps/assistant/evidence.py`) is rebuilt from the chat's own rows at the start of each turn: every
labelling tool row stores `evidence: {label: source}` in its `tool_result` (the full payload, never shown to the
model). There is no new table, for the same reason IP-015 D2 has no counter table.

**The citation contract**, in the system prompt's *Answer* section, replacing "Cite a passage as the publication's
title and page":

> A passage from `search_library_text` has a label such as E1: cite it as [E1] right after the sentence it
> supports. A sentence no passage supports gets no label; never make a label up.

**Resolution.** When the turn closes, the final answer's labels are matched against the ledger:

- `ChatMessage.citations` (new JSON column) stores the resolved list, in order of first appearance:
  `[{"label": "E3", "entry_id": "…", "title": "…", "page": 47, "section": "…"}]`.
- A `citations` SSE frame `{"citations": [...], "unknown": ["E9"]}` follows the `message` frame.
- **Unknown labels are reported, not removed.** The text has already been streamed chunk by chunk, so rewriting it
  on the server would make the stored answer differ from what the reader saw. An unknown label is logged and
  counted by the evaluation as a failure.
- The text keeps `[E3]` as written. The portal renders a known label as a link to the publication at that page,
  and leaves an unknown label as plain text (portal issue, Phase 1b).

Accepted forms: `[E3]`, `[E3, E5]`, `[E3; E5]`, `[E3][E5]`. Case is normalised.

### 2. One structured-output helper

`apps/assistant/structured.py`:

- `parse_object(content, valid=None)` tries, in order:
    1. a fenced block (`` ```json … ``` ``);
    2. the first JSON object that decodes (`json.JSONDecoder.raw_decode` from each `{`);
    3. staged repairs, each followed by the two steps again: first trailing commas removed, then also
       typographic quotes made straight. The order matters: Slovak quotes with „…“ inside string values, and
       straightening those would break an object whose only fault was a trailing comma.

  `valid(data)` rejects an object of the wrong shape, so the next strategy gets a chance.
- `RETRY_INSTRUCTION`: "Your previous answer was not the JSON object asked for. Answer again with only that JSON
  object: no explanation, no code fences."

Callers:

| Caller | Before | After |
|---|---|---|
| `explain` | Malformed JSON on a normal exit → the raw prose became `simple` | One retry without tools and with `format: ANSWER_SCHEMA`; prose only if the retry fails too |
| `highlight` | Already retried with `QUOTES_SCHEMA` | Uses `parse_object`; retry message unchanged |
| `semantic` | Always `format` | Uses `parse_object` |

The retry costs one model round on CPU, only when the first answer is unusable — which is exactly when the
reader would otherwise see a broken explanation.

### 3. Citation scoring in `assistant_eval`

The harness reads the new `citations` frame. Each outcome records `citations` (count) and `unknown_labels`.

- **Always:** any unknown label fails the case ("cited unknown labels E9"). This is the grounding check that needs
  no gold data: a label the model could not have seen is an invention.
- **`expect.cites: true`:** at least one resolved citation (for passage questions).
- **`expect.cites_entries`:** every listed publication id is cited (for gold cases, Phase 4).
- Summary: `cases_with_citations`, `unknown_labels`.

### 4. Support check (Phase 3, issue — shadow mode only)

After a cited answer, ask the model one small question per (sentence, cited passage) pair: does the passage state
or directly imply what the sentence attributes to it? The answer is a verdict (`supported`, `partial`,
`unsupported`) and a score from 0 to 100, constrained with `format`. The rubric is monad-knowledge's, adapted:

> Judge only the part of the sentence that the citation attributes to this passage. Use the sentences around it
> only to resolve references, never as evidence. A detail the passage is silent on is acceptable unless it
> changes what is claimed: a different number, a wider scope, an added condition or cause, or more certainty.

On CPU this is one extra prompt evaluation per citation (each pair ≈ 400 tokens), so it **never runs inside the
live turn**. As implemented (`apps/assistant/support.py`, prompt `prompts/support.md`) it runs only from
`assistant_eval` (Q5 option A):

- `split_sentences` knows Slovak, Czech and English abbreviations, initials and decimals, and attaches a label
  written after the full stop ("… falls. [E2]") to the sentence before it.
- `run --verify` adds a verdict per citation to each case and counts verdicts in the summary.
- `pairs` exports (sentence, context, passage) pairs from real answers with `supported: null` for a person to label.
- `calibrate --labels` scores labelled pairs and prints precision and recall per threshold on a **tuning half**
  fixed by pair id. Choose the threshold there and write it into this proposal; `calibrate --threshold N` then
  reports the **held-out half** — the numbers that decide whether the check may gate anything.

It gates nothing until then. A failed verifier call, or an answer that is not the schema, is `unverified`, not a
verdict; a pair the model could not score counts as not passing the gate.

### 5. Gold set and retrieval benchmark (Phase 4)

About 50 questions with known answer locations (publication and page range), written by a librarian or taken from
course material, in `apps/assistant/retrieval_gold.json` (format in `retrieval_bench.py`; the file ships empty).

- `assistant_eval retrieval` runs `discovery.search_passages` — the function behind `search_library_text` and
  `GET /api/v1/entries/passages` — with no model, and reports Recall@5, nDCG@10 and MRR at publication and page
  level (binary relevance; page metrics over cases that name pages).
- `assistant_eval run --gold` runs the same questions as full turns; each must call `search_library_text` and
  cite at least one gold publication (`expect.cites_any_entry`).

**Pre-registered bars** (`retrieval_bench.PREREGISTERED`), written before the first run:

| Metric | Bar |
|---|---|
| Publication Recall@5 | ≥ 0.70 |
| Publication nDCG@10 | ≥ 0.55 |
| Page Recall@5 | ≥ 0.40 |
| Page nDCG@10 | ≥ 0.30 |

They are a starting point chosen without data (see Q6). A retrieval or prompt change is accepted when the benchmark
says it is better. Known bias: gold pages chosen by people who found them with the current retriever favour it, so
the set compares changes; it cannot certify absolute recall.

### 6. Prompt registry (Phase 2)

The prompts were Python string constants in four modules, and the Slovak-not-Czech rule was written four times.
They are now Markdown templates in `apps/assistant/prompts/` (`chat`, `explain`, `highlight`, `semantic`,
`support`, `chapter_summary`, shared segment `_citations`), rendered by `prompts.render()`:

- `{{ name }}` takes values from the code that defines them (tool names, `MAX_QUOTES`); an unknown placeholder
  raises, so a renamed tool cannot leave a stale name in a prompt.
- `{{> segment }}` includes `_segment.md` (the citation contract).
- **Default and override.** The bundled templates are the default. `EVILFLOWERS_ASSISTANT_PROMPTS_DIR` may point
  at a directory that replaces any of them file by file; a file missing there falls back to the bundled one.
  Rendered at import, so a change takes effect on restart.
- `prompts.language_name()` and `prompts.not_czech()` replace the four copies of the language rule.
- **Fingerprint.** `prompts.fingerprint(system prompt, tool list)` — 12 hex characters — is stored on every
  assistant `ChatMessage` (`prompt_fingerprint`, migration `0007`) and in the `assistant_eval` summary
  (`prompt_fingerprints`; one per tool list, i.e. signed in or not).
- `tests/test_prompts.py` pins the sha256 of each rendered system prompt to the value of the old constant: the
  switch changed no byte. A deliberate prompt change updates the hash together with the evaluation that justified it.

Admin editing of prompts in the database is deliberately out of scope: every edit changes the cached prompt
prefix and should go through the evaluation.

### 7. Chapter summaries (Phase 5)

`ChapterSummary` (migration `0008`): one row per top-level chapter of `Entry.table_of_contents` (its children
when the outline has a single title heading), with the reader's start and end page, an 80–120 word summary
(`prompts/chapter_summary.md`: key concepts and terms, main arguments, methods; English), and a `digest` of the
chapter text and the prompt. A chapter is summarised again only when the digest changes; chapters that leave the
outline lose their row. The text is read from the stored PDF in the reader's page numbering, cut at 8,000
characters; chapters under 300 characters (title pages) are skipped.

Two deviations from the issue (#119), both for the CPU budget:

- **No `get_outline` tool.** A tool costs every chat its description in the prefix, and the signed-in plain chat
  is already at ~1,457 of #114's 1,500 tokens. Instead `get_entry` returns, once a publication has summaries,
  `chapters` (title, page, end page, summary; at most 20) and a `chapters_note` ("generated by the assistant
  from the text and unchecked; cite pages, not them") in place of the bare outline.
- **80–120 words**, not 150–200: monad-knowledge's length serves embedding; here every word is replayed to the
  model.

Nothing runs automatically: a chapter costs about a minute on CPU. `manage.py summarise_chapters --entry …`,
`--limit N` (most popular first) or `--queue` (Celery task `summarise_chapters`). Publications lent through
Readium LCP are never summarised, and an existing summary is deleted when one is licensed: a summary would carry
protected text to every reader who can see the record. This overlaps IP-012 summarisation; whichever lands first
owns it.

## Implementation Plan

### Phase 1: Evidence labels, structured output, citation scoring ✅ on the working tree

- [x] `apps/assistant/evidence.py`: `Ledger.for_chat`, `label_passages`, `cite`
- [x] `tools.run(..., ledger=None)`: labels `search_library_text` passages; `evidence` kept out of the model's view
- [x] `turn.py`: ledger per turn, `citations` stored on the closing answer, `citations` SSE frame
- [x] `ChatMessage.citations` + migration `0006_chatmessage_citations`; serializer field
- [x] System prompt: citation contract replaces "title and page"
- [x] `apps/assistant/structured.py`; `explain`, `highlight`, `semantic` use it; explain retries once
- [x] `evaluation.py`: `citations`, `unknown_labels`, `expect.cites`, `expect.cites_entries`; seed cases updated
- [x] Tests: `tests/test_evidence.py`, `tests/test_structured.py`, turn and evaluation tests

### Phase 1b: Portal

- [ ] elvira-portal: render `[E#]` with a known citation as a link to the publication and page (viewer for
  open-access entries, the publication page otherwise); handle the `citations` frame and the `citations` field
  when a chat is read back

### Phase 2: Prompt registry (#116) ✅ on the working tree

- [x] Template files, bundled default with an override directory, shared segment, `{{ }}` values from code
- [x] One language rule (`prompts.language_name`, `prompts.not_czech`)
- [x] Prompt fingerprint on `ChatMessage` (`0007`) and in the `assistant_eval` summary
- [x] Test: rendered prompts equal the old constants byte for byte (sha256 pinned)

### Phase 3: Support check (#117) ✅ tooling on the working tree

- [x] Sentence splitter, verifier prompt and schema, `unverified` on failure
- [x] `assistant_eval run --verify`: verdicts per citation in the report
- [x] `assistant_eval pairs` / `calibrate`: export for labelling, tuning and held-out halves
- [ ] Label ≥ 200 pairs from STU publications; choose the threshold on the tuning half; record it here; run the
  held-out half (needs a person and `elvira-assistant`)

### Phase 4: Gold set and retrieval benchmark (#118) ✅ tooling on the working tree

- [x] `assistant_eval retrieval`: Recall@5, nDCG@10, MRR at publication and page level, no model
- [x] `assistant_eval run --gold`, `expect.cites_any_entry`
- [x] Pre-registered bars in this proposal and in `retrieval_bench.PREREGISTERED`
- [ ] ~50 gold questions with publication and page ranges in `retrieval_gold.json` (needs a librarian)

### Phase 5: Chapter summaries (#119) ✅ on the working tree

- [x] `ChapterSummary` (`0008`), `chapters.py`, `summarise_chapters` command and Celery task, LCP excluded
- [x] Summaries on `get_entry` instead of a `get_outline` tool (prefix budget, #114)
- [ ] First batch on dev (`summarise_chapters --limit 20 --queue`) and a look at the summaries

### #114: First-round prefix ≤ 1.5K tokens ✅ enforced by test

- [x] `test_first_round_prefix_stays_within_the_cpu_budget`: ≤ 1,500 tokens (chars ÷ 4) signed in and anonymous;
  measured 1,358 anonymous, 1,457 signed in, 2,209 in a chat bound to a reader's copy
- [x] `PromptPrefixTests`: system prompt and tool list byte-identical across rounds, turns, chats and UI
  languages; one fingerprint for all of them
- [x] `apps.assistant.tools` fills the MCP registry itself (a management command or Celery worker never loaded
  the URLconf that used to do it, so `assistant_eval` raised `UnknownTool`)
- [ ] Measure the real first-round input tokens in Tempo on dev and re-run `tools/ollama-bench` (cluster)

## Technical Details

### Files (Phase 1)

| File | Change |
|---|---|
| `apps/assistant/evidence.py` | new: ledger, labelling, citation parsing |
| `apps/assistant/structured.py` | new: JSON object extraction and retry text |
| `apps/assistant/tools.py` | `run(..., ledger)`, labels on `search_library_text`, slim view without `evidence` |
| `apps/assistant/turn.py` | ledger per turn, `_close()` for the three exits, `citations` frame |
| `apps/assistant/services.py` | citation contract in `SYSTEM_PROMPT` |
| `apps/assistant/models/chat_message.py`, `migrations/0006_chatmessage_citations.py` | `citations` |
| `apps/assistant/serializers.py` | `citations` on messages |
| `apps/assistant/explain.py`, `highlight.py`, `semantic.py` | `structured.parse_object`; explain retry |
| `apps/assistant/evaluation.py`, `evaluation_cases.json` | citation scoring |

### Files (Phases 2–5)

| File | Change |
|---|---|
| `apps/assistant/prompts/` | new: templates, loader, fingerprint, language rule |
| `apps/assistant/support.py` | new: sentence splitter, pairs, verifier, calibration table |
| `apps/assistant/retrieval_bench.py`, `retrieval_gold.json` | new: metrics, gold format, pre-registered bars |
| `apps/assistant/chapters.py`, `models/chapter_summary.py`, `migrations/0008_chapter_summaries.py` | new |
| `apps/assistant/management/commands/summarise_chapters.py`, `tasks.py` | batch and Celery entry points |
| `apps/assistant/evidence.py` | `labels_in`; ledger keeps passage text for the verifier |
| `apps/assistant/evaluation.py`, `management/commands/assistant_eval.py` | `--verify`, `--gold`, `retrieval`, `pairs`, `calibrate`, fingerprints |
| `apps/assistant/models/chat_message.py`, `migrations/0007_chatmessage_prompt_fingerprint.py` | `prompt_fingerprint` |
| `apps/assistant/tools.py` | chapter summaries on `get_entry`; registry import |
| `evil_flowers_catalog/settings/base.py` | `EVILFLOWERS_ASSISTANT_PROMPTS_DIR` |

### Prompt cache

Nothing per request enters the prefix. The citation contract is part of the fixed system prompt. Labels are part
of tool results, which are stored once and replayed verbatim, so they never change after they are issued.

## Alternatives Considered

### Ask the model for `[[entry:ID:page:N]]` markers

Mirrors `[[page:N]]` in bound chats and needs no resolution step. But it asks a 3B-active model to copy a
36-character UUID per citation, which small models get wrong, and a wrong UUID cannot be told from a hidden
publication. A two-character label is easy to copy, and an invented one is detectable.

### Rewrite labels into markers on the server

Cleaner text for clients that do not know labels. But chunks are streamed before the answer is complete, so the
stored text would differ from the text the reader saw. The portal would also have to handle both forms.

### Labels per turn

Simpler counter, but labels collide in the replayed history (see §1).

### Run the support check inside the turn

Blocks the answer for one prompt evaluation per citation on CPU, and gates on an uncalibrated threshold.

## Trade-offs and Risks

- **The model may not cite.** Qwen3.6 with thinking off may ignore the contract. The evaluation measures it
  (`cases_with_citations`); the fallback is the answer without links, as today.
- **A label proves the passage was retrieved, not that it supports the sentence.** Only Phase 3 checks support.
  Until then a citation link means "the assistant read this", which is still more than today.
- **Explain retry latency.** One more round when the first answer is not JSON. Measure the retry rate in
  Phase 4 of IP-022; if it is high, the explain prompt needs fixing, not the parser.
- **Ledger cost.** Each turn reads the chat's tool rows that carry `evidence`. Chats are short; trimmed cold chats
  still keep their labels, because the ledger reads all rows, not the replayed tail.

## Success Criteria

- [ ] Evaluation on `elvira-assistant`: 0 unknown labels across the set
- [ ] ≥ 80 % of passage cases (`expect.cites`) answer with at least one resolved citation
- [ ] Explain returns valid JSON in every Phase 4 run, with the retry rate recorded
- [ ] Portal: a citation opens the publication at the cited page

## Future Considerations

- **Research mode.** A longer, explicitly requested turn ("prepare a reading list on X"): plan, several searches,
  a brief with one concrete claim per recommended publication, each cited. monad-knowledge's long-running agents
  show what such a mode needs: a call budget ("past N calls you are surveying; pick a direction"), a fixed number
  of replanning rounds, and an honest statement of what the library does not cover. On CPU this is a background
  job with a notification, not a chat turn.
- **Rhetorical roles in the viewer.** Label sentences of a selected passage as definition, claim, evidence,
  method or limitation, cached by paragraph digest and marked "generated, unchecked".
- **Anti-slop lint for answers.** A small rule list (filler openers, repeated card contents) checked in
  `assistant_eval`, so prompt edits that make answers wordier show up.

## References

- IP-012, IP-013, IP-018 (passages, `[[page:N]]`), IP-021 (paragraph vectors), IP-022 (tools, harness)
- EvilFlowersCatalog#104, #105
- `Sibyx/monad-knowledge` @ `e8860d38`: `monad_knowledge/domain/evidence.py`, `pipeline/support.py`,
  `agents/query.py`, `agents/prompts.py`, `agents/chapter_summary.py`,
  `notebooks/studies/2026-10-15-support-gate/README.md`
- Min et al., *FActScore* (2023); Song et al., *VeriScore* (2024)

## Discussion

None yet.

## Review Questions

**Status**: ⏳ Awaiting Answers
**Review Date**: 2026-11-09
**Reviewer**: Claude AI

The following questions must be answered before implementation is considered final. Phase 1 applies the
recommended option of each.

---

### Q1: How the portal shows a citation ⚠️ Medium

**Issue**: §1 keeps `[E3]` in the answer text and sends the resolved list separately. Clients that do not know
labels (today's portal, the MCP-free API consumers) show the raw `[E3]`.

**Context**: The portal change is Phase 1b. Until it ships, readers of dev see `[E3]` as plain text in answers
that cite library passages.

**Question**: What should a citation look like and open?

**Options**:
- [ ] **A**: A small chip with the label's number; hover shows title and page; click opens the viewer at the page
  for an open-access entry and the publication page otherwise (recommended)
- [ ] **B**: Replace the label with "(Title, p. N)" text, linked
- [ ] **C**: Hide labels in the text and list sources under the answer

**Answer**:
```
```

**Resolution**:
```
```

---

### Q2: Unknown labels in the stored answer ⚠️ Medium

**Issue**: §1 reports an unknown label (`[E9]` that was never issued) but leaves it in the text, because the text
was already streamed.

**Context**: An invented label is a small hallucination in front of the reader. Removing it on the server makes
the stored answer differ from what was shown; removing it in the portal keeps them consistent.

**Question**: Where is an unknown label removed, if anywhere?

**Options**:
- [ ] **A**: Nowhere on the server; the portal renders it as plain text, the evaluation fails it (implemented,
  recommended)
- [ ] **B**: Server strips it from the stored text after the turn; the live view and a reload differ
- [ ] **C**: Portal hides it

**Answer**:
```
```

**Resolution**:
```
```

---

### Q3: Explain retry ⚠️ Medium

**Issue**: §2 adds one constrained retry when explain's answer has no usable JSON, which costs one more model
round on CPU (tens of seconds).

**Context**: Without it, the reader sees the model's raw prose as the explanation, which is often still useful
text — but sometimes half a JSON object.

**Question**: Retry, or show the prose?

**Options**:
- [ ] **A**: Retry once (implemented, recommended); record the retry rate in IP-022 Phase 4
- [ ] **B**: Show the prose, never retry
- [ ] **C**: Retry only when the prose contains a `{` (a broken object rather than plain prose)

**Answer**:
```
```

**Resolution**:
```
```

---

### Q4: Labels in chats bound to a reader's copy ℹ️ Low

**Issue**: A bound chat cites its own publication as `[[page:N]]` (IP-018), and may also call
`search_library_text`, which now returns `[E#]` labels. One answer can contain both forms.

**Context**: Unifying on labels would make `search_paragraphs` results labelled too, but `[[page:N]]` already
works in the viewer and needs no resolution.

**Question**: Keep both forms?

**Options**:
- [ ] **A**: Keep both; `[[page:N]]` for the reader's copy, `[E#]` for the library (implemented, recommended)
- [ ] **B**: Label `search_paragraphs` passages too and resolve `[E#]` of the bound publication to the viewer page

**Answer**:
```
```

**Resolution**:
```
```

---

### Q5: Where the support check runs (Phase 3) ⚠️ Medium

**Issue**: §4 proposes a Celery task after each cited turn. With `OLLAMA_NUM_PARALLEL=2` on control, verifier
calls compete with live chats for the two slots and evict their prompt caches.

**Context**: A verdict nobody sees has no value to the reader until the threshold is calibrated; in evaluation it
has value immediately.

**Question**: Run it on live traffic before calibration?

**Options**:
- [ ] **A**: Evaluation only until calibrated; then decide (recommended)
- [ ] **B**: Background on live traffic in shadow mode, off-peak queue only
- [ ] **C**: Not at all; rely on the label check

**Answer**:
```
```

**Resolution**:
```
```

---

### Q6: Pre-registered retrieval bars ⚠️ Medium

**Issue**: §5 fixes pass bars (publication Recall@5 ≥ 0.70, nDCG@10 ≥ 0.55; page Recall@5 ≥ 0.40, nDCG@10 ≥ 0.30)
before any gold question exists. They were chosen by Claude without data from this library.

**Context**: Pre-registration only works if the bars are set before the first run. Set too high, every change
"fails"; too low, the benchmark accepts anything.

**Question**: Keep these bars, or set them once the gold set exists but before running it?

**Options**:
- [ ] **A**: Keep them as written; revise only with a changelog entry giving the reason (recommended)
- [ ] **B**: Librarian and maintainer set the bars after writing the gold questions, before the first run
- [ ] **C**: No bars; compare runs against each other only

**Answer**:
```
```

**Resolution**:
```
```

---

### Q7: Chapter summaries on `get_entry` instead of a `get_outline` tool ℹ️ Low

**Issue**: #119 asked for a `get_outline` tool. §7 puts the summaries on `get_entry` instead, because a new tool
would push the signed-in first-round prefix past #114's 1.5K tokens (1,457 now).

**Context**: `get_entry` results grow by up to 20 chapters × ~150 tokens once a publication has summaries, but only
in chats that ask about that publication, and as a tool result rather than in every chat's prefix.

**Question**: Accept the deviation?

**Options**:
- [ ] **A**: Yes (implemented, recommended)
- [ ] **B**: A `get_outline` tool, and shorten another tool's description to make room

**Answer**:
```
```

**Resolution**:
```
```

---

## Changelog

| Date | Author | Changes |
|------|--------|---------|
| 2026-11-09 | jdubec | Initial draft from a review of monad-knowledge; Phase 1 implemented and tested with a scripted model |
| 2026-11-09 | jdubec | Added Review Questions section |
| 2026-11-09 | jdubec | Linked issues #115–#119 and elvira-portal#37 |
| 2026-11-09 | jdubec | §2: staged JSON repair (commas before quotes) so Slovak „…“ quotation marks survive |
| 2026-11-09 | jdubec | Phases 2–5 implemented (#116–#119) and #114 prefix budget enforced; §4–§7 rewritten to match; pre-registered retrieval bars; Q6, Q7 added |
| 2026-11-13 | jdubec | Superseded by IP-030; Q1 resolved by the portal implementation (elvira-portal#37, branch `dev`); open items and Q2–Q7 carried into IP-030 |
