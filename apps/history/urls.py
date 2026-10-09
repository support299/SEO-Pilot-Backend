from django.urls import path

from . import views

app_name = "history"

urlpatterns = [
    path("history/<int:business_id>/", views.HistoryView.as_view(), name="list"),
]
