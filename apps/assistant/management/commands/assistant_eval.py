"""Run the assistant evaluation set against the configured model (#104, IP-022).

    # the cases shipped with the code, as a reader of the dev catalog
    python manage.py assistant_eval run --username reader@stuba.sk

    # another model or endpoint, without touching the environment
    python manage.py assistant_eval run --username reader@stuba.sk \\
        --model elvira-assistant --endpoint http://ollama.shared.svc.cluster.local/api/chat \\
        --output /tmp/qwen.json

    # recent real questions from dev chats as case skeletons (add `expect` by hand)
    python manage.py assistant_eval export --limit 50 > cases.json

    # check every citation of every answer against its passage (IP-024 §4, #117)
    python manage.py assistant_eval run --username reader@stuba.sk --verify

    # gold questions (#118): retrieval only, no model; then as full turns that must cite a gold publication
    python manage.py assistant_eval retrieval --username reader@stuba.sk --gold gold.json --output r.json
    python manage.py assistant_eval run --username reader@stuba.sk --gold gold.json

    # support-check calibration (#117): export pairs, label `supported` by hand, score them
    python manage.py assistant_eval pairs --limit 300 > pairs.json
    python manage.py assistant_eval calibrate --labels pairs.json                # tuning half: choose a threshold
    python manage.py assistant_eval calibrate --labels pairs.json --threshold 60 # held-out half: the verdict

Every `run` case is a real turn: it calls the model, runs the tools against
this catalog's database and counts against the user's daily quota. The
throw-away chats are deleted afterwards.
"""

import json
import sys
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.test.utils import override_settings

from apps.assistant import evaluation, retrieval_bench

DEFAULT_CASES = Path(__file__).resolve().parents[2] / "evaluation_cases.json"
DEFAULT_GOLD = Path(__file__).resolve().parents[2] / "retrieval_gold.json"


def _read_json(path: str, what: str):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CommandError(f"Cannot read {what}: {error}")


class Command(BaseCommand):
    help = "Evaluate the assistant against a model: tools chosen, cards, language, loops and latency."

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="action", required=True)

        run = sub.add_parser("run", help="Run the cases and print a report.")
        run.add_argument("--username", required=True, help="User the turns run as (scopes the catalog).")
        run.add_argument("--cases", default=str(DEFAULT_CASES), help="JSON file with a list of cases.")
        run.add_argument("--only", nargs="*", default=None, help="Run only these case ids.")
        run.add_argument("--model", help="Override EVILFLOWERS_ASSISTANT_OLLAMA_MODEL.")
        run.add_argument("--endpoint", help="Override EVILFLOWERS_ASSISTANT_OLLAMA_ENDPOINT.")
        run.add_argument("--api-key", help="Override EVILFLOWERS_ASSISTANT_OLLAMA_API_KEY ('' for none).")
        run.add_argument("--think", help="Override EVILFLOWERS_ASSISTANT_OLLAMA_THINK (0, 1 or '').")
        run.add_argument("--output", help="Write the full JSON report here.")
        run.add_argument("--verify", action="store_true", help="Check each citation against its passage (#117).")
        run.add_argument("--gold", help="Run these gold questions (#118) instead of --cases.")

        export = sub.add_parser("export", help="Print recent real questions as case skeletons.")
        export.add_argument("--limit", type=int, default=50)

        retrieval = sub.add_parser("retrieval", help="Score passage retrieval on gold questions; no model.")
        retrieval.add_argument("--username", required=True, help="User the searches run as (scopes the catalog).")
        retrieval.add_argument("--gold", default=str(DEFAULT_GOLD), help="JSON file with gold questions.")
        retrieval.add_argument("--output", help="Write the full JSON report here.")

        pairs = sub.add_parser("pairs", help="Print (sentence, passage) pairs from real answers to label.")
        pairs.add_argument("--limit", type=int, default=300)

        calibrate = sub.add_parser("calibrate", help="Score labelled pairs with the support check.")
        calibrate.add_argument("--labels", required=True, help="Pairs from `pairs` with `supported` filled in.")
        calibrate.add_argument("--threshold", type=int, help="Pre-registered threshold: score the held-out half.")
        calibrate.add_argument("--model", help="Override EVILFLOWERS_ASSISTANT_OLLAMA_MODEL.")
        calibrate.add_argument("--endpoint", help="Override EVILFLOWERS_ASSISTANT_OLLAMA_ENDPOINT.")

    def _print(self, data):
        json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")

    def _user(self, username):
        user_model = get_user_model()
        try:
            return user_model.objects.get(**{user_model.USERNAME_FIELD: username})
        except user_model.DoesNotExist:
            raise CommandError(f"No user {username!r}.")

    def handle(self, *args, **options):
        action = options["action"]
        if action == "export":
            return self._print(evaluation.export_questions(options["limit"]))
        if action == "pairs":
            return self._print(evaluation.export_pairs(options["limit"]))
        if action == "retrieval":
            return self._retrieval(options)

        overrides = {
            name: options[key]
            for key, name in (
                ("model", "EVILFLOWERS_ASSISTANT_OLLAMA_MODEL"),
                ("endpoint", "EVILFLOWERS_ASSISTANT_OLLAMA_ENDPOINT"),
                ("api_key", "EVILFLOWERS_ASSISTANT_OLLAMA_API_KEY"),
                ("think", "EVILFLOWERS_ASSISTANT_OLLAMA_THINK"),
            )
            if options.get(key) is not None
        }
        with override_settings(**overrides):
            if action == "calibrate":
                result = evaluation.calibrate(_read_json(options["labels"], "labels"), options.get("threshold"))
                return self._print(result)
            self._run(options)

    def _retrieval(self, options):
        gold = _read_json(options["gold"], "gold questions")
        if not gold:
            raise CommandError(
                f"{options['gold']} holds no gold questions yet (format: apps/assistant/retrieval_bench.py)."
            )
        request = evaluation.evaluation_request("get")
        request.user = self._user(options["username"])

        results = []
        for case in gold:
            result = retrieval_bench.run_case(request, case)
            results.append(result)
            if result.degraded:
                self.stdout.write(self.style.ERROR(f"DEGRADED {result.id} (no query embedding)"))
            else:
                self.stdout.write(
                    f"{result.id:<28} entry R@5 {result.entry_recall_5:.2f} nDCG@10 {result.entry_ndcg_10:.2f}"
                    f"  page R@5 {result.page_recall_5:.2f} nDCG@10 {result.page_ndcg_10:.2f}"
                )
        report = retrieval_bench.report(results, retrieval_bench.PREREGISTERED)
        self._print({"summary": report["summary"], "thresholds": report["thresholds"]})
        if options.get("output"):
            Path(options["output"]).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            self.stderr.write(f"Report written to {options['output']}")

    def _run(self, options):
        user = self._user(options["username"])

        if options.get("gold"):
            cases = retrieval_bench.as_eval_cases(_read_json(options["gold"], "gold questions"))
        else:
            cases = _read_json(options["cases"], "cases")
        if options["only"]:
            cases = [case for case in cases if case.get("id") in set(options["only"])]

        snapshot = {
            "model": settings.EVILFLOWERS_ASSISTANT_OLLAMA_MODEL,
            "endpoint": settings.EVILFLOWERS_ASSISTANT_OLLAMA_ENDPOINT,
            "think": settings.EVILFLOWERS_ASSISTANT_OLLAMA_THINK,
            "num_ctx": settings.EVILFLOWERS_ASSISTANT_OLLAMA_NUM_CTX,
            "prompts_dir": settings.EVILFLOWERS_ASSISTANT_PROMPTS_DIR or "bundled",
        }
        self.stderr.write(f"{len(cases)} cases against {snapshot['model']} at {snapshot['endpoint']}")

        outcomes = []
        for case in cases:
            outcome = evaluation.run_case(user, case, verify=options.get("verify", False))
            outcomes.append(outcome)
            mark = self.style.SUCCESS("PASS") if outcome.passed else self.style.ERROR("FAIL")
            timing = f"first {outcome.first_token_s}s total {outcome.total_s}s"
            self.stdout.write(f"{mark} {outcome.id:<28} {timing:<28} tools={','.join(outcome.tools) or '-'}")
            for failure in outcome.failures:
                self.stdout.write(f"     - {failure}")
            for item in outcome.support:
                self.stdout.write(f"     {item['label']} {item['verdict']} ({item['score']}): {item['sentence'][:80]}")

        report = evaluation.report(outcomes, snapshot)
        self.stdout.write(json.dumps(report["summary"], indent=2))
        if options.get("output"):
            Path(options["output"]).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            self.stderr.write(f"Report written to {options['output']}")
