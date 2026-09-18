from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


def get_default_program_pk():
    """Keep Phase 1 callers compatible while assigning every record to a Program."""
    return Program.objects.get_or_create(
        slug="sonder-house", defaults={"name": "Sonder House"}
    )[0].pk


class Program(models.Model):
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=80, unique=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("name", "id")
        verbose_name = "program"
        verbose_name_plural = "programs"

    def __str__(self):
        return self.name


class ProgramRole(models.TextChoices):
    OPERATIONAL = "operational", "Operational access"
    MANAGER = "manager", "Manager"


class ProgramMembership(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="program_memberships"
    )
    program = models.ForeignKey(
        Program, on_delete=models.PROTECT, related_name="memberships"
    )
    role = models.CharField(max_length=16, choices=ProgramRole.choices)
    is_active = models.BooleanField(default=True)
    receive_scheduled_reports = models.BooleanField(default=False)

    class Meta:
        ordering = ("program__name", "role", "user__username")
        verbose_name = "program assignment"
        verbose_name_plural = "program assignments"
        constraints = [
            models.UniqueConstraint(
                fields=("user", "program"), name="unique_user_program_membership"
            )
        ]

    def clean(self):
        super().clean()
        if self.receive_scheduled_reports and self.role != ProgramRole.MANAGER:
            raise ValidationError(
                {"receive_scheduled_reports": "Only Managers may receive scheduled reports."}
            )

    def __str__(self):
        return f"{self.user} — {self.program} ({self.get_role_display()})"


class StaffMember(models.Model):
    program = models.ForeignKey(
        Program, on_delete=models.PROTECT, related_name="staff_members"
    )
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)
    seed_key = models.SlugField(
        max_length=220, unique=True, null=True, blank=True, editable=False
    )

    class Meta:
        ordering = ("first_name", "last_name", "id")
        verbose_name = "staff member"
        verbose_name_plural = "staff"
        constraints = [
            models.UniqueConstraint(
                fields=("program", "first_name", "last_name"),
                name="unique_program_staff_name",
            )
        ]

    @property
    def display_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    def __str__(self):
        return self.display_name


class ActiveOrderedModel(models.Model):
    name = models.CharField(max_length=120)
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        abstract = True
        ordering = ("sort_order", "name", "id")

    def __str__(self):
        return self.name


class StaffCategory(ActiveOrderedModel):
    program = models.ForeignKey(
        Program,
        on_delete=models.PROTECT,
        related_name="staff_categories",
        default=get_default_program_pk,
    )
    slug = models.SlugField(max_length=80)

    class Meta(ActiveOrderedModel.Meta):
        verbose_name = "position"
        verbose_name_plural = "positions"
        constraints = [
            models.UniqueConstraint(
                fields=("program", "slug"), name="unique_program_category_slug"
            )
        ]


class Shift(ActiveOrderedModel):
    seed_key = models.SlugField(
        max_length=120, unique=True, null=True, blank=True, editable=False
    )
    start_time = models.TimeField()
    end_time = models.TimeField()

    class Meta(ActiveOrderedModel.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=("start_time", "end_time"), name="unique_shift_window"
            )
        ]

    @property
    def crosses_midnight(self):
        return self.end_time <= self.start_time


class ChecklistDefinition(ActiveOrderedModel):
    seed_key = models.SlugField(
        max_length=160, unique=True, null=True, blank=True, editable=False
    )
    category = models.ForeignKey(
        StaffCategory, on_delete=models.PROTECT, related_name="checklist_definitions"
    )
    shift = models.ForeignKey(
        Shift, on_delete=models.PROTECT, related_name="checklist_definitions"
    )
    sections = models.ManyToManyField(
        "ChecklistSection",
        through="AssignmentSectionMembership",
        related_name="assignments",
        blank=True,
    )

    class Meta(ActiveOrderedModel.Meta):
        verbose_name = "shift assignment"
        verbose_name_plural = "shift assignments"
        constraints = [
            models.UniqueConstraint(
                fields=("category", "shift"), name="unique_definition_category_shift"
            )
        ]

    def __str__(self):
        return f"{self.category.name} — {self.shift.name}"

    def _validate_operational_identity(self):
        if not self.pk:
            return
        original = type(self).objects.filter(pk=self.pk).values(
            "category_id", "shift_id"
        ).first()
        if not original or not self.instances.exists():
            return
        errors = {}
        if original["category_id"] != self.category_id:
            errors["category"] = (
                "Position cannot change after this definition has operational Chore Lists."
            )
        if original["shift_id"] != self.shift_id:
            errors["shift"] = (
                "Shift cannot change after this definition has operational Chore Lists."
            )
        if errors:
            raise ValidationError(errors)

    def clean(self):
        super().clean()
        self._validate_operational_identity()

    def save(self, *args, **kwargs):
        self._validate_operational_identity()
        return super().save(*args, **kwargs)


class ChecklistSection(ActiveOrderedModel):
    seed_key = models.SlugField(
        max_length=200, unique=True, null=True, blank=True, editable=False
    )
    tasks = models.ManyToManyField(
        "TaskDefinition",
        through="SectionTaskMembership",
        related_name="sections",
        blank=True,
    )

    class Meta(ActiveOrderedModel.Meta):
        verbose_name = "section"
        verbose_name_plural = "sections"

    def __str__(self):
        return self.name


class TaskDefinition(models.Model):
    seed_key = models.SlugField(
        max_length=240, unique=True, null=True, blank=True, editable=False
    )
    label = models.CharField(max_length=500)
    allow_na = models.BooleanField(default=False, verbose_name="Allow N/A")
    requires_completion_note = models.BooleanField(
        default=False, verbose_name="Require completion note"
    )
    is_active = models.BooleanField(default=True)
    scheduled_start = models.TimeField(null=True, blank=True)
    scheduled_end = models.TimeField(null=True, blank=True)

    class Meta:
        ordering = ("label", "id")
        verbose_name = "task"
        verbose_name_plural = "tasks"
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(scheduled_start__isnull=True, scheduled_end__isnull=True)
                    | Q(scheduled_start__isnull=False, scheduled_end__isnull=False)
                ),
                name="task_schedule_times_both_or_neither",
            ),
        ]

    def clean(self):
        super().clean()
        if (self.scheduled_start is None) != (self.scheduled_end is None):
            raise ValidationError("Scheduled start and end must be set together.")
    def __str__(self):
        from .presentation import display_task_text
        return display_task_text(self.label)


class AssignmentSectionMembership(models.Model):
    assignment = models.ForeignKey(
        ChecklistDefinition, on_delete=models.CASCADE, related_name="section_memberships"
    )
    section = models.ForeignKey(
        ChecklistSection, on_delete=models.PROTECT, related_name="assignment_memberships"
    )
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ("sort_order", "id")
        verbose_name = "shift assignment section"
        verbose_name_plural = "shift assignment sections"
        constraints = [
            models.UniqueConstraint(
                fields=("assignment", "section"),
                name="unique_assignment_section_membership",
            ),
            models.UniqueConstraint(
                fields=("assignment", "sort_order"),
                name="unique_assignment_section_order",
            ),
        ]

    def __str__(self):
        return f"{self.assignment}: {self.section}"


class SectionTaskMembership(models.Model):
    section = models.ForeignKey(
        ChecklistSection, on_delete=models.CASCADE, related_name="task_memberships"
    )
    task = models.ForeignKey(
        TaskDefinition, on_delete=models.PROTECT, related_name="section_memberships"
    )
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ("sort_order", "id")
        verbose_name = "section task"
        verbose_name_plural = "section tasks"
        constraints = [
            models.UniqueConstraint(
                fields=("section", "task"), name="unique_section_task_membership"
            ),
            models.UniqueConstraint(
                fields=("section", "sort_order"), name="unique_section_task_order"
            ),
        ]

    def __str__(self):
        return f"{self.section}: {self.task}"


class TaskState(models.TextChoices):
    PENDING = "pending", "Pending"
    COMPLETED = "completed", "Completed"
    NOT_APPLICABLE = "na", "N/A"


class ChecklistInstance(models.Model):
    program = models.ForeignKey(
        Program,
        on_delete=models.PROTECT,
        related_name="checklist_instances",
        default=get_default_program_pk,
    )
    operational_date = models.DateField()
    definition = models.ForeignKey(
        ChecklistDefinition, on_delete=models.PROTECT, related_name="instances"
    )
    category = models.ForeignKey(
        StaffCategory, on_delete=models.PROTECT, related_name="checklist_instances"
    )
    shift = models.ForeignKey(
        Shift, on_delete=models.PROTECT, related_name="checklist_instances"
    )
    category_name_snapshot = models.CharField(max_length=120)
    shift_name_snapshot = models.CharField(max_length=120)
    shift_start_snapshot = models.TimeField()
    shift_end_snapshot = models.TimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    is_mock_data = models.BooleanField(default=False)

    class Meta:
        ordering = ("-operational_date", "category_name_snapshot", "shift_start_snapshot")
        verbose_name = "report"
        verbose_name_plural = "reports"
        constraints = [
            models.UniqueConstraint(
                fields=("operational_date", "category", "shift"),
                name="unique_operational_checklist",
            )
        ]

    def __str__(self):
        return (
            f"{self.operational_date}: {self.category_name_snapshot} — "
            f"{self.shift_name_snapshot}"
        )

    def _validate_definition_identity(self):
        if not self.definition_id:
            return
        definition = self.definition
        errors = {}
        if self.category_id and definition.category_id != self.category_id:
            errors["category"] = "Position must match the Chore List definition."
        if self.shift_id and definition.shift_id != self.shift_id:
            errors["shift"] = "Shift must match the Chore List definition."
        if self.program_id and definition.category.program_id != self.program_id:
            errors["program"] = "Program must match the Chore List position."
        if errors:
            raise ValidationError(errors)

    def clean(self):
        super().clean()
        self._validate_definition_identity()

    def save(self, *args, **kwargs):
        self._validate_definition_identity()
        return super().save(*args, **kwargs)


class ChecklistItem(models.Model):
    instance = models.ForeignKey(
        ChecklistInstance, on_delete=models.PROTECT, related_name="items"
    )
    source_task = models.ForeignKey(
        TaskDefinition, on_delete=models.PROTECT, related_name="operational_items"
    )
    section_name_snapshot = models.CharField(max_length=120)
    section_order_snapshot = models.PositiveIntegerField()
    task_label_snapshot = models.CharField(max_length=500)
    task_order_snapshot = models.PositiveIntegerField()
    allow_na_snapshot = models.BooleanField(default=False)
    requires_completion_note_snapshot = models.BooleanField(default=False)
    scheduled_start_snapshot = models.TimeField(null=True, blank=True)
    scheduled_end_snapshot = models.TimeField(null=True, blank=True)
    current_state = models.CharField(
        max_length=16, choices=TaskState.choices, default=TaskState.PENDING
    )
    current_staff = models.ForeignKey(
        StaffMember,
        on_delete=models.PROTECT,
        related_name="current_checklist_items",
        null=True,
        blank=True,
    )
    state_changed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = (
            "section_order_snapshot",
            "task_order_snapshot",
            "scheduled_start_snapshot",
            "id",
        )
        constraints = [
            models.UniqueConstraint(
                fields=("instance", "source_task"), name="unique_instance_source_task"
            ),
            models.CheckConstraint(
                condition=Q(current_state__in=TaskState.values),
                name="valid_checklist_item_state",
            ),
            models.CheckConstraint(
                condition=(~Q(current_state=TaskState.NOT_APPLICABLE) | Q(allow_na_snapshot=True)),
                name="na_requires_snapshot_permission",
            ),
        ]
        verbose_name = "task instance"
        verbose_name_plural = "task instances"

    def __str__(self):
        return f"{self.instance}: {self.task_label_snapshot}"


class StaffContribution(models.Model):
    item = models.ForeignKey(
        ChecklistItem, on_delete=models.PROTECT, related_name="contributions"
    )
    staff = models.ForeignKey(
        StaffMember,
        on_delete=models.PROTECT,
        related_name="staff_contributions",
    )
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="recorded_staff_contributions",
        null=True,
        blank=True,
    )
    previous_state = models.CharField(max_length=16, choices=TaskState.choices)
    new_state = models.CharField(max_length=16, choices=TaskState.choices)
    activity_text = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at", "id")
        verbose_name = "task entry"
        verbose_name_plural = "task entries"
        constraints = [
            models.CheckConstraint(
                condition=Q(previous_state__in=TaskState.values),
                name="valid_contribution_previous_state",
            ),
            models.CheckConstraint(
                condition=Q(new_state__in=TaskState.values),
                name="valid_contribution_new_state",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Staff Contributions are append-only.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Staff Contributions are append-only.")

    def __str__(self):
        return f"{self.staff}: {self.previous_state} → {self.new_state}"


class DiscrepancyExplanation(models.Model):
    instance = models.ForeignKey(
        ChecklistInstance, on_delete=models.PROTECT, related_name="discrepancy_explanations"
    )
    staff = models.ForeignKey(
        StaffMember, on_delete=models.PROTECT, related_name="discrepancy_explanations"
    )
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="recorded_discrepancy_explanations",
        null=True,
        blank=True,
    )
    explanation = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("created_at", "id")
        verbose_name = "discrepancy explanation"
        verbose_name_plural = "discrepancy explanations"
        constraints = [
            models.UniqueConstraint(
                fields=("instance", "staff"),
                name="unique_instance_staff_discrepancy",
            )
        ]

    def __str__(self):
        return f"{self.instance} — {self.staff}"


class ReportCadence(models.TextChoices):
    DAILY = "daily", "Daily"
    WEEKLY = "weekly", "Weekly"
    MONTHLY = "monthly", "Monthly"
    ANNUAL = "annual", "Annual"


class DeliveryState(models.TextChoices):
    PENDING = "pending", "Pending"
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"


class ScheduledReportDelivery(models.Model):
    program = models.ForeignKey(
        Program, on_delete=models.PROTECT, related_name="report_deliveries"
    )
    cadence = models.CharField(max_length=16, choices=ReportCadence.choices)
    period_start = models.DateField()
    period_end = models.DateField()
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="scheduled_report_deliveries",
    )
    recipient_email = models.EmailField()
    generated_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    state = models.CharField(
        max_length=16, choices=DeliveryState.choices, default=DeliveryState.PENDING
    )
    subject = models.CharField(max_length=255)
    body_html = models.TextField()
    snapshot = models.JSONField(default=dict)
    error_message = models.TextField(blank=True)

    class Meta:
        ordering = ("-period_end", "program__name", "cadence", "recipient_email")
        verbose_name = "email report"
        verbose_name_plural = "email reports"
        constraints = [
            models.UniqueConstraint(
                fields=(
                    "program",
                    "cadence",
                    "period_start",
                    "period_end",
                    "recipient",
                ),
                name="unique_scheduled_report_delivery",
            )
        ]

    def __str__(self):
        return (
            f"{self.program} {self.get_cadence_display()} "
            f"{self.period_start}–{self.period_end} to {self.recipient_email}"
        )
