"""Run the assistant evaluation set against the configured model (#104, IP-022).

    # the cases shipped with the code, as a reader of the dev catalog
    python manage.py assistant_eval run --username reader@stuba.sk

    # another model or endpoint, without touching the environment
    python manage.py assistant_eval run --username reader@stuba.sk \\
        --model elvira-assistant --endpoint http://ollama.shared.svc.cluster.local/api/chat \\
        --output /tmp/qwen.json

    # recent real questions from dev chats as case skeletons (add `expect` by hand)
    python manage.py assistant_eval export --limit 50 > cases.json

Every case is a real turn: it calls the model, runs the tools against this
catalog's database and counts against the user's daily quota. The throw-away
chats are deleted afterwards.
"""

import json
import sys
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.test.utils import override_settings

from apps.assistant import evaluation

DEFAULT_CASES = Path(__file__).resolve().parents[2] / "evaluation_cases.json"


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

        export = sub.add_parser("export", help="Print recent real questions as case skeletons.")
        export.add_argument("--limit", type=int, default=50)

    def handle(self, *args, **options):
        if options["action"] == "export":
            json.dump(evaluation.export_questions(options["limit"]), sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
            return

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
            self._run(options)

    def _run(self, options):
        user_model = get_user_model()
        try:
            user = user_model.objects.get(**{user_model.USERNAME_FIELD: options["username"]})
        except user_model.DoesNotExist:
            raise CommandError(f"No user {options['username']!r}.")

        try:
            cases = json.loads(Path(options["cases"]).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CommandError(f"Cannot read cases: {error}")
        if options["only"]:
            cases = [case for case in cases if case.get("id") in set(options["only"])]

        snapshot = {
            "model": settings.EVILFLOWERS_ASSISTANT_OLLAMA_MODEL,
            "endpoint": settings.EVILFLOWERS_ASSISTANT_OLLAMA_ENDPOINT,
            "think": settings.EVILFLOWERS_ASSISTANT_OLLAMA_THINK,
            "num_ctx": settings.EVILFLOWERS_ASSISTANT_OLLAMA_NUM_CTX,
        }
        self.stderr.write(f"{len(cases)} cases against {snapshot['model']} at {snapshot['endpoint']}")

        outcomes = []
        for case in cases:
            outcome = evaluation.run_case(user, case)
            outcomes.append(outcome)
            mark = self.style.SUCCESS("PASS") if outcome.passed else self.style.ERROR("FAIL")
            timing = f"first {outcome.first_token_s}s total {outcome.total_s}s"
            self.stdout.write(f"{mark} {outcome.id:<28} {timing:<28} tools={','.join(outcome.tools) or '-'}")
            for failure in outcome.failures:
                self.stdout.write(f"     - {failure}")

        report = evaluation.report(outcomes, snapshot)
        self.stdout.write(json.dumps(report["summary"], indent=2))
        if options.get("output"):
            Path(options["output"]).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            self.stderr.write(f"Report written to {options['output']}")
