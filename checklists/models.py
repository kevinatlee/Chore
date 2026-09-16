from datetime import time

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


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
    slug = models.SlugField(max_length=80, unique=True)

    class Meta(ActiveOrderedModel.Meta):
        verbose_name_plural = "staff categories"


class Shift(ActiveOrderedModel):
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
    category = models.ForeignKey(
        StaffCategory, on_delete=models.PROTECT, related_name="checklist_definitions"
    )
    shift = models.ForeignKey(
        Shift, on_delete=models.PROTECT, related_name="checklist_definitions"
    )

    class Meta(ActiveOrderedModel.Meta):
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
                "Category cannot change after this definition has operational checklists."
            )
        if original["shift_id"] != self.shift_id:
            errors["shift"] = (
                "Shift cannot change after this definition has operational checklists."
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
    definition = models.ForeignKey(
        ChecklistDefinition, on_delete=models.PROTECT, related_name="sections"
    )

    class Meta(ActiveOrderedModel.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=("definition", "sort_order"), name="unique_section_order"
            )
        ]

    def __str__(self):
        return f"{self.definition}: {self.name}"


class Weekday(models.IntegerChoices):
    MONDAY = 0, "Monday"
    TUESDAY = 1, "Tuesday"
    WEDNESDAY = 2, "Wednesday"
    THURSDAY = 3, "Thursday"
    FRIDAY = 4, "Friday"
    SATURDAY = 5, "Saturday"
    SUNDAY = 6, "Sunday"


class TaskDefinition(models.Model):
    section = models.ForeignKey(
        ChecklistSection, on_delete=models.PROTECT, related_name="tasks"
    )
    label = models.CharField(max_length=500)
    sort_order = models.PositiveIntegerField(default=0)
    allow_na = models.BooleanField(default=False, verbose_name="Allow N/A")
    is_active = models.BooleanField(default=True)
    weekday = models.PositiveSmallIntegerField(
        choices=Weekday.choices, null=True, blank=True
    )
    scheduled_start = models.TimeField(null=True, blank=True)
    scheduled_end = models.TimeField(null=True, blank=True)

    class Meta:
        ordering = ("section__sort_order", "sort_order", "weekday", "id")
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(scheduled_start__isnull=True, scheduled_end__isnull=True)
                    | Q(scheduled_start__isnull=False, scheduled_end__isnull=False)
                ),
                name="task_schedule_times_both_or_neither",
            ),
            models.UniqueConstraint(
                fields=("section", "sort_order"),
                condition=Q(weekday__isnull=True),
                name="unique_general_task_order",
            ),
            models.UniqueConstraint(
                fields=("section", "weekday", "sort_order"),
                condition=Q(weekday__isnull=False),
                name="unique_weekday_task_order",
            ),
        ]

    def clean(self):
        super().clean()
        if (self.scheduled_start is None) != (self.scheduled_end is None):
            raise ValidationError("Scheduled start and end must be set together.")
        if (
            self.section_id
            and self.section.definition.category.slug == "life-skills"
            and self.scheduled_end
            and self.scheduled_end > time(15, 0)
        ):
            raise ValidationError(
                {"scheduled_end": "Life Skills items after 15:00 are outside Chore scope."}
            )

    def __str__(self):
        prefix = f"{self.get_weekday_display()}: " if self.weekday is not None else ""
        return f"{self.section} — {prefix}{self.label}"


class StaffAssignment(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="staff_assignments"
    )
    category = models.ForeignKey(
        StaffCategory, on_delete=models.PROTECT, related_name="staff_assignments"
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("user__username", "category__sort_order", "category__name")
        constraints = [
            models.UniqueConstraint(
                fields=("user", "category"), name="unique_staff_category_assignment"
            )
        ]

    def __str__(self):
        return f"{self.user} — {self.category}"


class TaskState(models.TextChoices):
    PENDING = "pending", "Pending"
    COMPLETED = "completed", "Completed"
    NOT_APPLICABLE = "na", "N/A"


class ChecklistInstance(models.Model):
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

    class Meta:
        ordering = ("-operational_date", "category_name_snapshot", "shift_start_snapshot")
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
            errors["category"] = "Category must match the checklist definition."
        if self.shift_id and definition.shift_id != self.shift_id:
            errors["shift"] = "Shift must match the checklist definition."
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
    weekday_snapshot = models.PositiveSmallIntegerField(
        choices=Weekday.choices, null=True, blank=True
    )
    scheduled_start_snapshot = models.TimeField(null=True, blank=True)
    scheduled_end_snapshot = models.TimeField(null=True, blank=True)
    current_state = models.CharField(
        max_length=16, choices=TaskState.choices, default=TaskState.PENDING
    )
    current_contributor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
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

    def __str__(self):
        return f"{self.instance}: {self.task_label_snapshot}"


class StaffContribution(models.Model):
    item = models.ForeignKey(
        ChecklistItem, on_delete=models.PROTECT, related_name="contributions"
    )
    staff = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="staff_contributions",
    )
    previous_state = models.CharField(max_length=16, choices=TaskState.choices)
    new_state = models.CharField(max_length=16, choices=TaskState.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at", "id")
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
