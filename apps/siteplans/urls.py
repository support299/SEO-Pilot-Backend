from django.urls import path

from . import views

app_name = "siteplans"

urlpatterns = [
    path("businesses/<int:business_id>/site-plans/", views.SitePlansView.as_view(), name="plans"),
    path("businesses/<int:business_id>/site-plans/<int:plan_id>/decision/", views.SitePlanDecisionView.as_view(), name="decision"),
    path("businesses/<int:business_id>/site-plans/tasks/<int:task_id>/", views.SitePlanTaskProgressView.as_view(), name="task-progress"),
]
