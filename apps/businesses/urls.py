from rest_framework.routers import DefaultRouter

from .views import BusinessViewSet

app_name = "businesses"

router = DefaultRouter()
router.register("businesses", BusinessViewSet, basename="business")

urlpatterns = router.urls
