from calendar import monthrange
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from django.db.models import Subquery

from .presentation import display_task_text
from .models import (
    ChecklistDefinition,
    ChecklistInstance,
    ChecklistItem,
    DiscrepancyExplanation,
    Program,
    ProgramRole,
    Shift,
    StaffCategory,
    StaffContribution,
    StaffMember,
    TaskState,
)
from .services import configured_tasks_for_definition, definition_available_on_date


@dataclass(frozen=True)
class ReportPeriod:
    kind: str
    start: date
    end: date
    label: str
    selected_date: date | None = None


def period_from_params(params, today=None):
    today = today or date.today()
    kind = params.get("period", "daily")
    if kind == "daily":
        selected = _date(params.get("date"), today)
        return ReportPeriod(
            kind,
            selected,
            selected,
            f"{selected:%B} {selected.day}, {selected.year}",
            selected,
        )
    if kind == "weekly":
        selected = _date(params.get("date"), today)
        start = selected - timedelta(days=selected.weekday())
        end = start + timedelta(days=6)
        return ReportPeriod(
            kind,
            start,
            end,
            f"{start:%b %d} – {end:%b %d, %Y}",
            selected,
        )
    if kind == "monthly":
        selected = _date(params.get("date"), None) or _legacy_month_date(
            params.get("month"), today
        )
        start = selected.replace(day=1)
        end = start.replace(day=monthrange(start.year, start.month)[1])
        return ReportPeriod(kind, start, end, start.strftime("%B %Y"), selected)
    if kind == "annual":
        selected = _date(params.get("date"), None) or _legacy_year_date(
            params.get("year"), today
        )
        start = date(selected.year, 1, 1)
        return ReportPeriod(
            kind,
            start,
            date(start.year, 12, 31),
            str(start.year),
            selected,
        )
    if kind == "rolling365":
        end = _date(params.get("date"), today)
        start = end - timedelta(days=364)
        return ReportPeriod(
            kind,
            start,
            end,
            f"{start:%b %d, %Y} – {end:%b %d, %Y}",
            end,
        )
    return period_from_params({"period": "daily", "date": today.isoformat()}, today)


def scheduled_periods(local_day):
    periods = [("daily", ReportPeriod("daily", local_day - timedelta(days=1), local_day - timedelta(days=1), ""))]
    if local_day.weekday() == 0:
        end = local_day - timedelta(days=1)
        periods.append(("weekly", ReportPeriod("weekly", end - timedelta(days=6), end, "")))
    if local_day.day == 1:
        end = local_day - timedelta(days=1)
        start = end.replace(day=1)
        periods.append(("monthly", ReportPeriod("monthly", start, end, "")))
    if local_day.month == 1 and local_day.day == 1:
        year = local_day.year - 1
        periods.append(("annual", ReportPeriod("annual", date(year, 1, 1), date(year, 12, 31), "")))
    return periods


def programs_for_reporting(user):
    if not user.is_authenticated or not user.is_active:
        return Program.objects.none()
    if user.is_superuser:
        return Program.objects.all()
    return Program.objects.filter(
        memberships__user=user,
        memberships__role=ProgramRole.MANAGER,
        memberships__is_active=True,
    ).distinct()


def selected_program(user, raw_program=None):
    programs = programs_for_reporting(user)
    if raw_program:
        program_id = _positive_int(raw_program)
        if program_id:
            return programs.filter(pk=program_id).first()
        return programs.filter(slug=raw_program).first()
    return programs.first()


def build_report(*, program, period, filters=None):
    filters = filters or {}
    instances = (
        ChecklistInstance.objects.filter(
            program=program,
            operational_date__range=(period.start, period.end),
        )
        .exclude(
            definition__weekdays_only=True,
            operational_date__week_day__in=(1, 7),
        )
        .select_related("definition", "category", "shift")
        .order_by("operational_date", "definition__sort_order", "definition_id", "id")
    )
    expected_definitions = (
        ChecklistDefinition.objects.filter(
            is_active=True,
            category__program=program,
            category__is_active=True,
            shift__is_active=True,
        )
        .select_related("category", "shift")
        .order_by("sort_order", "id")
    )

    category_id = _positive_int(filters.get("category"))
    if category_id:
        if StaffCategory.objects.filter(pk=category_id, program=program).exists():
            instances = instances.filter(category_id=category_id)
            expected_definitions = expected_definitions.filter(category_id=category_id)
        else:
            instances = instances.none()
            expected_definitions = expected_definitions.none()
    shift_id = _positive_int(filters.get("shift"))
    if shift_id:
        valid = ChecklistDefinition.objects.filter(
            category__program=program, shift_id=shift_id
        )
        if category_id:
            valid = valid.filter(category_id=category_id)
        if valid.exists():
            instances = instances.filter(shift_id=shift_id)
            expected_definitions = expected_definitions.filter(shift_id=shift_id)
        else:
            instances = instances.none()
            expected_definitions = expected_definitions.none()

    staff_id = _positive_int(filters.get("staff"))
    state_filter = filters.get("task_state", "")
    section_filter = filters.get("section", "").strip()
    status_filter = filters.get("status", "")
    rows = []
    totals = {
        "checklists": 0,
        "applicable": 0,
        "completed": 0,
        "na": 0,
        "pending": 0,
        "missing": 0,
    }

    # Nested prefetch expands every checklist-item id into one IN parameter
    # list. A full-year report exceeds SQLite's variable limit, so use fixed-
    # size subqueries and assemble the already-ordered records in memory.
    instance_ids = instances.values("pk").order_by()
    report_items = ChecklistItem.objects.filter(
        instance_id__in=Subquery(instance_ids)
    ).order_by("instance_id", "section_order_snapshot", "task_order_snapshot", "id")
    item_ids = report_items.values("pk").order_by()
    report_contributions = StaffContribution.objects.filter(
        item_id__in=Subquery(item_ids)
    ).select_related("staff").order_by("item_id", "created_at", "id")
    contributions_by_item = defaultdict(list)
    for contribution in report_contributions:
        contributions_by_item[contribution.item_id].append(contribution)
    items_by_instance = defaultdict(list)
    for item in report_items:
        items_by_instance[item.instance_id].append(item)
    discrepancies_by_instance = defaultdict(list)
    for discrepancy in DiscrepancyExplanation.objects.filter(
        instance_id__in=Subquery(instance_ids)
    ).select_related("staff").order_by("instance_id", "created_at", "id"):
        discrepancies_by_instance[discrepancy.instance_id].append(
            {
                "staff": discrepancy.staff.display_name,
                "staff_id": discrepancy.staff_id,
                "explanation": discrepancy.explanation,
                "created_at": discrepancy.created_at,
                "updated_at": discrepancy.updated_at,
            }
        )

    actual_instances = list(instances)
    expected_definitions = list(expected_definitions)
    expected_task_counts = {
        definition.pk: len(configured_tasks_for_definition(definition))
        for definition in expected_definitions
    }
    actual_by_expected_key = {
        (instance.operational_date, instance.definition_id): instance
        for instance in actual_instances
    }
    remaining_actual = {instance.pk: instance for instance in actual_instances}
    row_sources = []
    operational_date = period.start
    while operational_date <= period.end:
        for definition in expected_definitions:
            if not definition_available_on_date(definition, operational_date):
                continue
            instance = actual_by_expected_key.get((operational_date, definition.pk))
            if instance is not None:
                remaining_actual.pop(instance.pk, None)
            row_sources.append((operational_date, definition, instance))
        operational_date += timedelta(days=1)
    row_sources.extend(
        (instance.operational_date, instance.definition, instance)
        for instance in remaining_actual.values()
    )
    row_sources.sort(
        key=lambda source: (
            source[0],
            source[1].sort_order,
            source[1].pk,
            source[2].pk if source[2] is not None else 0,
        )
    )

    for operational_date, definition, instance in row_sources:
        if instance is None:
            if staff_id or state_filter in TaskState.values or section_filter:
                continue
            if status_filter in {"completed", "incomplete", "missing"} and status_filter != "missing":
                continue
            applicable = expected_task_counts[definition.pk]
            row = {
                "id": None,
                "definition_id": definition.pk,
                "operational_date": operational_date,
                "category": definition.category.name,
                "category_id": definition.category_id,
                "shift": definition.shift.name,
                "shift_id": definition.shift_id,
                "status": "missing",
                "applicable_count": applicable,
                "completed_count": 0,
                "na_count": 0,
                "pending_count": applicable,
                "completion_percentage": None,
                "contributors": [],
                "discrepancies": [],
                "tasks": [],
                "filtered_tasks": [],
            }
            rows.append(row)
            totals["checklists"] += 1
            totals["applicable"] += applicable
            totals["pending"] += applicable
            totals["missing"] += 1
            continue

        tasks = []
        checklist_contributors = {}
        for item in items_by_instance[instance.pk]:
            production_contributions = contributions_by_item[item.pk]
            effective_state = (
                production_contributions[-1].new_state
                if production_contributions
                else TaskState.PENDING
            )
            if effective_state == TaskState.NOT_APPLICABLE and not item.allow_na_snapshot:
                effective_state = TaskState.PENDING
            for contribution in production_contributions:
                checklist_contributors[contribution.staff_id] = _user_name(contribution.staff)
            tasks.append(
                {
                    "id": item.pk,
                    "section": item.section_name_snapshot,
                    "label": display_task_text(item.task_label_snapshot),
                    "state": effective_state,
                    "state_label": dict(TaskState.choices)[effective_state],
                    "allow_na": item.allow_na_snapshot,
                    "changed_at": production_contributions[-1].created_at if production_contributions else None,
                    "contributor": _user_name(production_contributions[-1].staff) if production_contributions else "",
                    "contributor_id": production_contributions[-1].staff_id if production_contributions else None,
                    "contributions": [
                        {
                            "staff": _user_name(contribution.staff),
                            "staff_id": contribution.staff_id,
                            "previous_state": contribution.previous_state,
                            "new_state": contribution.new_state,
                            "activity_text": contribution.activity_text,
                            "created_at": contribution.created_at,
                        }
                        for contribution in production_contributions
                    ],
                }
            )

        applicable = sum(task["state"] != TaskState.NOT_APPLICABLE for task in tasks)
        completed = sum(task["state"] == TaskState.COMPLETED for task in tasks)
        na_count = sum(task["state"] == TaskState.NOT_APPLICABLE for task in tasks)
        pending = sum(task["state"] == TaskState.PENDING for task in tasks)
        status = "completed" if pending == 0 else "incomplete"
        filtered_tasks = [
            task
            for task in tasks
            if (not staff_id or any(c["staff_id"] == staff_id for c in task["contributions"]))
            and (state_filter not in TaskState.values or task["state"] == state_filter)
            and (not section_filter or task["section"] == section_filter)
        ]
        if (staff_id or state_filter in TaskState.values or section_filter) and not filtered_tasks:
            continue
        if status_filter in {"completed", "incomplete", "missing"} and status != status_filter:
            continue

        row = {
            "id": instance.pk,
            "definition_id": instance.definition_id,
            "operational_date": instance.operational_date,
            "category": instance.category_name_snapshot,
            "category_id": instance.category_id,
            "shift": instance.shift_name_snapshot,
            "shift_id": instance.shift_id,
            "status": status,
            "applicable_count": applicable,
            "completed_count": completed,
            "na_count": na_count,
            "pending_count": pending,
            "completion_percentage": round((completed / applicable) * 100, 1) if applicable else 100.0,
            "contributors": list(checklist_contributors.values()),
            "discrepancies": discrepancies_by_instance[instance.pk],
            "tasks": tasks,
            "filtered_tasks": filtered_tasks,
        }
        rows.append(row)
        totals["checklists"] += 1
        totals["applicable"] += applicable
        totals["completed"] += completed
        totals["na"] += na_count
        totals["pending"] += pending

    totals["completion_percentage"] = (
        round((totals["completed"] / totals["applicable"]) * 100, 1)
        if totals["applicable"]
        else (100.0 if totals["checklists"] else 0.0)
    )
    category_choices = StaffCategory.objects.filter(program=program).order_by("sort_order", "name")
    shift_choices = Shift.objects.filter(
        is_active=True,
        checklist_definitions__is_active=True,
        checklist_definitions__category__program=program,
        checklist_definitions__category__is_active=True,
    )
    if category_id:
        shift_choices = shift_choices.filter(checklist_definitions__category_id=category_id)
    shift_options_by_position = {"": []}
    seen_all_shifts = set()
    valid_definitions = (
        ChecklistDefinition.objects.filter(
            is_active=True,
            category__program=program,
            category__is_active=True,
            shift__is_active=True,
        )
        .select_related("shift")
        .order_by("shift__sort_order", "shift__name", "shift_id")
    )
    for definition in valid_definitions:
        option = {"id": definition.shift_id, "name": definition.shift.name}
        position_options = shift_options_by_position.setdefault(
            str(definition.category_id), []
        )
        if not any(existing["id"] == definition.shift_id for existing in position_options):
            position_options.append(option)
        if definition.shift_id not in seen_all_shifts:
            shift_options_by_position[""].append(option)
            seen_all_shifts.add(definition.shift_id)
    staff_choices = StaffMember.objects.filter(
        program=program,
        staff_contributions__item__instance__program=program,
    ).distinct().order_by("first_name", "last_name", "id")
    sections = sorted(
        set(
            ChecklistInstance.objects.filter(program=program)
            .values_list("items__section_name_snapshot", flat=True)
            .exclude(items__section_name_snapshot__isnull=True)
        )
    )
    return {
        "program": program,
        "period": period,
        "rows": rows,
        "totals": totals,
        "filters": filters,
        "category_choices": category_choices,
        "shift_choices": shift_choices.distinct().order_by("sort_order", "name"),
        "shift_options_by_position": shift_options_by_position,
        "staff_choices": staff_choices,
        "section_choices": sections,
    }


def report_snapshot(report):
    return {
        "program": {"id": report["program"].pk, "name": report["program"].name},
        "period": {
            "kind": report["period"].kind,
            "start": report["period"].start.isoformat(),
            "end": report["period"].end.isoformat(),
        },
        "totals": report["totals"],
        "checklists": [
            {
                **{
                    key: value
                    for key, value in row.items()
                    if key not in {"tasks", "filtered_tasks", "discrepancies"}
                },
                "operational_date": row["operational_date"].isoformat(),
                "discrepancies": [
                    {
                        **{
                            key: value
                            for key, value in discrepancy.items()
                            if key not in {"created_at", "updated_at"}
                        },
                        "created_at": discrepancy["created_at"].isoformat(),
                        "updated_at": discrepancy["updated_at"].isoformat(),
                    }
                    for discrepancy in row["discrepancies"]
                ],
                "tasks": [
                    {
                        **{key: value for key, value in task.items() if key not in {"changed_at", "contributions"}},
                        "changed_at": task["changed_at"].isoformat() if task["changed_at"] else None,
                        "contributions": [
                            {
                                **{key: value for key, value in contribution.items() if key != "created_at"},
                                "created_at": contribution["created_at"].isoformat(),
                            }
                            for contribution in task["contributions"]
                        ],
                    }
                    for task in row["tasks"]
                ],
            }
            for row in report["rows"]
        ],
    }


def _date(raw, fallback):
    try:
        return date.fromisoformat(raw)
    except (TypeError, ValueError):
        return fallback


def _legacy_month_date(raw, fallback):
    try:
        year, month = (int(value) for value in raw.split("-", 1))
        return date(year, month, 1)
    except (AttributeError, TypeError, ValueError):
        return fallback


def _legacy_year_date(raw, fallback):
    try:
        return date(int(raw), 1, 1)
    except (TypeError, ValueError):
        return fallback


def _positive_int(raw):
    try:
        value = int(raw)
        return value if value > 0 else None
    except (TypeError, ValueError):
        return None


def _user_name(user):
    return user.display_name
