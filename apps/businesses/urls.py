from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import AgencyCustomersView, BusinessViewSet

app_name = "businesses"

router = DefaultRouter()
router.register("businesses", BusinessViewSet, basename="business")

urlpatterns = [path("agency/customers/", AgencyCustomersView.as_view(), name="agency-customers"), *router.urls]
