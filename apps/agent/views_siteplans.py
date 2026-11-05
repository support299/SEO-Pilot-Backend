"""
Agent endpoints for site plans (same `Authorization: Agent <key>` as the rest of the agent API).

An agent drafts a plan, adds tasks linked to checklist items, submits it, and records progress once a person has
approved it. It can never approve, reject or activate a plan.
"""

from datetime import timedelta

from django.conf import settings
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

from apps.history.models import Event
from apps.siteplans import services
from apps.siteplans.models import PlanTask, SitePlan
from apps.siteplans.serializers import PlanInputSerializer, TaskInputSerializer, TaskUpdateSerializer
from apps.siteplans.views import rejected
from apps.subscriptions.errors import RequestRejected
from common.permissions import get_business_or_404

from .views import AgentView


class _PlanWriteView(AgentView):
    """Shared guard: paused businesses are refused, and a looping agent hits a daily cap per website."""

    def guard(self, request, business):
        if business.is_effective_paused:
            return Response({"detail": "This business is paused."}, status=status.HTTP_400_BAD_REQUEST)
        cap = settings.AGENT_MAX_PLAN_WRITES_PER_BUSINESS_PER_DAY
        since = timezone.now() - timedelta(days=1)
        if Event.objects.filter(business=business, kind__startswith="siteplan.", metadata__has_key="agent", occurred_at__gte=since).count() >= cap:
            return Response({"detail": f"Daily limit of {cap} plan changes for this website reached."}, status=status.HTTP_429_TOO_MANY_REQUESTS)
        return None

    def run(self, fn):
        try:
            return fn()
        except RequestRejected as error:
            return rejected(error)


class AgentSitePlansView(_PlanWriteView):
    """GET: the active plan, the open draft (if any) and past versions. POST: start the next draft version."""

    def get(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        return Response(services.workspace(business, internal=True))

    def post(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        if (blocked := self.guard(request, business)) is not None:
            return blocked
        serializer = PlanInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        def create():
            plan = services.create_draft(business, agent_name=request.auth.name, summary=data.get("summary", ""), reassess_on=data.get("reassess_on"))
            return Response(services.plan_dict(plan, internal=True), status=status.HTTP_201_CREATED)

        return self.run(create)


class AgentSitePlanView(_PlanWriteView):
    """PATCH a draft's summary / reassess_on."""

    def patch(self, request, business_id, plan_id):
        business = get_business_or_404(request.user, business_id)
        plan = get_object_or_404(SitePlan, pk=plan_id, business=business)
        serializer = PlanInputSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        return self.run(lambda: Response(services.plan_dict(services.update_plan(plan, dict(serializer.validated_data), agent_name=request.auth.name), internal=True)))


class AgentSitePlanActionView(_PlanWriteView):
    """POST .../submit/ sends the draft for approval; POST .../withdraw/ abandons a draft or submitted plan."""

    def post(self, request, business_id, plan_id, action):
        business = get_business_or_404(request.user, business_id)
        if (blocked := self.guard(request, business)) is not None:
            return blocked
        plan = get_object_or_404(SitePlan, pk=plan_id, business=business)
        fn = {"submit": services.submit, "withdraw": services.withdraw}.get(action)
        if fn is None:
            return Response({"detail": "Unknown action. Use submit or withdraw."}, status=status.HTTP_404_NOT_FOUND)
        return self.run(lambda: Response(services.plan_dict(fn(plan, agent_name=request.auth.name), internal=True)))


class AgentPlanTasksView(_PlanWriteView):
    """POST a task to a draft plan."""

    def post(self, request, business_id, plan_id):
        business = get_business_or_404(request.user, business_id)
        if (blocked := self.guard(request, business)) is not None:
            return blocked
        plan = get_object_or_404(SitePlan, pk=plan_id, business=business)
        serializer = TaskInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return self.run(lambda: Response(services.task_dict(services.add_task(plan, dict(serializer.validated_data), agent_name=request.auth.name), internal=True), status=status.HTTP_201_CREATED))


class AgentPlanTaskView(_PlanWriteView):
    """PATCH: edit a draft's task, or record progress on an approved plan's task. DELETE: remove a draft's task."""

    def _task(self, business, task_id):
        return get_object_or_404(PlanTask.objects.select_related("plan", "feature"), pk=task_id, plan__business=business)

    def patch(self, request, business_id, task_id):
        business = get_business_or_404(request.user, business_id)
        if (blocked := self.guard(request, business)) is not None:
            return blocked
        task = self._task(business, task_id)
        serializer = TaskUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        return self.run(lambda: Response(services.task_dict(services.update_task(task, dict(serializer.validated_data), agent_name=request.auth.name), internal=True)))

    def delete(self, request, business_id, task_id):
        business = get_business_or_404(request.user, business_id)
        task = self._task(business, task_id)

        def remove():
            services.delete_task(task, agent_name=request.auth.name)
            return Response(status=status.HTTP_204_NO_CONTENT)

        return self.run(remove)
