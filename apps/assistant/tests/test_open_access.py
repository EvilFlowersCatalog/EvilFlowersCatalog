from types import SimpleNamespace
from unittest import mock

from django.test import RequestFactory, SimpleTestCase

from apps.assistant import services
from apps.assistant.pdf import DocumentUnavailable, ReaderDocument
from apps.assistant.views import ChatMessages


def _entry(readium: bool, **extra):
    config = {"readium_enabled": readium}
    return SimpleNamespace(
        pk="e1",
        title="Databases",
        summary="",
        read_config=lambda name: config.get(name),
        **extra,
    )


class BorrowNoteTests(SimpleTestCase):
    def test_a_readium_entry_tells_the_model_to_send_the_reader_to_borrow_it(self):
        note = services.entry_note(_entry(readium=True))
        self.assertIn("cannot read its text", note)
        self.assertIn("borrow it", note)

    def test_an_open_entry_has_no_borrow_note(self):
        self.assertNotIn("borrow", services.entry_note(_entry(readium=False)))


class OpenAccessDocumentTests(SimpleTestCase):
    def test_a_readium_entry_is_never_read(self):
        with self.assertRaises(DocumentUnavailable):
            ReaderDocument.for_open_access(_entry(readium=True))

    def test_an_entry_without_an_open_access_pdf_is_not_read(self):
        acquisitions = mock.MagicMock()
        acquisitions.filter.return_value.exclude.return_value.exclude.return_value.exclude.return_value.order_by.return_value.first.return_value = (
            None
        )
        with self.assertRaises(DocumentUnavailable):
            ReaderDocument.for_open_access(_entry(readium=False, acquisitions=acquisitions))

    def test_the_chat_gets_no_document_for_a_readium_entry_even_with_a_reader_copy(self):
        chat = SimpleNamespace(entry=_entry(readium=True), user_acquisition=object())
        request = RequestFactory().post("/")
        self.assertIsNone(ChatMessages()._chat_document(request, chat))

    def test_the_chat_reads_the_open_access_pdf_when_it_has_no_reader_copy(self):
        chat = SimpleNamespace(entry=None, user_acquisition=None)
        request = RequestFactory().post("/")
        document = ReaderDocument(["one"], toc=[])
        with mock.patch.object(ReaderDocument, "for_open_access", return_value=document) as read:
            self.assertIs(ChatMessages()._chat_document(request, chat, _entry(readium=False)), document)
        read.assert_called_once()
