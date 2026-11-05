from rest_framework import serializers

from .models import PlanTask


class PlanInputSerializer(serializers.Serializer):
    summary = serializers.CharField(max_length=4000, required=False, allow_blank=True)
    reassess_on = serializers.DateField(required=False, allow_null=True)


class TaskInputSerializer(serializers.Serializer):
    feature_id = serializers.CharField(max_length=10)
    title = serializers.CharField(max_length=200)
    description = serializers.CharField(max_length=4000, required=False, allow_blank=True)
    target = serializers.CharField(max_length=500, required=False, allow_blank=True)
    priority = serializers.ChoiceField(choices=PlanTask.Level.values, required=False)
    risk = serializers.ChoiceField(choices=PlanTask.Level.values, required=False)
    completion_criteria = serializers.CharField(max_length=2000)
    scope_justification = serializers.CharField(max_length=2000, required=False, allow_blank=True)
    dependencies = serializers.CharField(max_length=2000, required=False, allow_blank=True)
    planned_start = serializers.DateField(required=False, allow_null=True)
    planned_end = serializers.DateField(required=False, allow_null=True)
    estimate_minutes = serializers.IntegerField(required=False, allow_null=True, min_value=1, max_value=2400)
    position = serializers.IntegerField(required=False, min_value=0)


class TaskUpdateSerializer(TaskInputSerializer):
    """Every field optional. In a draft, details change; in an approved plan, only progress fields are accepted."""

    feature_id = None
    title = serializers.CharField(max_length=200, required=False)
    completion_criteria = serializers.CharField(max_length=2000, required=False)
    status = serializers.ChoiceField(choices=PlanTask.Status.values, required=False)
    status_note = serializers.CharField(max_length=2000, required=False, allow_blank=True)
    completion_evidence = serializers.CharField(max_length=4000, required=False, allow_blank=True)


class DecisionSerializer(serializers.Serializer):
    decision = serializers.ChoiceField(choices=["approve", "request_changes", "reject"])
    note = serializers.CharField(max_length=2000, required=False, allow_blank=True)
