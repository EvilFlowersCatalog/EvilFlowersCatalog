from django.test import SimpleTestCase

from apps.openapi.services import OpenApiService


class PathTests(SimpleTestCase):
    def test_join_never_doubles_the_slash(self):
        self.assertEqual(OpenApiService._join("", "extraction/v1/"), "/extraction/v1/")
        self.assertEqual(
            OpenApiService._join("/extraction/v1/", "runs/<uuid:run_id>"), "/extraction/v1/runs/<uuid:run_id>"
        )
        self.assertEqual(OpenApiService._join("/mcp/v1", ""), "/mcp/v1/")

    def test_built_paths_have_no_empty_segment(self):
        # #180: `/extraction/v1//runs/{run_id}` made generated clients build the wrong URL.
        paths = OpenApiService().build()["paths"]
        self.assertIn("/extraction/v1/runs/{run_id}/jobs/{job}", paths)
        self.assertEqual([path for path in paths if "//" in path], [])
