import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, time
from pathlib import Path
from threading import Barrier

from django.contrib.auth import authenticate, get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core import mail
from django.core.management import call_command
from django.db import IntegrityError, close_old_connections, transaction
from django.test import TestCase, TransactionTestCase, override_settings
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


class AdminUserCreationCompatibilityTests(Phase5FixtureMixin, TestCase):
    password = "Admin-hotfix-password-42"

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)
        self.add_url = reverse("admin:auth_user_add")

    def test_admin_user_add_form_includes_password_controls(self):
        response = self.client.get(self.add_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="username"')
        self.assertContains(response, 'name="usable_password"')
        self.assertContains(response, 'name="password1"')
        self.assertContains(response, 'name="password2"')

    def test_admin_can_create_user_with_usable_password(self):
        response = self.client.post(
            self.add_url,
            {
                "username": "AdminCreatedUser",
                "usable_password": "true",
                "password1": self.password,
                "password2": self.password,
                "_save": "Save",
            },
        )

        self.assertEqual(response.status_code, 302)
        created = get_user_model().objects.get(username="AdminCreatedUser")
        self.assertTrue(created.has_usable_password())
        self.assertTrue(created.check_password(self.password))

    def test_admin_rejects_username_that_differs_only_by_case(self):
        response = self.client.post(
            self.add_url,
            {
                "username": "jsmith",
                "usable_password": "true",
                "password1": self.password,
                "password2": self.password,
                "_save": "Save",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFormError(
            response.context["adminform"].form,
            "username",
            "A user with this username already exists, regardless of letter case.",
        )
        self.assertEqual(
            get_user_model().objects.filter(username__iexact="JSmith").count(),
            1,
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
            "Marking a task complete implies the work being reported is satisfactorily completed.",
        )
        self.assertContains(
            response,
            "These lists allow collaboration, updates from colleagues appear automatically.",
        )
        self.assertContains(response, 'class="shared-notice-line"', count=2)
        self.assertNotContains(response, "This Chore List is shared.")
        self.assertNotContains(
            response,
            "By submitting a contribution, you confirm that the information entered accurately represents the work being reported.",
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

    def test_discrepancy_authorization_rejects_cross_program_actor_and_staff(self):
        instance = resolve_checklist(self.assignment, self.operational_date)
        other_program = Program.objects.create(name="Other Program", slug="other-program")
        outsider = get_user_model().objects.create_user(
            "outsider5", password="phase-five-password"
        )
        ProgramMembership.objects.create(
            user=outsider, program=other_program, role=ProgramRole.OPERATIONAL
        )
        other_staff = StaffMember.objects.create(
            program=other_program, first_name="Other", last_name="Worker"
        )

        with self.assertRaises(PermissionDenied):
            save_discrepancy_explanation(
                instance=instance,
                actor=outsider,
                staff_member=self.staff_a,
                explanation="Unauthorized actor",
            )
        with self.assertRaises(PermissionDenied):
            save_discrepancy_explanation(
                instance=instance,
                actor=self.operator,
                staff_member=other_staff,
                explanation="Unauthorized staff context",
            )
        with self.assertRaises(PermissionDenied):
            save_discrepancy_explanation(
                instance=instance,
                actor=self.manager,
                staff_member=self.staff_a,
                explanation="Managers cannot make operational entries",
            )

        self.client.force_login(outsider)
        response = self.client.post(
            reverse("update-discrepancy", args=(instance.pk,)),
            {"staff": self.staff_a.pk, "explanation": "HTTP unauthorized actor"},
        )
        self.assertEqual(response.status_code, 403)

        self.client.force_login(self.operator)
        response = self.client.post(
            reverse("update-discrepancy", args=(instance.pk,)),
            {"staff": other_staff.pk, "explanation": "HTTP unauthorized staff"},
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(DiscrepancyExplanation.objects.exists())

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


class PresentationCorrectionTests(Phase5FixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.normal_task.allow_na = True
        self.normal_task.save(update_fields=("allow_na",))
        self.instance = resolve_checklist(self.assignment, self.operational_date)
        self.client.force_login(self.operator)

    def render_checklist(self):
        return self.client.get(
            reverse("checklist-detail", args=(self.assignment.pk,)),
            {"date": self.operational_date, "staff": self.staff_a.pk},
        )

    def test_chore_list_copy_programming_and_discrepancy_layout_markup(self):
        response = self.render_checklist()
        programming_item = self.instance.items.get(source_task=self.programming_task)

        self.assertContains(response, "<span>Complete</span>", html=True)
        self.assertNotContains(response, "<span>resolved</span>", html=True)
        self.assertNotContains(response, "Programming activity (required to complete)")
        self.assertContains(response, 'name="activity_text"')
        self.assertContains(response, f'form="task-form-{programming_item.pk}"')
        content = response.content.decode()
        programming_card = content[
            content.index(f'data-item-id="{programming_item.pk}"'):
            content.index("</article>", content.index(f'data-item-id="{programming_item.pk}"'))
        ]
        self.assertLess(
            programming_card.index("Complete programming and fill out programming report"),
            programming_card.index('name="activity_text"'),
        )
        self.assertLess(
            programming_card.index('name="activity_text"'),
            programming_card.index("data-item-status"),
        )
        self.assertContains(response, "<h2>Discrepancy Explanation</h2>", html=True)
        self.assertNotContains(
            response,
            "Explain any incomplete, unusual, or otherwise discrepant work for your contribution to this Chore List.",
        )
        self.assertNotContains(response, '<label for="discrepancy-explanation">')
        self.assertContains(response, 'aria-label="Discrepancy Explanation"')

    def test_initial_and_synchronized_actions_put_na_first(self):
        response = self.render_checklist()
        optional_item = self.instance.items.get(source_task=self.normal_task)
        content = response.content.decode()
        start = content.index(f'data-item-id="{optional_item.pk}"')
        card = content[start:content.index("</article>", start)]
        self.assertLess(card.index('value="na"'), card.index('value="completed"'))

        change_item_state(
            item_id=optional_item.pk,
            actor=self.operator,
            staff_member=self.staff_a,
            new_state=TaskState.COMPLETED,
        )
        content = self.render_checklist().content.decode()
        start = content.index(f'data-item-id="{optional_item.pk}"')
        card = content[start:content.index("</article>", start)]
        self.assertLess(card.index('value="na"'), card.index('value="pending"'))

        sync_source = (
            Path(__file__).parent / "static" / "checklists" / "checklist_sync.js"
        ).read_text(encoding="utf-8")
        refresh_actions = sync_source[
            sync_source.index("const refreshActions"):
            sync_source.index("const applyItem")
        ]
        self.assertLess(
            refresh_actions.index('makeButton("N/A"'),
            refresh_actions.index('makeButton("Complete"'),
        )
        self.assertLess(
            refresh_actions.index('makeButton("Complete"'),
            refresh_actions.index('makeButton("Reset"'),
        )


class ConcurrentDiscrepancyTests(Phase5FixtureMixin, TransactionTestCase):
    reset_sequences = True

    def _run_together(self, operations):
        barrier = Barrier(len(operations))

        def run(operation):
            close_old_connections()
            try:
                barrier.wait()
                return operation()
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=len(operations)) as executor:
            futures = [executor.submit(run, operation) for operation in operations]
            return [future.result(timeout=15) for future in futures]

    def _update(self, instance_id, staff_id, explanation):
        def operation():
            return save_discrepancy_explanation(
                instance=ChecklistInstance.objects.get(pk=instance_id),
                actor=get_user_model().objects.get(pk=self.operator.pk),
                staff_member=StaffMember.objects.get(pk=staff_id),
                explanation=explanation,
            ).pk

        return operation

    def test_concurrent_updates_preserve_contributor_isolation_and_single_row_safety(self):
        instance = resolve_checklist(self.assignment, self.operational_date)
        save_discrepancy_explanation(
            instance=instance,
            actor=self.operator,
            staff_member=self.staff_a,
            explanation="Alice initial",
        )
        save_discrepancy_explanation(
            instance=instance,
            actor=self.operator,
            staff_member=self.staff_b,
            explanation="Bob initial",
        )

        self._run_together(
            (
                self._update(instance.pk, self.staff_a.pk, "Alice concurrent update"),
                self._update(instance.pk, self.staff_b.pk, "Bob concurrent update"),
            )
        )
        self.assertEqual(
            DiscrepancyExplanation.objects.get(instance=instance, staff=self.staff_a).explanation,
            "Alice concurrent update",
        )
        self.assertEqual(
            DiscrepancyExplanation.objects.get(instance=instance, staff=self.staff_b).explanation,
            "Bob concurrent update",
        )

        self._run_together(
            (
                self._update(instance.pk, self.staff_a.pk, "Alice race one"),
                self._update(instance.pk, self.staff_a.pk, "Alice race two"),
            )
        )
        self.assertEqual(
            DiscrepancyExplanation.objects.filter(instance=instance, staff=self.staff_a).count(),
            1,
        )
        self.assertIn(
            DiscrepancyExplanation.objects.get(instance=instance, staff=self.staff_a).explanation,
            {"Alice race one", "Alice race two"},
        )
        self.assertEqual(
            DiscrepancyExplanation.objects.get(instance=instance, staff=self.staff_b).explanation,
            "Bob concurrent update",
        )
        self.assertEqual(DiscrepancyExplanation.objects.filter(instance=instance).count(), 2)


class RoleAndAdminAuthorizationTests(Phase5FixtureMixin, TestCase):
    def test_manager_can_view_contributor_audit_but_cannot_make_operational_entries(self):
        instance = resolve_checklist(self.assignment, self.operational_date)
        item = instance.items.get(source_task=self.normal_task)
        change_item_state(
            item_id=item.pk,
            actor=self.operator,
            staff_member=self.staff_b,
            new_state=TaskState.COMPLETED,
        )
        self.client.force_login(self.manager)
        response = self.client.get(reverse("report-detail", args=(instance.pk,)))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.staff_b.display_name)
        self.assertContains(response, "Latest production contribution by")

        response = self.client.post(
            reverse("update-item-state", args=(item.pk,)),
            {"state": TaskState.PENDING, "staff": self.staff_a.pk},
        )
        self.assertEqual(response.status_code, 403)
        item.refresh_from_db()
        self.assertEqual(item.current_state, TaskState.COMPLETED)
        self.assertEqual(item.current_staff, self.staff_b)

    def test_admin_can_manage_reusable_relationships_from_both_sides(self):
        self.client.force_login(self.admin)
        task_response = self.client.get(
            reverse("admin:checklists_taskdefinition_change", args=(self.normal_task.pk,))
        )
        self.assertEqual(task_response.status_code, 200)
        self.assertContains(task_response, "Section memberships")
        self.assertContains(task_response, "section_memberships-TOTAL_FORMS")

        section_response = self.client.get(
            reverse("admin:checklists_checklistsection_change", args=(self.section.pk,))
        )
        self.assertEqual(section_response.status_code, 200)
        self.assertContains(section_response, "Ordered tasks")
        self.assertContains(section_response, "task_memberships-TOTAL_FORMS")
        self.assertContains(section_response, "Shift assignment memberships")
        self.assertContains(section_response, "assignment_memberships-TOTAL_FORMS")

        assignment_response = self.client.get(
            reverse("admin:checklists_checklistdefinition_change", args=(self.assignment.pk,))
        )
        self.assertEqual(assignment_response.status_code, 200)
        self.assertContains(assignment_response, "Ordered sections")
        self.assertContains(assignment_response, "section_memberships-TOTAL_FORMS")

        task_from_admin = TaskDefinition.objects.create(label="Admin reusable task")
        response = self.client.post(
            reverse(
                "admin:checklists_taskdefinition_change", args=(task_from_admin.pk,)
            ),
            {
                "label": task_from_admin.label,
                "is_active": "on",
                "section_memberships-TOTAL_FORMS": "1",
                "section_memberships-INITIAL_FORMS": "0",
                "section_memberships-MIN_NUM_FORMS": "0",
                "section_memberships-MAX_NUM_FORMS": "1000",
                "section_memberships-0-section": str(self.section.pk),
                "section_memberships-0-sort_order": "30",
                "_save": "Save",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            SectionTaskMembership.objects.filter(
                section=self.section, task=task_from_admin, sort_order=30
            ).exists()
        )

        section_from_admin = ChecklistSection.objects.create(
            name="Admin reusable section", sort_order=90
        )
        reverse_task = TaskDefinition.objects.create(label="Admin reverse task")
        response = self.client.post(
            reverse(
                "admin:checklists_checklistsection_change", args=(section_from_admin.pk,)
            ),
            {
                "name": section_from_admin.name,
                "sort_order": "90",
                "is_active": "on",
                "task_memberships-TOTAL_FORMS": "1",
                "task_memberships-INITIAL_FORMS": "0",
                "task_memberships-MIN_NUM_FORMS": "0",
                "task_memberships-MAX_NUM_FORMS": "1000",
                "task_memberships-0-sort_order": "10",
                "task_memberships-0-task": str(reverse_task.pk),
                "assignment_memberships-TOTAL_FORMS": "1",
                "assignment_memberships-INITIAL_FORMS": "0",
                "assignment_memberships-MIN_NUM_FORMS": "0",
                "assignment_memberships-MAX_NUM_FORMS": "1000",
                "assignment_memberships-0-assignment": str(self.assignment.pk),
                "assignment_memberships-0-sort_order": "20",
                "_save": "Save",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            SectionTaskMembership.objects.filter(
                section=section_from_admin, task=reverse_task, sort_order=10
            ).exists()
        )
        self.assertTrue(
            AssignmentSectionMembership.objects.filter(
                assignment=self.assignment, section=section_from_admin, sort_order=20
            ).exists()
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
        self.assertContains(response, ">Chore Reports</span>")
        self.assertContains(response, ">Tasks</span>")
        self.assertContains(response, ">Complete</span>")
        self.assertNotContains(response, "<span>%</span>", html=True)
        printable = self.client.get(reverse("report-print"), {"date": self.operational_date})
        self.assertContains(printable, 'class="print-segment"')
        self.assertContains(printable, "page-break-before:always")
        self.assertNotContains(printable, ">Report</a>")
        self.assertContains(printable, ">Print / Save PDF</button>")
        self.assertContains(printable, "<h1>Chore Reports</h1>", html=True)
        self.assertNotContains(printable, "<h1>Chore Report</h1>", html=True)

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
