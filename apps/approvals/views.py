from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from common.permissions import get_business_or_404

from .models import Approval
from .services import list_approvals, record_decision, serialize_approval

DECISIONS = {Approval.Status.PENDING, Approval.Status.APPROVED, Approval.Status.REJECTED}


class ApprovalListView(APIView):
    def get(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        return Response({"approvals": list_approvals(business)})


class ApprovalDecisionView(APIView):
    def post(self, request, business_id, approval_id):
        business = get_business_or_404(request.user, business_id)
        approval = get_object_or_404(Approval, id=approval_id, business=business)
        decision = request.data.get("decision")
        if decision not in DECISIONS:
            return Response({"detail": "Decision must be approved, rejected, or pending."}, status=status.HTTP_400_BAD_REQUEST)
        if approval.status == Approval.Status.RESOLVED:
            return Response({"detail": "This issue is no longer in the latest crawl."}, status=status.HTTP_409_CONFLICT)
        return Response(serialize_approval(record_decision(approval, decision, request.user)))
