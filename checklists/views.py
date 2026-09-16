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

from .models import (
    ChecklistDefinition,
    ChecklistItem,
    StaffContribution,
    StaffMember,
    TaskState,
)
from .services import (
    can_operate_program,
    change_item_state,
    programs_for_operations,
    resolve_checklist,
)
from .reporting import build_report, period_from_params, programs_for_reporting, selected_program


def _selected_date(request):
    raw_date = request.GET.get("date") or request.POST.get("date")
    if not raw_date:
        return timezone.localdate()
    try:
        return date.fromisoformat(raw_date)
    except ValueError as exc:
        raise ValidationError("Use a valid date in YYYY-MM-DD format.") from exc


@login_required
def home(request):
    if request.user.is_superuser:
        return redirect("reports")
    if programs_for_reporting(request.user).exists():
        return redirect("reports")
    if programs_for_operations(request.user).exists():
        return redirect("dashboard")
    raise PermissionDenied("This account has no active Chore access.")


@login_required
def dashboard(request):
    available_programs = programs_for_operations(request.user)
    raw_program = request.GET.get("program")
    try:
        program_id = int(raw_program) if raw_program else None
    except ValueError:
        program_id = None
    program = available_programs.filter(pk=program_id).first() if program_id else available_programs.first()
    if program is None:
        raise PermissionDenied("Operational-entry access is required.")
    try:
        operational_date = _selected_date(request)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        operational_date = timezone.localdate()

    definitions = ChecklistDefinition.objects.filter(
        is_active=True,
        category__program=program,
        category__is_active=True,
        category__program__is_active=True,
        shift__is_active=True,
    ).select_related("category", "shift").order_by(
        "category__sort_order", "shift__sort_order"
    )
    categories = []
    seen_categories = set()
    for definition in definitions:
        if definition.category_id not in seen_categories:
            categories.append(definition.category)
            seen_categories.add(definition.category_id)
    try:
        selected_category_id = int(request.GET.get("category", ""))
    except ValueError:
        selected_category_id = categories[0].pk if categories else None
    if selected_category_id not in seen_categories:
        selected_category_id = categories[0].pk if categories else None
    return render(
        request,
        "checklists/dashboard.html",
        {
            "available_programs": available_programs,
            "program": program,
            "operational_date": operational_date,
            "staff_members": StaffMember.objects.filter(program=program, is_active=True),
            "categories": categories,
            "definitions": definitions,
            "selected_definitions": definitions.filter(
                category_id=selected_category_id
            ),
            "selected_category_id": selected_category_id,
        },
    )


@login_required
def open_checklist(request):
    try:
        operational_date = _selected_date(request)
    except ValidationError as exc:
        return HttpResponseBadRequest(" ".join(exc.messages))
    definition = get_object_or_404(
        ChecklistDefinition.objects.select_related("category__program", "shift"),
        category_id=request.GET.get("category"),
        shift_id=request.GET.get("shift"),
        is_active=True,
        category__is_active=True,
        category__program__is_active=True,
        shift__is_active=True,
    )
    if not can_operate_program(request.user, definition.category.program_id):
        raise PermissionDenied("Operational-entry access is required for this Program.")
    staff_member = get_object_or_404(
        StaffMember,
        pk=request.GET.get("staff"),
        program=definition.category.program,
        is_active=True,
    )
    resolve_checklist(definition, operational_date)
    target = reverse("checklist-detail", args=(definition.pk,))
    return redirect(
        f"{target}?date={operational_date.isoformat()}&staff={staff_member.pk}"
    )


@login_required
def checklist_detail(request, definition_id):
    definition = get_object_or_404(
        ChecklistDefinition.objects.select_related("category__program", "shift"),
        pk=definition_id,
        is_active=True,
        category__is_active=True,
        category__program__is_active=True,
        shift__is_active=True,
    )
    if not can_operate_program(request.user, definition.category.program_id):
        raise PermissionDenied("Operational-entry access is required for this Program.")
    staff_member = get_object_or_404(
        StaffMember,
        pk=request.GET.get("staff"),
        program=definition.category.program,
        is_active=True,
    )
    try:
        operational_date = _selected_date(request)
    except ValidationError as exc:
        return HttpResponseBadRequest(" ".join(exc.messages))

    instance = resolve_checklist(definition, operational_date)
    items = instance.items.select_related("current_staff").order_by(
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
            "staff_member": staff_member,
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
            item_id=item_id,
            actor=request.user,
            staff_member=get_object_or_404(
                StaffMember,
                pk=request.POST.get("staff"),
                program=item.instance.program,
                is_active=True,
            ),
            new_state=request.POST.get("state", ""),
        )
    except ValidationError as exc:
        return HttpResponseBadRequest(" ".join(exc.messages))

    if contribution:
        messages.success(request, "Task updated and Staff Contribution recorded.")
    target = reverse("checklist-detail", args=(item.instance.definition_id,))
    return redirect(
        f"{target}?date={item.instance.operational_date.isoformat()}&staff={request.POST.get('staff')}"
    )


def _report_for_request(request):
    program = selected_program(request.user, request.GET.get("program"))
    if program is None:
        raise PermissionDenied("Manager reporting access is required.")
    period = period_from_params(request.GET, timezone.localdate())
    report = build_report(
        program=program,
        period=period,
        filters=request.GET,
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
