"""The `/api/chat` request the client sends (#104): think, context, residency, Host, format."""

from unittest import mock

from django.test import SimpleTestCase, override_settings

from apps.assistant.llm import OllamaClient

BASE = {
    "EVILFLOWERS_ASSISTANT_OLLAMA_ENDPOINT": "http://ollama.shared.svc.cluster.local/api/chat",
    "EVILFLOWERS_ASSISTANT_OLLAMA_MODEL": "elvira-assistant",
    "EVILFLOWERS_ASSISTANT_OLLAMA_API_KEY": "",
    "EVILFLOWERS_ASSISTANT_OLLAMA_TIMEOUT": 300,
    "EVILFLOWERS_ASSISTANT_OLLAMA_CONNECT_TIMEOUT": 10,
    "EVILFLOWERS_ASSISTANT_OLLAMA_HOST": "",
    "EVILFLOWERS_ASSISTANT_OLLAMA_THINK": "0",
    "EVILFLOWERS_ASSISTANT_OLLAMA_NUM_CTX": 32768,
    "EVILFLOWERS_ASSISTANT_OLLAMA_KEEP_ALIVE": "-1",
    "EVILFLOWERS_ASSISTANT_OLLAMA_TEMPERATURE": "",
}

MESSAGES = [{"role": "user", "content": "Ahoj"}]


class PayloadTests(SimpleTestCase):
    @override_settings(**BASE)
    def test_defaults_turn_thinking_off_and_pin_context_and_residency(self):
        payload = OllamaClient().payload(MESSAGES)
        self.assertIs(payload["think"], False)
        self.assertEqual(payload["options"], {"num_ctx": 32768})
        self.assertEqual(payload["keep_alive"], -1)
        self.assertEqual(payload["model"], "elvira-assistant")
        self.assertTrue(payload["stream"])
        self.assertNotIn("format", payload)
        self.assertNotIn("tools", payload)

    @override_settings(**{**BASE, "EVILFLOWERS_ASSISTANT_OLLAMA_THINK": "1"})
    def test_think_can_be_turned_on(self):
        self.assertIs(OllamaClient().payload(MESSAGES)["think"], True)

    @override_settings(
        **{
            **BASE,
            "EVILFLOWERS_ASSISTANT_OLLAMA_THINK": "",
            "EVILFLOWERS_ASSISTANT_OLLAMA_NUM_CTX": 0,
            "EVILFLOWERS_ASSISTANT_OLLAMA_KEEP_ALIVE": "",
        }
    )
    def test_blank_settings_send_nothing(self):
        # Ollama Cloud and older servers get exactly the pre-#104 request.
        payload = OllamaClient().payload(MESSAGES)
        for key in ("think", "options", "keep_alive"):
            self.assertNotIn(key, payload)

    @override_settings(**{**BASE, "EVILFLOWERS_ASSISTANT_OLLAMA_KEEP_ALIVE": "30m"})
    def test_keep_alive_accepts_a_duration(self):
        self.assertEqual(OllamaClient().payload(MESSAGES)["keep_alive"], "30m")

    @override_settings(**{**BASE, "EVILFLOWERS_ASSISTANT_OLLAMA_TEMPERATURE": "0.2"})
    def test_temperature(self):
        self.assertEqual(OllamaClient().payload(MESSAGES)["options"]["temperature"], 0.2)

    @override_settings(**BASE)
    def test_format_and_tools(self):
        schema = {"type": "object"}
        payload = OllamaClient().payload(MESSAGES, tools=[{"type": "function"}], response_format=schema)
        self.assertEqual(payload["format"], schema)
        self.assertEqual(payload["tools"], [{"type": "function"}])

    @override_settings(**BASE)
    def test_payload_is_identical_across_calls(self):
        # Nothing per request may leak in, or the prompt cache misses (IP-015 D3).
        self.assertEqual(OllamaClient().payload(MESSAGES), OllamaClient().payload(MESSAGES))


class HeaderTests(SimpleTestCase):
    @override_settings(**BASE)
    def test_no_host_or_authorization_header_when_unset(self):
        headers = OllamaClient()._headers()
        self.assertNotIn("Host", headers)
        self.assertNotIn("Authorization", headers)

    @override_settings(
        **{
            **BASE,
            "EVILFLOWERS_ASSISTANT_OLLAMA_HOST": "llm.elvira.internal",
            "EVILFLOWERS_ASSISTANT_OLLAMA_API_KEY": "k",
        }
    )
    def test_host_and_bearer(self):
        headers = OllamaClient()._headers()
        self.assertEqual(headers["Host"], "llm.elvira.internal")
        self.assertEqual(headers["Authorization"], "Bearer k")


class StreamTests(SimpleTestCase):
    @override_settings(**BASE)
    def test_connect_and_read_timeouts_and_thinking_is_not_content(self):
        lines = [
            '{"message": {"role": "assistant", "content": "", "thinking": "hmm"}, "done": false}',
            '{"message": {"role": "assistant", "content": "Dobrý deň"}, "done": false}',
            '{"message": {"role": "assistant", "content": ""}, "done": true, "prompt_eval_count": 1200, '
            '"eval_count": 8, "prompt_eval_duration": 20000000000}',
        ]
        response = mock.Mock()
        response.iter_lines.return_value = lines
        with mock.patch("apps.assistant.llm.requests.post", return_value=response) as post:
            deltas = list(OllamaClient().chat(MESSAGES))

        self.assertEqual(post.call_args.kwargs["timeout"], (10, 300))
        self.assertEqual("".join(delta.content for delta in deltas), "Dobrý deň")
        self.assertEqual(deltas[-1].prompt_tokens, 1200)
        self.assertIs(post.call_args.kwargs["json"]["think"], False)
