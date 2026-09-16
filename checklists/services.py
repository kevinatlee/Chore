import time

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, OperationalError, connection, transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    ChecklistInstance,
    ChecklistItem,
    StaffAssignment,
    StaffContribution,
    TaskState,
)


SQLITE_LOCK_ATTEMPTS = 5
SQLITE_LOCK_BACKOFF_SECONDS = 0.05


def _run_serialized_write(operation):
    """Run a write with bounded recovery from transient SQLite lock errors."""
    attempts = SQLITE_LOCK_ATTEMPTS if connection.vendor == "sqlite" else 1
    for attempt in range(attempts):
        try:
            return operation()
        except OperationalError as exc:
            is_locked = "locked" in str(exc).lower()
            if not is_locked or attempt == attempts - 1:
                raise
            time.sleep(SQLITE_LOCK_BACKOFF_SECONDS * (attempt + 1))


def _configuration_is_active(definition):
    return definition.is_active and definition.category.is_active and definition.shift.is_active


def resolve_checklist(definition, operational_date):
    """Return the single shared checklist, lazily snapshotting it when first opened."""
    if not _configuration_is_active(definition):
        raise PermissionDenied("This checklist configuration is inactive.")
    lookup = {
        "operational_date": operational_date,
        "category": definition.category,
        "shift": definition.shift,
    }

    def write():
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
                sections = definition.sections.filter(is_active=True).order_by(
                    "sort_order", "id"
                )
                items = []
                for section in sections:
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

    try:
        return _run_serialized_write(write)
    except IntegrityError:
        # A concurrent request may have won the unique(date, category, shift) race.
        return ChecklistInstance.objects.get(**lookup)


def change_item_state(*, item_id, staff, new_state):
    if not staff.is_authenticated or not staff.is_active:
        raise PermissionDenied("An active staff account is required.")
    if new_state not in TaskState.values:
        raise ValidationError({"state": "Unknown checklist state."})

    def write():
        with transaction.atomic():
            item = (
                ChecklistItem.objects.select_for_update()
                .select_related(
                    "instance__category",
                    "instance__shift",
                    "instance__definition__category",
                    "instance__definition__shift",
                    "source_task__section",
                )
                .get(pk=item_id)
            )
            definition = item.instance.definition
            configuration_is_active = (
                _configuration_is_active(definition)
                and item.source_task.is_active
                and item.source_task.section.is_active
            )
            if not configuration_is_active:
                raise PermissionDenied("This checklist configuration is inactive.")
            if (
                definition.category_id != item.instance.category_id
                or definition.shift_id != item.instance.shift_id
            ):
                raise PermissionDenied("Checklist configuration identity is inconsistent.")
            if not staff.is_staff and not StaffAssignment.objects.filter(
                user=staff,
                category_id=item.instance.category_id,
                is_active=True,
            ).exists():
                raise PermissionDenied("This staff account is not assigned to the category.")
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

    return _run_serialized_write(write)
