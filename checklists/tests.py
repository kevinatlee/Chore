import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, time, timedelta
from io import StringIO
from threading import Barrier
from unittest.mock import call, patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.db import IntegrityError, OperationalError, close_old_connections, models, transaction
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.urls import reverse

from .models import (
    ChecklistDefinition,
    ChecklistInstance,
    ChecklistSection,
    Program,
    ProgramMembership,
    ProgramRole,
    Shift,
    StaffCategory,
    StaffContribution,
    StaffMember,
    TaskDefinition,
    TaskState,
)
from .seed_data import DEFINITIONS, LIFE_SKILLS_SLOTS, STAFF_ROSTER
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
        self.section = ChecklistSection.objects.create(
            definition=self.definition, name="Office", sort_order=10
        )
        self.regular = TaskDefinition.objects.create(
            section=self.section, label="Regular task", sort_order=10
        )
        self.optional = TaskDefinition.objects.create(
            section=self.section, label="Optional task", sort_order=20, allow_na=True
        )


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
        self.section.sort_order = 90
        self.section.save(update_fields=("name", "sort_order"))
        self.regular.label = "Changed task"
        self.regular.sort_order = 90
        self.regular.scheduled_start = time(10)
        self.regular.scheduled_end = time(11)
        self.regular.save(
            update_fields=("label", "sort_order", "scheduled_start", "scheduled_end")
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
        self.assertEqual(history[0].previous_state, TaskState.PENDING)
        self.assertEqual(history[1].previous_state, history[0].new_state)
        item.refresh_from_db()
        self.assertEqual(item.current_state, history[1].new_state)


class SelectorAndRoleTests(OperationalFixtureMixin, TestCase):
    def test_selector_is_compact_dropdown_workflow_using_staff_records(self):
        self.client.force_login(self.operator)
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, 'name="staff"')
        self.assertContains(response, "Alfred Sampare")
        self.assertContains(response, 'name="category"')
        self.assertContains(response, 'name="shift"')
        self.assertNotContains(response, "definition-card")

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

    def test_staff_selection_does_not_change_shared_checklist_identity(self):
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

    def test_exact_staff_roster_has_no_authentication_accounts(self):
        expected = [tuple(name.split(" ", 1)) for name in STAFF_ROSTER]
        actual = list(StaffMember.objects.values_list("first_name", "last_name"))
        self.assertEqual(actual, expected)
        usernames = set(get_user_model().objects.values_list("username", flat=True))
        self.assertNotIn("teststaff", usernames)
        self.assertTrue(usernames.isdisjoint(set(STAFF_ROSTER)))
        self.assertFalse(hasattr(StaffMember, "username"))
        self.assertFalse(hasattr(StaffMember, "password"))
        operator = get_user_model().objects.get(username="sonderhouse")
        self.assertTrue(operator.check_password("operational-password"))
        self.assertEqual(operator.program_memberships.get().role, ProgramRole.OPERATIONAL)

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
        self.assertContains(
            self.client.get(reverse("dashboard")),
            "For Night, use the date on which the shift starts.",
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
        for category_key, shift_key, _, _, expected_sections in DEFINITIONS:
            definition = ChecklistDefinition.objects.get(
                seed_key=f"definition-{category_key}-{shift_key}"
            )
            sections = definition.sections.filter(is_active=True).order_by("sort_order")
            self.assertEqual(
                [section.name for section in sections],
                [name for name, _ in expected_sections],
            )
            for section, (_, expected_labels) in zip(sections, expected_sections):
                self.assertEqual(
                    list(
                        section.tasks.filter(is_active=True)
                        .order_by("sort_order")
                        .values_list("label", flat=True)
                    ),
                    expected_labels,
                )

        life_definition = ChecklistDefinition.objects.get(
            seed_key="definition-life-skills-morning"
        )
        life_tasks = TaskDefinition.objects.filter(
            section__definition=life_definition, is_active=True
        )
        self.assertEqual(life_tasks.count(), 40)
        self.assertFalse(life_tasks.filter(scheduled_end__gt=time(15)).exists())
        for order, (slot_key, start, end, labels) in enumerate(LIFE_SKILLS_SLOTS, start=1):
            for weekday, label in enumerate(labels):
                task = life_tasks.get(seed_key=f"task-life-skills-{slot_key}-{weekday}")
                self.assertEqual(
                    (task.label, task.sort_order, task.weekday),
                    (label, order * 10, weekday),
                )
                self.assertEqual(task.scheduled_start, time.fromisoformat(start))
                self.assertEqual(task.scheduled_end, time.fromisoformat(end))

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
        life_section = ChecklistSection.objects.get(seed_key="section-life-skills-morning-01")
        removed_task = TaskDefinition.objects.create(
            section=life_section,
            weekday=0,
            sort_order=500,
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
        self.assertEqual(removed_task.seed_key, "task-life-skills-retired-break-1000-0")


class MockDataTests(TestCase):
    def setUp(self):
        call_command("seed_development", verbosity=0)

    def test_generator_uses_roster_valid_combinations_and_no_users(self):
        user_count = get_user_model().objects.count()
        call_command("generate_mock_data", days=7, seed=1234, verbosity=0)
        generated = ChecklistInstance.objects.filter(is_mock_data=True)
        self.assertTrue(generated.exists())
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
        roster_count = StaffMember.objects.count()
        call_command("generate_mock_data", days=3, seed=99, verbosity=0)
        self.assertTrue(ChecklistInstance.objects.filter(is_mock_data=True).exists())
        call_command("generate_mock_data", clear=True, verbosity=0)
        self.assertTrue(ChecklistInstance.objects.filter(pk=legitimate.pk).exists())
        self.assertFalse(ChecklistInstance.objects.filter(is_mock_data=True).exists())
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
