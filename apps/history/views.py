from rest_framework.response import Response
from rest_framework.views import APIView

from common.pagination import DefaultPagination
from common.permissions import get_business_or_404

from .models import Event
from .services import score_trend, serialize_event


class HistoryView(APIView):
    def get(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        paginator = DefaultPagination()
        page = paginator.paginate_queryset(Event.objects.filter(business=business).select_related("actor"), request, view=self)
        response = paginator.get_paginated_response([serialize_event(event) for event in page])
        response.data["score_trend"] = score_trend(business)
        return response
