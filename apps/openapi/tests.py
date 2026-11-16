import io
from collections import Counter
from unittest import skipUnless

import yaml
from django.conf import settings
from django.core.management import call_command
from django.test import SimpleTestCase

from apps.openapi import sections
from apps.openapi.schema import HTTP_METHODS
from apps.openapi.services import OpenApiService


class PathTests(SimpleTestCase):
    def test_join_never_doubles_the_slash(self):
        self.assertEqual(OpenApiService._join("", "extraction/v1/"), "/extraction/v1/")
        self.assertEqual(
            OpenApiService._join("/extraction/v1/", "runs/<uuid:run_id>"), "/extraction/v1/runs/<uuid:run_id>"
        )
        # An include's root is routed without a trailing slash (`path("mcp/v1", include(...))` + `path("", ...)`).
        self.assertEqual(OpenApiService._join("/mcp/v1", ""), "/mcp/v1")
        self.assertEqual(OpenApiService._join("/api/v1/", ""), "/api/v1/")

    def test_built_paths_have_no_empty_segment(self):
        # #180: `/extraction/v1//runs/{run_id}` made generated clients build the wrong URL.
        paths = OpenApiService().build()["paths"]
        self.assertIn("/extraction/v1/runs/{run_id}/jobs/{job}", paths)
        self.assertEqual([path for path in paths if "//" in path], [])


class SpecTests(SimpleTestCase):
    """The reference on elvira.digital is built from this document; Redoc hides what these tests forbid."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.spec = OpenApiService().build()
        cls.operations = {
            (method, path): operation
            for path, item in cls.spec["paths"].items()
            for method, operation in item.items()
            if method in HTTP_METHODS
        }

    def test_every_operation_has_a_section(self):
        untagged = [key for key, operation in self.operations.items() if not operation.get("tags")]
        self.assertEqual(untagged, [], "add the route to sections.PATH_RULES or tag its view")

    def test_every_tag_is_declared_in_exactly_one_group(self):
        used = {tag for operation in self.operations.values() for tag in operation["tags"]}
        grouped = Counter(tag for group in self.spec["x-tagGroups"] for tag in group["tags"])
        self.assertNotIn("Other", [group["name"] for group in self.spec["x-tagGroups"]])
        self.assertEqual(used - set(grouped), set(), "declare the tag in sections.GROUPS")
        self.assertEqual([tag for tag, count in grouped.items() if count > 1], [])
        self.assertEqual({tag["name"] for tag in self.spec["tags"]}, used)
        self.assertTrue(all(tag.get("description") for tag in self.spec["tags"]))

    def test_operation_ids_are_unique(self):
        counts = Counter(operation["operationId"] for operation in self.operations.values())
        self.assertEqual([operation_id for operation_id, count in counts.items() if count > 1], [])

    def test_paths_are_templates_not_regular_expressions(self):
        self.assertEqual([path for path in self.spec["paths"] if any(char in path for char in "()?<>^$\\")], [])
        content = self.spec["paths"]["/readium/v1/content/{lcp_content_id}"]
        self.assertEqual(content["parameters"][0]["name"], "lcp_content_id")
        self.assertEqual(content["parameters"][0]["schema"]["pattern"], "^[a-f0-9-]+$")

    def test_every_operation_answers_and_every_answer_is_described(self):
        for key, operation in self.operations.items():
            statuses = [int(status) for status in operation["responses"]]
            self.assertTrue(any(200 <= status < 400 for status in statuses), key)
            for status, response in operation["responses"].items():
                self.assertTrue(response.get("description"), (key, status))
            self.assertTrue(operation.get("summary"), key)

    def test_curated_summaries_name_live_operations(self):
        operation_ids = {operation["operationId"] for operation in self.operations.values()}
        self.assertEqual(set(sections.SUMMARIES) - operation_ids, set())

    def test_protocol_surfaces_are_documented(self):
        paths = ["/opds/v1.2/{catalog_name}", "/opds/v2/{catalog_name}/publications", "/oai"]
        if settings.EVILFLOWERS_MCP_ENABLED:  # opt-out; unmounted routes are not documented
            paths.append("/mcp/v1")
        for path in paths:
            self.assertIn(path, self.spec["paths"])
        # Inherited handlers count, under the routed view's name.
        latest = self.spec["paths"]["/opds/v1.2/{catalog_name}/new"]["get"]
        self.assertEqual(latest["operationId"], "opds1.LatestFeedView.get")
        self.assertEqual(latest["tags"], ["OPDS 1.2"])
        self.assertIn("application/atom+xml", latest["responses"][200]["content"])

    @skipUnless(settings.EVILFLOWERS_ASSISTANT_ENABLED, "the assistant is not mounted")
    def test_read_only_detail_views_document_no_writes(self):
        self.assertEqual(list(self.spec["paths"]["/assistant/v1/highlights/{annotation_id}"]), ["parameters", "get"])

    def test_serializers_publish_plain_json_schema_in_serialization_mode(self):
        # OpenAPI 3.1 = JSON Schema 2020-12, exactly what pydantic writes: optional values stay `anyOf [.., null]`.
        self.assertEqual(self.spec["openapi"], "3.1.0")
        schemas = self.spec["components"]["schemas"]
        detail = schemas["apps__core__errors__ProblemDetail"]["properties"]["detail"]
        self.assertIn({"type": "null"}, detail["anyOf"])
        self.assertNotIn("nullable", detail)
        # Serialization mode: `@computed_field`s are part of the answer, so of the document.
        self.assertIn("token", schemas["apps__api__serializers__api_keys__ApiKeySerializer__Created"]["properties"])

    def test_every_reference_resolves_to_the_model_it_names(self):
        # `JobSerializer.Base.current_run` and `ChatSerializer.Detailed.messages` both pointed at a bare `Base`
        # (whichever serializer was generated first); definitions are module-qualified now (`ProtocolJsonSchema`).
        schemas = self.spec["components"]["schemas"]
        refs: set[str] = set()

        def collect(node):
            if isinstance(node, dict):
                if isinstance(node.get("$ref"), str):
                    refs.add(node["$ref"].removeprefix("#/components/schemas/"))
                for value in node.values():
                    collect(value)
            elif isinstance(node, list):
                for value in node:
                    collect(value)

        collect(self.spec)
        self.assertEqual(refs - set(schemas), set())
        current_run = schemas["apps__api__serializers__jobs__JobSerializer__Base"]["properties"]["current_run"]
        self.assertIn(
            {"$ref": "#/components/schemas/apps__api__serializers__jobs__RunSerializer__Base"}, current_run["anyOf"]
        )
        self.assertNotIn("Base", schemas)

    def test_stacked_decorators_keep_declared_responses(self):
        # `@openapi.response` under `@openapi.metadata` used to be dropped by the wrappers.
        cover = self.spec["paths"]["/data/v1/covers/{entry_id}"]["get"]
        self.assertEqual({int(status) for status in cover["responses"]}, {200, 304, 404})
        self.assertEqual(cover["tags"], ["Files"])

    def test_views_that_never_read_credentials_are_public(self):
        self.assertEqual(self.spec["paths"]["/.well-known/tdmrep.json"]["get"]["security"], [])
        self.assertNotIn("security", self.spec["paths"]["/api/v1/catalogs"]["get"])
        # A view that authenticates refuses credentials it cannot verify.
        self.assertIn(401, {int(status) for status in self.spec["paths"]["/api/v1/catalogs"]["get"]["responses"]})

    def test_yaml_output_is_plain_yaml(self):
        out = io.StringIO()
        call_command("openapi", format="yaml", stdout=out)
        document = yaml.safe_load(out.getvalue())
        self.assertIn("200", {str(status) for status in document["paths"]["/api/v1/status"]["get"]["responses"]})
