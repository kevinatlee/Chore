from django.contrib import admin
from django.db.models import Count

from .presentation import display_task_text
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


class ClearAdminMixin:
    """Apply consistent labels, guidance, and safer configuration controls."""

    field_labels = {}
    field_help_texts = {}
    save_on_top = True

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        formfield = super().formfield_for_dbfield(db_field, request, **kwargs)
        if formfield:
            formfield.label = self.field_labels.get(db_field.name, formfield.label)
            formfield.help_text = self.field_help_texts.get(
                db_field.name, formfield.help_text
            )
        return formfield

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions

    class Media:
        css = {"all": ("checklists/admin_clarity.css",)}
        js = ("checklists/admin_clarity.js",)


class ActiveConfigurationAdminMixin(ClearAdminMixin):
    field_labels = {"is_active": "Available for new work"}
    field_help_texts = {
        "is_active": (
            "Turn this off to remove the configuration from new Chore Lists. "
            "Existing Chore Lists, Staff Contributions, and report history are preserved."
        )
    }

    @admin.display(boolean=True, description="Available for new work")
    def availability(self, obj):
        return obj.is_active


@admin.register(Program)
class ProgramAdmin(ActiveConfigurationAdminMixin, admin.ModelAdmin):
    list_display = ("name", "slug", "availability")
    search_fields = ("name", "slug")
    field_labels = {
        **ActiveConfigurationAdminMixin.field_labels,
        "name": "Program name",
        "slug": "Stable identifier",
    }
    field_help_texts = {
        **ActiveConfigurationAdminMixin.field_help_texts,
        "name": "The service or site name shown throughout Chore.",
        "slug": "Used internally in stable links and seed data. Avoid changing it after setup.",
    }
    fieldsets = (
        (
            "Program identity",
            {
                "fields": ("name", "slug"),
                "description": "A Program is the security boundary for staff, Chore Lists, and Manager reports.",
            },
        ),
        (
            "Availability",
            {
                "fields": ("is_active",),
                "description": "Deactivation stops new operational use; it does not delete history.",
            },
        ),
    )


@admin.register(ProgramMembership)
class ProgramMembershipAdmin(ClearAdminMixin, admin.ModelAdmin):
    list_display = (
        "user",
        "program",
        "role",
        "receive_scheduled_reports",
        "is_active",
    )
    list_filter = ("program", "role", "receive_scheduled_reports", "is_active")
    search_fields = ("user__username", "user__first_name", "user__last_name")
    field_labels = {
        "user": "Application account",
        "program": "Program",
        "role": "Access level",
        "receive_scheduled_reports": "Email scheduled reports",
        "is_active": "Access enabled",
    }
    field_help_texts = {
        "user": "The sign-in account receiving this Program access.",
        "program": "Access and data remain isolated to this Program.",
        "role": "Operational access enters Chore List work. Managers review reports only.",
        "receive_scheduled_reports": (
            "Managers with an active account and active membership receive scheduled reports "
            "at the email address on their application account."
        ),
        "is_active": "Turn off to revoke this Program access without deleting the account or history.",
    }
    fieldsets = (
        (
            "Who and where",
            {
                "fields": ("user", "program"),
                "description": "Membership connects one application account to one Program.",
            },
        ),
        (
            "Access and email routing",
            {
                "fields": ("role", "is_active", "receive_scheduled_reports"),
                "description": "Scheduled email is available only for active Manager memberships.",
            },
        ),
    )


@admin.register(StaffCategory)
class StaffCategoryAdmin(ActiveConfigurationAdminMixin, admin.ModelAdmin):
    list_display = ("name", "program", "slug", "sort_order", "availability")
    list_editable = ("sort_order",)
    list_filter = ("program", "is_active")
    search_fields = ("name", "slug")
    field_labels = {
        **ActiveConfigurationAdminMixin.field_labels,
        "name": "Position name",
        "slug": "Stable identifier",
        "sort_order": "Display order",
    }
    field_help_texts = {
        **ActiveConfigurationAdminMixin.field_help_texts,
        "name": "The staff Position shown when starting a shared Chore List.",
        "program": "Positions and their Chore Lists are visible only inside this Program.",
        "slug": "Internal stable identifier. Avoid changing it after operational use begins.",
        "sort_order": "Lower numbers appear first in staff selectors and admin lists.",
    }
    fieldsets = (
        ("Position", {"fields": ("program", "name", "slug", "sort_order")}),
        (
            "Availability",
            {
                "fields": ("is_active",),
                "description": "Deactivate instead of deleting to keep historical Chore Lists understandable.",
            },
        ),
    )


@admin.register(Shift)
class ShiftAdmin(ActiveConfigurationAdminMixin, admin.ModelAdmin):
    list_display = ("name", "start_time", "end_time", "sort_order", "availability")
    list_editable = ("sort_order",)
    field_labels = {
        **ActiveConfigurationAdminMixin.field_labels,
        "sort_order": "Display order",
    }
    field_help_texts = {
        **ActiveConfigurationAdminMixin.field_help_texts,
        "name": "The shift label staff see when selecting a Chore List.",
        "start_time": "Boundary metadata used to identify and report the shift.",
        "end_time": "An end time at or before the start time means the shift crosses midnight.",
        "sort_order": "Lower numbers appear first.",
    }
    fieldsets = (
        (
            "Shift window",
            {
                "fields": ("name", ("start_time", "end_time"), "sort_order"),
                "description": "Shifts pair with Positions to define which shared Chore List staff open.",
            },
        ),
        ("Availability", {"fields": ("is_active",)}),
    )


@admin.register(ChecklistDefinition)
class ChecklistDefinitionAdmin(ActiveConfigurationAdminMixin, admin.ModelAdmin):
    list_display = ("name", "category", "shift", "sort_order", "availability")
    list_editable = ("sort_order",)
    list_filter = ("category__program", "category", "shift", "is_active")
    field_labels = {
        **ActiveConfigurationAdminMixin.field_labels,
        "name": "Chore List name",
        "category": "Position",
        "sort_order": "Display order",
    }
    field_help_texts = {
        **ActiveConfigurationAdminMixin.field_help_texts,
        "name": "Administrative name for this Position and Shift combination.",
        "category": "The Position whose staff share this Chore List.",
        "shift": "The Shift paired with the Position. One shared Chore List is created per operational date.",
        "sort_order": "Lower numbers appear first.",
    }
    fieldsets = (
        (
            "Shared Chore List",
            {
                "fields": ("name", "category", "shift", "sort_order"),
                "description": (
                    "This configuration creates one shared Chore List for a Position, Shift, and date — "
                    "not one list per staff member. Position and Shift lock after operational use begins."
                ),
            },
        ),
        ("Availability", {"fields": ("is_active",)}),
    )

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.instances.exists():
            return ("category", "shift")
        return ()


@admin.register(ChecklistSection)
class ChecklistSectionAdmin(ActiveConfigurationAdminMixin, admin.ModelAdmin):
    list_display = ("name", "definition", "sort_order", "availability")
    list_editable = ("sort_order",)
    list_filter = ("definition__category", "definition", "is_active")
    search_fields = ("name",)
    field_labels = {
        **ActiveConfigurationAdminMixin.field_labels,
        "definition": "Chore List",
        "sort_order": "Display order",
    }
    field_help_texts = {
        **ActiveConfigurationAdminMixin.field_help_texts,
        "name": "Heading used to group tasks on the staff Chore List.",
        "definition": "The Position and Shift Chore List containing this section.",
        "sort_order": "Lower numbers appear first.",
    }
    fieldsets = (
        ("Section", {"fields": ("definition", "name", "sort_order")}),
        (
            "Availability",
            {
                "fields": ("is_active",),
                "description": "Deactivation affects only Chore Lists created afterward; existing snapshots remain unchanged.",
            },
        ),
    )


@admin.register(TaskDefinition)
class TaskDefinitionAdmin(ActiveConfigurationAdminMixin, admin.ModelAdmin):
    list_display = (
        "short_label",
        "section",
        "weekday",
        "scheduled_start",
        "sort_order",
        "allow_na",
        "availability",
    )
    list_editable = ("sort_order", "allow_na")
    list_filter = (
        "section__definition__category",
        "section__definition",
        "section",
        "weekday",
        "allow_na",
        "is_active",
    )
    search_fields = ("label",)
    field_labels = {
        **ActiveConfigurationAdminMixin.field_labels,
        "section": "Chore List section",
        "label": "Task instructions",
        "allow_na": "Allow staff to choose N/A",
        "weekday": "Only on weekday",
        "scheduled_start": "Scheduled start",
        "scheduled_end": "Scheduled end",
        "sort_order": "Display order",
    }
    field_help_texts = {
        **ActiveConfigurationAdminMixin.field_help_texts,
        "section": "Controls which shared Chore List and section contains this task.",
        "label": "Use concise operational wording. Editing it does not rewrite historical snapshots.",
        "allow_na": (
            "When enabled, staff may resolve this task as N/A. N/A remains unavailable on every other task. "
            "The setting is copied into each new Chore List snapshot."
        ),
        "weekday": "Leave blank for every day, or choose one weekday for a weekly task.",
        "scheduled_start": "Optional display time; set both start and end or leave both blank.",
        "scheduled_end": "Optional display time; set both start and end or leave both blank.",
        "sort_order": "Lower numbers appear first within the section.",
    }
    fieldsets = (
        (
            "Task",
            {
                "fields": ("section", "label", "sort_order"),
                "description": "Task content is snapshotted when a new operational Chore List is first opened.",
            },
        ),
        (
            "Schedule and N/A",
            {
                "fields": ("weekday", ("scheduled_start", "scheduled_end"), "allow_na"),
                "description": "These settings affect future Chore Lists only; historical snapshots are preserved.",
            },
        ),
        ("Availability", {"fields": ("is_active",)}),
    )

    @admin.display(description="Task")
    def short_label(self, obj):
        return display_task_text(obj.label)[:80]


@admin.register(StaffMember)
class StaffMemberAdmin(ActiveConfigurationAdminMixin, admin.ModelAdmin):
    list_display = ("first_name", "last_name", "program", "availability", "contribution_count")
    list_filter = ("program", "is_active")
    search_fields = ("first_name", "last_name")
    ordering = ("first_name", "last_name")
    field_labels = {
        "program": "Program",
        "first_name": "First name",
        "last_name": "Last name",
        "is_active": "Available for staff selection",
    }
    field_help_texts = {
        "program": "Staff may contribute only to shared Chore Lists in this Program.",
        "first_name": "Operational roster name; this is not a sign-in account.",
        "last_name": "Operational roster name used for Staff Contribution attribution.",
        "is_active": (
            "Turn off to hide this person from new Chore List sessions. Existing Staff Contributions "
            "and reports keep their name."
        ),
    }
    fieldsets = (
        (
            "Operational staff record",
            {
                "fields": ("program", ("first_name", "last_name")),
                "description": "This roster identity records Staff Contributions; it does not grant application access.",
            },
        ),
        ("Availability", {"fields": ("is_active",)}),
    )

    @admin.display(boolean=True, description="Available for staff selection")
    def availability(self, obj):
        return obj.is_active

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_contribution_count=Count("staff_contributions"))

    @admin.display(description="Contributions")
    def contribution_count(self, obj):
        return obj._contribution_count

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
class ChecklistInstanceAdmin(ClearAdminMixin, admin.ModelAdmin):
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
    fieldsets = (
        (
            "Historical Chore List identity",
            {
                "fields": (
                    "operational_date",
                    "definition",
                    "category",
                    "shift",
                    "category_name_snapshot",
                    "shift_name_snapshot",
                    "shift_start_snapshot",
                    "shift_end_snapshot",
                    "created_at",
                ),
                "description": "Read-only operational history. Configuration edits do not change these snapshots.",
            },
        ),
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ChecklistItem)
class ChecklistItemAdmin(ClearAdminMixin, admin.ModelAdmin):
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
class StaffContributionAdmin(ClearAdminMixin, admin.ModelAdmin):
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
class ScheduledReportDeliveryAdmin(ClearAdminMixin, admin.ModelAdmin):
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
    fieldsets = (
        (
            "Delivery routing",
            {
                "fields": (
                    "program",
                    "cadence",
                    "period_start",
                    "period_end",
                    "recipient",
                    "recipient_email",
                ),
                "description": "Recipient routing was resolved from the active Manager membership when this report was generated.",
            },
        ),
        (
            "Delivery result",
            {
                "fields": ("generated_at", "sent_at", "state", "error_message"),
                "description": "Delivery rows are retained as historical audit records and cannot be edited or deleted here.",
            },
        ),
        (
            "Preserved report snapshot",
            {
                "fields": ("subject", "body_html", "snapshot"),
                "description": "This content remains unchanged even if live Chore List data is later updated.",
            },
        ),
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
