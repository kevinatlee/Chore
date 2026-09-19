from django import forms
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AdminUserCreationForm, UserChangeForm
from django.contrib.auth.models import Group
from django.db.models import Count

from .models import (
    AssignmentSectionMembership,
    ChecklistDefinition,
    ChecklistInstance,
    ChecklistItem,
    ChecklistSection,
    DiscrepancyExplanation,
    Program,
    ProgramMembership,
    ScheduledReportDelivery,
    SectionTaskMembership,
    Shift,
    StaffCategory,
    StaffContribution,
    StaffMember,
    TaskDefinition,
)
from .presentation import display_task_text


admin.site.site_header = "Chore administration"
admin.site.site_title = "Chore admin"
admin.site.index_title = ""
if Group in admin.site._registry:
    admin.site.unregister(Group)


class ClearAdminMixin:
    save_on_top = True

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions

    class Media:
        css = {"all": ("checklists/admin_clarity.css",)}
        js = ("checklists/admin_clarity.js",)


class ActiveConfigurationAdminMixin(ClearAdminMixin):
    @admin.display(boolean=True, description="Available for new work")
    def availability(self, obj):
        return obj.is_active


class AssignmentSectionInline(admin.TabularInline):
    model = AssignmentSectionMembership
    extra = 0
    autocomplete_fields = ("section",)
    fields = ("sort_order", "section")
    ordering = ("sort_order",)
    verbose_name = "ordered section"
    verbose_name_plural = "Ordered sections"


class SectionAssignmentInline(admin.TabularInline):
    model = AssignmentSectionMembership
    fk_name = "section"
    extra = 0
    autocomplete_fields = ("assignment",)
    fields = ("assignment", "sort_order")
    ordering = ("assignment", "sort_order")
    verbose_name = "shift assignment membership"


class SectionTaskInline(admin.TabularInline):
    model = SectionTaskMembership
    fk_name = "section"
    extra = 0
    autocomplete_fields = ("task",)
    fields = ("sort_order", "task")
    ordering = ("sort_order",)
    verbose_name = "ordered task"
    verbose_name_plural = "Ordered tasks"


class TaskSectionInline(admin.TabularInline):
    model = SectionTaskMembership
    fk_name = "task"
    extra = 0
    autocomplete_fields = ("section",)
    fields = ("section", "sort_order")
    ordering = ("section", "sort_order")
    verbose_name = "section membership"


@admin.register(Program)
class ProgramAdmin(ActiveConfigurationAdminMixin, admin.ModelAdmin):
    list_display = ("name", "slug", "availability")
    search_fields = ("name", "slug")


@admin.register(ProgramMembership)
class ProgramMembershipAdmin(ClearAdminMixin, admin.ModelAdmin):
    list_display = ("user", "program", "role", "receive_scheduled_reports", "is_active")
    list_filter = ("program", "role", "receive_scheduled_reports", "is_active")
    search_fields = ("user__username", "user__first_name", "user__last_name")


@admin.register(StaffCategory)
class StaffCategoryAdmin(ActiveConfigurationAdminMixin, admin.ModelAdmin):
    list_display = ("name", "program", "slug", "sort_order", "availability")
    list_editable = ("sort_order",)
    list_filter = ("program", "is_active")
    search_fields = ("name", "slug")


@admin.register(Shift)
class ShiftAdmin(ActiveConfigurationAdminMixin, admin.ModelAdmin):
    list_display = ("name", "start_time", "end_time", "sort_order", "availability")
    list_editable = ("sort_order",)
    search_fields = ("name",)


@admin.register(ChecklistDefinition)
class ChecklistDefinitionAdmin(ActiveConfigurationAdminMixin, admin.ModelAdmin):
    list_display = ("name", "category", "shift", "sort_order", "section_count", "availability")
    list_editable = ("sort_order",)
    list_filter = ("category__program", "category", "shift", "is_active")
    search_fields = ("name", "category__name", "shift__name")
    inlines = (AssignmentSectionInline,)

    def get_readonly_fields(self, request, obj=None):
        return ("category", "shift") if obj and obj.instances.exists() else ()

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_section_count=Count("section_memberships"))

    @admin.display(description="Sections", ordering="_section_count")
    def section_count(self, obj):
        return obj._section_count


@admin.register(ChecklistSection)
class ChecklistSectionAdmin(ActiveConfigurationAdminMixin, admin.ModelAdmin):
    list_display = ("name", "task_count", "assignment_count", "availability")
    list_filter = ("is_active",)
    search_fields = ("name", "task_memberships__task__label")
    inlines = (SectionTaskInline, SectionAssignmentInline)

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            _task_count=Count("task_memberships", distinct=True),
            _assignment_count=Count("assignment_memberships", distinct=True),
        )

    @admin.display(description="Tasks", ordering="_task_count")
    def task_count(self, obj):
        return obj._task_count

    @admin.display(description="Shift assignments", ordering="_assignment_count")
    def assignment_count(self, obj):
        return obj._assignment_count


@admin.register(TaskDefinition)
class TaskDefinitionAdmin(ActiveConfigurationAdminMixin, admin.ModelAdmin):
    list_display = ("short_label", "section_count", "allow_na", "requires_completion_note", "availability")
    list_editable = ("allow_na", "requires_completion_note")
    list_filter = ("allow_na", "requires_completion_note", "is_active", "sections")
    search_fields = ("label",)
    inlines = (TaskSectionInline,)

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_section_count=Count("section_memberships"))

    @admin.display(description="Task")
    def short_label(self, obj):
        return display_task_text(obj.label)[:100]

    @admin.display(description="Sections", ordering="_section_count")
    def section_count(self, obj):
        return obj._section_count


@admin.register(StaffMember)
class StaffMemberAdmin(ActiveConfigurationAdminMixin, admin.ModelAdmin):
    list_display = ("first_name", "last_name", "program", "availability", "contribution_count")
    list_filter = ("program", "is_active")
    search_fields = ("first_name", "last_name")

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_contribution_count=Count("staff_contributions"))

    @admin.display(description="Task entries", ordering="_contribution_count")
    def contribution_count(self, obj):
        return obj._contribution_count

    def has_delete_permission(self, request, obj=None):
        return False


class ChecklistItemInline(admin.TabularInline):
    model = ChecklistItem
    extra = 0
    can_delete = False
    fields = ("section_name_snapshot", "task_label_snapshot", "current_state", "current_staff", "state_changed_at")
    readonly_fields = fields
    show_change_link = True


class DiscrepancyInline(admin.TabularInline):
    model = DiscrepancyExplanation
    extra = 0
    can_delete = False
    fields = ("staff", "explanation", "recorded_by", "created_at", "updated_at")
    readonly_fields = fields


@admin.register(ChecklistInstance)
class ChecklistInstanceAdmin(ClearAdminMixin, admin.ModelAdmin):
    list_display = ("operational_date", "category_name_snapshot", "shift_name_snapshot", "created_at")
    list_filter = ("category", "shift", "operational_date")
    search_fields = ("category_name_snapshot", "shift_name_snapshot")
    readonly_fields = tuple(field.name for field in ChecklistInstance._meta.fields)
    inlines = (ChecklistItemInline, DiscrepancyInline)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ChecklistItem)
class ChecklistItemAdmin(ClearAdminMixin, admin.ModelAdmin):
    list_display = ("task_label_snapshot", "instance", "current_state", "current_staff", "state_changed_at")
    list_filter = ("current_state", "instance__category", "instance__shift")
    search_fields = ("task_label_snapshot",)
    readonly_fields = tuple(field.name for field in ChecklistItem._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(StaffContribution)
class StaffContributionAdmin(ClearAdminMixin, admin.ModelAdmin):
    list_display = ("staff", "item", "previous_state", "new_state", "recorded_by", "created_at")
    list_filter = ("new_state", "item__instance__category", "item__instance__shift")
    search_fields = ("staff__first_name", "staff__last_name", "recorded_by__username", "activity_text")
    readonly_fields = tuple(field.name for field in StaffContribution._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(DiscrepancyExplanation)
class DiscrepancyExplanationAdmin(ClearAdminMixin, admin.ModelAdmin):
    list_display = ("instance", "staff", "updated_at")
    search_fields = ("staff__first_name", "staff__last_name", "explanation")
    readonly_fields = tuple(field.name for field in DiscrepancyExplanation._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ScheduledReportDelivery)
class ScheduledReportDeliveryAdmin(ClearAdminMixin, admin.ModelAdmin):
    list_display = ("program", "cadence", "period_start", "period_end", "recipient_email", "state", "sent_at")
    list_filter = ("program", "cadence", "state")
    search_fields = ("recipient_email", "subject")
    readonly_fields = tuple(field.name for field in ScheduledReportDelivery._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class CaseInsensitiveUsernameForm(UserChangeForm):
    def clean_username(self):
        username = self.cleaned_data["username"]
        User = get_user_model()
        matches = User.objects.filter(username__iexact=username)
        if self.instance.pk:
            matches = matches.exclude(pk=self.instance.pk)
        if matches.exists():
            raise forms.ValidationError("A user with this username already exists, regardless of letter case.")
        return username


class CaseInsensitiveUsernameCreationForm(AdminUserCreationForm):
    def clean_username(self):
        username = self.cleaned_data["username"]
        if get_user_model().objects.filter(username__iexact=username).exists():
            raise forms.ValidationError(
                "A user with this username already exists, regardless of letter case."
            )
        return username


User = get_user_model()
if User in admin.site._registry:
    admin.site._registry[User].form = CaseInsensitiveUsernameForm
    admin.site._registry[User].add_form = CaseInsensitiveUsernameCreationForm
