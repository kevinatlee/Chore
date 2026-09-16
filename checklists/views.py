import csv
from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.db.models import Q

from .models import (
    ChecklistDefinition,
    ChecklistItem,
    ProgramRole,
    StaffAssignment,
    StaffContribution,
    TaskState,
)
from .services import change_item_state, resolve_checklist
from .reporting import build_report, period_from_params, programs_for_reporting, selected_program


def _selected_date(request):
    raw_date = request.GET.get("date") or request.POST.get("date")
    if not raw_date:
        return timezone.localdate()
    try:
        return date.fromisoformat(raw_date)
    except ValueError as exc:
        raise ValidationError("Use a valid date in YYYY-MM-DD format.") from exc


def _can_access_category(user, category_id):
    if user.is_superuser:
        return True
    if user.is_staff and user.program_memberships.filter(
        program__staff_categories__id=category_id,
        role=ProgramRole.MANAGER,
        is_active=True,
    ).exists():
        return True
    return StaffAssignment.objects.filter(
        user=user, category_id=category_id, is_active=True
    ).exists()


@login_required
def dashboard(request):
    try:
        operational_date = _selected_date(request)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        operational_date = timezone.localdate()

    definitions = ChecklistDefinition.objects.filter(
        is_active=True,
        category__is_active=True,
        category__program__is_active=True,
        shift__is_active=True,
    ).select_related("category", "shift")
    if not request.user.is_superuser:
        definitions = definitions.filter(
            Q(
                category__staff_assignments__user=request.user,
                category__staff_assignments__is_active=True,
            )
            | Q(
                category__program__memberships__user=request.user,
                category__program__memberships__role=ProgramRole.MANAGER,
                category__program__memberships__is_active=True,
            )
        )
    return render(
        request,
        "checklists/dashboard.html",
        {"definitions": definitions.distinct(), "operational_date": operational_date},
    )


@login_required
def checklist_detail(request, definition_id):
    definition = get_object_or_404(
        ChecklistDefinition.objects.select_related("category", "shift"),
        pk=definition_id,
        is_active=True,
        category__is_active=True,
        category__program__is_active=True,
        shift__is_active=True,
    )
    if not _can_access_category(request.user, definition.category_id):
        from django.core.exceptions import PermissionDenied

        raise PermissionDenied
    try:
        operational_date = _selected_date(request)
    except ValidationError as exc:
        return HttpResponseBadRequest(" ".join(exc.messages))

    instance = resolve_checklist(definition, operational_date)
    items = instance.items.select_related("current_contributor").order_by(
        "section_order_snapshot", "task_order_snapshot", "scheduled_start_snapshot", "id"
    )
    sections = []
    for item in items:
        if not sections or sections[-1]["name"] != item.section_name_snapshot:
            sections.append({"name": item.section_name_snapshot, "items": []})
        sections[-1]["items"].append(item)

    contributions = (
        StaffContribution.objects.filter(item__instance=instance)
        .select_related("staff", "item")
        .order_by("-created_at", "-id")[:10]
    )
    total_count = sum(len(section["items"]) for section in sections)
    completed_count = sum(
        item.current_state != TaskState.PENDING
        for section in sections
        for item in section["items"]
    )
    return render(
        request,
        "checklists/checklist_detail.html",
        {
            "instance": instance,
            "sections": sections,
            "contributions": contributions,
            "states": TaskState,
            "total_count": total_count,
            "completed_count": completed_count,
        },
    )


@login_required
@require_POST
def update_item_state(request, item_id):
    item = get_object_or_404(
        ChecklistItem.objects.select_related("instance__category", "instance__definition"),
        pk=item_id,
    )
    try:
        _, contribution = change_item_state(
            item_id=item_id, staff=request.user, new_state=request.POST.get("state", "")
        )
    except ValidationError as exc:
        return HttpResponseBadRequest(" ".join(exc.messages))

    if contribution:
        messages.success(request, "Task updated and Staff Contribution recorded.")
    target = reverse("checklist-detail", args=(item.instance.definition_id,))
    return redirect(f"{target}?date={item.instance.operational_date.isoformat()}")


def _report_for_request(request):
    program = selected_program(request.user, request.GET.get("program"))
    if program is None:
        raise PermissionDenied("Manager reporting access is required.")
    period = period_from_params(request.GET, timezone.localdate())
    include_test = request.user.is_superuser and request.GET.get("include_test") == "1"
    report = build_report(
        program=program,
        period=period,
        filters=request.GET,
        include_test=include_test,
    )
    report["available_programs"] = programs_for_reporting(request.user)
    report["query_string"] = request.GET.urlencode()
    return report


@login_required
def reports(request):
    return render(request, "checklists/report.html", _report_for_request(request))


@login_required
def report_detail(request, instance_id):
    report = _report_for_request(request)
    report["row"] = next((row for row in report["rows"] if row["id"] == instance_id), None)
    if report["row"] is None:
        raise PermissionDenied("This checklist is outside the authorized report.")
    return render(request, "checklists/report_detail.html", report)


@login_required
def report_print(request):
    return render(request, "checklists/report_print.html", _report_for_request(request))


@login_required
def report_csv(request):
    report = _report_for_request(request)
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    filename = f"chore-{report['program'].slug}-{report['period'].start}-{report['period'].end}.csv"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    writer = csv.writer(response)
    writer.writerow(
        [
            "Operational date",
            "Staff category",
            "Shift",
            "Checklist state",
            "Applicable tasks",
            "Completed tasks",
            "N/A tasks",
            "Pending tasks",
            "Completion percentage",
            "Checklist contributors",
            "Section",
            "Task",
            "Task state",
            "Latest contributor",
            "Latest contribution timestamp",
        ]
    )
    for row in report["rows"]:
        for task in row["filtered_tasks"]:
            writer.writerow(
                [
                    row["operational_date"].isoformat(),
                    row["category"],
                    row["shift"],
                    row["status"],
                    row["applicable_count"],
                    row["completed_count"],
                    row["na_count"],
                    row["pending_count"],
                    row["completion_percentage"],
                    "; ".join(row["contributors"]),
                    task["section"],
                    task["label"],
                    task["state_label"],
                    task["contributor"],
                    task["changed_at"].isoformat() if task["changed_at"] else "",
                ]
            )
    return response
