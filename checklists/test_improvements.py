import importlib
import os
import sqlite3
import uuid
from contextlib import closing
from datetime import time, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from chore.sqlite_backups import BackupError, refresh_database_from_backup
from .models import (
    ActiveTaskCompletion,
    AssignmentSectionMembership,
    ChecklistDefinition,
    ChecklistSection,
    FeedbackEmailState,
    FeedbackSubmission,
    Program,
    ProgramMembership,
    ProgramRole,
    SectionTaskMembership,
    Shift,
    StaffCategory,
    StaffMember,
    TaskDefinition,
    TaskState,
)
from .reporting import ReportPeriod, build_report
from .services import change_item_state, resolve_checklist


class ImprovementFixtureMixin:
    def setUp(self):
        self.today = timezone.localdate()
        User = get_user_model()
        self.program = Program.objects.create(name="Sonder House", slug="sonder-test")
        self.operator = User.objects.create_user("operator-new", password="password")
        ProgramMembership.objects.create(
            user=self.operator, program=self.program, role=ProgramRole.OPERATIONAL
        )
        self.manager = User.objects.create_user(
            "manager-new", email="manager@example.com", password="password"
        )
        ProgramMembership.objects.create(
            user=self.manager,
            program=self.program,
            role=ProgramRole.MANAGER,
            receive_scheduled_reports=True,
        )
        self.admin = User.objects.create_superuser(
            "admin-new", "admin-account@example.com", "password"
        )
        self.staff_a = StaffMember.objects.create(
            program=self.program, first_name="Jane", last_name="Smith"
        )
        self.staff_b = StaffMember.objects.create(
            program=self.program, first_name="John", last_name="Brown"
        )
        self.staff_c = StaffMember.objects.create(
            program=self.program, first_name="Taylor", last_name="Jones"
        )
        self.shift = Shift.objects.create(
            name="Morning", start_time=time(7), end_time=time(15)
        )
        self.category = StaffCategory.objects.create(
            program=self.program, name="Support", slug="support"
        )
        self.definition = ChecklistDefinition.objects.create(
            name="Support Morning", category=self.category, shift=self.shift
        )
        self.section = ChecklistSection.objects.create(name="Office")
        AssignmentSectionMembership.objects.create(
            assignment=self.definition, section=self.section, sort_order=10
        )
        self.task = TaskDefinition.objects.create(
            label="Two-person task", required_staff_completions=2
        )
        self.na_task = TaskDefinition.objects.create(
            label="Optional two-person task",
            allow_na=True,
            required_staff_completions=2,
        )
        SectionTaskMembership.objects.create(
            section=self.section, task=self.task, sort_order=10
        )
        SectionTaskMembership.objects.create(
            section=self.section, task=self.na_task, sort_order=20
        )
        self.instance = resolve_checklist(self.definition, self.today)

    def item(self, task=None):
        return self.instance.items.get(source_task=task or self.task)

    def change(self, staff, state, task=None):
        return change_item_state(
            item_id=self.item(task).pk,
            actor=self.operator,
            staff_member=staff,
            new_state=state,
        )


class MultiStaffCompletionTests(ImprovementFixtureMixin, TestCase):
    def test_required_completion_count_has_model_and_database_minimum(self):
        invalid = TaskDefinition(label="Invalid", required_staff_completions=0)
        with self.assertRaises(ValidationError):
            invalid.full_clean()
        with self.assertRaises(IntegrityError), transaction.atomic():
            TaskDefinition.objects.create(
                label="Database invalid", required_staff_completions=0
            )

    def test_distinct_staff_complete_and_reset_independently_with_history(self):
        item = self.item()
        self.assertEqual(item.required_staff_completions_snapshot, 2)

        first_item, first_history = self.change(self.staff_a, TaskState.COMPLETED)
        self.assertEqual(first_item.current_state, TaskState.PENDING)
        self.assertEqual(first_history.previous_state, TaskState.PENDING)
        self.assertEqual(first_history.new_state, TaskState.PENDING)
        self.assertEqual(item.active_completions.filter(is_active=True).count(), 1)

        _same_item, duplicate = self.change(self.staff_a, TaskState.COMPLETED)
        self.assertIsNone(duplicate)
        self.assertEqual(item.active_completions.filter(is_active=True).count(), 1)

        second_item, _ = self.change(self.staff_b, TaskState.COMPLETED)
        self.assertEqual(second_item.current_state, TaskState.COMPLETED)
        self.assertEqual(item.active_completions.filter(is_active=True).count(), 2)

        reset_item, _ = self.change(self.staff_a, TaskState.PENDING)
        self.assertEqual(reset_item.current_state, TaskState.PENDING)
        self.assertEqual(
            list(
                item.active_completions.filter(is_active=True).values_list(
                    "staff_id", flat=True
                )
            ),
            [self.staff_b.pk],
        )
        self.assertTrue(
            item.active_completions.filter(
                staff=self.staff_a, is_active=False, reset_at__isnull=False
            ).exists()
        )
        self.assertEqual(item.contributions.count(), 3)

    def test_three_staff_requirement_and_database_uniqueness(self):
        self.task.required_staff_completions = 3
        self.task.save(update_fields=("required_staff_completions",))
        second_instance = resolve_checklist(
            self.definition, self.today - timedelta(days=1)
        )
        item = second_instance.items.get(source_task=self.task)
        for index, staff in enumerate((self.staff_a, self.staff_b, self.staff_c), start=1):
            changed, _ = change_item_state(
                item_id=item.pk,
                actor=self.operator,
                staff_member=staff,
                new_state=TaskState.COMPLETED,
            )
            self.assertEqual(
                changed.current_state,
                TaskState.COMPLETED if index == 3 else TaskState.PENDING,
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            ActiveTaskCompletion.objects.create(item=item, staff=self.staff_a)

    def test_na_is_task_level_and_never_counts_as_staff_completion(self):
        self.change(self.staff_a, TaskState.COMPLETED, self.na_task)
        changed, _ = self.change(self.staff_b, TaskState.NOT_APPLICABLE, self.na_task)
        self.assertEqual(changed.current_state, TaskState.NOT_APPLICABLE)
        self.assertEqual(changed.active_completions.filter(is_active=True).count(), 0)
        reset, _ = self.change(self.staff_a, TaskState.PENDING, self.na_task)
        self.assertEqual(reset.current_state, TaskState.PENDING)

    def test_reporting_counts_multi_staff_task_once_and_shows_people(self):
        self.change(self.staff_a, TaskState.COMPLETED)
        partial = build_report(
            program=self.program,
            period=ReportPeriod("daily", self.today, self.today, "today"),
        )
        task = partial["rows"][0]["tasks"][0]
        self.assertEqual(task["state"], TaskState.PENDING)
        self.assertEqual(task["actual_completion_count"], 1)
        self.assertEqual(task["required_completion_count"], 2)
        self.assertEqual(partial["totals"]["applicable"], 2)
        self.change(self.staff_b, TaskState.COMPLETED)
        completed = build_report(
            program=self.program,
            period=ReportPeriod("daily", self.today, self.today, "today"),
        )
        task = completed["rows"][0]["tasks"][0]
        self.assertEqual(task["state"], TaskState.COMPLETED)
        self.assertEqual(
            [entry["staff"] for entry in task["active_completions"]],
            ["Jane Smith", "John Brown"],
        )

    def test_each_staff_sees_their_own_action(self):
        self.change(self.staff_a, TaskState.COMPLETED)
        self.client.force_login(self.operator)
        url = reverse("checklist-detail", args=(self.definition.pk,))
        jane = self.client.get(url, {"date": self.today, "staff": self.staff_a.pk})
        john = self.client.get(url, {"date": self.today, "staff": self.staff_b.pk})
        self.assertContains(jane, "1 of 2 Completed")
        self.assertContains(john, "1 of 2 Completed")
        jane_card = jane.content.decode().split(f'data-item-id="{self.item().pk}"', 1)[1].split("</article>", 1)[0]
        john_card = john.content.decode().split(f'data-item-id="{self.item().pk}"', 1)[1].split("</article>", 1)[0]
        self.assertIn('value="pending">Reset', jane_card)
        self.assertNotIn('value="completed">Complete', jane_card)
        self.assertIn('value="completed">Complete', john_card)
        self.assertNotIn('value="pending">Reset', john_card)


class NameConfirmationTests(ImprovementFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.operator)
        self.url = reverse("update-discrepancy", args=(self.instance.pk,))

    def test_case_and_surrounding_space_are_accepted(self):
        response = self.client.post(
            self.url,
            {
                "staff": self.staff_a.pk,
                "explanation": "Keep this comment",
                "name_confirmation": "  jAnE sMiTh  ",
            },
        )
        self.assertRedirects(response, reverse("dashboard"))
        self.assertEqual(
            self.instance.discrepancy_explanations.get(staff=self.staff_a).explanation,
            "Keep this comment",
        )

    def test_wrong_partial_and_blank_names_fail_without_losing_comment(self):
        for value in ("John Brown", "Jane", ""):
            with self.subTest(value=value):
                response = self.client.post(
                    self.url,
                    {
                        "staff": self.staff_a.pk,
                        "explanation": "Unsaved but preserved",
                        "name_confirmation": value,
                    },
                )
                self.assertEqual(response.status_code, 400)
                self.assertContains(response, "Unsaved but preserved", status_code=400)
                self.assertEqual(
                    response.context["confirmation_error"],
                    "Enter Your Name Here must match the selected staff member's complete name.",
                )
                self.assertContains(
                    response,
                    "Enter Your Name Here must match",
                    status_code=400,
                )
                self.assertFalse(
                    self.instance.discrepancy_explanations.filter(staff=self.staff_a).exists()
                )


@override_settings(
    EMAIL_ENABLED=True,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="Chore Admin <admin@example.com>",
)
class FeedbackTests(ImprovementFixtureMixin, TestCase):
    def post_feedback(self, user, *, staff=None, token=None, text="Line one\nLine two"):
        self.client.force_login(user)
        return self.client.post(
            reverse("feedback"),
            {
                "program": self.program.pk,
                "staff": (staff or self.staff_a).pk,
                "feedback": text,
                "submission_token": token or uuid.uuid4(),
            },
        )

    def test_users_managers_and_admins_can_access_feedback(self):
        for user in (self.operator, self.manager, self.admin):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.get(
                    reverse("feedback"), {"program": self.program.pk}
                )
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Jane Smith")

    def test_submission_persists_and_routes_formatted_email(self):
        response = self.post_feedback(self.operator)
        self.assertRedirects(response, reverse("feedback"))
        submission = FeedbackSubmission.objects.get()
        self.assertEqual(submission.email_state, FeedbackEmailState.SENT)
        self.assertEqual(submission.content, "Line one\nLine two")
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(
            message.subject,
            f"Feedback --- Jane Smith --- {timezone.localdate().isoformat()}",
        )
        self.assertEqual(message.from_email, "Chore Admin <admin@example.com>")
        self.assertCountEqual(
            message.to,
            ["manager@example.com", "Chore Admin <admin@example.com>"],
        )
        self.assertIn("Program: Sonder House", message.body)
        self.assertIn("Line one\nLine two", message.body)

    def test_cross_program_staff_is_rejected(self):
        other = Program.objects.create(name="Other", slug="other")
        outsider = StaffMember.objects.create(
            program=other, first_name="Outside", last_name="Person"
        )
        response = self.post_feedback(self.operator, staff=outsider)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Select a valid choice")
        self.assertFalse(FeedbackSubmission.objects.exists())

    def test_duplicate_token_is_idempotent(self):
        token = uuid.uuid4()
        self.post_feedback(self.operator, token=token)
        self.post_feedback(self.operator, token=token)
        self.assertEqual(FeedbackSubmission.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_email_failure_preserves_feedback_and_reports_failure(self):
        with patch("django.core.mail.message.EmailMessage.send", side_effect=RuntimeError("SMTP down")):
            self.post_feedback(self.operator)
        submission = FeedbackSubmission.objects.get()
        self.assertEqual(submission.email_state, FeedbackEmailState.FAILED)
        self.assertIn("SMTP down", submission.email_error)

    @override_settings(
        EMAIL_ENABLED=False,
        EMAIL_BACKEND="checklists.email_backends.DisabledEmailBackend",
    )
    def test_test_mode_suppresses_feedback_email(self):
        self.post_feedback(self.operator)
        self.assertEqual(
            FeedbackSubmission.objects.get().email_state,
            FeedbackEmailState.SUPPRESSED,
        )
        self.assertEqual(len(mail.outbox), 0)


class ConfigurationPreservationTests(TestCase):
    targets = (
        ("phase5-task-0ea65e10607f1b3945a0", "Review IR binder, Communication Book, WISH, Inclusion, schedule, and DNA list"),
        ("phase5-task-03a8dcc33316f2ca4e41", "Review Heat Hut"),
        ("phase5-task-ca72368919b93134d763", "Check and reply to emails"),
        ("phase5-task-08179a09b70ca6748b49", "Complete leave requests and maintenance requests"),
        ("phase5-task-e07e816fcb3b44ca0aad", "Submit leave requests"),
        ("phase5-task-856c28710c15e5859166", "Complete Citation training"),
    )

    def test_activation_changes_only_reviewed_completion_counts(self):
        program = Program.objects.create(name="Config", slug="config")
        category = StaffCategory.objects.create(program=program, name="Support", slug="support")
        shift = Shift.objects.create(name="Config shift", start_time=time(1), end_time=time(2))
        definition = ChecklistDefinition.objects.create(name="Config list", category=category, shift=shift, sort_order=70)
        section = ChecklistSection.objects.create(name="Config section", sort_order=80)
        AssignmentSectionMembership.objects.create(assignment=definition, section=section, sort_order=90)
        for index, (seed_key, label) in enumerate(self.targets, start=1):
            task = TaskDefinition.objects.create(
                seed_key=seed_key,
                label=label,
                allow_na=index % 2 == 0,
                requires_completion_note=index == 3,
                is_active=index != 6,
                scheduled_start=time(index, 0),
                scheduled_end=time(index, 30),
            )
            SectionTaskMembership.objects.create(section=section, task=task, sort_order=index * 10)
        unrelated = TaskDefinition.objects.create(seed_key="phase5-task-unrelated", label="Custom production task", allow_na=True, is_active=False)
        before_tasks = list(TaskDefinition.objects.order_by("pk").values())
        before_sections = list(SectionTaskMembership.objects.order_by("pk").values())
        before_assignments = list(AssignmentSectionMembership.objects.order_by("pk").values())

        migration = importlib.import_module(
            "checklists.migrations.0011_activate_verified_multi_staff_tasks"
        )
        migration.activate_and_backfill(apps, None)

        after_tasks = list(TaskDefinition.objects.order_by("pk").values())
        for before, after in zip(before_tasks, after_tasks):
            changed = {key for key in before if before[key] != after[key]}
            if before["seed_key"] in {target[0] for target in self.targets}:
                self.assertEqual(changed, {"required_staff_completions"})
                self.assertEqual(after["required_staff_completions"], 2)
            else:
                self.assertEqual(before, after)
        self.assertEqual(before_sections, list(SectionTaskMembership.objects.order_by("pk").values()))
        self.assertEqual(before_assignments, list(AssignmentSectionMembership.objects.order_by("pk").values()))
        unrelated.refresh_from_db()
        self.assertEqual(unrelated.required_staff_completions, 1)


class ManualRefreshTests(SimpleTestCase):
    def create_database(self, path, marker):
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("CREATE TABLE django_migrations (id integer PRIMARY KEY)")
            connection.execute("CREATE TABLE marker (value text)")
            connection.execute("INSERT INTO marker VALUES (?)", (marker,))
            connection.commit()

    def marker(self, path):
        with closing(sqlite3.connect(path)) as connection:
            return connection.execute("SELECT value FROM marker").fetchone()[0]

    def test_explicit_snapshot_refresh_creates_recovery_and_removes_sidecars(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            backups = root / "backups"
            backups.mkdir()
            source = backups / "manual-snapshot.sqlite3"
            destination = root / "test" / "db.sqlite3"
            destination.parent.mkdir()
            self.create_database(source, "production-copy")
            self.create_database(destination, "test-before")
            Path(f"{destination}-wal").write_bytes(b"stale")
            Path(f"{destination}-shm").write_bytes(b"stale")

            chosen, copied = refresh_database_from_backup(
                backups, destination, source_database=source
            )

            self.assertEqual(chosen, source)
            self.assertEqual(copied, destination)
            self.assertEqual(self.marker(destination), "production-copy")
            recovery = next((destination.parent / "refresh-backups").glob("*.sqlite3"))
            self.assertEqual(self.marker(recovery), "test-before")
            self.assertFalse(Path(f"{destination}-wal").exists())
            self.assertFalse(Path(f"{destination}-shm").exists())
            self.assertEqual(self.marker(source), "production-copy")

    def test_explicit_source_outside_backup_mount_is_refused(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            backups = root / "backups"
            backups.mkdir()
            outside = root / "outside.sqlite3"
            self.create_database(outside, "outside")
            with self.assertRaisesRegex(BackupError, "inside the read-only backup"):
                refresh_database_from_backup(
                    backups,
                    root / "test" / "db.sqlite3",
                    source_database=outside,
                )

    def test_normal_entrypoint_contains_no_refresh_call(self):
        source = (Path(__file__).resolve().parent.parent / "docker" / "entrypoint.py").read_text()
        self.assertNotIn("refresh_database_from_backup", source)
        self.assertNotIn("refresh_test_database()", source)

