from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

urlpatterns = [
    path("", views.chat_page, name="chat"),
    path("chat/send", views.send, name="chat-send"),
    path("chat/key", views.submit_key, name="chat-key"),
    path("chat/secret", views.submit_secret, name="chat-secret"),
    path("chat/upload", views.upload, name="chat-upload"),
    path("chat/approve", views.approve, name="chat-approve"),
    path("chat/reset", views.reset, name="chat-reset"),
    path("chat/stream", views.stream, name="chat-stream"),
    path("login/", auth_views.LoginView.as_view(template_name="assistant/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("signup/", views.signup, name="signup"),
]
