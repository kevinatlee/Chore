import time

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, OperationalError, connection, transaction
from django.utils import timezone

from .models import (
    AssignmentSectionMembership,
    ChecklistInstance,
    ChecklistItem,
    DiscrepancyExplanation,
    ProgramRole,
    SectionTaskMembership,
    StaffContribution,
    StaffMember,
    TaskState,
)
from .operational_dates import validate_operational_entry_date


SQLITE_LOCK_ATTEMPTS = 5
SQLITE_LOCK_BACKOFF_SECONDS = 0.05


def programs_for_operations(user):
    from .models import Program

    if not user.is_authenticated or not user.is_active:
        return Program.objects.none()
    if user.is_superuser:
        return Program.objects.filter(is_active=True)
    return Program.objects.filter(
        is_active=True,
        memberships__user=user,
        memberships__role=ProgramRole.OPERATIONAL,
        memberships__is_active=True,
    ).distinct()


def can_operate_program(user, program_id):
    return programs_for_operations(user).filter(pk=program_id).exists()


def can_view_contributor_audit(user, program_id):
    if not user.is_authenticated or not user.is_active:
        return False
    if user.is_superuser:
        return True
    return user.program_memberships.filter(
        program_id=program_id,
        role=ProgramRole.MANAGER,
        is_active=True,
    ).exists()


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
    return (
        definition.is_active
        and definition.category.is_active
        and definition.category.program.is_active
        and definition.shift.is_active
    )


def definition_available_on_date(definition, operational_date):
    """Return whether a Shift Assignment permits operational use on this date."""
    return not definition.weekdays_only or operational_date.weekday() < 5


def configured_tasks_for_definition(definition):
    """Return active configured tasks in snapshot order, deduplicated by task."""
    memberships = (
        AssignmentSectionMembership.objects.filter(
            assignment=definition,
            section__is_active=True,
        )
        .select_related("section")
        .order_by("sort_order", "id")
    )
    configured_tasks = []
    seen_task_ids = set()
    for section_membership in memberships:
        section = section_membership.section
        task_memberships = (
            SectionTaskMembership.objects.filter(
                section=section,
                task__is_active=True,
            )
            .select_related("task")
            .order_by("sort_order", "id")
        )
        for task_membership in task_memberships:
            task = task_membership.task
            if task.pk in seen_task_ids:
                continue
            seen_task_ids.add(task.pk)
            configured_tasks.append(
                {
                    "task": task,
                    "section": section,
                    "section_order": section_membership.sort_order,
                    "task_order": task_membership.sort_order,
                }
            )
    return configured_tasks


def resolve_checklist(definition, operational_date):
    """Return the single shared Chore List, lazily snapshotting it when first opened."""
    if not _configuration_is_active(definition):
        raise PermissionDenied("This Chore List configuration is inactive.")
    if not definition_available_on_date(definition, operational_date):
        raise PermissionDenied("This Chore List is not available on this date.")
    lookup = {
        "program": definition.category.program,
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
                items = []
                for configured_task in configured_tasks_for_definition(definition):
                    task = configured_task["task"]
                    items.append(
                        ChecklistItem(
                            instance=instance,
                            source_task=task,
                            section_name_snapshot=configured_task["section"].name,
                            section_order_snapshot=configured_task["section_order"],
                            task_label_snapshot=task.label,
                            task_order_snapshot=configured_task["task_order"],
                            allow_na_snapshot=task.allow_na,
                            requires_completion_note_snapshot=task.requires_completion_note,
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


def change_item_state(
    *, item_id, actor, staff_member, new_state, activity_text="", system=False
):
    if not system and (
        actor is None or not actor.is_authenticated or not actor.is_active
    ):
        raise PermissionDenied("An active application account is required.")
    if new_state not in TaskState.values:
        raise ValidationError({"state": "Unknown task state."})
    normalized_activity_text = (activity_text or "").strip()

    def write():
        with transaction.atomic():
            item = (
                ChecklistItem.objects.select_for_update()
                .select_related(
                    "instance__category",
                    "instance__shift",
                    "instance__definition__category",
                    "instance__definition__shift",
                    "instance__program",
                    "source_task",
                )
                .get(pk=item_id)
            )
            definition = item.instance.definition
            if not system:
                validate_operational_entry_date(item.instance.operational_date)
            configuration_is_active = (
                _configuration_is_active(definition)
                and definition_available_on_date(
                    definition, item.instance.operational_date
                )
                and item.source_task.is_active
                and SectionTaskMembership.objects.filter(
                    task=item.source_task,
                    section__is_active=True,
                    section__assignment_memberships__assignment=definition,
                ).exists()
            )
            if not configuration_is_active:
                raise PermissionDenied("This Chore List configuration is inactive.")
            if (
                definition.category_id != item.instance.category_id
                or definition.shift_id != item.instance.shift_id
                or definition.category.program_id != item.instance.program_id
            ):
                raise PermissionDenied("Chore List configuration identity is inconsistent.")
            if not system:
                operational_access = actor.program_memberships.filter(
                    program_id=item.instance.program_id,
                    role=ProgramRole.OPERATIONAL,
                    is_active=True,
                ).exists()
                if not actor.is_superuser and not operational_access:
                    raise PermissionDenied(
                        "This account is not authorized for operational entry."
                    )
            try:
                selected_staff = StaffMember.objects.get(pk=staff_member.pk)
            except (AttributeError, StaffMember.DoesNotExist) as exc:
                raise ValidationError({"staff": "Select a valid staff member."}) from exc
            if (
                not selected_staff.is_active
                or selected_staff.program_id != item.instance.program_id
            ):
                raise PermissionDenied(
                    "The selected staff member is not active in this Program."
                )
            if new_state == TaskState.NOT_APPLICABLE and not item.allow_na_snapshot:
                raise ValidationError({"state": "This task cannot be marked N/A."})
            if (
                new_state == TaskState.COMPLETED
                and item.requires_completion_note_snapshot
                and not normalized_activity_text
            ):
                raise ValidationError(
                    {"activity_text": "Describe the programming activity before completing this task."}
                )
            if new_state == item.current_state:
                return item, None

            contribution = StaffContribution.objects.create(
                item=item,
                staff=selected_staff,
                recorded_by=None if system else actor,
                previous_state=item.current_state,
                new_state=new_state,
                activity_text=(
                    normalized_activity_text
                    if new_state == TaskState.COMPLETED
                    and item.requires_completion_note_snapshot
                    else ""
                ),
            )
            item.current_state = new_state
            item.current_staff = selected_staff
            item.state_changed_at = timezone.now()
            item.save(
                update_fields=(
                    "current_state",
                    "current_staff",
                    "state_changed_at",
                    "updated_at",
                )
            )
            return item, contribution

    return _run_serialized_write(write)


def save_discrepancy_explanation(*, instance, actor, staff_member, explanation):
    if actor is None or not actor.is_authenticated or not actor.is_active:
        raise PermissionDenied("An active application account is required.")
    validate_operational_entry_date(instance.operational_date)
    if not definition_available_on_date(
        instance.definition, instance.operational_date
    ):
        raise PermissionDenied("This Chore List is not available on this date.")
    if not can_operate_program(actor, instance.program_id):
        raise PermissionDenied("Operational-entry access is required for this Program.")
    try:
        selected_staff = StaffMember.objects.get(pk=staff_member.pk)
    except (AttributeError, StaffMember.DoesNotExist) as exc:
        raise ValidationError({"staff": "Select a valid staff member."}) from exc
    if not selected_staff.is_active or selected_staff.program_id != instance.program_id:
        raise PermissionDenied("The selected staff member is not active in this Program.")
    explanation = (explanation or "").strip()
    if not explanation:
        return None

    def write():
        with transaction.atomic():
            locked_instance = ChecklistInstance.objects.select_for_update().get(pk=instance.pk)
            record, _ = DiscrepancyExplanation.objects.update_or_create(
                instance=locked_instance,
                staff=selected_staff,
                defaults={"recorded_by": actor, "explanation": explanation},
            )
            return record

    return _run_serialized_write(write)
