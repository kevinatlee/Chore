from django.contrib import admin

from .models import (
    ChecklistDefinition,
    ChecklistInstance,
    ChecklistItem,
    ChecklistSection,
    Program,
    ProgramMembership,
    ScheduledReportDelivery,
    Shift,
    StaffCategory,
    StaffContribution,
    StaffMember,
    TaskDefinition,
)


admin.site.site_header = "Chore administration"
admin.site.site_title = "Chore admin"
admin.site.index_title = "Chore configuration and reporting"


@admin.register(Program)
class ProgramAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "is_active")
    list_editable = ("is_active",)
    search_fields = ("name", "slug")


@admin.register(ProgramMembership)
class ProgramMembershipAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "program",
        "role",
        "receive_scheduled_reports",
        "is_active",
    )
    list_filter = ("program", "role", "receive_scheduled_reports", "is_active")
    search_fields = ("user__username", "user__first_name", "user__last_name")


@admin.register(StaffCategory)
class StaffCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "program", "slug", "sort_order", "is_active")
    list_editable = ("sort_order", "is_active")
    list_filter = ("program", "is_active")
    search_fields = ("name", "slug")


@admin.register(Shift)
class ShiftAdmin(admin.ModelAdmin):
    list_display = ("name", "start_time", "end_time", "sort_order", "is_active")
    list_editable = ("sort_order", "is_active")


@admin.register(ChecklistDefinition)
class ChecklistDefinitionAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "shift", "sort_order", "is_active")
    list_editable = ("sort_order", "is_active")
    list_filter = ("category__program", "category", "shift", "is_active")

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.instances.exists():
            return ("category", "shift")
        return ()


@admin.register(ChecklistSection)
class ChecklistSectionAdmin(admin.ModelAdmin):
    list_display = ("name", "definition", "sort_order", "is_active")
    list_editable = ("sort_order", "is_active")
    list_filter = ("definition__category", "definition", "is_active")
    search_fields = ("name",)


@admin.register(TaskDefinition)
class TaskDefinitionAdmin(admin.ModelAdmin):
    list_display = (
        "short_label",
        "section",
        "weekday",
        "scheduled_start",
        "sort_order",
        "allow_na",
        "is_active",
    )
    list_editable = ("sort_order", "allow_na", "is_active")
    list_filter = (
        "section__definition__category",
        "section__definition",
        "section",
        "weekday",
        "allow_na",
        "is_active",
    )
    search_fields = ("label",)

    @admin.display(description="Task")
    def short_label(self, obj):
        return obj.label[:80]


@admin.register(StaffMember)
class StaffMemberAdmin(admin.ModelAdmin):
    list_display = ("first_name", "last_name", "program", "is_active", "contribution_count")
    list_editable = ("is_active",)
    list_filter = ("program", "is_active")
    search_fields = ("first_name", "last_name")
    ordering = ("first_name", "last_name")
    fields = ("program", "first_name", "last_name", "is_active")

    @admin.display(description="Contributions")
    def contribution_count(self, obj):
        return obj.staff_contributions.count()

    def has_delete_permission(self, request, obj=None):
        return False


class ChecklistItemInline(admin.TabularInline):
    model = ChecklistItem
    extra = 0
    can_delete = False
    fields = (
        "section_name_snapshot",
        "task_label_snapshot",
        "current_state",
        "current_staff",
        "state_changed_at",
    )
    readonly_fields = fields
    show_change_link = True


@admin.register(ChecklistInstance)
class ChecklistInstanceAdmin(admin.ModelAdmin):
    list_display = (
        "operational_date",
        "category_name_snapshot",
        "shift_name_snapshot",
        "created_at",
    )
    list_filter = ("category", "shift", "operational_date")
    search_fields = ("category_name_snapshot", "shift_name_snapshot")
    readonly_fields = (
        "operational_date",
        "definition",
        "category",
        "shift",
        "category_name_snapshot",
        "shift_name_snapshot",
        "shift_start_snapshot",
        "shift_end_snapshot",
        "created_at",
    )
    inlines = (ChecklistItemInline,)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ChecklistItem)
class ChecklistItemAdmin(admin.ModelAdmin):
    list_display = (
        "task_label_snapshot",
        "instance",
        "current_state",
        "current_staff",
        "state_changed_at",
    )
    list_filter = ("current_state", "instance__category", "instance__shift")
    search_fields = ("task_label_snapshot",)
    readonly_fields = (
        "instance",
        "source_task",
        "section_name_snapshot",
        "section_order_snapshot",
        "task_label_snapshot",
        "task_order_snapshot",
        "allow_na_snapshot",
        "weekday_snapshot",
        "scheduled_start_snapshot",
        "scheduled_end_snapshot",
        "current_state",
        "current_staff",
        "state_changed_at",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(StaffContribution)
class StaffContributionAdmin(admin.ModelAdmin):
    list_display = ("staff", "item", "previous_state", "new_state", "recorded_by", "created_at")
    list_filter = ("new_state", "item__instance__category", "item__instance__shift")
    search_fields = ("staff__first_name", "staff__last_name", "recorded_by__username")
    readonly_fields = ("item", "staff", "recorded_by", "previous_state", "new_state", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return request.method in ("GET", "HEAD", "OPTIONS")

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ScheduledReportDelivery)
class ScheduledReportDeliveryAdmin(admin.ModelAdmin):
    list_display = (
        "program",
        "cadence",
        "period_start",
        "period_end",
        "recipient_email",
        "state",
        "sent_at",
    )
    list_filter = ("program", "cadence", "state")
    search_fields = ("recipient_email", "subject")
    readonly_fields = (
        "program",
        "cadence",
        "period_start",
        "period_end",
        "recipient",
        "recipient_email",
        "generated_at",
        "sent_at",
        "state",
        "subject",
        "body_html",
        "snapshot",
        "error_message",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
