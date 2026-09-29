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

from apps import openapi
from apps.api.response import Ordering, PaginationResponse, SingleResponse
from apps.assistant import services, turn
from apps.assistant.forms import ChatForm, MessageForm
from apps.assistant.models import Chat
from apps.assistant.serializers import ChatSerializer
from apps.core.errors import ProblemDetailException, UnauthorizedException, ValidationException
from apps.core.views import SecuredView


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
        chat = Chat.objects.filter(pk=chat_id, user=request.user).first()
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
            Chat.objects.filter(user=user),
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
        description="Permanently delete one of the authenticated user's conversations and its messages.",
        tags=["Assistant"],
        summary="Delete a chat",
    )
    def delete(self, request, chat_id: UUID):
        self._require_user(request)
        chat = self._chat(request, chat_id)
        chat.delete()

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
            turn.run(request, chat, form.cleaned_data["message"]),
            content_type="text/event-stream",
        )
        response["Cache-Control"] = "no-cache"
        # Stops nginx buffering the stream into a single delivery at the end.
        response["X-Accel-Buffering"] = "no"
        return response
