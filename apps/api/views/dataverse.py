import json
from http import HTTPStatus

from django.http import HttpResponse
from django.utils.translation import gettext as _
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator

from apps import openapi
from apps.core.errors import ProblemDetailException


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
