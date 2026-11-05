from celery import shared_task


@shared_task
def apply_due_plan_changes() -> int:
    from .services import apply_due_plan_changes as run

    return run()
