import os
from datetime import date, time
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase
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
from .services import change_item_state, resolve_checklist


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
        self.assertEqual(ChecklistDefinition.objects.count(), 7)

    def test_life_skills_seed_has_no_items_after_1500(self):
        definition = ChecklistDefinition.objects.get(category__slug="life-skills")
        tasks = TaskDefinition.objects.filter(section__definition=definition)

        self.assertEqual(tasks.count(), 55)
        self.assertFalse(tasks.filter(scheduled_end__gt=time(15, 0)).exists())

        # 2026-09-15 is a Tuesday; only that weekday's 11 schedule items are snapshotted.
        instance = resolve_checklist(definition, date(2026, 9, 15))
        self.assertEqual(instance.items.count(), 11)
        self.assertFalse(instance.items.filter(scheduled_end_snapshot__gt=time(15, 0)).exists())

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
