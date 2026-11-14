"""IP-030 §5: the llama-server client and the per-turn slot lease (against recorded responses, not a server)."""

import json
from unittest import mock

from django.test import SimpleTestCase, override_settings

from apps.assistant import llm, slots
from apps.assistant.llm import LlamaServerClient, LLMUnavailable, to_openai

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "llama-slots"}}
LLAMA = dict(
    EVILFLOWERS_ASSISTANT_BACKEND="llama-server",
    EVILFLOWERS_ASSISTANT_LLAMA_ENDPOINT="http://llm.test",
    EVILFLOWERS_ASSISTANT_LLAMA_MODEL="elvira-assistant",
    EVILFLOWERS_ASSISTANT_LLAMA_SLOTS=2,
    EVILFLOWERS_ASSISTANT_LLAMA_SLOT_WAIT=0,
    EVILFLOWERS_ASSISTANT_OLLAMA_THINK="0",
    CACHES=LOCMEM,
)

HISTORY = [
    {"role": "system", "content": "You are Elvíra."},
    {"role": "user", "content": "Bernoulli?"},
    {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"function": {"name": "search_entries", "arguments": {"query": "Bernoulli", "limit": 5}}}],
    },
    {"role": "tool", "content": '{"items": []}'},
]


def _lines(*events):
    return [f"data: {json.dumps(event)}" for event in events] + ["data: [DONE]"]


class TranslationTests(SimpleTestCase):
    def test_stored_rows_translate_deterministically(self):
        first, second = to_openai(HISTORY), to_openai(json.loads(json.dumps(HISTORY)))
        self.assertEqual(first, second)
        call = first[2]["tool_calls"][0]
        self.assertEqual(call["id"], "call_2_0")
        self.assertEqual(call["function"]["arguments"], '{"query": "Bernoulli", "limit": 5}')
        self.assertEqual(first[3]["tool_call_id"], "call_2_0")

    def test_images_become_image_parts(self):
        converted = to_openai([{"role": "user", "content": "What?", "images": ["QUJD"]}])
        self.assertEqual(converted[0]["content"][1]["image_url"]["url"], "data:image/png;base64,QUJD")

    @override_settings(**LLAMA)
    def test_payload(self):
        client = LlamaServerClient()
        payload = client.payload(
            HISTORY, tools=[{"type": "function"}], response_format={"type": "object"}, max_tokens=600, slot=1
        )
        self.assertEqual(payload["id_slot"], 1)
        self.assertEqual(payload["max_tokens"], 600)
        self.assertEqual(payload["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(payload["response_format"]["type"], "json_schema")
        self.assertTrue(payload["cache_prompt"])
        self.assertIsInstance(llm.client(), LlamaServerClient)


@override_settings(**LLAMA)
class StreamTests(SimpleTestCase):
    def setUp(self):
        from django.core.cache import cache

        cache.clear()

    def _post(self, lines):
        response = mock.Mock(status_code=200)
        response.iter_lines.return_value = lines
        response.json.return_value = {"n_restored": 10}
        return mock.patch("apps.assistant.llm.requests.post", return_value=response)

    def test_text_tool_calls_and_timings(self):
        lines = _lines(
            {"choices": [{"delta": {"content": "Hľa"}}]},
            {
                "choices": [
                    {"delta": {"tool_calls": [{"index": 0, "function": {"name": "search_", "arguments": '{"que'}}]}}
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [{"index": 0, "function": {"name": "entries", "arguments": 'ry": "x"}'}}]
                        }
                    }
                ]
            },
            {
                "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
                "timings": {
                    "prompt_n": 40,
                    "cache_n": 8000,
                    "prompt_ms": 900,
                    "predicted_n": 12,
                    "predicted_ms": 1500,
                },
            },
        )
        with self._post(lines) as post:
            deltas = list(LlamaServerClient().chat(HISTORY, tools=[{"type": "function"}]))
        self.assertEqual(post.call_args.args[0], "http://llm.test/v1/chat/completions")
        self.assertIn(post.call_args.kwargs["json"]["id_slot"], (0, 1))
        self.assertEqual(deltas[0].content, "Hľa")
        self.assertEqual(deltas[1].tool_calls, [{"function": {"name": "search_entries", "arguments": {"query": "x"}}}])
        done = deltas[-1]
        self.assertTrue(done.done)
        self.assertEqual((done.prompt_tokens, done.cached_tokens, done.completion_tokens), (8040, 8000, 12))
        self.assertEqual(done.model_seconds, 2.4)

    def test_a_turn_restores_runs_on_one_slot_and_saves(self):
        client = LlamaServerClient()
        lines = _lines({"choices": [{"delta": {"content": "A"}}], "timings": {"prompt_n": 1}})
        with self._post(lines) as post:
            client.begin_turn("chat-1")
            slot = client.lease.slot
            list(client.chat(HISTORY))
            list(client.chat(HISTORY))
            client.end_turn()
        actions = [(c.args[0], c.kwargs.get("params"), c.kwargs["json"].get("filename")) for c in post.call_args_list]
        self.assertEqual(actions[0], (f"http://llm.test/slots/{slot}", {"action": "restore"}, "chat-chat-1.bin"))
        self.assertEqual(actions[-1], (f"http://llm.test/slots/{slot}", {"action": "save"}, "chat-chat-1.bin"))
        self.assertEqual({c.kwargs["json"].get("id_slot") for c in post.call_args_list[1:3]}, {slot})
        self.assertIsNone(client.lease)


@override_settings(**LLAMA)
class LeaseTests(SimpleTestCase):
    def setUp(self):
        from django.core.cache import cache

        cache.clear()

    def test_leases_are_exclusive_and_released(self):
        first, second = slots.acquire(), slots.acquire()
        self.assertNotEqual(first.slot, second.slot)
        with self.assertRaises(LLMUnavailable):
            slots.acquire(wait=0)
        first.release()
        self.assertEqual(slots.acquire().slot, first.slot)

    def test_releasing_an_expired_lease_never_frees_another_turns(self):
        from django.core.cache import cache

        lease = slots.acquire()
        cache.set(slots._key(lease.slot), "someone-else")
        lease.release()
        self.assertEqual(cache.get(slots._key(lease.slot)), "someone-else")

    def test_a_request_outside_a_turn_leases_its_own_slot(self):
        held = slots.acquire()
        lines = _lines({"choices": [{"delta": {"content": "x"}}]})
        response = mock.Mock(status_code=200)
        response.iter_lines.return_value = lines
        with mock.patch("apps.assistant.llm.requests.post", return_value=response) as post:
            list(LlamaServerClient().chat(HISTORY))
        self.assertNotEqual(post.call_args.kwargs["json"]["id_slot"], held.slot)
        # and gave it back
        self.assertEqual(slots.acquire().slot, 1 - held.slot)
