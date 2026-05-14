import json
import logging
import os

from django.core.management.base import BaseCommand
from django.test import RequestFactory

from apps.api.views.dataverse import DataversePrepublishIngest


class Command(BaseCommand):
    help = "Run the Dataverse prepublish handler locally and log everything for debugging."

    def add_arguments(self, parser):
        parser.add_argument("--dataset-id", required=True, help="Dataverse dataset numeric ID")
        parser.add_argument("--global-id", required=True, help="Dataverse global ID (persistentId)")
        parser.add_argument("--title", required=False, default="", help="Title to pass to EF")
        parser.add_argument("--invocation-id", required=False, help="Workflow invocation ID (used by resume callback)")
        parser.add_argument(
            "--secret",
            required=False,
            default=os.getenv("DATAVERSE_WORKFLOW_SECRET", ""),
            help="Prepublish workflow secret (must match DATAVERSE_WORKFLOW_SECRET)",
        )

    def handle(self, *args, **options):
        # Make sure our logs are visible at DEBUG
        logging.getLogger("apps.api.views.dataverse").setLevel(logging.DEBUG)
        logging.getLogger("requests").setLevel(logging.INFO)
        logging.getLogger("urllib3").setLevel(logging.INFO)

        payload = {
            "dataset_id": options["dataset_id"],
            "global_id": options["global_id"],
            "title": options.get("title") or "",
            "secret": options.get("secret") or "",
        }

        invocation_id = options.get("invocation_id")
        if invocation_id:
            payload["invocation_id"] = invocation_id

        self.stdout.write("Running Dataverse prepublish handler (in-process)...\n")
        self.stdout.write(f"payload={json.dumps(payload)}\n")

        request = RequestFactory().post(
            "/api/v1/dataverse-prepublish/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        view = DataversePrepublishIngest.as_view()

        try:
            response = view(request, invocation_id=invocation_id)
            self.stdout.write("\n=== Response ===\n")
            self.stdout.write(f"status: {response.status_code}\n")
            self.stdout.write(f"content: {response.content.decode('utf-8')}\n")
        except Exception as e:
            self.stderr.write("\n=== Exception ===\n")
            self.stderr.write(f"{type(e).__name__}: {e}\n")
            raise
