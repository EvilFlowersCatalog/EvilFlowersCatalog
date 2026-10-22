"""Chat endpoints: `/assistant/v1/`.

Authentication is `SecuredView`'s, exactly as the REST API and the MCP endpoint
use it — `Authorization: Bearer <api key JWT>` or `Basic`. Unlike MCP there is
no anonymous mode: every chat belongs to a user, is quota'd per user and is
readable only by that user.
"""

from http import HTTPStatus
from uuid import UUID

from django.http import StreamingHttpResponse
from django.utils import timezone
from django.utils.translation import gettext as _
from object_checker.base_object_checker import has_object_permission

from apps import openapi
from apps.api.response import Ordering, PaginationResponse, SingleResponse
from apps.assistant import explain, services, turn
from apps.assistant.forms import ChatForm, ExplainForm, MessageForm
from apps.assistant.llm import LLMUnavailable
from apps.assistant.models import Chat
from apps.assistant.pdf import DocumentUnavailable, ReaderDocument
from apps.assistant.serializers import ChatSerializer, ExplanationSerializer
from apps.core.errors import AuthorizationException, ProblemDetailException, UnauthorizedException, ValidationException
from apps.core.models import UserAcquisition
from apps.core.views import SecuredView
from apps.files.views import _check_ip_block


class AssistantView(SecuredView):
    """Shared preconditions for every chat endpoint."""

    def _require_user(self, request):
        if not request.user.is_authenticated:
            raise UnauthorizedException(detail=_("The assistant requires an authenticated session"))
        return request.user

    def _chat(self, request, chat_id: UUID) -> Chat:
        """The caller's chat, or 404.

        A chat belonging to someone else reports as missing rather than
        forbidden, so the endpoint cannot be used to discover that a given id
        exists.
        """
        chat = Chat.objects.filter(pk=chat_id, user=request.user, is_active=True).first()
        if chat is None:
            raise ProblemDetailException(_("Chat not found"), status=HTTPStatus.NOT_FOUND)
        return chat

    @staticmethod
    def _blocked(error: services.UserBlocked) -> ProblemDetailException:
        return ProblemDetailException(
            _("Assistant access is blocked"),
            status=HTTPStatus.FORBIDDEN,
            detail=error.reason or None,
        )


class ChatManagement(AssistantView):
    @openapi.metadata(
        description=(
            "Open a new conversation with the library assistant. Optionally pass `entry_id` to start "
            "the conversation about a specific publication. Returns the chat id used by the message "
            "endpoint."
        ),
        tags=["Assistant"],
        summary="Start a chat",
    )
    @openapi.response(HTTPStatus.CREATED, "The created chat", ChatSerializer.Base)
    def post(self, request):
        """Start a conversation."""
        user = self._require_user(request)

        try:
            services.assert_not_blocked(user)
        except services.UserBlocked as error:
            raise self._blocked(error) from error

        form = ChatForm.create_from_request(request)
        if not form.is_valid():
            raise ValidationException(form)

        chat = Chat(
            user=user,
            entry=form.cleaned_data.get("entry_id"),
            catalog=form.cleaned_data.get("catalog_id"),
        )
        chat.save()

        return SingleResponse(
            request,
            data=ChatSerializer.Base.model_validate(chat),
            status=HTTPStatus.CREATED,
        )

    @openapi.metadata(
        description=(
            "List the authenticated user's conversations, newest first. A user only ever sees their " "own chats."
        ),
        tags=["Assistant"],
        summary="List chats",
    )
    def get(self, request):
        """List the caller's conversations, newest first unless asked otherwise."""
        user = self._require_user(request)

        # `PaginationResponse` rebuilds ordering from the request and would
        # otherwise fall back to `created_at` ascending, which puts the oldest
        # conversation first. Supply a newest-first default while still letting
        # a client override it with `?order_by=`.
        ordering = Ordering.create_from_request(request) if "order_by" in request.GET else Ordering(["-created_at"])

        return PaginationResponse(
            request,
            Chat.objects.filter(user=user, is_active=True),
            serializer=ChatSerializer.Base,
            ordering=ordering,
        )


class ChatDetail(AssistantView):
    @openapi.metadata(
        description=(
            "Retrieve one conversation together with its message history, in chronological order. "
            "Only `user` and `assistant` messages are returned — tool traffic stays internal — and "
            "publications shown during a turn are on that turn's answer as `displayed_entries`, "
            "matching what the `entries` stream event rendered. This replaces the resume step of the "
            "previous standalone assistant: turns are stateless, so a client simply reads the history back."
        ),
        tags=["Assistant"],
        summary="Get a chat",
    )
    @openapi.response(HTTPStatus.OK, "The chat and its messages", ChatSerializer.Detailed)
    def get(self, request, chat_id: UUID):
        """One conversation with its full message history."""
        self._require_user(request)
        chat = self._chat(request, chat_id)

        return SingleResponse(request, data=ChatSerializer.Detailed.model_validate(chat))

    @openapi.metadata(
        description=(
            "Delete one of the authenticated user's conversations. It disappears from the chat list "
            "and can no longer be read or continued."
        ),
        tags=["Assistant"],
        summary="Delete a chat",
    )
    def delete(self, request, chat_id: UUID):
        self._require_user(request)
        chat = self._chat(request, chat_id)
        # Soft delete: the daily quota is counted from message rows, so removing
        # them would hand the user their allowance back.
        chat.is_active = False
        chat.save(update_fields=["is_active", "updated_at"])

        return SingleResponse(request, status=HTTPStatus.NO_CONTENT)


class ChatMessages(AssistantView):
    @openapi.metadata(
        description=(
            "Send a message and receive the assistant's answer as a Server-Sent Events stream "
            "(`text/event-stream`). Event names are `chunk` (a fragment of the answer as it is "
            "generated), `message` (a completed answer), `entries` (publication ids the assistant "
            "wants rendered as cards), `error`, and `done`. Responds 429 when the caller's daily "
            "allowance is spent and 403 when their assistant access is blocked."
        ),
        tags=["Assistant"],
        summary="Send a message",
    )
    def post(self, request, chat_id: UUID):
        """Send a message and stream the answer as Server-Sent Events."""
        user = self._require_user(request)
        chat = self._chat(request, chat_id)

        try:
            services.assert_not_blocked(user)
        except services.UserBlocked as error:
            raise self._blocked(error) from error

        form = MessageForm.create_from_request(request)
        if not form.is_valid():
            raise ValidationException(form)

        try:
            services.assert_within_quota(user)
        except services.QuotaExceeded as error:
            retry_after = max(int((error.reset_at - timezone.localtime()).total_seconds()), 1)
            raise ProblemDetailException(
                _("Daily assistant limit exceeded"),
                status=HTTPStatus.TOO_MANY_REQUESTS,
                detail=_("Used %(used)d of %(limit)d messages today.") % {"used": error.used, "limit": error.limit},
                extra_headers=(("Retry-After", str(retry_after)),),
            ) from error

        response = StreamingHttpResponse(
            turn.run(request, chat, form.cleaned_data["message"], entry=form.cleaned_data.get("entry_id")),
            content_type="text/event-stream",
        )
        response["Cache-Control"] = "no-cache"
        # Stops nginx buffering the stream into a single delivery at the end.
        response["X-Accel-Buffering"] = "no"
        return response


class Explain(AssistantView):
    @openapi.metadata(
        description=(
            "Explain a passage the reader selected in the viewer. The model is given the page the "
            "selection is on and reads further pages of the reader's copy, or searches its text, until "
            "it can say what the passage means in context. Returns a short explanation, examples and "
            "the pages it drew on. Responds 429 when the caller's daily allowance is spent, 403 when "
            "their assistant access is blocked and 503 when the model is unavailable."
        ),
        tags=["Assistant"],
        summary="Explain a passage",
    )
    @openapi.response(HTTPStatus.OK, "The explanation", ExplanationSerializer.Base)
    def post(self, request):
        user = self._require_user(request)

        try:
            services.assert_not_blocked(user)
        except services.UserBlocked as error:
            raise self._blocked(error) from error

        form = ExplainForm.create_from_request(request)
        if not form.is_valid():
            raise ValidationException(form)

        try:
            services.assert_within_quota(user)
        except services.QuotaExceeded as error:
            retry_after = max(int((error.reset_at - timezone.localtime()).total_seconds()), 1)
            raise ProblemDetailException(
                _("Daily assistant limit exceeded"),
                status=HTTPStatus.TOO_MANY_REQUESTS,
                detail=_("Used %(used)d of %(limit)d messages today.") % {"used": error.used, "limit": error.limit},
                extra_headers=(("Retry-After", str(retry_after)),),
            ) from error

        # The same checks the download of this copy makes: the model reads
        # exactly what the reader is allowed to open, and nothing more.
        user_acquisition = form.cleaned_data["user_acquisition_id"]
        if user_acquisition.type == UserAcquisition.UserAcquisitionType.PERSONAL:
            if not has_object_permission("check_user_acquisition_read", user, user_acquisition):
                raise AuthorizationException(request)
        entry = user_acquisition.acquisition.entry
        _check_ip_block(request, entry)

        try:
            document = ReaderDocument.for_user_acquisition(user_acquisition)
        except DocumentUnavailable as error:
            raise ProblemDetailException(
                _("This publication cannot be explained"),
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
                detail=_("Only PDF publications can be read by the assistant."),
            ) from error

        try:
            explanation = explain.explain(
                document,
                text=form.cleaned_data["text"],
                page=form.cleaned_data.get("page"),
                title=entry.title,
                authors=", ".join(author.full_name for author in entry.authors.all()),
                language=form.cleaned_data.get("language") or None,
            )
        except LLMUnavailable as error:
            raise ProblemDetailException(
                _("The assistant is temporarily unavailable."),
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            ) from error

        return SingleResponse(request, data=ExplanationSerializer.Base.model_validate(explanation))
