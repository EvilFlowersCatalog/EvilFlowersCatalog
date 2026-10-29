from django.urls import path

from apps.assistant.views import ChatDetail, ChatManagement, ChatMessages, Explain, HighlightDetail, Highlights

urlpatterns = [
    path("chats", ChatManagement.as_view(), name="chats"),
    path("chats/<uuid:chat_id>", ChatDetail.as_view(), name="chat-detail"),
    path("chats/<uuid:chat_id>/messages", ChatMessages.as_view(), name="chat-messages"),
    path("explain", Explain.as_view(), name="explain"),
    path("highlights", Highlights.as_view(), name="highlights"),
    path("highlights/<uuid:annotation_id>", HighlightDetail.as_view(), name="highlight-detail"),
]
