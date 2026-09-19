from django.utils import timezone

from .models import (
    AssignmentSectionMembership,
    ChecklistDefinition,
    ChecklistSection,
    Program,
    ProgramMembership,
    SectionTaskMembership,
    Shift,
    StaffCategory,
    StaffMember,
    TaskDefinition,
)


EXPORT_FORMAT_VERSION = 1
APPLICATION_CONFIG_VERSION = "phase-5"


def build_configuration_export():
    """Return deterministic, secret-free administrative configuration data."""
    return {
        "metadata": {
            "format": "chore-administrative-configuration",
            "schema_version": EXPORT_FORMAT_VERSION,
            "application_config_version": APPLICATION_CONFIG_VERSION,
            "exported_at": timezone.now().isoformat(),
        },
        "programs": [
            {"id": row.pk, "name": row.name, "slug": row.slug, "is_active": row.is_active}
            for row in Program.objects.order_by("slug", "pk")
        ],
        "positions": [
            {
                "id": row.pk, "program_slug": row.program.slug, "name": row.name,
                "slug": row.slug, "sort_order": row.sort_order, "is_active": row.is_active,
            }
            for row in StaffCategory.objects.select_related("program").order_by("program__slug", "sort_order", "slug", "pk")
        ],
        "shifts": [
            {
                "id": row.pk, "name": row.name, "start_time": row.start_time.isoformat(),
                "end_time": row.end_time.isoformat(), "sort_order": row.sort_order,
                "is_active": row.is_active,
            }
            for row in Shift.objects.order_by("sort_order", "name", "pk")
        ],
        "shift_assignments": [
            {
                "id": row.pk, "name": row.name, "position_slug": row.category.slug,
                "program_slug": row.category.program.slug, "shift": row.shift.name,
                "sort_order": row.sort_order, "weekdays_only": row.weekdays_only,
                "is_active": row.is_active,
            }
            for row in ChecklistDefinition.objects.select_related("category__program", "shift").order_by("category__program__slug", "category__sort_order", "shift__sort_order", "pk")
        ],
        "sections": [
            {"id": row.pk, "name": row.name, "is_active": row.is_active}
            for row in ChecklistSection.objects.order_by("name", "pk")
        ],
        "tasks": [
            {
                "id": row.pk, "label": row.label, "allow_na": row.allow_na,
                "requires_completion_note": row.requires_completion_note,
                "is_active": row.is_active,
                "scheduled_start": row.scheduled_start.isoformat() if row.scheduled_start else None,
                "scheduled_end": row.scheduled_end.isoformat() if row.scheduled_end else None,
            }
            for row in TaskDefinition.objects.order_by("label", "pk")
        ],
        "assignment_sections": [
            {"assignment_id": row.assignment_id, "section_id": row.section_id, "sort_order": row.sort_order}
            for row in AssignmentSectionMembership.objects.order_by("assignment_id", "sort_order", "pk")
        ],
        "section_tasks": [
            {"section_id": row.section_id, "task_id": row.task_id, "sort_order": row.sort_order}
            for row in SectionTaskMembership.objects.order_by("section_id", "sort_order", "pk")
        ],
        "staff": [
            {
                "id": row.pk, "program_slug": row.program.slug,
                "first_name": row.first_name, "last_name": row.last_name,
                "is_active": row.is_active,
            }
            for row in StaffMember.objects.select_related("program").order_by("program__slug", "first_name", "last_name", "pk")
        ],
        "program_assignments": [
            {
                "account_username": row.user.username,
                "program_slug": row.program.slug,
                "role": row.role,
                "is_active": row.is_active,
                "receive_scheduled_reports": row.receive_scheduled_reports,
            }
            for row in ProgramMembership.objects.select_related("user", "program").order_by("program__slug", "user__username", "pk")
        ],
    }
