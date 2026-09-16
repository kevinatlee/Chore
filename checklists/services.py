from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    ChecklistInstance,
    ChecklistItem,
    StaffContribution,
    TaskState,
)


def resolve_checklist(definition, operational_date):
    """Return the single shared checklist, lazily snapshotting it when first opened."""
    lookup = {
        "operational_date": operational_date,
        "category": definition.category,
        "shift": definition.shift,
    }
    try:
        with transaction.atomic():
            instance, created = ChecklistInstance.objects.get_or_create(
                **lookup,
                defaults={
                    "definition": definition,
                    "category_name_snapshot": definition.category.name,
                    "shift_name_snapshot": definition.shift.name,
                    "shift_start_snapshot": definition.shift.start_time,
                    "shift_end_snapshot": definition.shift.end_time,
                },
            )
            if created:
                task_definitions = (
                    definition.sections.filter(is_active=True)
                    .prefetch_related("tasks")
                    .order_by("sort_order", "id")
                )
                items = []
                for section in task_definitions:
                    tasks = section.tasks.filter(is_active=True).filter(
                        Q(weekday__isnull=True) | Q(weekday=operational_date.weekday())
                    )
                    for task in tasks.order_by("sort_order", "weekday", "id"):
                        items.append(
                            ChecklistItem(
                                instance=instance,
                                source_task=task,
                                section_name_snapshot=section.name,
                                section_order_snapshot=section.sort_order,
                                task_label_snapshot=task.label,
                                task_order_snapshot=task.sort_order,
                                allow_na_snapshot=task.allow_na,
                                weekday_snapshot=task.weekday,
                                scheduled_start_snapshot=task.scheduled_start,
                                scheduled_end_snapshot=task.scheduled_end,
                            )
                        )
                ChecklistItem.objects.bulk_create(items)
            return instance
    except IntegrityError:
        # A concurrent request may have won the unique(date, category, shift) race.
        return ChecklistInstance.objects.get(**lookup)


def change_item_state(*, item_id, staff, new_state):
    if not staff.is_authenticated or not staff.is_active:
        raise PermissionDenied("An active staff account is required.")
    if new_state not in TaskState.values:
        raise ValidationError({"state": "Unknown checklist state."})

    with transaction.atomic():
        item = (
            ChecklistItem.objects.select_for_update()
            .select_related("instance")
            .get(pk=item_id)
        )
        if new_state == TaskState.NOT_APPLICABLE and not item.allow_na_snapshot:
            raise ValidationError({"state": "This task cannot be marked N/A."})
        if new_state == item.current_state:
            return item, None

        contribution = StaffContribution.objects.create(
            item=item,
            staff=staff,
            previous_state=item.current_state,
            new_state=new_state,
        )
        item.current_state = new_state
        item.current_contributor = staff
        item.state_changed_at = timezone.now()
        item.save(
            update_fields=(
                "current_state",
                "current_contributor",
                "state_changed_at",
                "updated_at",
            )
        )
        return item, contribution

