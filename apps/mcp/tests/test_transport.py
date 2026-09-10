"""IP-014: JSON-RPC / Streamable HTTP conformance of the `/mcp/v1` endpoint.

Deliberately `SimpleTestCase`: none of these exchanges may touch the database,
and `SimpleTestCase` fails the test if one does. That is the assertion — a
client can negotiate, enumerate tools and get its arguments rejected without the
catalog issuing a single query.
"""

import json
from http import HTTPStatus

from django.test import SimpleTestCase, override_settings

from apps.mcp.protocol import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    LATEST_PROTOCOL_VERSION,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    PROTOCOL_VERSION_HEADER,
)

URL = "/mcp/v1"

READ_TOOLS = {
    "get_category",
    "get_entry",
    "get_feed",
    "get_my_shelf",
    "list_authors",
    "list_catalogs",
    "list_categories",
    "list_feeds",
    "list_my_loans",
    "search_entries",
    "whoami",
}

WRITE_TOOLS = {
    "create_category",
    "create_feed",
    "delete_category",
    "delete_feed",
    "update_category",
    "update_feed",
}


class TransportTestCase(SimpleTestCase):
    def rpc(self, payload, headers=None):
        # Behave like a current client: announce the revision on every call
        # after initialize, which is what unlocks `structuredContent`.
        merged = {PROTOCOL_VERSION_HEADER: LATEST_PROTOCOL_VERSION}
        merged.update(headers or {})
        return self.client.post(
            URL,
            data=json.dumps(payload),
            content_type="application/json",
            headers=merged,
        )

    def result(self, payload, headers=None):
        response = self.rpc(payload, headers)
        self.assertEqual(response.status_code, HTTPStatus.OK)
        return json.loads(response.content)


class InitializeTests(TransportTestCase):
    def test_initialize_echoes_a_supported_protocol_version(self):
        body = self.result(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}},
            }
        )
        self.assertEqual(body["result"]["protocolVersion"], "2025-06-18")

    def test_initialize_falls_back_for_an_unknown_client_revision(self):
        body = self.result(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "1999-01-01", "capabilities": {}, "clientInfo": {"name": "t"}},
            }
        )
        # We answer with the newest we speak rather than refusing — the client
        # then decides whether it can work with it.
        self.assertEqual(body["result"]["protocolVersion"], "2025-06-18")

    def test_initialize_advertises_every_surface_this_server_offers(self):
        body = self.result(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
        )
        capabilities = body["result"]["capabilities"]
        self.assertEqual(set(capabilities), {"tools", "resources", "prompts", "completions"})
        # Stateless: there is no channel on which to deliver change notifications.
        self.assertFalse(capabilities["resources"]["subscribe"])
        self.assertFalse(capabilities["resources"]["listChanged"])
        self.assertIn("instructions", body["result"])

    def test_response_header_reports_the_negotiated_revision(self):
        response = self.rpc(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}}
        )
        self.assertEqual(response.headers[PROTOCOL_VERSION_HEADER], "2024-11-05")


class ToolListingTests(TransportTestCase):
    def test_tools_list_returns_the_whole_registry(self):
        body = self.result({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual({tool["name"] for tool in body["result"]["tools"]}, READ_TOOLS | WRITE_TOOLS)

    def test_read_tools_are_annotated_read_only(self):
        body = self.result({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        for tool in body["result"]["tools"]:
            if tool["name"] in READ_TOOLS:
                self.assertTrue(tool["annotations"]["readOnlyHint"], tool["name"])
                self.assertFalse(tool["annotations"]["destructiveHint"], tool["name"])

    def test_write_tools_are_annotated_so_clients_can_prompt(self):
        body = self.result({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        annotations = {tool["name"]: tool["annotations"] for tool in body["result"]["tools"]}

        for name in WRITE_TOOLS:
            self.assertFalse(annotations[name]["readOnlyHint"], name)

        # Creates are neither destructive nor idempotent; updates and deletes
        # overwrite or remove, which is what makes a client ask first.
        self.assertFalse(annotations["create_feed"]["destructiveHint"])
        self.assertFalse(annotations["create_feed"]["idempotentHint"])
        for name in ("update_feed", "delete_feed", "update_category", "delete_category"):
            self.assertTrue(annotations[name]["destructiveHint"], name)

    def test_every_tool_declares_an_output_schema(self):
        body = self.result({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        for tool in body["result"]["tools"]:
            self.assertIn("outputSchema", tool, tool["name"])
            self.assertEqual(tool["outputSchema"]["type"], "object", tool["name"])

    @override_settings(EVILFLOWERS_MCP_ALLOW_WRITE=False)
    def test_disabling_writes_unadvertises_the_management_tools(self):
        body = self.result({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual({tool["name"] for tool in body["result"]["tools"]}, READ_TOOLS)

    def test_every_tool_declares_a_closed_object_schema(self):
        body = self.result({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        for tool in body["result"]["tools"]:
            self.assertEqual(tool["inputSchema"]["type"], "object", tool["name"])
            self.assertFalse(tool["inputSchema"]["additionalProperties"], tool["name"])


class MessageHandlingTests(TransportTestCase):
    def test_ping(self):
        body = self.result({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertEqual(body["result"], {})

    def test_a_notification_is_answered_with_202_and_no_body(self):
        response = self.rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(response.status_code, HTTPStatus.ACCEPTED)
        self.assertEqual(response.content, b"")

    def test_unknown_method(self):
        body = self.result({"jsonrpc": "2.0", "id": 1, "method": "does/not/exist"})
        self.assertEqual(body["error"]["code"], METHOD_NOT_FOUND)

    def test_wrong_jsonrpc_version(self):
        body = self.result({"jsonrpc": "1.0", "id": 1, "method": "ping"})
        self.assertEqual(body["error"]["code"], INVALID_REQUEST)

    def test_malformed_json_is_a_parse_error(self):
        response = self.client.post(URL, data="{nope", content_type="application/json")
        self.assertEqual(response.status_code, HTTPStatus.BAD_REQUEST)
        self.assertEqual(json.loads(response.content)["error"]["code"], PARSE_ERROR)

    def test_non_json_content_type_is_rejected(self):
        response = self.client.post(URL, data="ping", content_type="text/plain")
        self.assertEqual(response.status_code, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)

    def test_batch_answers_only_the_requests(self):
        body = self.result(
            [
                {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "ping"},
            ]
        )
        self.assertEqual([message["id"] for message in body], [1, 2])

    def test_batch_of_notifications_only_yields_202(self):
        response = self.rpc([{"jsonrpc": "2.0", "method": "notifications/initialized"}])
        self.assertEqual(response.status_code, HTTPStatus.ACCEPTED)


class HttpMethodTests(TransportTestCase):
    def test_get_is_rejected_because_no_sse_stream_is_offered(self):
        response = self.client.get(URL)
        self.assertEqual(response.status_code, HTTPStatus.METHOD_NOT_ALLOWED)
        self.assertEqual(response.headers["Allow"], "POST")

    def test_delete_is_rejected_because_there_is_no_session(self):
        response = self.client.delete(URL)
        self.assertEqual(response.status_code, HTTPStatus.METHOD_NOT_ALLOWED)


class ProtocolVersionHeaderTests(TransportTestCase):
    def test_an_unsupported_header_is_rejected_after_initialize(self):
        response = self.rpc(
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={PROTOCOL_VERSION_HEADER: "1999-01-01"},
        )
        self.assertEqual(response.status_code, HTTPStatus.BAD_REQUEST)

    def test_an_unsupported_header_does_not_block_initialize(self):
        # Rejecting the negotiation itself would leave the client with no way to
        # discover which revisions we do speak.
        response = self.rpc(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}},
            headers={PROTOCOL_VERSION_HEADER: "1999-01-01"},
        )
        self.assertEqual(response.status_code, HTTPStatus.OK)

    def test_a_missing_header_is_accepted(self):
        self.assertEqual(self.rpc({"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code, HTTPStatus.OK)


class ToolCallErrorTests(TransportTestCase):
    def call(self, name, arguments=None):
        return self.result(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            }
        )

    def test_unknown_tool_is_a_protocol_error(self):
        body = self.call("teleport")
        self.assertEqual(body["error"]["code"], INVALID_PARAMS)
        # The message lists what *is* available so the model can recover in one turn.
        self.assertIn("search_entries", body["error"]["message"])

    def test_unknown_argument_is_a_tool_error_not_a_crash(self):
        body = self.call("search_entries", {"nonsense": 1})
        self.assertTrue(body["result"]["isError"])
        self.assertIn("Unknown argument", body["result"]["content"][0]["text"])

    def test_malformed_uuid_is_a_tool_error(self):
        body = self.call("get_entry", {"entry_id": "banana"})
        self.assertTrue(body["result"]["isError"])
        self.assertIn("not a valid UUID", body["result"]["content"][0]["text"])

    def test_limit_above_the_ceiling_is_a_tool_error(self):
        body = self.call("search_entries", {"limit": 5000})
        self.assertTrue(body["result"]["isError"])

    def test_personal_tools_refuse_an_anonymous_session(self):
        for name in ("get_my_shelf", "list_my_loans"):
            with self.subTest(tool=name):
                body = self.call(name)
                self.assertTrue(body["result"]["isError"])
                self.assertIn("credentials", body["result"]["content"][0]["text"])

    def test_whoami_works_anonymously(self):
        body = self.call("whoami")
        self.assertFalse(body["result"]["isError"])
        self.assertFalse(body["result"]["structuredContent"]["authenticated"])
