import os
from datetime import date, time, timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.db import models
from django.test import TestCase
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
from .seed_data import STAFF_ROSTER
from .services import change_item_state, resolve_checklist


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
        instance = resolve_checklist(self.definition, self.operational_date)
        item = instance.items.get(source_task=self.regular)
        self.category.name = "Renamed"
        self.category.save(update_fields=("name",))
        self.shift.name = "Changed"
        self.shift.save(update_fields=("name",))
        self.regular.label = "Changed task"
        self.regular.save(update_fields=("label",))
        instance.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(instance.category_name_snapshot, "Support")
        self.assertEqual(instance.shift_name_snapshot, "Morning")
        self.assertEqual(item.task_label_snapshot, "Regular task")


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

    def test_life_skills_uses_morning_and_valid_combinations_are_exact(self):
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
        call_command("seed_development", reset_passwords=True, verbosity=0)
        self.assertEqual(ids["staff"], list(StaffMember.objects.values_list("pk", flat=True)))
        self.assertEqual(ids["shifts"], list(Shift.objects.values_list("pk", flat=True)))
        self.assertEqual(ids["definitions"], list(ChecklistDefinition.objects.values_list("pk", flat=True)))


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
