"""IP-030 in a whole turn: failed turns stored, disconnects, the pack, streamed citations, study cards, quota."""

import json
from types import SimpleNamespace
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

from apps.assistant import evaluation, services, tools, turn
from apps.assistant.llm import Delta, LLMUnavailable
from apps.assistant.models import AssistantUserPolicy, Chat, ChatMessage
from apps.assistant.tests import fixtures
from apps.assistant.tests.test_turn_tools import ScriptedClient, _call


class FailingClient(ScriptedClient):
    def chat(self, messages, **kwargs):
        self.requests.append({"messages": messages, **kwargs})
        raise LLMUnavailable("402 Payment Required")
        yield  # pragma: no cover


class TimedClient(ScriptedClient):
    def chat(self, messages, tools=None, response_format=None, span_attributes=None, max_tokens=None):
        self.requests.append({"messages": messages, "tools": tools, "max_tokens": max_tokens})
        step = self.rounds.pop(0)
        if isinstance(step, str):
            for piece in [step[i : i + 3] for i in range(0, len(step), 3)]:
                yield Delta(content=piece)
        else:
            yield Delta(tool_calls=step)
        yield Delta(done=True, prompt_tokens=100, completion_tokens=10, prompt_seconds=2.0, generation_seconds=1.5)


class TurnBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.reader = fixtures.user()
        cls.open = fixtures.catalog(cls.reader)
        cls.entry, cls.extraction, cls.paragraphs, cls.venturi, _shear = fixtures.publication(cls.reader, cls.open)

    def _turn(self, client, text="Ako funguje Bernoulliho rovnica?", chat=None, **kwargs):
        chat = chat or Chat.objects.create(user=self.reader)
        with mock.patch.object(turn, "make_client", return_value=client):
            frames = list(turn.run(fixtures.request_for(self.reader), chat, text, language="sk", **kwargs))
        return chat, evaluation.parse_frames(frames)


class FailedTurnTests(TurnBase):
    def test_a_failed_turn_stores_a_notice_that_is_shown_but_never_replayed(self):
        chat, events = self._turn(FailingClient([]))
        error = dict(events)["error"]
        row = ChatMessage.objects.get(pk=error["message_id"])
        self.assertEqual(row.error, "unavailable")
        self.assertTrue(error["retryable"])
        self.assertIn(row, list(chat.messages.visible()))
        self.assertEqual([event for event, _ in events][-1], "done")

        retry = ScriptedClient(["Teraz áno."])
        self._turn(retry, chat=chat)
        replayed = [m["content"] for m in retry.requests[0]["messages"]]
        self.assertNotIn(row.text, replayed)

    def test_a_reader_who_leaves_stops_the_turn_without_more_frames(self):
        closed = []

        class Slow(ScriptedClient):
            def chat(self, messages, **kwargs):
                try:
                    yield Delta(content="Prvá ")
                    yield Delta(content="druhá")
                finally:
                    closed.append(True)

            def end_turn(self):
                closed.append("ended")

        chat = Chat.objects.create(user=self.reader)
        with mock.patch.object(turn, "make_client", return_value=Slow([])):
            stream = turn.run(fixtures.request_for(self.reader), chat, "Otázka?", language="sk")
            frames = [next(stream) for _ in range(2)]  # status, first chunk
            stream.close()  # the reader closed the tab
        self.assertIn("chunk", frames[1])
        self.assertEqual(closed, [True, "ended"])
        self.assertFalse(chat.messages.filter(role=ChatMessage.Role.ASSISTANT).exists())


class StreamedCitationTests(TurnBase):
    def _search(self):
        hits = [
            SimpleNamespace(
                entry_id=self.entry.pk,
                passages=[
                    SimpleNamespace(
                        paragraph_id=self.paragraphs[2].pk, page=3, section="2 Bernoulli", score=0.9, text="Text"
                    )
                ],
            )
        ]
        return mock.patch(
            "apps.extraction.discovery.search_passages",
            return_value=SimpleNamespace(degraded=False, items=hits, indexed=1),
        )

    def test_citations_arrive_while_streaming_and_the_text_is_unchanged(self):
        answer = "Tlak klesá, keď rýchlosť rastie [E1]. Hotovo."
        with self._search():
            chat, events = self._turn(TimedClient([[_call(tools.SEARCH_PASSAGES, query="Bernoulli")], answer]))
        names = [event for event, _ in events]
        self.assertEqual("".join(data["text"] for event, data in events if event == "chunk"), answer)
        live = [data for event, data in events if event == "citation"]
        self.assertEqual([c["label"] for c in live], ["E1"])
        self.assertLess(names.index("citation"), names.index("message"))
        self.assertEqual(live[0]["paragraph_id"], str(self.paragraphs[2].pk))
        self.assertIn("quads", live[0])
        final = dict(events)["citations"]["citations"][0]
        self.assertEqual(final["bbox"], fixtures.BBOX)
        self.assertIn("suggestions", names)

        answer_row = chat.messages.visible().filter(role=ChatMessage.Role.ASSISTANT).get()
        self.assertEqual(answer_row.model_seconds, 3.5)
        self.assertEqual(TimedClient([]).__class__.__name__, "TimedClient")


@override_settings(EVILFLOWERS_ASSISTANT_PREFETCH=True)
class PrefetchTests(TurnBase):
    def test_the_pack_is_a_stored_call_before_round_one_and_the_prefix_stays_append_only(self):
        payload = {
            "query": "Ako funguje Bernoulliho rovnica?",
            "publications": [],
            "passages": [
                {
                    "kind": "passage",
                    "entry_id": str(self.entry.pk),
                    "title": self.entry.title,
                    "page": 3,
                    "paragraph_id": str(self.paragraphs[2].pk),
                    "text": "Bernoulli's principle relates pressure and speed.",
                    "score": 0.9,
                    "label": "E1",
                }
            ],
            "figures": [],
            "evidence": {
                "E1": {
                    "kind": "passage",
                    "entry_id": str(self.entry.pk),
                    "page": 3,
                    "paragraph_id": str(self.paragraphs[2].pk),
                }
            },
        }

        def gather(context, query, entry=None):
            context.ledger.issue(payload["evidence"]["E1"])
            return payload

        client = ScriptedClient(["Tlak klesá [E1]."])
        with mock.patch("apps.assistant.pack.gather", side_effect=gather):
            chat, events = self._turn(client)
        names = [event for event, _ in events]
        self.assertLess(names.index("evidence"), names.index("chunk"))
        self.assertEqual(dict(events)["evidence"]["sources"][0]["label"], "E1")
        self.assertIn(tools.GATHER_EVIDENCE, [spec["function"]["name"] for spec in client.requests[0]["tools"]])
        messages = client.requests[0]["messages"]
        self.assertEqual(messages[-2]["tool_calls"][0]["function"]["name"], tools.GATHER_EVIDENCE)
        self.assertEqual(messages[-1]["role"], "tool")
        self.assertEqual(dict(events)["citations"]["citations"][0]["label"], "E1")

        followup = ScriptedClient(["Áno."])
        with mock.patch("apps.assistant.pack.gather", side_effect=gather):
            self._turn(followup, text="A čo Venturi?", chat=chat)
        self.assertEqual(followup.requests[0]["messages"][: len(messages)], messages)

    def test_no_pack_for_a_greeting(self):
        client = ScriptedClient(["Dobrý deň."])
        with mock.patch("apps.assistant.pack.gather") as gather:
            _chat, events = self._turn(client, text="Ahoj")
        gather.assert_not_called()
        self.assertNotIn("evidence", [event for event, _ in events])


class StudyTurnTests(TurnBase):
    def test_a_study_card_closes_the_turn_and_is_read_back(self):
        from apps.assistant.tests.test_ip030_tools import JsonClient

        card = JsonClient({"points": [{"text": "a", "labels": ["E1"]}, {"text": "b", "labels": ["E2"]}]})
        client = ScriptedClient([[_call(tools.STUDY, mode="walkthrough", entry_id=str(self.entry.pk))]])
        with mock.patch("apps.assistant.llm.client", return_value=card):
            chat, events = self._turn(client, text="Prejdi so mnou knihu po kapitolách.")
        self.assertEqual(len(client.requests), 1)
        study = dict(events)["study"]
        self.assertEqual(study["mode"], "walkthrough")
        answer = chat.messages.visible().filter(role=ChatMessage.Role.ASSISTANT).get()
        self.assertEqual(answer.study["data"]["section"], "1 Introduction")
        self.assertEqual(
            dict(events)["suggestions"]["suggestions"][0]["message"], "Pokračuj v prechádzaní: 2 Bernoulli"
        )


class QuotaTests(TurnBase):
    @override_settings(EVILFLOWERS_ASSISTANT_DAILY_LIMIT_SECONDS=5)
    def test_the_quota_counts_model_seconds(self):
        self._turn(TimedClient(["Áno."]))
        self.assertEqual(services.usage(self.reader).seconds, 3.5)
        services.assert_within_quota(self.reader)
        self._turn(TimedClient(["Áno."]))
        with self.assertRaises(services.QuotaExceeded) as raised:
            services.assert_within_quota(self.reader)
        self.assertEqual(raised.exception.unit, "seconds")

    def test_a_policy_overrides_the_seconds_limit(self):
        AssistantUserPolicy.objects.create(user=self.reader, daily_seconds_limit=1)
        self._turn(TimedClient(["Áno."]))
        with self.assertRaises(services.QuotaExceeded):
            services.assert_within_quota(self.reader)


class EndpointTests(TurnBase):
    def setUp(self):
        import base64

        self.reader.set_password("secret-pass")
        self.reader.save()
        token = base64.b64encode(f"{self.reader.username}:secret-pass".encode()).decode()
        self.client.defaults["HTTP_AUTHORIZATION"] = f"Basic {token}"

    def test_feedback_on_an_answer(self):
        chat, _events = self._turn(ScriptedClient(["Áno."]))
        answer = chat.messages.visible().get(role=ChatMessage.Role.ASSISTANT)
        url = reverse("assistant:chat-message-feedback", kwargs={"chat_id": chat.pk, "message_id": answer.pk})
        response = self.client.post(url, {"rating": -1, "reason": "wrong page"}, content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        answer.refresh_from_db()
        self.assertEqual((answer.feedback["rating"], answer.feedback["reason"]), (-1, "wrong page"))
        exported = evaluation.export_feedback(10)
        self.assertEqual(exported[0]["question"], "Ako funguje Bernoulliho rovnica?")

    def test_feedback_on_someone_elses_chat_is_not_found(self):
        other = fixtures.user("other")
        chat = Chat.objects.create(user=other)
        row = ChatMessage.objects.create(chat=chat, user=other, role=ChatMessage.Role.ASSISTANT, text="x")
        url = reverse("assistant:chat-message-feedback", kwargs={"chat_id": chat.pk, "message_id": row.pk})
        response = self.client.post(url, {"rating": 1}, content_type="application/json")
        self.assertEqual(response.status_code, 404)

    def test_capabilities_depend_on_the_session(self):
        response = self.client.get(reverse("assistant:capabilities"), {"language": "en"})
        ids = [item["id"] for item in response.json()["response"]["capabilities"]]
        self.assertIn("loans", ids)
        self.assertNotIn("walkthrough", ids)
        response = self.client.get(
            reverse("assistant:capabilities"), {"language": "sk", "entry_id": str(self.entry.pk)}
        )
        ids = [item["id"] for item in response.json()["response"]["capabilities"]]
        self.assertIn("walkthrough", ids)
