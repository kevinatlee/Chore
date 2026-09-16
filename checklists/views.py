from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import (
    ChecklistDefinition,
    ChecklistItem,
    StaffAssignment,
    StaffContribution,
    TaskState,
)
from .services import change_item_state, resolve_checklist


def _selected_date(request):
    raw_date = request.GET.get("date") or request.POST.get("date")
    if not raw_date:
        return timezone.localdate()
    try:
        return date.fromisoformat(raw_date)
    except ValueError as exc:
        raise ValidationError("Use a valid date in YYYY-MM-DD format.") from exc


def _can_access_category(user, category_id):
    if user.is_staff:
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
        is_active=True, category__is_active=True, shift__is_active=True
    ).select_related("category", "shift")
    if not request.user.is_staff:
        definitions = definitions.filter(
            category__staff_assignments__user=request.user,
            category__staff_assignments__is_active=True,
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
