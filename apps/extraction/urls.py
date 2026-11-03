from django.urls import path

from apps.extraction.views import RunDetail, RunJob, RunParagraphs, RunRegions, RunStart

urlpatterns = [
    path("runs/<uuid:run_id>", RunDetail.as_view(), name="run-detail"),
    path("runs/<uuid:run_id>/start", RunStart.as_view(), name="run-start"),
    path("runs/<uuid:run_id>/regions", RunRegions.as_view(), name="run-regions"),
    path("runs/<uuid:run_id>/paragraphs", RunParagraphs.as_view(), name="run-paragraphs"),
    path("runs/<uuid:run_id>/jobs/<str:job>", RunJob.as_view(), name="run-job"),
]
