import json
import mimetypes
import os
import tempfile
from http import HTTPStatus
from uuid import UUID, uuid4

import requests
from django.core.files import File
from django.http import HttpResponse
from django.utils.translation import gettext as _
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator

from apps import openapi
from apps.core.errors import ProblemDetailException
from apps.core.models import Entry, Acquisition, Catalog, User


@method_decorator(csrf_exempt, name="dispatch")
class DataverseSync(View):
    @openapi.metadata(description="Dataverse workflow post-publish sync", tags=["Dataverse"])
    def post(self, request):
        try:
            raw = request.body.decode("utf-8") if request.body else "{}"
            payload = json.loads(raw)
        except Exception as e:
            raise ProblemDetailException(_("Invalid JSON payload"), status=HTTPStatus.BAD_REQUEST, previous=e)

        dataset_id = payload.get("dataset_id")
        global_id = payload.get("global_id")
        title = payload.get("title")

        if dataset_id is None or global_id is None:
            raise ProblemDetailException(
                _("Missing required fields: dataset_id and global_id"),
                status=HTTPStatus.BAD_REQUEST,
            )

        print(
            f"[DATAVERSE-SYNC] received publish event: dataset_id={dataset_id} global_id={global_id} title={title}",
            flush=True,
        )

        return HttpResponse("OK", status=HTTPStatus.OK, content_type="text/plain; charset=utf-8")


@method_decorator(csrf_exempt, name="dispatch")
class DataversePrepublishIngest(View):
    @openapi.metadata(description="Dataverse workflow pre-publish log file URLs", tags=["Dataverse"])
    def post(self, request):
        try:
            raw = request.body.decode("utf-8") if request.body else "{}"
            payload = json.loads(raw)
        except Exception as e:
            raise ProblemDetailException(_("Invalid JSON payload"), status=HTTPStatus.BAD_REQUEST, previous=e)

        secret = payload.get("secret")
        dataset_id = payload.get("dataset_id")
        global_id = payload.get("global_id")
        title = payload.get("title")

        expected_secret = os.getenv("DATAVERSE_WORKFLOW_SECRET", "")
        if not expected_secret or secret != expected_secret:
            raise ProblemDetailException(_("Forbidden"), status=HTTPStatus.FORBIDDEN)

        if dataset_id is None or global_id is None:
            raise ProblemDetailException(
                _("Missing required fields: dataset_id and global_id"),
                status=HTTPStatus.BAD_REQUEST,
            )

        dv_base_internal = os.getenv("DV_BASE_INTERNAL", "http://dataverse:8080").rstrip("/")
        dv_public_base = os.getenv("DV_PUBLIC_BASE", dv_base_internal).rstrip("/")
        dv_token = os.getenv("DV_API_TOKEN", "").strip()

        if not dv_token:
            raise ProblemDetailException(_("Missing DV_API_TOKEN"), status=HTTPStatus.INTERNAL_SERVER_ERROR)

        files_url = f"{dv_base_internal}/api/datasets/{dataset_id}/versions/:draft/files"
        resp = requests.get(files_url, headers={"X-Dataverse-key": dv_token}, timeout=60)

        if resp.status_code != 200:
            raise ProblemDetailException(
                _("Dataverse file listing failed"),
                status=HTTPStatus.BAD_GATEWAY,
                detail=f"status={resp.status_code} body={resp.text[:2000]}",
            )

        items = (resp.json() or {}).get("data") or []

        print(
            f"[DATAVERSE-PREPUBLISH] dataset_id={dataset_id} global_id={global_id} title={title} draft_files={len(items)}",
            flush=True,
        )

        for it in items:
            df = it.get("dataFile") or {}
            datafile_id = df.get("id")
            filename = df.get("filename")
            content_type = df.get("contentType")
            if not datafile_id:
                continue

            browser_url = f"{dv_public_base}/api/access/datafile/{datafile_id}"
            print(
                f"[DATAVERSE-PREPUBLISH] datafile_id={datafile_id} filename={filename} contentType={content_type} url={browser_url}",
                flush=True,
            )

        return HttpResponse("OK", status=HTTPStatus.OK, content_type="text/plain; charset=utf-8")