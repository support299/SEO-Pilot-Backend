from django.urls import path

from . import views, views_data, views_siteplans, views_workspace

app_name = "agent"

urlpatterns = [
    path("businesses/", views.BusinessListView.as_view(), name="businesses"),
    path("businesses/<int:business_id>/context/", views.BusinessContextView.as_view(), name="context"),
    path("businesses/<int:business_id>/wordpress/pages/", views.WordPressInventoryView.as_view(), name="inventory"),
    path("businesses/<int:business_id>/local-visibility/", views.LocalVisibilityView.as_view(), name="local-visibility"),
    path("businesses/<int:business_id>/pages/", views_data.AgentPagesView.as_view(), name="pages"),
    path("businesses/<int:business_id>/crawl/", views_data.AgentStartCrawlView.as_view(), name="crawl"),
    path("businesses/<int:business_id>/search-console/", views_data.AgentSearchConsoleOverviewView.as_view(), name="sc-overview"),
    path("businesses/<int:business_id>/search-console/queries/", views_data.AgentSearchConsoleQueriesView.as_view(), name="sc-queries"),
    path("businesses/<int:business_id>/search-console/pages/", views_data.AgentSearchConsolePagesView.as_view(), name="sc-pages"),
    path("businesses/<int:business_id>/search-console/sync/", views_data.AgentSearchConsoleSyncView.as_view(), name="sc-sync"),
    path("businesses/<int:business_id>/analytics/", views_data.AgentAnalyticsOverviewView.as_view(), name="ga-overview"),
    path("businesses/<int:business_id>/analytics/sync/", views_data.AgentAnalyticsSyncView.as_view(), name="ga-sync"),
    path("businesses/<int:business_id>/history/", views_data.AgentHistoryView.as_view(), name="history"),
    path("businesses/<int:business_id>/connections/", views_data.AgentConnectionsView.as_view(), name="connections"),
    path("businesses/<int:business_id>/plan/", views_workspace.AgentPlanView.as_view(), name="plan"),
    path("businesses/<int:business_id>/benefits/", views_workspace.AgentBenefitsView.as_view(), name="benefits"),
    path("businesses/<int:business_id>/checklist/", views_workspace.AgentChecklistView.as_view(), name="checklist"),
    path("businesses/<int:business_id>/checklist/<str:feature_id>/", views_workspace.AgentChecklistItemView.as_view(), name="checklist-item"),
    path("businesses/<int:business_id>/scope-requests/", views_workspace.AgentScopeRequestsView.as_view(), name="scope-requests"),
    path("businesses/<int:business_id>/site-plans/", views_siteplans.AgentSitePlansView.as_view(), name="site-plans"),
    path("businesses/<int:business_id>/site-plans/<int:plan_id>/", views_siteplans.AgentSitePlanView.as_view(), name="site-plan"),
    path("businesses/<int:business_id>/site-plans/<int:plan_id>/tasks/", views_siteplans.AgentPlanTasksView.as_view(), name="site-plan-tasks"),
    path("businesses/<int:business_id>/site-plans/<int:plan_id>/<str:action>/", views_siteplans.AgentSitePlanActionView.as_view(), name="site-plan-action"),
    path("businesses/<int:business_id>/site-plan-tasks/<int:task_id>/", views_siteplans.AgentPlanTaskView.as_view(), name="site-plan-task"),
    path("businesses/<int:business_id>/proposals/", views.ProposalsView.as_view(), name="proposals"),
]
