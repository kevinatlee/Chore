import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, time
from io import StringIO
from threading import Barrier
from unittest.mock import call, patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, OperationalError, close_old_connections, transaction
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.urls import reverse

from .models import (
    ChecklistDefinition,
    ChecklistInstance,
    ChecklistSection,
    Shift,
    StaffAssignment,
    StaffCategory,
    StaffContribution,
    TaskDefinition,
    TaskState,
)
from .services import (
    SQLITE_LOCK_ATTEMPTS,
    SQLITE_LOCK_BACKOFF_SECONDS,
    _run_serialized_write,
    change_item_state,
    resolve_checklist,
)
from .seed_data import DEFINITIONS, LIFE_SKILLS_SLOTS


class SharedChecklistDomainTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.staff_a = User.objects.create_user(
            username="staff-a", password="safe-test-password", first_name="Alex"
        )
        self.staff_b = User.objects.create_user(
            username="staff-b", password="safe-test-password", first_name="Blair"
        )
        self.category = StaffCategory.objects.create(
            name="Support", slug="support-test", sort_order=10
        )
        self.shift = Shift.objects.create(
            name="07:00–15:00", start_time=time(7), end_time=time(15), sort_order=10
        )
        self.definition = ChecklistDefinition.objects.create(
            name="Support Day Checklist",
            category=self.category,
            shift=self.shift,
            sort_order=10,
        )
        self.section = ChecklistSection.objects.create(
            definition=self.definition, name="Office Responsibilities", sort_order=10
        )
        self.regular_task = TaskDefinition.objects.create(
            section=self.section,
            label="Complete Shift Exchange",
            sort_order=10,
            allow_na=False,
            scheduled_start=time(8),
            scheduled_end=time(9),
        )
        self.optional_task = TaskDefinition.objects.create(
            section=self.section,
            label="Submit Leave Requests as needed",
            sort_order=20,
            allow_na=True,
        )
        for user in (self.staff_a, self.staff_b):
            StaffAssignment.objects.create(user=user, category=self.category)
        self.operational_date = date(2026, 9, 15)

    def test_two_staff_resolve_to_the_same_operational_checklist(self):
        first = resolve_checklist(self.definition, self.operational_date)
        second = resolve_checklist(self.definition, self.operational_date)

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(ChecklistInstance.objects.count(), 1)
        self.assertEqual(first.items.count(), 2)

    def test_completion_updates_shared_state_and_records_contributor(self):
        instance = resolve_checklist(self.definition, self.operational_date)
        item = instance.items.get(source_task=self.regular_task)

        changed_item, contribution = change_item_state(
            item_id=item.id, staff=self.staff_a, new_state=TaskState.COMPLETED
        )

        self.assertEqual(changed_item.current_state, TaskState.COMPLETED)
        self.assertEqual(changed_item.current_contributor, self.staff_a)
        self.assertEqual(contribution.staff, self.staff_a)
        self.assertEqual(contribution.previous_state, TaskState.PENDING)
        self.assertEqual(contribution.new_state, TaskState.COMPLETED)

    def test_another_staff_member_sees_completed_shared_task(self):
        instance = resolve_checklist(self.definition, self.operational_date)
        item = instance.items.get(source_task=self.regular_task)
        change_item_state(item_id=item.id, staff=self.staff_a, new_state=TaskState.COMPLETED)

        self.client.force_login(self.staff_b)
        response = self.client.get(
            reverse("checklist-detail", args=(self.definition.id,)),
            {"date": self.operational_date.isoformat()},
        )

        self.assertContains(response, "Complete Shift Exchange")
        self.assertContains(response, "Completed")
        self.assertContains(response, "Staff Contribution by Alex")

    def test_allow_na_task_can_be_marked_not_applicable(self):
        instance = resolve_checklist(self.definition, self.operational_date)
        item = instance.items.get(source_task=self.optional_task)

        changed_item, contribution = change_item_state(
            item_id=item.id, staff=self.staff_a, new_state=TaskState.NOT_APPLICABLE
        )

        self.assertEqual(changed_item.current_state, TaskState.NOT_APPLICABLE)
        self.assertEqual(contribution.new_state, TaskState.NOT_APPLICABLE)

    def test_non_allow_na_task_rejects_direct_backend_request(self):
        instance = resolve_checklist(self.definition, self.operational_date)
        item = instance.items.get(source_task=self.regular_task)
        self.client.force_login(self.staff_a)

        response = self.client.post(
            reverse("update-item-state", args=(item.id,)),
            {"state": TaskState.NOT_APPLICABLE, "date": self.operational_date.isoformat()},
        )

        self.assertEqual(response.status_code, 400)
        item.refresh_from_db()
        self.assertEqual(item.current_state, TaskState.PENDING)
        self.assertFalse(item.contributions.exists())

    def test_ui_only_offers_na_for_eligible_tasks(self):
        self.client.force_login(self.staff_a)

        response = self.client.get(
            reverse("checklist-detail", args=(self.definition.id,)),
            {"date": self.operational_date.isoformat()},
        )

        self.assertContains(response, 'name="state" value="na"', count=1)

    def test_non_allow_na_task_rejects_service_call(self):
        instance = resolve_checklist(self.definition, self.operational_date)
        item = instance.items.get(source_task=self.regular_task)

        with self.assertRaises(ValidationError):
            change_item_state(
                item_id=item.id,
                staff=self.staff_a,
                new_state=TaskState.NOT_APPLICABLE,
            )

    def test_deactivating_staff_retains_historical_contribution(self):
        instance = resolve_checklist(self.definition, self.operational_date)
        item = instance.items.get(source_task=self.regular_task)
        change_item_state(item_id=item.id, staff=self.staff_a, new_state=TaskState.COMPLETED)

        self.staff_a.is_active = False
        self.staff_a.save(update_fields=("is_active",))

        contribution = StaffContribution.objects.get(item=item)
        self.assertEqual(contribution.staff_id, self.staff_a.id)
        self.assertEqual(contribution.staff.first_name, "Alex")

    def test_task_definition_edit_does_not_rewrite_historical_snapshot(self):
        instance = resolve_checklist(self.definition, self.operational_date)
        item = instance.items.get(source_task=self.regular_task)

        self.regular_task.label = "A newly edited task label"
        self.regular_task.allow_na = True
        self.regular_task.save(update_fields=("label", "allow_na"))
        item.refresh_from_db()

        self.assertEqual(item.task_label_snapshot, "Complete Shift Exchange")
        self.assertFalse(item.allow_na_snapshot)

    def test_configuration_edits_do_not_rewrite_any_historical_snapshot(self):
        instance = resolve_checklist(self.definition, self.operational_date)
        item = instance.items.get(source_task=self.regular_task)

        self.category.name = "Renamed Support"
        self.category.save(update_fields=("name",))
        self.shift.name = "Renamed Day"
        self.shift.start_time = time(6)
        self.shift.end_time = time(14)
        self.shift.save(update_fields=("name", "start_time", "end_time"))
        self.section.name = "Renamed Office"
        self.section.sort_order = 90
        self.section.save(update_fields=("name", "sort_order"))
        self.regular_task.label = "Renamed task"
        self.regular_task.sort_order = 90
        self.regular_task.scheduled_start = time(10)
        self.regular_task.scheduled_end = time(11)
        self.regular_task.save(
            update_fields=("label", "sort_order", "scheduled_start", "scheduled_end")
        )

        instance.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(instance.category_name_snapshot, "Support")
        self.assertEqual(instance.shift_name_snapshot, "07:00–15:00")
        self.assertEqual(instance.shift_start_snapshot, time(7))
        self.assertEqual(instance.shift_end_snapshot, time(15))
        self.assertEqual(item.section_name_snapshot, "Office Responsibilities")
        self.assertEqual(item.section_order_snapshot, 10)
        self.assertEqual(item.task_label_snapshot, "Complete Shift Exchange")
        self.assertEqual(item.task_order_snapshot, 10)
        self.assertEqual(item.scheduled_start_snapshot, time(8))
        self.assertEqual(item.scheduled_end_snapshot, time(9))

    def test_definition_identity_is_immutable_after_operational_use(self):
        resolve_checklist(self.definition, self.operational_date)
        other_category = StaffCategory.objects.create(
            name="Other", slug="other", sort_order=20
        )
        other_shift = Shift.objects.create(
            name="15:00–23:00", start_time=time(15), end_time=time(23), sort_order=20
        )

        self.definition.category = other_category
        with self.assertRaises(ValidationError):
            self.definition.save()

        self.definition.refresh_from_db()
        self.definition.shift = other_shift
        with self.assertRaises(ValidationError):
            self.definition.save()

    def test_unused_definition_category_can_change(self):
        other_category = StaffCategory.objects.create(
            name="Other", slug="unused-other", sort_order=20
        )

        self.definition.category = other_category
        self.definition.save(update_fields=("category",))

        self.definition.refresh_from_db()
        self.assertEqual(self.definition.category, other_category)

    def test_unused_definition_shift_can_change(self):
        other_shift = Shift.objects.create(
            name="15:00–23:00", start_time=time(15), end_time=time(23), sort_order=20
        )

        self.definition.shift = other_shift
        self.definition.save(update_fields=("shift",))

        self.definition.refresh_from_db()
        self.assertEqual(self.definition.shift, other_shift)

    def test_database_constraint_prevents_duplicate_operational_checklist(self):
        instance = resolve_checklist(self.definition, self.operational_date)

        with self.assertRaises(IntegrityError), transaction.atomic():
            ChecklistInstance.objects.create(
                operational_date=self.operational_date,
                definition=self.definition,
                category=self.category,
                shift=self.shift,
                category_name_snapshot="Support",
                shift_name_snapshot="Duplicate",
                shift_start_snapshot=time(7),
                shift_end_snapshot=time(15),
            )

        self.assertEqual(ChecklistInstance.objects.get().pk, instance.pk)

    def test_state_changes_append_history_in_order(self):
        instance = resolve_checklist(self.definition, self.operational_date)
        item = instance.items.get(source_task=self.regular_task)
        change_item_state(item_id=item.id, staff=self.staff_a, new_state=TaskState.COMPLETED)
        change_item_state(item_id=item.id, staff=self.staff_b, new_state=TaskState.PENDING)

        contributions = list(item.contributions.all())
        self.assertEqual(len(contributions), 2)
        self.assertEqual(contributions[0].staff, self.staff_a)
        self.assertEqual(contributions[1].staff, self.staff_b)
        self.assertEqual(contributions[1].previous_state, TaskState.COMPLETED)
        self.assertEqual(contributions[1].new_state, TaskState.PENDING)

    def test_unassigned_staff_cannot_open_category(self):
        outsider = get_user_model().objects.create_user(
            username="outsider", password="safe-test-password"
        )
        self.client.force_login(outsider)

        response = self.client.get(
            reverse("checklist-detail", args=(self.definition.id,)),
            {"date": self.operational_date.isoformat()},
        )

        self.assertEqual(response.status_code, 403)

    def test_unassigned_staff_cannot_post_state_change(self):
        outsider = get_user_model().objects.create_user(
            username="state-outsider", password="safe-test-password"
        )
        instance = resolve_checklist(self.definition, self.operational_date)
        item = instance.items.get(source_task=self.regular_task)
        self.client.force_login(outsider)

        response = self.client.post(
            reverse("update-item-state", args=(item.id,)),
            {"state": TaskState.COMPLETED, "date": self.operational_date.isoformat()},
        )

        self.assertEqual(response.status_code, 403)
        item.refresh_from_db()
        self.assertEqual(item.current_state, TaskState.PENDING)
        self.assertFalse(item.contributions.exists())

    def test_anonymous_user_cannot_post_state_change(self):
        instance = resolve_checklist(self.definition, self.operational_date)
        item = instance.items.get(source_task=self.regular_task)

        response = self.client.post(
            reverse("update-item-state", args=(item.id,)),
            {"state": TaskState.COMPLETED, "date": self.operational_date.isoformat()},
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)
        item.refresh_from_db()
        self.assertEqual(item.current_state, TaskState.PENDING)
        self.assertEqual(item.contributions.count(), 0)

    def test_deactivated_user_cannot_post_state_change(self):
        instance = resolve_checklist(self.definition, self.operational_date)
        item = instance.items.get(source_task=self.regular_task)
        self.client.force_login(self.staff_a)
        self.staff_a.is_active = False
        self.staff_a.save(update_fields=("is_active",))

        response = self.client.post(
            reverse("update-item-state", args=(item.id,)),
            {"state": TaskState.COMPLETED, "date": self.operational_date.isoformat()},
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)
        item.refresh_from_db()
        self.assertEqual(item.current_state, TaskState.PENDING)
        self.assertEqual(item.contributions.count(), 0)

    def test_inactive_configuration_rejects_direct_state_change_posts(self):
        instance = resolve_checklist(self.definition, self.operational_date)
        item = instance.items.get(source_task=self.regular_task)
        self.client.force_login(self.staff_a)
        configuration = (
            self.category,
            self.shift,
            self.definition,
            self.section,
            self.regular_task,
        )

        for configured_object in configuration:
            with self.subTest(model=type(configured_object).__name__):
                configured_object.is_active = False
                configured_object.save(update_fields=("is_active",))
                contribution_count = item.contributions.count()
                response = self.client.post(
                    reverse("update-item-state", args=(item.id,)),
                    {
                        "state": TaskState.COMPLETED,
                        "date": self.operational_date.isoformat(),
                    },
                )
                self.assertEqual(response.status_code, 403)
                item.refresh_from_db()
                self.assertEqual(item.current_state, TaskState.PENDING)
                self.assertEqual(item.contributions.count(), contribution_count)
                configured_object.is_active = True
                configured_object.save(update_fields=("is_active",))


class SQLiteRetryPolicyTests(SimpleTestCase):
    def test_lock_error_is_retried_until_operation_succeeds(self):
        attempts = 0

        def operation():
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
            result = _run_serialized_write(operation)

        self.assertEqual(result, "completed")
        self.assertEqual(attempts, 3)
        self.assertEqual(
            mocked_sleep.call_args_list,
            [
                call(SQLITE_LOCK_BACKOFF_SECONDS),
                call(SQLITE_LOCK_BACKOFF_SECONDS * 2),
            ],
        )

    def test_unrelated_operational_error_is_not_retried(self):
        attempts = 0

        def operation():
            nonlocal attempts
            attempts += 1
            raise OperationalError("disk I/O error")

        with (
            patch("checklists.services.connection") as mocked_connection,
            patch("checklists.services.time.sleep") as mocked_sleep,
        ):
            mocked_connection.vendor = "sqlite"
            with self.assertRaisesMessage(OperationalError, "disk I/O error"):
                _run_serialized_write(operation)

        self.assertEqual(attempts, 1)
        mocked_sleep.assert_not_called()

    def test_persistent_lock_error_stops_at_configured_attempt_limit(self):
        attempts = 0

        def operation():
            nonlocal attempts
            attempts += 1
            raise OperationalError("database is locked")

        with (
            patch("checklists.services.connection") as mocked_connection,
            patch("checklists.services.time.sleep") as mocked_sleep,
        ):
            mocked_connection.vendor = "sqlite"
            with self.assertRaisesMessage(OperationalError, "database is locked"):
                _run_serialized_write(operation)

        self.assertEqual(attempts, SQLITE_LOCK_ATTEMPTS)
        self.assertEqual(mocked_sleep.call_count, SQLITE_LOCK_ATTEMPTS - 1)
        self.assertEqual(
            mocked_sleep.call_args_list,
            [
                call(SQLITE_LOCK_BACKOFF_SECONDS * attempt)
                for attempt in range(1, SQLITE_LOCK_ATTEMPTS)
            ],
        )


class ConcurrentChecklistTests(TransactionTestCase):
    def setUp(self):
        User = get_user_model()
        self.staff_a = User.objects.create_user(username="thread-a", password="safe-password")
        self.staff_b = User.objects.create_user(username="thread-b", password="safe-password")
        self.category = StaffCategory.objects.create(
            name="Concurrent Support", slug="concurrent-support", sort_order=10
        )
        self.shift = Shift.objects.create(
            name="07:00–15:00", start_time=time(7), end_time=time(15), sort_order=10
        )
        self.definition = ChecklistDefinition.objects.create(
            name="Concurrent Checklist",
            category=self.category,
            shift=self.shift,
            sort_order=10,
        )
        section = ChecklistSection.objects.create(
            definition=self.definition, name="Office", sort_order=10
        )
        self.task = TaskDefinition.objects.create(
            section=section, label="Concurrent task", sort_order=10, allow_na=True
        )
        for user in (self.staff_a, self.staff_b):
            StaffAssignment.objects.create(user=user, category=self.category)
        self.operational_date = date(2026, 9, 15)

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

    def test_concurrent_lazy_creation_returns_one_shared_checklist(self):
        def resolve():
            definition = ChecklistDefinition.objects.select_related(
                "category", "shift"
            ).get(pk=self.definition.pk)
            return resolve_checklist(definition, self.operational_date).pk

        instance_ids = self._run_together((resolve, resolve))

        self.assertEqual(instance_ids[0], instance_ids[1])
        self.assertEqual(ChecklistInstance.objects.count(), 1)
        self.assertEqual(ChecklistInstance.objects.get().items.count(), 1)

    def test_concurrent_state_changes_are_serialized_with_coherent_history(self):
        instance = resolve_checklist(self.definition, self.operational_date)
        item = instance.items.get(source_task=self.task)

        def change(user_id, state):
            def operation():
                staff = get_user_model().objects.get(pk=user_id)
                changed_item, contribution = change_item_state(
                    item_id=item.pk, staff=staff, new_state=state
                )
                return changed_item.current_state, contribution.pk

            return operation

        results = self._run_together(
            (
                change(self.staff_a.pk, TaskState.COMPLETED),
                change(self.staff_b.pk, TaskState.NOT_APPLICABLE),
            )
        )

        contributions = list(item.contributions.order_by("created_at", "id"))
        self.assertEqual(len(results), 2)
        self.assertEqual(len(contributions), 2)
        self.assertEqual(contributions[0].previous_state, TaskState.PENDING)
        self.assertEqual(contributions[1].previous_state, contributions[0].new_state)
        item.refresh_from_db()
        self.assertEqual(item.current_state, contributions[1].new_state)


class SeedConfigurationTests(TestCase):
    @patch.dict(
        os.environ,
        {
            "CHORE_TEST_STAFF_PASSWORD": "test-seed-password",
            "CHORE_ADMIN_PASSWORD": "admin-seed-password",
        },
    )
    def setUp(self):
        call_command("seed_development", verbosity=0)

    def test_seed_is_repeatable_and_does_not_create_kevin_account(self):
        original_test_password = get_user_model().objects.get(username="teststaff").password
        original_admin_password = get_user_model().objects.get(username="choreadmin").password
        day_shift = Shift.objects.get(seed_key="shift-day")
        day_shift_id = day_shift.pk
        day_shift.name = "Edited day shift"
        day_shift.start_time = time(6)
        day_shift.end_time = time(14)
        day_shift.save()
        section = ChecklistSection.objects.get(seed_key="section-support-day-01")
        section_id = section.pk
        section.name = "Edited section"
        section.sort_order = 15
        section.save()
        task = TaskDefinition.objects.get(seed_key="task-support-day-01-001")
        task_id = task.pk
        task.label = "Edited task"
        task.sort_order = 15
        task.save()

        with patch.dict(
            os.environ,
            {
                "CHORE_TEST_STAFF_PASSWORD": "test-seed-password",
                "CHORE_ADMIN_PASSWORD": "admin-seed-password",
            },
        ):
            call_command("seed_development", verbosity=0)

        User = get_user_model()
        self.assertEqual(User.objects.filter(username="teststaff").count(), 1)
        self.assertEqual(User.objects.filter(username="choreadmin").count(), 1)
        self.assertFalse(User.objects.filter(first_name="Kevin", last_name="Atlee").exists())
        self.assertEqual(StaffCategory.objects.count(), 4)
        self.assertEqual(Shift.objects.count(), 4)
        self.assertEqual(ChecklistDefinition.objects.count(), 7)
        self.assertEqual(ChecklistSection.objects.count(), 19)
        self.assertEqual(TaskDefinition.objects.count(), 190)
        self.assertEqual(TaskDefinition.objects.filter(is_active=True).count(), 190)
        day_shift.refresh_from_db()
        section.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual(day_shift.pk, day_shift_id)
        self.assertEqual(day_shift.name, "07:00–15:00")
        self.assertEqual(day_shift.start_time, time(7))
        self.assertEqual(day_shift.end_time, time(15))
        self.assertEqual(section.pk, section_id)
        self.assertEqual(section.name, "Office Responsibilities")
        self.assertEqual(section.sort_order, 10)
        self.assertEqual(task.pk, task_id)
        self.assertEqual(task.label, "Carry cell phone, fanny pack")
        self.assertEqual(task.sort_order, 10)
        self.assertEqual(User.objects.get(username="teststaff").password, original_test_password)
        self.assertEqual(User.objects.get(username="choreadmin").password, original_admin_password)

    def test_life_skills_seed_has_no_items_after_1500(self):
        definition = ChecklistDefinition.objects.get(category__slug="life-skills")
        tasks = TaskDefinition.objects.filter(section__definition=definition)

        self.assertEqual(tasks.count(), 40)
        self.assertFalse(tasks.filter(scheduled_end__gt=time(15, 0)).exists())

        # 2026-09-15 is a Tuesday; only that weekday's eight active items are snapshotted.
        instance = resolve_checklist(definition, date(2026, 9, 15))
        self.assertEqual(instance.items.count(), 8)
        self.assertFalse(instance.items.filter(scheduled_end_snapshot__gt=time(15, 0)).exists())

    def test_seed_matches_all_source_backed_labels_and_life_skills_schedule(self):
        for category_key, shift_key, _, _, expected_sections in DEFINITIONS:
            definition = ChecklistDefinition.objects.get(
                seed_key=f"definition-{category_key}-{shift_key}"
            )
            actual_sections = definition.sections.filter(is_active=True).order_by("sort_order")
            self.assertEqual(
                [section.name for section in actual_sections],
                [name for name, _ in expected_sections],
            )
            for section, (_, expected_labels) in zip(actual_sections, expected_sections):
                self.assertEqual(
                    list(
                        section.tasks.filter(is_active=True).order_by("sort_order").values_list(
                            "label", flat=True
                        )
                    ),
                    expected_labels,
                )

        life_definition = ChecklistDefinition.objects.get(
            seed_key="definition-life-skills-life-skills-day"
        )
        life_tasks = TaskDefinition.objects.filter(
            section__definition=life_definition, is_active=True
        )
        for order, (slot_key, start, end, labels) in enumerate(
            LIFE_SKILLS_SLOTS, start=1
        ):
            for weekday, label in enumerate(labels):
                task = life_tasks.get(seed_key=f"task-life-skills-{slot_key}-{weekday}")
                self.assertEqual(task.label, label)
                self.assertEqual(task.sort_order, order * 10)
                self.assertEqual(task.weekday, weekday)
                self.assertEqual(task.scheduled_start, time.fromisoformat(start))
                self.assertEqual(task.scheduled_end, time.fromisoformat(end))

    def test_removed_life_skills_rows_are_retired_not_restored(self):
        life_section = ChecklistSection.objects.get(
            seed_key="section-life-skills-life-skills-day-01"
        )
        removed_task = TaskDefinition.objects.create(
            section=life_section,
            weekday=0,
            sort_order=500,
            label="Break",
            scheduled_start=time(10),
            scheduled_end=time(10, 15),
        )

        call_command("seed_development", verbosity=0)

        removed_task.refresh_from_db()
        self.assertFalse(removed_task.is_active)
        self.assertEqual(
            removed_task.seed_key, "task-life-skills-retired-break-1000-0"
        )
        self.assertFalse(
            TaskDefinition.objects.filter(
                section=life_section,
                is_active=True,
                scheduled_start__in=(time(10), time(10, 15), time(12), time(14, 30)),
            ).exists()
        )

    def test_seed_without_password_variables_does_not_warn_for_usable_accounts(self):
        output = StringIO()
        with patch.dict(
            os.environ,
            {"CHORE_TEST_STAFF_PASSWORD": "", "CHORE_ADMIN_PASSWORD": ""},
        ):
            call_command("seed_development", stdout=output, verbosity=0)

        self.assertNotIn("has no usable password", output.getvalue())

    def test_overnight_operational_date_is_the_shift_start_date(self):
        definition = ChecklistDefinition.objects.get(
            seed_key="definition-awake-night-overnight"
        )
        shift_start_date = date(2026, 9, 15)

        instance = resolve_checklist(definition, shift_start_date)

        self.assertTrue(definition.shift.crosses_midnight)
        self.assertEqual(instance.operational_date, shift_start_date)
        test_staff = get_user_model().objects.get(username="teststaff")
        self.client.force_login(test_staff)
        response = self.client.get(reverse("dashboard"))
        self.assertContains(
            response, "For an overnight shift, use the date on which the shift starts."
        )

    def test_seeded_test_staff_is_assigned_to_all_categories(self):
        test_staff = get_user_model().objects.get(username="teststaff")
        admin = get_user_model().objects.get(username="choreadmin")
        self.assertFalse(test_staff.is_staff)
        self.assertTrue(test_staff.check_password("test-seed-password"))
        self.assertNotEqual(test_staff.password, "test-seed-password")
        self.assertTrue(admin.is_staff)
        self.assertTrue(admin.is_superuser)
        self.assertTrue(admin.check_password("admin-seed-password"))
        self.assertEqual(test_staff.staff_assignments.filter(is_active=True).count(), 4)
