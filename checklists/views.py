import csv
import json
from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.admin.views.decorators import staff_member_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .models import (
    ChecklistDefinition,
    ChecklistInstance,
    ChecklistItem,
    DiscrepancyExplanation,
    StaffContribution,
    StaffMember,
    TaskState,
)
from .operational_dates import operational_entry_bounds, validate_operational_entry_date
from .presentation import display_task_text
from .configuration_export import build_configuration_export
from .services import (
    can_view_contributor_audit,
    can_operate_program,
    change_item_state,
    programs_for_operations,
    resolve_checklist,
    save_discrepancy_explanation,
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


def _selected_operational_date(request):
    return validate_operational_entry_date(_selected_date(request))


def _active_definition_for_operator(request, definition_id):
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
    return definition


def _checklist_revision(instance):
    latest_contribution_id = (
        StaffContribution.objects.filter(item__instance=instance)
        .order_by("-id")
        .values_list("id", flat=True)
        .first()
    )
    return str(latest_contribution_id or 0)


def _checklist_state_payload(instance, *, include_audit=False):
    items = list(
        instance.items.select_related("current_staff").order_by(
            "section_order_snapshot",
            "task_order_snapshot",
            "scheduled_start_snapshot",
            "id",
        )
    )
    contributions = list(
        StaffContribution.objects.filter(item__instance=instance)
        .select_related("staff", "item")
        .order_by("-created_at", "-id")[:10]
    )
    resolved_count = sum(item.current_state != TaskState.PENDING for item in items)
    return {
        "changed": True,
        "revision": str(contributions[0].id) if contributions else "0",
        "resolved_count": resolved_count,
        "total_count": len(items),
        "items": [
            ({
                "id": item.id,
                "state": item.current_state,
                "state_label": item.get_current_state_display(),
            } | ({
                "staff_name": item.current_staff.display_name if item.current_staff else "",
                "changed_at": item.state_changed_at.isoformat() if item.state_changed_at else "",
                "changed_at_label": (
                    timezone.localtime(item.state_changed_at)
                    .strftime("%b %d, %H:%M")
                    .replace(" 0", " ")
                    if item.state_changed_at
                    else ""
                ),
            } if include_audit else {}))
            for item in items
        ],
        "contributions": ([
            {
                "id": contribution.id,
                "staff_name": contribution.staff.display_name,
                "task_label": display_task_text(contribution.item.task_label_snapshot),
                "previous_state_label": contribution.get_previous_state_display(),
                "new_state_label": contribution.get_new_state_display(),
                "created_at": contribution.created_at.isoformat(),
                "created_at_label": timezone.localtime(contribution.created_at)
                .strftime("%b %d, %Y %H:%M")
                .replace(" 0", " "),
            }
            for contribution in contributions
        ] if include_audit else []),
    }


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
        operational_date = _selected_operational_date(request)
    except ValidationError as exc:
        return HttpResponseBadRequest(" ".join(exc.messages))

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
    staff_members = StaffMember.objects.filter(program=program, is_active=True)
    try:
        requested_staff_id = int(request.GET.get("staff", ""))
    except ValueError:
        requested_staff_id = None
    selected_staff_id = (
        requested_staff_id
        if requested_staff_id and staff_members.filter(pk=requested_staff_id).exists()
        else None
    )
    selected_definitions = definitions.filter(category_id=selected_category_id)
    shift_options_by_category = {}
    for definition in definitions:
        shift_options_by_category.setdefault(str(definition.category_id), []).append(
            {"id": definition.shift_id, "name": definition.shift.name}
        )
    valid_shift_ids = list(selected_definitions.values_list("shift_id", flat=True))
    try:
        requested_shift_id = int(request.GET.get("shift", ""))
    except ValueError:
        requested_shift_id = None
    selected_shift_id = (
        requested_shift_id
        if requested_shift_id in valid_shift_ids
        else next(iter(valid_shift_ids), None)
    )
    earliest_operational_date, latest_operational_date = operational_entry_bounds()
    return render(
        request,
        "checklists/dashboard.html",
        {
            "available_programs": available_programs,
            "program": program,
            "operational_date": operational_date,
            "earliest_operational_date": earliest_operational_date,
            "latest_operational_date": latest_operational_date,
            "staff_members": staff_members,
            "selected_staff_id": selected_staff_id,
            "categories": categories,
            "definitions": definitions,
            "selected_definitions": selected_definitions,
            "shift_options_by_category": shift_options_by_category,
            "selected_category_id": selected_category_id,
            "selected_shift_id": selected_shift_id,
        },
    )


@login_required
def open_checklist(request):
    try:
        operational_date = _selected_operational_date(request)
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
    definition = _active_definition_for_operator(request, definition_id)
    staff_member = get_object_or_404(
        StaffMember,
        pk=request.GET.get("staff"),
        program=definition.category.program,
        is_active=True,
    )
    try:
        operational_date = _selected_operational_date(request)
    except ValidationError as exc:
        return HttpResponseBadRequest(" ".join(exc.messages))

    instance = resolve_checklist(definition, operational_date)
    show_audit = can_view_contributor_audit(request.user, instance.program_id)
    items = instance.items.select_related("current_staff", "source_task").order_by(
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
    ) if show_audit else StaffContribution.objects.none()
    discrepancy = DiscrepancyExplanation.objects.filter(
        instance=instance, staff=staff_member
    ).first()
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
            "discrepancy": discrepancy,
            "show_audit": show_audit,
            "states": TaskState,
            "total_count": total_count,
            "completed_count": completed_count,
            "staff_member": staff_member,
            "checklist_revision": _checklist_revision(instance),
        },
    )


@login_required
@require_GET
def checklist_state(request, definition_id):
    definition = _active_definition_for_operator(request, definition_id)
    try:
        operational_date = _selected_operational_date(request)
    except ValidationError as exc:
        return HttpResponseBadRequest(" ".join(exc.messages))
    instance = get_object_or_404(
        ChecklistInstance,
        definition=definition,
        operational_date=operational_date,
        program=definition.category.program,
        category=definition.category,
        shift=definition.shift,
    )
    revision = _checklist_revision(instance)
    if request.GET.get("revision") == revision:
        return JsonResponse({"changed": False, "revision": revision})
    return JsonResponse(
        _checklist_state_payload(
            instance,
            include_audit=can_view_contributor_audit(request.user, instance.program_id),
        )
    )


@login_required
@require_POST
def update_item_state(request, item_id):
    item = get_object_or_404(
        ChecklistItem.objects.select_related("instance__category", "instance__definition"),
        pk=item_id,
    )
    try:
        changed_item, contribution = change_item_state(
            item_id=item_id,
            actor=request.user,
            staff_member=get_object_or_404(
                StaffMember,
                pk=request.POST.get("staff"),
                program=item.instance.program,
                is_active=True,
            ),
            new_state=request.POST.get("state", ""),
            activity_text=request.POST.get("activity_text", ""),
        )
    except ValidationError as exc:
        return HttpResponseBadRequest(" ".join(exc.messages))

    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse(
            _checklist_state_payload(
                changed_item.instance,
                include_audit=can_view_contributor_audit(
                    request.user, changed_item.instance.program_id
                ),
            )
        )

    if contribution:
        messages.success(request, "Task updated and Staff Contribution recorded.")
    target = reverse("checklist-detail", args=(item.instance.definition_id,))
    return redirect(
        f"{target}?date={item.instance.operational_date.isoformat()}&staff={request.POST.get('staff')}"
    )


@login_required
@require_POST
def update_discrepancy(request, instance_id):
    instance = get_object_or_404(
        ChecklistInstance.objects.select_related("program"), pk=instance_id
    )
    staff_member = get_object_or_404(
        StaffMember,
        pk=request.POST.get("staff"),
        program=instance.program,
        is_active=True,
    )
    try:
        save_discrepancy_explanation(
            instance=instance,
            actor=request.user,
            staff_member=staff_member,
            explanation=request.POST.get("explanation", ""),
        )
    except ValidationError as exc:
        return HttpResponseBadRequest(" ".join(exc.messages))
    messages.success(request, "Discrepancy explanation saved for this staff member.")
    target = reverse("checklist-detail", args=(instance.definition_id,))
    return redirect(
        f"{target}?date={instance.operational_date.isoformat()}&staff={staff_member.pk}"
    )


@staff_member_required
@require_GET
def export_configuration(request):
    payload = build_configuration_export()
    response = HttpResponse(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        content_type="application/json; charset=utf-8",
    )
    filename = f"chore-config-{timezone.localdate().isoformat()}.json"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


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
    query_params = request.GET.copy()
    query_params["period"] = period.kind
    query_params["date"] = period.selected_date.isoformat()
    query_params.pop("month", None)
    query_params.pop("year", None)
    report["query_string"] = query_params.urlencode()
    return report


@login_required
def reports(request):
    return render(request, "checklists/report.html", _report_for_request(request))


@login_required
def report_detail(request, instance_id):
    report = _report_for_request(request)
    report["row"] = next((row for row in report["rows"] if row["id"] == instance_id), None)
    if report["row"] is None:
        raise PermissionDenied("This Chore List is outside the authorized report.")
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
            "Position",
            "Shift",
            "Completion",
            "Applicable tasks",
            "Completed tasks",
            "N/A tasks",
            "Pending tasks",
            "Completion percentage",
            "Chore List staff",
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
