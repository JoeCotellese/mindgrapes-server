"""Account URLs: passkey enrollment, mounted under /accounts/ ahead of allauth."""

from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    # GET lands on a confirm page (consumes nothing); POST spends the token (#69).
    path("enroll/<str:token>/", views.enroll_confirm, name="enroll"),
    path("enroll/<str:token>/register/", views.enroll, name="enroll_consume"),
    path("passkeys/add/", views.EnrollPasskeyView.as_view(), name="passkey_add"),
]
