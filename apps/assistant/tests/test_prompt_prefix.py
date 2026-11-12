"""#138: every request of a conversation extends the previous one.

The model is hybrid (Gated DeltaNet), so Ollama can only reuse a prompt-cache
checkpoint taken at the end of an earlier prompt, and only when that whole
prompt is a prefix of the new one. These tests pin that at message level.
"""

from datetime import timedelta
from unittest import mock

from django.test import RequestFactory, SimpleTestCase, override_settings
from django.utils import timezone

from apps.assistant import services, turn
from apps.assistant.models import Chat, ChatMessage
from apps.assistant.tests.test_turn_tools import ScriptedClient, TurnTestCase, _call

SEARCH = [_call("search_entries", query="Analytical", author=None, language_codes=[])]


class PromptPrefixTests(TurnTestCase):
    def _run(self, chat, rounds, text, language="sk"):
        client = ScriptedClient(rounds)
        request = RequestFactory().post("/assistant/v1/chats")
        request.user = self.reader
        with mock.patch.object(turn, "OllamaClient", return_value=client):
            list(turn.run(request, chat, text, language=language))
        return client.requests

    def assertExtends(self, before, after):
        self.assertEqual(after["messages"][: len(before["messages"])], before["messages"])
        self.assertEqual(after["tools"], before["tools"])

    def test_rounds_of_a_turn_extend_each_other(self):
        chat = Chat.objects.create(user=self.reader)
        requests = self._run(chat, [SEARCH, "Nenašiel som nič."], "Máte niečo o Analytical Engine?")

        self.assertEqual(len(requests), 2)
        self.assertExtends(requests[0], requests[1])
        # The language pin follows the user message instead of closing the request.
        first = requests[0]["messages"]
        self.assertEqual(first[-2]["role"], "user")
        self.assertEqual(first[-1], {"role": "system", "content": services.language_instruction("sk")})
        self.assertEqual(requests[1]["messages"][-1]["role"], "tool")

        attributes = requests[1]["span_attributes"]
        self.assertEqual(attributes["evilflowers.assistant.prompt.shared_messages"], len(first))
        self.assertEqual(attributes["evilflowers.assistant.prompt.previous_messages"], len(first))

    def test_the_next_turn_extends_the_last_round(self):
        chat = Chat.objects.create(user=self.reader)
        first = self._run(chat, [SEARCH, "Toto som našiel."], "Máte niečo o Analytical Engine?")
        chat.refresh_from_db()
        second = self._run(chat, ["Áno."], "A niečo novšie?")

        self.assertExtends(first[-1], second[0])

    @override_settings(EVILFLOWERS_ASSISTANT_TRIM_MESSAGES=5, EVILFLOWERS_ASSISTANT_CACHE_TTL=60)
    def test_a_cold_turn_cuts_once_at_a_user_message_and_keeps_the_cut(self):
        chat = Chat.objects.create(user=self.reader)
        for number in range(6):
            ChatMessage.objects.create(chat=chat, user=self.reader, role=ChatMessage.Role.USER, text=f"q{number}")
            ChatMessage.objects.create(
                chat=chat, user=self.reader, role=ChatMessage.Role.ASSISTANT, text="", tool_calls=SEARCH
            )
            ChatMessage.objects.create(chat=chat, user=self.reader, role=ChatMessage.Role.TOOL, text="{}")
            ChatMessage.objects.create(chat=chat, user=self.reader, role=ChatMessage.Role.ASSISTANT, text=f"a{number}")
        Chat.objects.filter(pk=chat.pk).update(last_message_at=timezone.now() - timedelta(hours=1))
        chat.refresh_from_db()

        cold = self._run(chat, [SEARCH, SEARCH, "Hotovo."], "Ďalšia otázka")
        chat.refresh_from_db()

        self.assertIsNotNone(chat.history_start)
        start = ChatMessage.objects.get(chat=chat, created_at=chat.history_start)
        self.assertEqual(start.role, ChatMessage.Role.USER)
        # The cut did not slide while the turn added rows.
        self.assertExtends(cold[0], cold[1])
        self.assertExtends(cold[1], cold[2])
        history = [m for m in cold[0]["messages"] if m["role"] != "system"]
        self.assertEqual(history[0]["role"], "user")
        self.assertLessEqual(len(history), 5)

        # The next, warm turn replays from the same cut.
        warm = self._run(chat, ["Áno."], "Ešte jedna")
        self.assertExtends(cold[-1], warm[0])

    def test_warm_turns_do_not_cut(self):
        chat = Chat.objects.create(user=self.reader)
        self._run(chat, ["Áno."], "Prvá")
        chat.refresh_from_db()
        self._run(chat, ["Áno."], "Druhá")
        chat.refresh_from_db()
        self.assertIsNone(chat.history_start)


class SharedPrefixTests(SimpleTestCase):
    def test_counts_leading_equal_messages(self):
        a = {"role": "user", "content": "a"}
        b = {"role": "assistant", "content": "b"}
        c = {"role": "tool", "content": "c"}
        self.assertEqual(services.shared_prefix(None, [a]), 0)
        self.assertEqual(services.shared_prefix([a, b], [a, b, c]), 2)
        self.assertEqual(services.shared_prefix([a, b], [a, c]), 1)
