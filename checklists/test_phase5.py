import json
from datetime import date, time

from django.contrib.auth import authenticate, get_user_model
from django.core.exceptions import ValidationError
from django.core import mail
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import (
    AssignmentSectionMembership,
    ChecklistDefinition,
    ChecklistInstance,
    ChecklistSection,
    DiscrepancyExplanation,
    Program,
    ProgramMembership,
    ProgramRole,
    SectionTaskMembership,
    Shift,
    StaffCategory,
    StaffContribution,
    StaffMember,
    TaskDefinition,
    TaskState,
)
from .reporting import ReportPeriod, build_report
from .seed_data import ASSIGNMENTS, PROGRAMMING_TASK_LABEL
from .services import change_item_state, resolve_checklist, save_discrepancy_explanation


class Phase5FixtureMixin:
    operational_date = date(2026, 9, 18)

    def setUp(self):
        User = get_user_model()
        self.program = Program.objects.create(name="Sonder House", slug="phase5")
        self.operator = User.objects.create_user("JSmith", password="phase-five-password")
        ProgramMembership.objects.create(
            user=self.operator, program=self.program, role=ProgramRole.OPERATIONAL
        )
        self.manager = User.objects.create_user("manager5", password="phase-five-password")
        ProgramMembership.objects.create(
            user=self.manager, program=self.program, role=ProgramRole.MANAGER
        )
        self.admin = User.objects.create_superuser(
            "admin5", "admin5@example.com", "phase-five-password"
        )
        self.staff_a = StaffMember.objects.create(
            program=self.program, first_name="Alice", last_name="Worker"
        )
        self.staff_b = StaffMember.objects.create(
            program=self.program, first_name="Bob", last_name="Worker"
        )
        self.shift = Shift.objects.create(
            name="Morning", start_time=time(7), end_time=time(15), sort_order=10
        )
        self.position = StaffCategory.objects.create(
            program=self.program, name="Support", slug="support", sort_order=10
        )
        self.assignment = ChecklistDefinition.objects.create(
            name="Support Morning", category=self.position, shift=self.shift
        )
        self.section = ChecklistSection.objects.create(name="Tenant Support")
        AssignmentSectionMembership.objects.create(
            assignment=self.assignment, section=self.section, sort_order=10
        )
        self.normal_task = TaskDefinition.objects.create(label="Normal task")
        self.programming_task = TaskDefinition.objects.create(
            label=PROGRAMMING_TASK_LABEL, requires_completion_note=True
        )
        SectionTaskMembership.objects.create(
            section=self.section, task=self.normal_task, sort_order=10
        )
        SectionTaskMembership.objects.create(
            section=self.section, task=self.programming_task, sort_order=20
        )


class LoginAndReusableConfigurationTests(Phase5FixtureMixin, TestCase):
    def test_login_is_case_insensitive_and_preserves_display_casing(self):
        self.assertEqual(
            authenticate(username="jsmith", password="phase-five-password").pk,
            self.operator.pk,
        )
        self.assertEqual(
            authenticate(username="JSMITH", password="phase-five-password").pk,
            self.operator.pk,
        )
        self.operator.refresh_from_db()
        self.assertEqual(self.operator.username, "JSmith")
        with self.assertRaises(IntegrityError), transaction.atomic():
            get_user_model().objects.create_user("jSmItH", password="different")

    def test_reusable_relationships_deduplicate_first_configured_path(self):
        second_section = ChecklistSection.objects.create(name="Also visible")
        AssignmentSectionMembership.objects.create(
            assignment=self.assignment, section=second_section, sort_order=20
        )
        SectionTaskMembership.objects.create(
            section=second_section, task=self.normal_task, sort_order=10
        )
        other_assignment = ChecklistDefinition.objects.create(
            name="Other assignment",
            category=StaffCategory.objects.create(
                program=self.program, name="Other", slug="other"
            ),
            shift=self.shift,
        )
        AssignmentSectionMembership.objects.create(
            assignment=other_assignment, section=self.section, sort_order=10
        )

        instance = resolve_checklist(self.assignment, self.operational_date)
        normal_items = instance.items.filter(source_task=self.normal_task)
        self.assertEqual(normal_items.count(), 1)
        self.assertEqual(normal_items.get().section_name_snapshot, "Tenant Support")
        self.assertEqual(
            build_report(
                program=self.program,
                period=ReportPeriod("daily", self.operational_date, self.operational_date, ""),
            )["totals"]["applicable"],
            2,
        )


class PrivacyAndEntryTests(Phase5FixtureMixin, TestCase):
    def test_staff_page_and_json_do_not_leak_contributor_identity(self):
        instance = resolve_checklist(self.assignment, self.operational_date)
        item = instance.items.get(source_task=self.normal_task)
        change_item_state(
            item_id=item.pk,
            actor=self.operator,
            staff_member=self.staff_b,
            new_state=TaskState.COMPLETED,
        )
        self.client.force_login(self.operator)
        response = self.client.get(
            reverse("checklist-detail", args=(self.assignment.pk,)),
            {"date": self.operational_date, "staff": self.staff_a.pk},
        )
        self.assertContains(
            response,
            "This Chore List is shared. Updates from coworkers appear automatically. By submitting a contribution, you confirm that the information entered accurately represents the work being reported.",
        )
        self.assertNotContains(response, "Chore List selection")
        self.assertNotContains(response, "Recent Staff Contributions")
        self.assertNotContains(response, self.staff_b.display_name)
        self.assertContains(response, "Sonder House")
        payload = self.client.get(
            reverse("checklist-state", args=(self.assignment.pk,)),
            {"date": self.operational_date},
        ).json()
        self.assertNotIn("staff_name", payload["items"][0])
        self.assertEqual(payload["contributions"], [])

        self.client.force_login(self.admin)
        audit_response = self.client.get(
            reverse("checklist-detail", args=(self.assignment.pk,)),
            {"date": self.operational_date, "staff": self.staff_a.pk},
        )
        self.assertContains(audit_response, self.staff_b.display_name)

    def test_discrepancies_are_contributor_scoped_and_reported(self):
        instance = resolve_checklist(self.assignment, self.operational_date)
        save_discrepancy_explanation(
            instance=instance,
            actor=self.operator,
            staff_member=self.staff_a,
            explanation="Alice explanation",
        )
        save_discrepancy_explanation(
            instance=instance,
            actor=self.operator,
            staff_member=self.staff_b,
            explanation="Bob explanation",
        )
        save_discrepancy_explanation(
            instance=instance,
            actor=self.operator,
            staff_member=self.staff_a,
            explanation="Alice revised explanation",
        )
        self.assertEqual(DiscrepancyExplanation.objects.count(), 2)
        self.assertEqual(
            DiscrepancyExplanation.objects.get(staff=self.staff_b).explanation,
            "Bob explanation",
        )
        report = build_report(
            program=self.program,
            period=ReportPeriod("daily", self.operational_date, self.operational_date, ""),
        )
        self.assertCountEqual(
            [row["explanation"] for row in report["rows"][0]["discrepancies"]],
            ["Alice revised explanation", "Bob explanation"],
        )

    def test_programming_completion_note_is_required_atomic_and_persistent(self):
        instance = resolve_checklist(self.assignment, self.operational_date)
        item = instance.items.get(source_task=self.programming_task)
        with self.assertRaisesMessage(ValidationError, "Describe the programming activity"):
            change_item_state(
                item_id=item.pk,
                actor=self.operator,
                staff_member=self.staff_a,
                new_state=TaskState.COMPLETED,
            )
        item.refresh_from_db()
        self.assertEqual(item.current_state, TaskState.PENDING)
        self.assertFalse(item.contributions.exists())
        _, entry = change_item_state(
            item_id=item.pk,
            actor=self.operator,
            staff_member=self.staff_a,
            new_state=TaskState.COMPLETED,
            activity_text="Community meal preparation",
        )
        self.assertEqual(entry.activity_text, "Community meal preparation")
        report_task = next(
            task
            for task in build_report(
                program=self.program,
                period=ReportPeriod("daily", self.operational_date, self.operational_date, ""),
            )["rows"][0]["tasks"]
            if task["id"] == item.pk
        )
        self.assertEqual(
            report_task["contributions"][0]["activity_text"],
            "Community meal preparation",
        )


class ReportExportAndSeedTests(Phase5FixtureMixin, TestCase):
    def test_report_columns_and_print_segmentation_markup(self):
        resolve_checklist(self.assignment, self.operational_date)
        self.client.force_login(self.manager)
        response = self.client.get(reverse("reports"), {"date": self.operational_date})
        self.assertContains(response, "<th>Tasks</th>", html=True)
        self.assertContains(response, "<th>%</th>", html=True)
        self.assertNotContains(response, "<th>Pending</th>", html=True)
        self.assertNotContains(response, "<th>N/A</th>", html=True)
        self.assertContains(response, ">Report</a>")
        printable = self.client.get(reverse("report-print"), {"date": self.operational_date})
        self.assertContains(printable, 'class="print-segment"')
        self.assertContains(printable, "page-break-before:always")
        self.assertContains(printable, ">Report</a>")

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        EMAIL_ENABLED=True,
        DEFAULT_FROM_EMAIL="chore@example.com",
    )
    def test_daily_email_exact_subject_summary_and_accuracy_wording(self):
        self.manager.email = "manager@example.com"
        self.manager.save(update_fields=("email",))
        membership = self.manager.program_memberships.get()
        membership.receive_scheduled_reports = True
        membership.save(update_fields=("receive_scheduled_reports",))
        instance = resolve_checklist(self.assignment, date(2026, 9, 17))
        item = instance.items.get(source_task=self.normal_task)
        change_item_state(
            item_id=item.pk,
            actor=None,
            staff_member=self.staff_a,
            new_state=TaskState.COMPLETED,
            system=True,
        )
        call_command("send_scheduled_reports", at="2026-09-18T08:00:00", verbosity=0)
        message = mail.outbox[0]
        self.assertEqual(
            message.subject,
            "Chore Daily Report --- Sonder House --- 2026-09-17",
        )
        html = message.alternatives[0].content
        self.assertIn("Complete · 1/2 Tasks Complete", html)
        self.assertIn(
            "This report is accurate at the time of delivery, but is subject to change.",
            html,
        )
        self.assertNotIn("pending", html.lower())

    def test_configuration_export_is_admin_only_and_excludes_history_and_secrets(self):
        instance = resolve_checklist(self.assignment, self.operational_date)
        programming_item = instance.items.get(source_task=self.programming_task)
        change_item_state(
            item_id=programming_item.pk,
            actor=self.operator,
            staff_member=self.staff_a,
            new_state=TaskState.COMPLETED,
            activity_text="SENSITIVE OPERATIONAL ACTIVITY",
        )
        save_discrepancy_explanation(
            instance=instance,
            actor=self.operator,
            staff_member=self.staff_a,
            explanation="SENSITIVE DISCREPANCY",
        )
        self.client.force_login(self.operator)
        self.assertEqual(self.client.get(reverse("export-configuration")).status_code, 302)
        self.client.force_login(self.admin)
        response = self.client.get(reverse("export-configuration"))
        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertIn("shift_assignments", payload)
        self.assertIn("assignment_sections", payload)
        self.assertIn("section_tasks", payload)
        exported = response.content.decode()
        self.assertNotIn("SENSITIVE OPERATIONAL ACTIVITY", exported)
        self.assertNotIn("SENSITIVE DISCREPANCY", exported)
        self.assertNotIn(self.operator.password, exported)
        self.assertNotIn("password", exported.lower())
        self.assertNotIn("secret", exported.lower())

    def test_seed_resolves_all_authoritative_assignments_exactly_every_day(self):
        call_command("seed_development", verbosity=0)
        for category_key, shift_key, _, _, expected_sections in ASSIGNMENTS:
            assignment = ChecklistDefinition.objects.get(
                seed_key=f"definition-{category_key}-{shift_key}"
            )
            instance = resolve_checklist(assignment, date(2026, 9, 14))
            expected = [
                (section_name, task["label"], task["allow_na"])
                for section_name, tasks in expected_sections
                for task in tasks
            ]
            actual = [
                (item.section_name_snapshot, item.task_label_snapshot, item.allow_na_snapshot)
                for item in instance.items.all()
            ]
            self.assertEqual(actual, expected)
            self.assertEqual(len(actual), len({item.source_task_id for item in instance.items.all()}))
        life = ChecklistDefinition.objects.get(seed_key="definition-life-skills-morning")
        monday = list(resolve_checklist(life, date(2026, 9, 14)).items.values_list("task_label_snapshot", flat=True))
        sunday = list(resolve_checklist(life, date(2026, 9, 20)).items.values_list("task_label_snapshot", flat=True))
        self.assertEqual(monday, sunday)
        self.assertEqual(len(monday), 28)
