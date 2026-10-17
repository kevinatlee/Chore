import django.core.validators
import django.db.models.deletion
import django.utils.timezone
import uuid

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("checklists", "0009_weekday_availability_and_operational_corrections"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ActiveTaskCompletion",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("completed_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("is_active", models.BooleanField(default=True)),
                ("reset_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={"ordering": ("completed_at", "id")},
        ),
        migrations.CreateModel(
            name="FeedbackSubmission",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("content", models.TextField()),
                ("submitted_at", models.DateTimeField(auto_now_add=True)),
                ("idempotency_key", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ("email_state", models.CharField(choices=[("pending", "Pending"), ("sent", "Sent"), ("failed", "Failed"), ("suppressed", "Suppressed")], default="pending", max_length=16)),
                ("email_sent_at", models.DateTimeField(blank=True, null=True)),
                ("email_error", models.TextField(blank=True)),
            ],
            options={
                "verbose_name": "feedback submission",
                "verbose_name_plural": "feedback submissions",
                "ordering": ("-submitted_at", "-id"),
            },
        ),
        migrations.AddField(
            model_name="checklistitem",
            name="required_staff_completions_snapshot",
            field=models.PositiveIntegerField(default=1, validators=[django.core.validators.MinValueValidator(1)]),
        ),
        migrations.AddField(
            model_name="taskdefinition",
            name="required_staff_completions",
            field=models.PositiveIntegerField(default=1, help_text="Number of distinct staff who must complete each new task instance.", validators=[django.core.validators.MinValueValidator(1)], verbose_name="Required Staff Completions"),
        ),
        migrations.AddConstraint(
            model_name="checklistitem",
            constraint=models.CheckConstraint(condition=models.Q(("required_staff_completions_snapshot__gte", 1)), name="item_required_staff_completions_gte_1"),
        ),
        migrations.AddConstraint(
            model_name="taskdefinition",
            constraint=models.CheckConstraint(condition=models.Q(("required_staff_completions__gte", 1)), name="task_required_staff_completions_gte_1"),
        ),
        migrations.AddField(
            model_name="activetaskcompletion",
            name="item",
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="active_completions", to="checklists.checklistitem"),
        ),
        migrations.AddField(
            model_name="activetaskcompletion",
            name="recorded_by",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="recorded_active_task_completions", to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name="activetaskcompletion",
            name="reset_by",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="reset_active_task_completions", to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name="activetaskcompletion",
            name="staff",
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="active_task_completions", to="checklists.staffmember"),
        ),
        migrations.AddField(
            model_name="feedbacksubmission",
            name="program",
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="feedback_submissions", to="checklists.program"),
        ),
        migrations.AddField(
            model_name="feedbacksubmission",
            name="staff",
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="feedback_submissions", to="checklists.staffmember"),
        ),
        migrations.AddField(
            model_name="feedbacksubmission",
            name="submitted_by",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="feedback_submissions", to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddConstraint(
            model_name="activetaskcompletion",
            constraint=models.UniqueConstraint(condition=models.Q(("is_active", True)), fields=("item", "staff"), name="unique_active_item_staff_completion"),
        ),
    ]

