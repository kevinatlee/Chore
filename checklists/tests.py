import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, time, timedelta
from io import StringIO
from pathlib import Path
from threading import Barrier
from unittest.mock import call, patch

from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.forms import AdminPasswordChangeForm
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.db import IntegrityError, OperationalError, close_old_connections, models, transaction
from django.test import Client, SimpleTestCase, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    AssignmentSectionMembership,
    ChecklistDefinition,
    ChecklistInstance,
    ChecklistItem,
    ChecklistSection,
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
from .seed_data import ASSIGNMENTS, STAFF_ROSTER
from .presentation import display_task_text
from .services import (
    SQLITE_LOCK_ATTEMPTS,
    SQLITE_LOCK_BACKOFF_SECONDS,
    _run_serialized_write,
    change_item_state,
    resolve_checklist,
)


class OperationalFixtureMixin:
    operational_date = date(2026, 9, 16)

    def setUp(self):
        User = get_user_model()
        self.program = Program.objects.create(name="Operational Fixture", slug="operational-fixture")
        self.operator = User.objects.create_user("operations", password="password")
        ProgramMembership.objects.create(
            user=self.operator,
            program=self.program,
            role=ProgramRole.OPERATIONAL,
        )
        self.manager = User.objects.create_user("manager", password="password")
        ProgramMembership.objects.create(
            user=self.manager,
            program=self.program,
            role=ProgramRole.MANAGER,
        )
        self.admin = User.objects.create_superuser("admin", "admin@example.com", "password")
        self.staff_a = StaffMember.objects.create(
            program=self.program, first_name="Alfred", last_name="Sampare"
        )
        self.staff_b = StaffMember.objects.create(
            program=self.program, first_name="Chelsea", last_name="Brown"
        )
        self.shift = Shift.objects.create(
            name="Morning", start_time=time(7), end_time=time(15), sort_order=10
        )
        self.category = StaffCategory.objects.create(
            program=self.program, name="Support", slug="support", sort_order=10
        )
        self.definition = ChecklistDefinition.objects.create(
            name="Support Morning Checklist",
            category=self.category,
            shift=self.shift,
            sort_order=10,
        )
        self.section = ChecklistSection.objects.create(name="Office", sort_order=10)
        self.assignment_section = AssignmentSectionMembership.objects.create(
            assignment=self.definition, section=self.section, sort_order=10
        )
        self.regular = TaskDefinition.objects.create(
            label="Regular task"
        )
        self.optional = TaskDefinition.objects.create(
            label="Optional task", allow_na=True
        )
        self.regular_membership = SectionTaskMembership.objects.create(
            section=self.section, task=self.regular, sort_order=10
        )
        self.optional_membership = SectionTaskMembership.objects.create(
            section=self.section, task=self.optional, sort_order=20
        )


class TaskPresentationTests(SimpleTestCase):
    def test_sentence_case_slash_spacing_and_protected_terms(self):
        self.assertEqual(display_task_text("Sweep/Mop Hallways"), "Sweep / mop hallways")
        self.assertEqual(
            display_task_text("Disinfect Phone/Door Handles"),
            "Disinfect phone / door handles",
        )
        self.assertEqual(
            display_task_text("Keep work area clean/organized/dusted; N/A if closed"),
            "Keep work area clean / organized / dusted; N/A if closed",
        )
        self.assertEqual(
            display_task_text("Check wish/ComVida, dna, hr, Facebook"),
            "Check WISH / ComVida, DNA, HR, Facebook",
        )
        self.assertEqual(
            display_task_text("Review https://example.test/a/b and /var/log/chore"),
            "Review https://example.test/a/b and /var/log/chore",
        )


class PasswordPolicyTests(OperationalFixtureMixin, TestCase):
    simple_password = "123"

    def test_only_shared_operational_account_receives_password_exception(self):
        validate_password(self.simple_password, self.operator)
        for user in (self.manager, self.admin):
            with self.subTest(user=user.username):
                with self.assertRaises(ValidationError):
                    validate_password(self.simple_password, user)

        operational_form = AdminPasswordChangeForm(
            self.operator,
            data={"password1": self.simple_password, "password2": self.simple_password},
        )
        self.assertTrue(operational_form.is_valid(), operational_form.errors)
        operational_form.save()
        self.operator.refresh_from_db()
        self.assertNotEqual(self.operator.password, self.simple_password)
        self.assertTrue(self.operator.check_password(self.simple_password))
        self.assertIsNotNone(
            authenticate(username=self.operator.username, password=self.simple_password)
        )
        self.assertIsNone(authenticate(username=self.operator.username, password="wrong"))

        for user in (self.manager, self.admin):
            with self.subTest(admin_form=user.username):
                form = AdminPasswordChangeForm(
                    user,
                    data={
                        "password1": self.simple_password,
                        "password2": self.simple_password,
                    },
                )
                self.assertFalse(form.is_valid())
                self.assertIn("password2", form.errors)


class SharedChecklistDomainTests(OperationalFixtureMixin, TestCase):
    def test_two_staff_open_the_same_shared_checklist(self):
        first = resolve_checklist(self.definition, self.operational_date)
        second = resolve_checklist(self.definition, self.operational_date)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(ChecklistInstance.objects.count(), 1)

    def test_state_change_uses_operational_staff_and_records_actor_separately(self):
        instance = resolve_checklist(self.definition, self.operational_date)
        item = instance.items.get(source_task=self.regular)
        changed, contribution = change_item_state(
            item_id=item.pk,
            actor=self.operator,
            staff_member=self.staff_a,
            new_state=TaskState.COMPLETED,
        )
        self.assertEqual(changed.current_staff, self.staff_a)
        self.assertEqual(contribution.staff, self.staff_a)
        self.assertEqual(contribution.recorded_by, self.operator)
        self.assertNotEqual(contribution.staff.__class__, get_user_model())

    def test_na_is_per_task_and_append_only(self):
        instance = resolve_checklist(self.definition, self.operational_date)
        regular = instance.items.get(source_task=self.regular)
        optional = instance.items.get(source_task=self.optional)
        with self.assertRaises(ValidationError):
            change_item_state(
                item_id=regular.pk,
                actor=self.operator,
                staff_member=self.staff_a,
                new_state=TaskState.NOT_APPLICABLE,
            )
        _, contribution = change_item_state(
            item_id=optional.pk,
            actor=self.operator,
            staff_member=self.staff_a,
            new_state=TaskState.NOT_APPLICABLE,
        )
        with self.assertRaises(ValidationError):
            contribution.delete()

    def test_inactive_staff_retains_history_but_cannot_make_new_contributions(self):
        instance = resolve_checklist(self.definition, self.operational_date)
        item = instance.items.get(source_task=self.regular)
        change_item_state(
            item_id=item.pk,
            actor=self.operator,
            staff_member=self.staff_a,
            new_state=TaskState.COMPLETED,
        )
        self.staff_a.is_active = False
        self.staff_a.save(update_fields=("is_active",))
        self.assertEqual(item.contributions.get().staff.display_name, "Alfred Sampare")
        with self.assertRaises(PermissionDenied):
            change_item_state(
                item_id=item.pk,
                actor=self.operator,
                staff_member=self.staff_a,
                new_state=TaskState.PENDING,
            )

    def test_manager_cannot_make_operational_entry(self):
        item = resolve_checklist(self.definition, self.operational_date).items.first()
        with self.assertRaises(PermissionDenied):
            change_item_state(
                item_id=item.pk,
                actor=self.manager,
                staff_member=self.staff_a,
                new_state=TaskState.COMPLETED,
            )

    def test_historical_snapshots_do_not_follow_configuration_edits(self):
        self.regular.scheduled_start = time(8)
        self.regular.scheduled_end = time(9)
        self.regular.save(update_fields=("scheduled_start", "scheduled_end"))
        instance = resolve_checklist(self.definition, self.operational_date)
        item = instance.items.get(source_task=self.regular)
        self.category.name = "Renamed"
        self.category.save(update_fields=("name",))
        self.shift.name = "Changed"
        self.shift.start_time = time(6)
        self.shift.end_time = time(14)
        self.shift.save(update_fields=("name", "start_time", "end_time"))
        self.section.name = "Changed section"
        self.assignment_section.sort_order = 90
        self.assignment_section.save(update_fields=("sort_order",))
        self.section.save(update_fields=("name",))
        self.regular.label = "Changed task"
        self.regular_membership.sort_order = 90
        self.regular_membership.save(update_fields=("sort_order",))
        self.regular.scheduled_start = time(10)
        self.regular.scheduled_end = time(11)
        self.regular.save(
            update_fields=("label", "scheduled_start", "scheduled_end")
        )
        instance.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(instance.category_name_snapshot, "Support")
        self.assertEqual(instance.shift_name_snapshot, "Morning")
        self.assertEqual(instance.shift_start_snapshot, time(7))
        self.assertEqual(instance.shift_end_snapshot, time(15))
        self.assertEqual(item.section_name_snapshot, "Office")
        self.assertEqual(item.section_order_snapshot, 10)
        self.assertEqual(item.task_label_snapshot, "Regular task")
        self.assertEqual(item.task_order_snapshot, 10)
        self.assertEqual(item.scheduled_start_snapshot, time(8))
        self.assertEqual(item.scheduled_end_snapshot, time(9))

    def test_task_display_is_normalized_without_rewriting_history_or_attribution(self):
        source_label = "Sweep/Mop Hallways; Check WISH/ComVida, DNA, HR, Facebook"
        self.regular.label = source_label
        self.regular.save(update_fields=("label",))
        self.client.force_login(self.admin)
        admin_response = self.client.get(reverse("admin:checklists_taskdefinition_changelist"))
        self.assertContains(
            admin_response,
            "Sweep / mop hallways; check WISH / ComVida, DNA, HR, Facebook",
        )
        instance = resolve_checklist(self.definition, self.operational_date)
        item = instance.items.get(source_task=self.regular)
        change_item_state(
            item_id=item.pk,
            actor=self.operator,
            staff_member=self.staff_a,
            new_state=TaskState.COMPLETED,
        )
        self.regular.label = "Later configuration wording"
        self.regular.save(update_fields=("label",))

        item.refresh_from_db()
        self.assertEqual(item.task_label_snapshot, source_label)
        self.assertEqual(item.current_staff, self.staff_a)
        self.client.force_login(self.operator)
        response = self.client.get(
            reverse("checklist-detail", args=(self.definition.pk,)),
            {"date": self.operational_date, "staff": self.staff_a.pk},
        )
        self.assertContains(
            response,
            "Sweep / mop hallways; check WISH / ComVida, DNA, HR, Facebook",
        )
        self.assertNotContains(response, "Later configuration wording")

    def test_definition_identity_uniqueness_and_ordered_history_invariants(self):
        instance = resolve_checklist(self.definition, self.operational_date)
        other_category = StaffCategory.objects.create(
            program=self.program, name="Other", slug="other", sort_order=20
        )
        third_category = StaffCategory.objects.create(
            program=self.program, name="Third", slug="third", sort_order=30
        )
        evening = Shift.objects.create(
            name="Evening", start_time=time(15), end_time=time(23), sort_order=20
        )
        night = Shift.objects.create(
            name="Night", start_time=time(23), end_time=time(7), sort_order=30
        )

        self.definition.category = other_category
        with self.assertRaises(ValidationError):
            self.definition.save()
        self.definition.refresh_from_db()
        self.definition.shift = evening
        with self.assertRaises(ValidationError):
            self.definition.save()
        self.definition.refresh_from_db()

        unused = ChecklistDefinition.objects.create(
            name="Unused", category=other_category, shift=evening
        )
        unused.category = third_category
        unused.save(update_fields=("category",))
        unused.shift = night
        unused.save(update_fields=("shift",))
        self.assertEqual((unused.category, unused.shift), (third_category, night))

        with self.assertRaises(IntegrityError), transaction.atomic():
            ChecklistInstance.objects.create(
                program=self.program,
                operational_date=self.operational_date,
                definition=self.definition,
                category=self.category,
                shift=self.shift,
                category_name_snapshot="Support",
                shift_name_snapshot="Morning",
                shift_start_snapshot=time(7),
                shift_end_snapshot=time(15),
            )
        self.assertEqual(ChecklistInstance.objects.get().pk, instance.pk)

        item = instance.items.get(source_task=self.optional)
        change_item_state(
            item_id=item.pk,
            actor=self.operator,
            staff_member=self.staff_a,
            new_state=TaskState.COMPLETED,
        )
        change_item_state(
            item_id=item.pk,
            actor=self.operator,
            staff_member=self.staff_b,
            new_state=TaskState.PENDING,
        )
        history = list(item.contributions.all())
        self.assertEqual([entry.staff for entry in history], [self.staff_a, self.staff_b])
        self.assertEqual(history[1].previous_state, TaskState.COMPLETED)
        self.assertEqual(history[1].new_state, TaskState.PENDING)

    def test_na_ui_backend_and_write_access_guards(self):
        instance = resolve_checklist(self.definition, self.operational_date)
        regular = instance.items.get(source_task=self.regular)
        self.client.force_login(self.operator)
        detail = self.client.get(
            reverse("checklist-detail", args=(self.definition.pk,)),
            {"date": self.operational_date, "staff": self.staff_a.pk},
        )
        self.assertContains(detail, 'name="state" value="na"', count=1)
        response = self.client.post(
            reverse("update-item-state", args=(regular.pk,)),
            {"state": TaskState.NOT_APPLICABLE, "staff": self.staff_a.pk},
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(regular.contributions.exists())

        self.client.logout()
        response = self.client.post(
            reverse("update-item-state", args=(regular.pk,)),
            {"state": TaskState.COMPLETED, "staff": self.staff_a.pk},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)

        self.operator.is_active = False
        self.operator.save(update_fields=("is_active",))
        with self.assertRaises(PermissionDenied):
            change_item_state(
                item_id=regular.pk,
                actor=self.operator,
                staff_member=self.staff_a,
                new_state=TaskState.COMPLETED,
            )
        self.operator.is_active = True
        self.operator.save(update_fields=("is_active",))
        self.definition.is_active = False
        self.definition.save(update_fields=("is_active",))
        self.client.force_login(self.operator)
        response = self.client.post(
            reverse("update-item-state", args=(regular.pk,)),
            {"state": TaskState.COMPLETED, "staff": self.staff_a.pk},
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(regular.contributions.exists())


class SQLiteRetryPolicyTests(SimpleTestCase):
    def test_retry_policy_handles_only_bounded_sqlite_lock_errors(self):
        attempts = 0

        def succeeds_after_locks():
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise OperationalError("database is locked")
            return "completed"

        with (
            patch("checklists.services.connection") as mocked_connection,
            patch("checklists.services.time.sleep") as mocked_sleep,
        ):
            mocked_connection.vendor = "sqlite"
            self.assertEqual(_run_serialized_write(succeeds_after_locks), "completed")
        self.assertEqual(attempts, 3)
        self.assertEqual(
            mocked_sleep.call_args_list,
            [call(SQLITE_LOCK_BACKOFF_SECONDS), call(SQLITE_LOCK_BACKOFF_SECONDS * 2)],
        )

        attempts = 0

        def unrelated_error():
            nonlocal attempts
            attempts += 1
            raise OperationalError("disk I/O error")

        with (
            patch("checklists.services.connection") as mocked_connection,
            patch("checklists.services.time.sleep") as mocked_sleep,
        ):
            mocked_connection.vendor = "sqlite"
            with self.assertRaisesMessage(OperationalError, "disk I/O error"):
                _run_serialized_write(unrelated_error)
        self.assertEqual(attempts, 1)
        mocked_sleep.assert_not_called()

        attempts = 0

        def permanently_locked():
            nonlocal attempts
            attempts += 1
            raise OperationalError("database is locked")

        with (
            patch("checklists.services.connection") as mocked_connection,
            patch("checklists.services.time.sleep") as mocked_sleep,
        ):
            mocked_connection.vendor = "sqlite"
            with self.assertRaisesMessage(OperationalError, "database is locked"):
                _run_serialized_write(permanently_locked)
        self.assertEqual(attempts, SQLITE_LOCK_ATTEMPTS)
        self.assertEqual(mocked_sleep.call_count, SQLITE_LOCK_ATTEMPTS - 1)


class ConcurrentChecklistTests(OperationalFixtureMixin, TransactionTestCase):
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
            return [future.result(timeout=10) for future in futures]

    def test_concurrent_creation_and_state_changes_remain_serialized(self):
        def resolve():
            definition = ChecklistDefinition.objects.select_related(
                "category__program", "shift"
            ).get(pk=self.definition.pk)
            return resolve_checklist(definition, self.operational_date).pk

        instance_ids = self._run_together((resolve, resolve))
        self.assertEqual(instance_ids[0], instance_ids[1])
        self.assertEqual(ChecklistInstance.objects.count(), 1)
        instance = ChecklistInstance.objects.get()
        item = instance.items.get(source_task=self.optional)

        def change(staff_id, state):
            def operation():
                actor = get_user_model().objects.get(pk=self.operator.pk)
                staff = StaffMember.objects.get(pk=staff_id)
                changed_item, contribution = change_item_state(
                    item_id=item.pk,
                    actor=actor,
                    staff_member=staff,
                    new_state=state,
                )
                return changed_item.current_state, contribution.pk

            return operation

        self._run_together(
            (
                change(self.staff_a.pk, TaskState.COMPLETED),
                change(self.staff_b.pk, TaskState.NOT_APPLICABLE),
            )
        )
        history = list(item.contributions.order_by("created_at", "id"))
        self.assertEqual(len(history), 2)
        self.assertEqual({entry.staff_id for entry in history}, {self.staff_a.pk, self.staff_b.pk})
        self.assertEqual(history[0].previous_state, TaskState.PENDING)
        self.assertEqual(history[1].previous_state, history[0].new_state)
        item.refresh_from_db()
        self.assertEqual(item.current_state, history[1].new_state)
        self.assertEqual(item.current_staff_id, history[1].staff_id)


class SharedChecklistSynchronizationTests(OperationalFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.instance = resolve_checklist(self.definition, self.operational_date)
        self.regular_item = self.instance.items.get(source_task=self.regular)
        self.optional_item = self.instance.items.get(source_task=self.optional)
        self.client_a = Client()
        self.client_b = Client()
        self.client_a.force_login(self.operator)
        self.client_b.force_login(self.operator)

    def _state_url(self):
        return reverse("checklist-state", args=(self.definition.pk,))

    def _ajax_change(self, client, item, staff, state):
        return client.post(
            reverse("update-item-state", args=(item.pk,)),
            {"state": state, "staff": staff.pk},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ACCEPT="application/json",
        )

    def test_two_staff_sessions_converge_through_canonical_state(self):
        detail_a = self.client_a.get(
            reverse("checklist-detail", args=(self.definition.pk,)),
            {"date": self.operational_date, "staff": self.staff_a.pk},
        )
        detail_b = self.client_b.get(
            reverse("checklist-detail", args=(self.definition.pk,)),
            {"date": self.operational_date, "staff": self.staff_b.pk},
        )
        self.assertEqual(detail_a.context["instance"].pk, detail_b.context["instance"].pk)
        self.assertContains(
            detail_a,
            "Marking a task complete implies the work being reported is satisfactorily completed.",
        )
        self.assertContains(detail_a, "checklist_sync.js")

        response_a = self._ajax_change(
            self.client_a, self.regular_item, self.staff_a, TaskState.COMPLETED
        )
        self.assertEqual(response_a.status_code, 200)
        first_state = response_a.json()
        regular = next(item for item in first_state["items"] if item["id"] == self.regular_item.pk)
        self.assertEqual(regular["state"], TaskState.COMPLETED)
        self.assertNotIn("staff_name", regular)
        self.assertEqual(first_state["contributions"], [])

        poll_b = self.client_b.get(
            self._state_url(),
            {"date": self.operational_date, "revision": "0"},
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(poll_b.status_code, 200)
        self.assertTrue(poll_b.json()["changed"])
        self.assertEqual(poll_b.json()["revision"], first_state["revision"])

        response_b = self._ajax_change(
            self.client_b, self.optional_item, self.staff_b, TaskState.NOT_APPLICABLE
        )
        self.assertEqual(response_b.status_code, 200)
        canonical = response_b.json()
        by_id = {item["id"]: item for item in canonical["items"]}
        self.assertEqual(by_id[self.regular_item.pk]["state"], TaskState.COMPLETED)
        self.assertNotIn("staff_name", by_id[self.regular_item.pk])
        self.assertEqual(by_id[self.optional_item.pk]["state"], TaskState.NOT_APPLICABLE)
        self.assertNotIn("staff_name", by_id[self.optional_item.pk])
        self.assertEqual(canonical["resolved_count"], 2)

        poll_a = self.client_a.get(
            self._state_url(),
            {"date": self.operational_date, "revision": first_state["revision"]},
            HTTP_ACCEPT="application/json",
        )
        self.assertTrue(poll_a.json()["changed"])
        self.assertEqual(poll_a.json()["revision"], canonical["revision"])
        unchanged = self.client_a.get(
            self._state_url(),
            {"date": self.operational_date, "revision": canonical["revision"]},
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(unchanged.json(), {"changed": False, "revision": canonical["revision"]})

    def test_sync_endpoint_preserves_program_and_role_boundaries(self):
        operational_response = self.client_a.get(
            self._state_url(), {"date": self.operational_date}
        )
        self.assertEqual(operational_response.status_code, 200)

        manager_client = Client()
        manager_client.force_login(self.manager)
        manager_response = manager_client.get(
            self._state_url(), {"date": self.operational_date}
        )
        self.assertEqual(manager_response.status_code, 403)

        admin_client = Client()
        admin_client.force_login(self.admin)
        admin_response = admin_client.get(
            self._state_url(), {"date": self.operational_date}
        )
        self.assertEqual(admin_response.status_code, 200)

        other_program = Program.objects.create(name="Other Program", slug="other-program")
        other_category = StaffCategory.objects.create(
            program=other_program, name="Other", slug="other", sort_order=10
        )
        other_definition = ChecklistDefinition.objects.create(
            name="Other Morning", category=other_category, shift=self.shift, sort_order=10
        )
        other_section = ChecklistSection.objects.create(name="Other section", sort_order=10)
        AssignmentSectionMembership.objects.create(
            assignment=other_definition, section=other_section, sort_order=10
        )
        other_task = TaskDefinition.objects.create(label="Other task")
        SectionTaskMembership.objects.create(
            section=other_section, task=other_task, sort_order=10
        )
        resolve_checklist(other_definition, self.operational_date)
        cross_program = self.client_a.get(
            reverse("checklist-state", args=(other_definition.pk,)),
            {"date": self.operational_date},
        )
        self.assertEqual(cross_program.status_code, 403)


class AdministrationClarityTests(OperationalFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)

    def test_admin_header_is_compact_and_keeps_configuration_export(self):
        index = self.client.get(reverse("admin:index"))
        self.assertNotContains(index, "Configuration affects future Chore Lists.")
        self.assertNotContains(index, "preserving historical Chore Lists")
        self.assertNotContains(index, "Chore configuration and reporting")

        task_page = self.client.get(reverse("admin:checklists_taskdefinition_add"))
        self.assertContains(task_page, "Allow N/A")
        self.assertContains(task_page, "Require completion note")
        self.assertContains(task_page, "admin_clarity.js")
        self.assertContains(index, "Export Configuration")
        self.assertNotContains(index, "Groups")

    def test_shift_assignment_admin_exposes_editable_sort_order(self):
        response = self.client.get(
            reverse("admin:checklists_checklistdefinition_changelist")
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sort order")
        self.assertContains(response, 'name="form-0-sort_order"')
        self.assertContains(response, "Monday–Friday only")
        self.assertContains(response, 'name="form-0-weekdays_only"')


class SelectorAndRoleTests(OperationalFixtureMixin, TestCase):
    def test_selector_is_compact_dropdown_workflow_using_staff_records(self):
        self.client.force_login(self.operator)
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, self.program.name.upper())
        self.assertContains(response, "<h1>Select your Shift</h1>", html=True)
        self.assertContains(response, "For Night shifts, use the date on which the shift starts.")
        self.assertNotContains(response, "Choose an operational date")
        self.assertNotContains(response, "Operational date")
        self.assertContains(response, '<option value="">Select Staff</option>', html=True)
        self.assertContains(response, '<button class="button primary" type="submit">Select Shift</button>', html=True)
        today = timezone.localdate()
        self.assertContains(
            response, f'min="{(today - timedelta(days=7)).isoformat()}"'
        )
        self.assertContains(response, f'max="{today.isoformat()}"')
        self.assertContains(response, 'type="date"')
        self.assertContains(response, 'class="date-control"')
        self.assertContains(response, 'class="date-control-display"')
        self.assertContains(response, 'class="date-control-input"')
        self.assertContains(response, 'id="date"')
        self.assertContains(response, 'name="date"')
        self.assertContains(response, "required")
        self.assertContains(
            response, f">{today.strftime('%b')} {today.day}, {today.year}</span>"
        )
        self.assertContains(response, "date_control.js")
        self.assertContains(response, 'name="staff"')
        self.assertContains(response, "Alfred Sampare")
        self.assertContains(response, 'name="category"')
        self.assertContains(response, "Position")
        self.assertNotContains(response, "Staff category")
        self.assertContains(response, 'name="shift"')
        self.assertNotContains(response, "definition-card")

    def test_user_facing_navigation_uses_chore_list_while_domain_names_remain_stable(self):
        self.client.force_login(self.operator)
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, ">Chores</a>")
        self.assertNotContains(response, ">Chore Lists</a>")
        self.assertNotContains(response, ">Checklists</a>")
        self.assertEqual(ChecklistDefinition.__name__, "ChecklistDefinition")
        self.assertEqual(ChecklistInstance.__name__, "ChecklistInstance")

    def test_selected_person_and_date_survive_position_and_shift_selection(self):
        evening = Shift.objects.create(
            name="Evening", start_time=time(15), end_time=time(23), sort_order=20
        )
        ChecklistDefinition.objects.create(
            name="Support Evening Checklist",
            category=self.category,
            shift=evening,
            sort_order=20,
        )
        other_category = StaffCategory.objects.create(
            program=self.program, name="Front Desk", slug="front-desk", sort_order=20
        )
        ChecklistDefinition.objects.create(
            name="Front Desk Morning Checklist",
            category=other_category,
            shift=self.shift,
            sort_order=10,
        )
        self.client.force_login(self.operator)
        params = {
            "date": "2026-09-15",
            "staff": self.staff_b.pk,
            "category": self.category.pk,
            "shift": evening.pk,
        }
        response = self.client.get(reverse("dashboard"), params)
        self.assertEqual(response.context["selected_staff_id"], self.staff_b.pk)
        self.assertEqual(response.context["selected_shift_id"], evening.pk)
        self.assertEqual(response.context["operational_date"], date(2026, 9, 15))
        self.assertContains(
            response,
            f'<option value="{self.staff_b.pk}" selected>Chelsea Brown</option>',
            html=True,
        )
        self.assertContains(response, "dashboard_selector.js")
        self.assertNotContains(response, "window.location='/checklists/?category=")
        self.assertEqual(
            list(response.context["selected_definitions"].values_list("shift__name", flat=True)),
            ["Morning", "Evening"],
        )

        params["shift"] = self.shift.pk
        shifted = self.client.get(reverse("dashboard"), params)
        self.assertEqual(shifted.context["selected_staff_id"], self.staff_b.pk)
        self.assertEqual(shifted.context["selected_shift_id"], self.shift.pk)

        params["category"] = other_category.pk
        changed_position = self.client.get(reverse("dashboard"), params)
        self.assertEqual(changed_position.context["selected_staff_id"], self.staff_b.pk)
        self.assertEqual(changed_position.context["operational_date"], date(2026, 9, 15))
        self.assertEqual(
            list(
                changed_position.context["selected_definitions"].values_list(
                    "shift__name", flat=True
                )
            ),
            ["Morning"],
        )

    def test_inactive_staff_is_not_available_for_new_work(self):
        self.staff_a.is_active = False
        self.staff_a.save(update_fields=("is_active",))
        self.client.force_login(self.operator)
        response = self.client.get(reverse("dashboard"))
        self.assertNotContains(response, "Alfred Sampare")
        response = self.client.get(
            reverse("open-checklist"),
            {
                "date": self.operational_date,
                "staff": self.staff_a.pk,
                "category": self.category.pk,
                "shift": self.shift.pk,
            },
        )
        self.assertEqual(response.status_code, 404)

    def test_open_selector_rejects_invalid_category_shift_combination(self):
        invalid_shift = Shift.objects.create(
            name="Evening", start_time=time(15), end_time=time(23)
        )
        self.client.force_login(self.operator)
        response = self.client.get(
            reverse("open-checklist"),
            {
                "date": self.operational_date,
                "staff": self.staff_a.pk,
                "category": self.category.pk,
                "shift": invalid_shift.pk,
            },
        )
        self.assertEqual(response.status_code, 404)

    def test_selected_person_does_not_change_shared_checklist_identity(self):
        self.client.force_login(self.operator)
        base = {
            "date": self.operational_date,
            "category": self.category.pk,
            "shift": self.shift.pk,
        }
        first = self.client.get(reverse("open-checklist"), {**base, "staff": self.staff_a.pk})
        second = self.client.get(reverse("open-checklist"), {**base, "staff": self.staff_b.pk})
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(ChecklistInstance.objects.count(), 1)

    def test_selected_staff_receives_contribution_attribution(self):
        self.client.force_login(self.operator)
        instance = resolve_checklist(self.definition, self.operational_date)
        item = instance.items.get(source_task=self.regular)
        response = self.client.post(
            reverse("update-item-state", args=(item.pk,)),
            {"state": "completed", "staff": self.staff_b.pk},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(item.contributions.get().staff, self.staff_b)

    def test_role_aware_home_routing_and_navigation(self):
        self.client.force_login(self.manager)
        self.assertRedirects(self.client.get(reverse("home")), reverse("reports"))
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 403)
        report = self.client.get(reverse("reports"))
        self.assertNotContains(report, ">Checklists</a>")
        self.assertNotContains(report, ">Chores</a>")

        self.client.force_login(self.operator)
        self.assertRedirects(self.client.get(reverse("home")), reverse("dashboard"))

    def test_global_admin_needs_no_membership_and_can_manage_operational_staff(self):
        self.client.force_login(self.admin)
        self.assertRedirects(self.client.get(reverse("home")), reverse("reports"))
        response = self.client.get(reverse("admin:checklists_staffmember_changelist"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Alfred")

    def test_login_page_has_no_preauthentication_program_or_staff_copy(self):
        response = self.client.get(reverse("login"))
        self.assertContains(response, "Sign in to Chore")
        self.assertNotContains(response, "SONDER HOUSE")
        self.assertNotContains(response, "Use your staff account")


class OperationalDateWindowTests(OperationalFixtureMixin, TestCase):
    today = date(2026, 9, 16)

    def setUp(self):
        super().setUp()
        self.localdate = patch(
            "checklists.operational_dates.timezone.localdate", return_value=self.today
        )
        self.localdate.start()
        self.addCleanup(self.localdate.stop)
        self.client.force_login(self.operator)

    def selector_params(self, operational_date):
        return {
            "date": operational_date.isoformat(),
            "staff": self.staff_a.pk,
            "category": self.category.pk,
            "shift": self.shift.pk,
        }

    def test_today_and_exactly_seven_days_ago_are_accepted(self):
        for operational_date in (self.today, self.today - timedelta(days=7)):
            with self.subTest(operational_date=operational_date):
                response = self.client.get(
                    reverse("open-checklist"), self.selector_params(operational_date)
                )
                self.assertEqual(response.status_code, 302)
                self.assertTrue(
                    ChecklistInstance.objects.filter(
                        definition=self.definition,
                        operational_date=operational_date,
                    ).exists()
                )

    def test_future_and_eight_days_ago_are_rejected(self):
        for operational_date in (self.today + timedelta(days=1), self.today - timedelta(days=8)):
            with self.subTest(operational_date=operational_date):
                dashboard = self.client.get(
                    reverse("dashboard"), {"date": operational_date.isoformat()}
                )
                response = self.client.get(
                    reverse("open-checklist"), self.selector_params(operational_date)
                )
                self.assertEqual(dashboard.status_code, 400)
                self.assertEqual(response.status_code, 400)
                self.assertFalse(
                    ChecklistInstance.objects.filter(
                        definition=self.definition,
                        operational_date=operational_date,
                    ).exists()
                )

    def test_crafted_historical_detail_and_update_are_rejected_without_mutation(self):
        historical_date = self.today - timedelta(days=8)
        instance = resolve_checklist(self.definition, historical_date)
        item = instance.items.get(source_task=self.regular)
        change_item_state(
            item_id=item.pk,
            actor=None,
            staff_member=self.staff_a,
            new_state=TaskState.COMPLETED,
            system=True,
        )
        before = list(
            item.contributions.values_list("previous_state", "new_state", "staff_id")
        )

        detail = self.client.get(
            reverse("checklist-detail", args=(self.definition.pk,)),
            {"date": historical_date.isoformat(), "staff": self.staff_a.pk},
        )
        update = self.client.post(
            reverse("update-item-state", args=(item.pk,)),
            {"state": TaskState.PENDING, "staff": self.staff_a.pk},
        )
        self.assertEqual(detail.status_code, 400)
        self.assertEqual(update.status_code, 400)
        item.refresh_from_db()
        self.assertEqual(item.current_state, TaskState.COMPLETED)
        self.assertEqual(
            list(item.contributions.values_list("previous_state", "new_state", "staff_id")),
            before,
        )

    def test_night_operational_date_is_the_selected_shift_start_date(self):
        night = Shift.objects.create(
            name="Night", start_time=time(23), end_time=time(7), sort_order=30
        )
        night_definition = ChecklistDefinition.objects.create(
            name="Support Night Checklist",
            category=self.category,
            shift=night,
            sort_order=30,
        )
        response = self.client.get(
            reverse("open-checklist"),
            {
                "date": self.today.isoformat(),
                "staff": self.staff_a.pk,
                "category": self.category.pk,
                "shift": night.pk,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            ChecklistInstance.objects.get(definition=night_definition).operational_date,
            self.today,
        )


class SimpleOperationalSeedTests(TestCase):
    def test_fresh_seed_accepts_simple_hashed_operational_password(self):
        with patch.dict(os.environ, {"CHORE_OPERATIONAL_PASSWORD": "123"}, clear=False):
            call_command("seed_development", reset_passwords=True, verbosity=0)
        operator = get_user_model().objects.get(username="sonderhouse")
        self.assertNotEqual(operator.password, "123")
        self.assertTrue(operator.check_password("123"))
        self.assertIsNotNone(authenticate(username="sonderhouse", password="123"))


class SeedCorrectionTests(TestCase):
    def setUp(self):
        self.environment = patch.dict(
            os.environ,
            {"CHORE_OPERATIONAL_PASSWORD": "operational-password"},
            clear=False,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        call_command("seed_development", reset_passwords=True, verbosity=0)

    def test_exact_staff_roster_is_separate_from_authentication_accounts(self):
        expected = [tuple(name.split(" ", 1)) for name in STAFF_ROSTER]
        actual = list(StaffMember.objects.values_list("first_name", "last_name"))
        self.assertEqual(actual, expected)
        usernames = set(get_user_model().objects.values_list("username", flat=True))
        self.assertTrue(usernames.isdisjoint(set(STAFF_ROSTER)))
        self.assertFalse(hasattr(StaffMember, "username"))
        self.assertFalse(hasattr(StaffMember, "password"))
        operator = get_user_model().objects.get(username="sonderhouse")
        self.assertTrue(operator.check_password("operational-password"))
        self.assertEqual(operator.program_memberships.get().role, ProgramRole.OPERATIONAL)
        with patch.dict(os.environ, {"CHORE_OPERATIONAL_PASSWORD": "123"}, clear=False):
            call_command("seed_development", reset_passwords=True, verbosity=0)
        operator.refresh_from_db()
        self.assertTrue(operator.check_password("123"))
        self.assertIsNotNone(authenticate(username="sonderhouse", password="123"))
        self.assertIsNone(authenticate(username="sonderhouse", password="incorrect"))

    def test_exact_three_shift_identities_and_metadata(self):
        self.assertEqual(
            list(Shift.objects.values_list("name", "start_time", "end_time")),
            [
                ("Morning", time(7), time(15)),
                ("Evening", time(15), time(23)),
                ("Night", time(23), time(7)),
            ],
        )
        self.assertEqual([str(shift) for shift in Shift.objects.all()], ["Morning", "Evening", "Night"])
        self.assertFalse(Shift.objects.filter(start_time=time(8), end_time=time(15)).exists())
        self.assertTrue(Shift.objects.get(name="Night").crosses_midnight)
        self.client.force_login(get_user_model().objects.get(username="sonderhouse"))
        dashboard = self.client.get(reverse("dashboard"))
        self.assertContains(dashboard, "SONDER HOUSE")
        self.assertNotContains(dashboard, "SONDER HOUSE OPERATIONS")
        self.assertContains(
            dashboard,
            "For Night shifts, use the date on which the shift starts.",
        )

    def test_seed_matches_source_configuration_and_life_skills_use_morning(self):
        combinations = set(
            ChecklistDefinition.objects.values_list("category__name", "shift__name")
        )
        self.assertEqual(
            combinations,
            {
                ("Front Desk", "Morning"),
                ("Front Desk", "Evening"),
                ("Front Desk", "Night"),
                ("Support", "Morning"),
                ("Support", "Evening"),
                ("Awake Night", "Night"),
                ("Life Skills", "Morning"),
            },
        )
        self.assertEqual(
            list(ChecklistDefinition.objects.values_list("name", "sort_order")),
            [
                ("Front Desk Morning", 10),
                ("Front Desk Evening", 20),
                ("Front Desk Night", 30),
                ("Support Morning", 40),
                ("Support Evening", 50),
                ("Awake Night", 60),
                ("Life Skills Morning", 70),
            ],
        )
        for category_key, shift_key, _, _, expected_sections in ASSIGNMENTS:
            definition = ChecklistDefinition.objects.get(
                seed_key=f"definition-{category_key}-{shift_key}"
            )
            section_memberships = definition.section_memberships.select_related(
                "section"
            ).filter(section__is_active=True).order_by("sort_order")
            self.assertEqual(
                [membership.section.name for membership in section_memberships],
                [name for name, _ in expected_sections],
            )
            for membership, (_, expected_tasks) in zip(
                section_memberships, expected_sections
            ):
                self.assertEqual(
                    [
                        (row.task.label, row.task.allow_na)
                        for row in membership.section.task_memberships.select_related(
                            "task"
                        ).filter(task__is_active=True).order_by("sort_order")
                    ],
                    [(task["label"], task["allow_na"]) for task in expected_tasks],
                )

        life_definition = ChecklistDefinition.objects.get(
            seed_key="definition-life-skills-morning"
        )
        expected_life_labels = [
            task["label"]
            for _, tasks in next(
                assignment[4]
                for assignment in ASSIGNMENTS
                if assignment[0:2] == ("life-skills", "morning")
            )
            for task in tasks
        ]
        self.assertEqual(len(expected_life_labels), 28)
        for offset in range(5):
            instance = resolve_checklist(life_definition, date(2026, 9, 14) + timedelta(days=offset))
            self.assertEqual(
                list(instance.items.values_list("task_label_snapshot", flat=True)),
                expected_life_labels,
            )
        for weekend_date in (date(2026, 9, 19), date(2026, 9, 20)):
            with self.assertRaises(PermissionDenied):
                resolve_checklist(life_definition, weekend_date)
        self.assertTrue(life_definition.weekdays_only)
        self.assertFalse(any(field.name == "weekday" for field in TaskDefinition._meta.fields))

    def test_corrected_seed_content_and_authoritative_counts(self):
        active_tasks = TaskDefinition.objects.filter(is_active=True)
        self.assertFalse(active_tasks.filter(label="Greet / buzz in tenants").exists())
        self.assertEqual(
            active_tasks.get(label="Complete leave requests and maintenance requests").allow_na,
            True,
        )
        self.assertEqual(
            active_tasks.get(label="Print and maintain an ample supply of paperwork").allow_na,
            True,
        )
        self.assertFalse(
            active_tasks.filter(allow_na=True, label__regex=r"(?i)(as needed|as required)").exists()
        )
        self.assertTrue(
            active_tasks.filter(
                allow_na=False,
                label="Connect with the Ministry as needed to support tenants",
            ).exists()
        )

        awake = ChecklistDefinition.objects.get(seed_key="definition-awake-night-night")
        support = ChecklistDefinition.objects.get(seed_key="definition-support-morning")
        awake_hallways = awake.sections.get(name="Hallways")
        support_hallways = support.sections.get(name="Hallways")
        expected_hallways = [
            "Sweep / mop first floor hallway",
            "Sweep / mop second floor hallway",
            "Sweep / mop third floor hallway",
            "Wipe first floor window ledges",
            "Wipe second floor window ledges",
            "Wipe third floor window ledges",
        ]
        self.assertEqual(
            list(
                awake_hallways.task_memberships.order_by("sort_order").values_list(
                    "task__label", flat=True
                )
            ),
            expected_hallways,
        )
        self.assertEqual(
            list(
                support_hallways.task_memberships.order_by("sort_order").values_list(
                    "task__label", flat=True
                )
            ),
            expected_hallways[:3],
        )
        self.assertNotEqual(awake_hallways.pk, support_hallways.pk)
        self.assertEqual(Program.objects.count(), 1)
        self.assertEqual(StaffCategory.objects.count(), 4)
        self.assertEqual(Shift.objects.count(), 3)
        self.assertEqual(ChecklistDefinition.objects.filter(is_active=True).count(), 7)
        self.assertEqual(ChecklistSection.objects.filter(is_active=True).count(), 33)
        self.assertEqual(active_tasks.count(), 116)
        self.assertEqual(
            SectionTaskMembership.objects.filter(
                section__is_active=True, task__is_active=True
            ).count(),
            172,
        )
        self.assertEqual(
            AssignmentSectionMembership.objects.filter(
                assignment__is_active=True, section__is_active=True
            ).count(),
            43,
        )
        expanded = sum(
            membership.section.task_memberships.filter(task__is_active=True).count()
            for membership in AssignmentSectionMembership.objects.filter(
                assignment__is_active=True, section__is_active=True
            ).select_related("section")
        )
        self.assertEqual(expanded, 225)

    def test_selector_only_renders_shifts_valid_for_selected_category(self):
        operator = get_user_model().objects.get(username="sonderhouse")
        support = StaffCategory.objects.get(slug="support")
        self.client.force_login(operator)
        response = self.client.get(reverse("dashboard"), {"category": support.pk})
        shift_options = response.context["selected_definitions"]
        self.assertEqual(
            list(shift_options.values_list("shift__name", flat=True)),
            ["Morning", "Evening"],
        )
        self.assertNotContains(response, ">Night</option>")

    def test_dashboard_date_query_recalculates_life_skills_and_preserves_staff(self):
        operator = get_user_model().objects.get(username="sonderhouse")
        staff = StaffMember.objects.first()
        life_skills = StaffCategory.objects.get(slug="life-skills")
        self.client.force_login(operator)

        friday = self.client.get(
            reverse("dashboard"),
            {
                "date": "2026-09-18",
                "staff": staff.pk,
                "category": life_skills.pk,
            },
        )
        self.assertEqual(friday.status_code, 200)
        self.assertEqual(friday.context["operational_date"], date(2026, 9, 18))
        self.assertEqual(friday.context["selected_staff_id"], staff.pk)
        self.assertEqual(friday.context["selected_category_id"], life_skills.pk)
        self.assertIn(
            "Life Skills",
            [category.name for category in friday.context["categories"]],
        )

        saturday = self.client.get(
            reverse("dashboard"),
            {
                "date": "2026-09-19",
                "staff": staff.pk,
                "category": life_skills.pk,
            },
        )
        self.assertEqual(saturday.status_code, 200)
        self.assertEqual(saturday.context["operational_date"], date(2026, 9, 19))
        self.assertEqual(saturday.context["selected_staff_id"], staff.pk)
        self.assertNotEqual(saturday.context["selected_category_id"], life_skills.pk)
        self.assertNotIn(
            "Life Skills",
            [category.name for category in saturday.context["categories"]],
        )

    def test_dashboard_date_refresh_uses_committed_change_event(self):
        operator = get_user_model().objects.get(username="sonderhouse")
        self.client.force_login(operator)
        response = self.client.get(reverse("dashboard"), {"date": "2026-09-18"})
        self.assertContains(response, 'data-dashboard-url="/checklists/"')
        self.assertContains(response, "dashboard_selector.js")

        script = (
            Path(__file__).parent / "static" / "checklists" / "dashboard_selector.js"
        ).read_text(encoding="utf-8")
        self.assertIn('date.addEventListener("change", refreshDashboard)', script)
        self.assertNotIn('date.addEventListener("input"', script)
        self.assertIn('window.location.assign(', script)
        self.assertIn('["staff", "category", "shift", "program"]', script)
        self.assertNotIn("form.submit", script)

    def test_seed_rerun_is_stable(self):
        ids = {
            "staff": list(StaffMember.objects.values_list("pk", flat=True)),
            "shifts": list(Shift.objects.values_list("pk", flat=True)),
            "definitions": list(ChecklistDefinition.objects.values_list("pk", flat=True)),
        }
        operator = get_user_model().objects.get(username="sonderhouse")
        staff = StaffMember.objects.first()
        definition = ChecklistDefinition.objects.get(seed_key="definition-support-morning")
        instance = resolve_checklist(definition, date(2026, 9, 15))
        item = instance.items.first()
        snapshot = item.task_label_snapshot
        change_item_state(
            item_id=item.pk,
            actor=operator,
            staff_member=staff,
            new_state=TaskState.COMPLETED,
        )
        removed_task = TaskDefinition.objects.create(
            label="Break",
            scheduled_start=time(10),
            scheduled_end=time(10, 15),
        )
        call_command("seed_development", reset_passwords=True, verbosity=0)
        self.assertEqual(ids["staff"], list(StaffMember.objects.values_list("pk", flat=True)))
        self.assertEqual(ids["shifts"], list(Shift.objects.values_list("pk", flat=True)))
        self.assertEqual(ids["definitions"], list(ChecklistDefinition.objects.values_list("pk", flat=True)))
        item.refresh_from_db()
        contribution = item.contributions.get()
        self.assertEqual(item.task_label_snapshot, snapshot)
        self.assertEqual(item.current_state, TaskState.COMPLETED)
        self.assertEqual(contribution.staff, staff)
        self.assertEqual(contribution.recorded_by, operator)
        removed_task.refresh_from_db()
        self.assertFalse(removed_task.is_active)


class MockDataTests(TestCase):
    def setUp(self):
        call_command("seed_development", verbosity=0)

    def test_generator_uses_roster_valid_combinations_and_no_users(self):
        user_count = get_user_model().objects.count()
        call_command("generate_mock_data", days=7, seed=1234, verbosity=0)
        generated = ChecklistInstance.objects.filter(is_mock_data=True)
        self.assertTrue(generated.exists())
        self.assertEqual(generated.count(), 47)
        self.assertFalse(
            generated.filter(
                category__slug="life-skills", operational_date__week_day__in=(1, 7)
            ).exists()
        )
        self.assertEqual(get_user_model().objects.count(), user_count)
        self.assertFalse(
            generated.exclude(
                definition__category_id=models.F("category_id"),
                definition__shift_id=models.F("shift_id"),
            ).exists()
        )
        self.assertFalse(
            generated.filter(category__slug="life-skills").exclude(shift__name="Morning").exists()
        )
        self.assertFalse(
            generated.filter(items__current_state="na", items__allow_na_snapshot=False).exists()
        )
        self.assertTrue(
            generated.annotate(
                contributor_count=models.Count("items__contributions__staff", distinct=True)
            ).filter(contributor_count__gt=1).exists()
        )

    def test_cleanup_removes_only_marked_operational_history(self):
        definition = ChecklistDefinition.objects.first()
        legitimate = resolve_checklist(definition, date(2020, 1, 1))
        legitimate_item = legitimate.items.first()
        legitimate_staff = StaffMember.objects.filter(
            program=legitimate.program
        ).first()
        _, legitimate_contribution = change_item_state(
            item_id=legitimate_item.pk,
            actor=None,
            staff_member=legitimate_staff,
            new_state=TaskState.COMPLETED,
            system=True,
        )
        roster_count = StaffMember.objects.count()
        call_command("generate_mock_data", days=3, seed=99, verbosity=0)
        self.assertTrue(ChecklistInstance.objects.filter(is_mock_data=True).exists())
        self.assertTrue(
            StaffContribution.objects.filter(item__instance__is_mock_data=True).exists()
        )
        call_command("generate_mock_data", clear=True, verbosity=0)
        self.assertTrue(ChecklistInstance.objects.filter(pk=legitimate.pk).exists())
        self.assertTrue(ChecklistItem.objects.filter(pk=legitimate_item.pk).exists())
        self.assertTrue(
            StaffContribution.objects.filter(
                pk=legitimate_contribution.pk
            ).exists()
        )
        self.assertFalse(ChecklistInstance.objects.filter(is_mock_data=True).exists())
        self.assertFalse(
            StaffContribution.objects.filter(item__instance__is_mock_data=True).exists()
        )
        self.assertEqual(StaffMember.objects.count(), roster_count)
        self.assertEqual(ChecklistDefinition.objects.count(), 7)

    def test_regeneration_is_deterministic_and_idempotent(self):
        call_command("generate_mock_data", days=3, seed=777, verbosity=0)
        first = (
            ChecklistInstance.objects.filter(is_mock_data=True).count(),
            StaffContribution.objects.filter(item__instance__is_mock_data=True).count(),
        )
        call_command("generate_mock_data", days=3, seed=777, verbosity=0)
        second = (
            ChecklistInstance.objects.filter(is_mock_data=True).count(),
            StaffContribution.objects.filter(item__instance__is_mock_data=True).count(),
        )
        self.assertEqual(first, second)
    SectionTaskMembership,
