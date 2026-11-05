from django.urls import path

from . import views, views_extras

app_name = "subscriptions"

urlpatterns = [
    path("businesses/<int:business_id>/plan/", views.PlanView.as_view(), name="plan"),
    path("businesses/<int:business_id>/scope-requests/", views.ScopeRequestsView.as_view(), name="scope-requests"),
    path("businesses/<int:business_id>/scope-requests/<int:request_id>/decision/", views.ScopeRequestDecisionView.as_view(), name="scope-decision"),
    path("businesses/<int:business_id>/plan-changes/", views.PlanChangesView.as_view(), name="plan-changes"),
    path("businesses/<int:business_id>/plan-changes/<int:change_id>/", views.PlanChangeActionView.as_view(), name="plan-change-action"),
    path("businesses/<int:business_id>/benefits/", views_extras.BenefitsView.as_view(), name="benefits"),
    path("businesses/<int:business_id>/benefits/<str:benefit>/", views_extras.BenefitDecisionView.as_view(), name="benefit-decision"),
    path("businesses/<int:business_id>/checklist/", views_extras.ChecklistView.as_view(), name="checklist"),
    path("businesses/<int:business_id>/checklist/<str:feature_id>/", views_extras.ChecklistItemView.as_view(), name="checklist-item"),
    path("businesses/<int:business_id>/subscription/", views.SubscriptionView.as_view(), name="subscription"),
]
