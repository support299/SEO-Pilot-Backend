from django.urls import path

from . import views

app_name = "wordpress"

urlpatterns = [
    path("wordpress/<int:business_id>/status/", views.ConnectionStatusView.as_view(), name="status"),
    path("wordpress/<int:business_id>/connect/", views.ConnectView.as_view(), name="connect"),
    path("wordpress/<int:business_id>/disconnect/", views.DisconnectView.as_view(), name="disconnect"),
    path("wordpress/<int:business_id>/inventory/", views.InventoryView.as_view(), name="inventory"),
    path("wordpress/<int:business_id>/change-packets/", views.ChangePacketListCreateView.as_view(), name="change-packets"),
    path("wordpress/<int:business_id>/change-packets/<int:packet_id>/decision/", views.ChangePacketDecisionView.as_view(), name="change-packet-decision"),
    path("wordpress/<int:business_id>/change-packets/<int:packet_id>/apply/", views.ChangePacketApplyView.as_view(), name="change-packet-apply"),
    path("wordpress/<int:business_id>/change-packets/<int:packet_id>/rollback/", views.ChangePacketRollbackView.as_view(), name="change-packet-rollback"),
]
