from django.urls import path

from apps.assistant.views import (
    Capabilities,
    ChatDetail,
    ChatManagement,
    ChatMessages,
    Explain,
    HighlightDetail,
    Highlights,
    MessageFeedback,
)

urlpatterns = [
    path("capabilities", Capabilities.as_view(), name="capabilities"),
    path("chats", ChatManagement.as_view(), name="chats"),
    path("chats/<uuid:chat_id>", ChatDetail.as_view(), name="chat-detail"),
    path("chats/<uuid:chat_id>/messages", ChatMessages.as_view(), name="chat-messages"),
    path(
        "chats/<uuid:chat_id>/messages/<uuid:message_id>/feedback",
        MessageFeedback.as_view(),
        name="chat-message-feedback",
    ),
    path("explain", Explain.as_view(), name="explain"),
    path("highlights", Highlights.as_view(), name="highlights"),
    path("highlights/<uuid:annotation_id>", HighlightDetail.as_view(), name="highlight-detail"),
]
