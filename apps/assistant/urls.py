from django.urls import path

from apps.assistant.views import ChatDetail, ChatManagement, ChatMessages, Explain

urlpatterns = [
    path("chats", ChatManagement.as_view(), name="chats"),
    path("chats/<uuid:chat_id>", ChatDetail.as_view(), name="chat-detail"),
    path("chats/<uuid:chat_id>/messages", ChatMessages.as_view(), name="chat-messages"),
    path("explain", Explain.as_view(), name="explain"),
]
