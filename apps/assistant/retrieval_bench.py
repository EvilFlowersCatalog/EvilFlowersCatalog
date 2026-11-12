"""Passage-retrieval benchmark on a gold question set (IP-024 §5, #118).

A gold case is a question and where its answer is:

    {"id": "sk-3nf", "language": "sk", "question": "Čo je tretia normálna forma?",
     "gold": [{"entry_id": "…", "pages": [112, 113]}]}

`run()` asks `discovery.search_passages` — the same function behind `search_library_text` and
`GET /api/v1/entries/passages` — and scores the ranking without any language model:

- **publication level**: is a gold publication among the first k publications? Recall@5, nDCG@10, MRR;
- **page level**: is a (gold publication, gold page) among the first k passages? Recall@5, nDCG@10, MRR.

Relevance is binary. Recall@k is the share of a case's gold items found in the top k, averaged over cases.

Known bias: gold pages chosen by people who found them with the current retriever favour it, so the set
cannot certify absolute recall; it compares retrieval changes against each other.
"""

import math
import statistics
from dataclasses import asdict, dataclass, field
from typing import Iterable, List, Optional, Sequence, Set, Tuple

#: Publications and passages per publication asked for: enough for nDCG@10 at both levels.
LIMIT = 10
PER_ENTRY = 3

#: Pass bars, written down before the first run on the gold set (IP-024 §5). Changing them after seeing results
#: defeats their purpose: record any change, and why, in the proposal's changelog.
PREREGISTERED = {"entry_recall_5": 0.70, "entry_ndcg_10": 0.55, "page_recall_5": 0.40, "page_ndcg_10": 0.30}


def recall_at(ranked: Sequence, relevant: Set, k: int) -> float:
    if not relevant:
        return 0.0
    return len(set(ranked[:k]) & relevant) / len(relevant)


def ndcg_at(ranked: Sequence, relevant: Set, k: int) -> float:
    dcg = sum(1 / math.log2(rank + 2) for rank, item in enumerate(ranked[:k]) if item in relevant)
    ideal = sum(1 / math.log2(rank + 2) for rank in range(min(len(relevant), k)))
    return dcg / ideal if ideal else 0.0


def reciprocal_rank(ranked: Sequence, relevant: Set) -> float:
    for rank, item in enumerate(ranked, start=1):
        if item in relevant:
            return 1 / rank
    return 0.0


@dataclass
class CaseResult:
    id: str
    question: str
    degraded: bool = False
    #: The case names gold pages; page-level metrics average over these cases only.
    has_pages: bool = False
    entries: List[str] = field(default_factory=list)
    passages: List[Tuple[str, int]] = field(default_factory=list)
    entry_recall_5: float = 0.0
    entry_ndcg_10: float = 0.0
    entry_rr: float = 0.0
    page_recall_5: float = 0.0
    page_ndcg_10: float = 0.0
    page_rr: float = 0.0


def gold_sets(case: dict) -> Tuple[Set[str], Set[Tuple[str, int]]]:
    entries, pages = set(), set()
    for item in case.get("gold") or []:
        entry_id = str(item["entry_id"])
        entries.add(entry_id)
        for page in item.get("pages") or []:
            pages.add((entry_id, int(page)))
    return entries, pages


def score_case(case: dict, entries: List[str], passages: List[Tuple[str, int]]) -> CaseResult:
    gold_entries, gold_pages = gold_sets(case)
    result = CaseResult(id=str(case.get("id")), question=case["question"], entries=entries, passages=passages)
    result.entry_recall_5 = recall_at(entries, gold_entries, 5)
    result.entry_ndcg_10 = ndcg_at(entries, gold_entries, 10)
    result.entry_rr = reciprocal_rank(entries, gold_entries)
    if gold_pages:
        result.has_pages = True
        result.page_recall_5 = recall_at(passages, gold_pages, 5)
        result.page_ndcg_10 = ndcg_at(passages, gold_pages, 10)
        result.page_rr = reciprocal_rank(passages, gold_pages)
    return result


def rankings(result) -> Tuple[List[str], List[Tuple[str, int]]]:
    """Publications in rank order, and (publication, page) of every passage by descending score."""
    entries = [str(item.entry_id) for item in result.items]
    scored = [(passage.score, str(item.entry_id), passage.page) for item in result.items for passage in item.passages]
    passages = []
    for _score, entry_id, page in sorted(scored, key=lambda row: -row[0]):
        if (entry_id, page) not in passages:
            passages.append((entry_id, page))
    return entries, passages


def run_case(request, case: dict) -> CaseResult:
    from apps.extraction.discovery import search_passages

    result = search_passages(request, case["question"], limit=LIMIT, per_entry=PER_ENTRY)
    # Without query embeddings only word matches remain: not the retriever being measured.
    if result.degraded:
        return CaseResult(id=str(case.get("id")), question=case["question"], degraded=True)
    entries, passages = rankings(result)
    return score_case(case, entries, passages)


def summary(results: Iterable[CaseResult]) -> dict:
    results = list(results)
    scored = [result for result in results if not result.degraded]
    paged = [result for result in scored if result.has_pages]

    def mean(values) -> Optional[float]:
        values = list(values)
        return round(statistics.fmean(values), 3) if values else None

    return {
        "cases": len(results),
        "degraded": len(results) - len(scored),
        "entry_recall_5": mean(r.entry_recall_5 for r in scored),
        "entry_ndcg_10": mean(r.entry_ndcg_10 for r in scored),
        "entry_mrr": mean(r.entry_rr for r in scored),
        "page_recall_5": mean(r.page_recall_5 for r in paged),
        "page_ndcg_10": mean(r.page_ndcg_10 for r in paged),
        "page_mrr": mean(r.page_rr for r in paged),
    }


def report(results: List[CaseResult], thresholds: Optional[dict] = None) -> dict:
    """The summary, each case, and — with pre-registered `thresholds` — which bars were met."""
    summary_ = summary(results)
    out = {"summary": summary_, "cases": [asdict(result) for result in results]}
    if thresholds:
        out["thresholds"] = {
            metric: {"bar": bar, "value": summary_.get(metric), "met": (summary_.get(metric) or 0) >= bar}
            for metric, bar in thresholds.items()
        }
    return out


def as_eval_cases(gold: List[dict]) -> List[dict]:
    """Gold questions as `assistant_eval run` cases: the answer must cite a gold publication."""
    cases = []
    for case in gold:
        entries, _pages = gold_sets(case)
        cases.append(
            {
                "id": f"gold-{case.get('id')}",
                "language": case.get("language"),
                "question": case["question"],
                "expect": {"tools_any": ["search_library_text"], "cites": True, "cites_any_entry": sorted(entries)},
            }
        )
    return cases
