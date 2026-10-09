from django.urls import path

from . import views

app_name = "approvals"

urlpatterns = [
    path("approvals/<int:business_id>/", views.ApprovalListView.as_view(), name="list"),
    path("approvals/<int:business_id>/<int:approval_id>/decision/", views.ApprovalDecisionView.as_view(), name="decision"),
]
