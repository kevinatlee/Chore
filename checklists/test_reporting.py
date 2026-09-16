from datetime import date, datetime, time, timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import (
    ChecklistDefinition,
    ChecklistInstance,
    ChecklistSection,
    Program,
    ProgramMembership,
    ProgramRole,
    ReportCadence,
    ScheduledReportDelivery,
    Shift,
    StaffAssignment,
    StaffCategory,
    TaskDefinition,
    TaskState,
)
from .reporting import ReportPeriod, build_report, period_from_params, scheduled_periods
from .services import change_item_state, resolve_checklist


class ReportingFixtureMixin:
    operational_date = date(2026, 9, 15)

    def setUp(self):
        User = get_user_model()
        self.program_a = Program.objects.create(name="Program Alpha", slug="alpha")
        self.program_b = Program.objects.create(name="Program Beta", slug="beta")
        self.manager_a = User.objects.create_user(
            "manager-a", email="manager-a@example.com", password="password", is_staff=True
        )
        self.manager_b = User.objects.create_user(
            "manager-b", email="manager-b@example.com", password="password", is_staff=True
        )
        self.admin = User.objects.create_superuser(
            "admin-report", "admin@example.com", "password"
        )
        self.staff_a = User.objects.create_user(
            "staff-prod", first_name="Production", last_name="Staff", password="password"
        )
        self.staff_a2 = User.objects.create_user(
            "staff-prod-2", first_name="Second", last_name="Staff", password="password"
        )
        self.test_staff = User.objects.create_user(
            "test-report", first_name="Test", last_name="Staff", password="password"
        )
        self.ordinary_staff = User.objects.create_user("ordinary", password="password")
        self.membership_a = ProgramMembership.objects.create(
            user=self.manager_a,
            program=self.program_a,
            role=ProgramRole.MANAGER,
            receive_scheduled_reports=True,
        )
        ProgramMembership.objects.create(
            user=self.manager_b,
            program=self.program_b,
            role=ProgramRole.MANAGER,
            receive_scheduled_reports=True,
        )
        ProgramMembership.objects.create(
            user=self.test_staff,
            program=self.program_a,
            role=ProgramRole.STAFF,
            is_test_staff=True,
        )
        self.shift = Shift.objects.create(
            name="07:00–15:00", start_time=time(7), end_time=time(15), sort_order=10
        )
        self.category_a = StaffCategory.objects.create(
            program=self.program_a, name="Support", slug="support", sort_order=10
        )
        self.category_b = StaffCategory.objects.create(
            program=self.program_b, name="Support", slug="support", sort_order=10
        )
        self.definition_a, self.regular_a, self.optional_a = self._definition(self.category_a, "Alpha")
        self.definition_b, self.regular_b, self.optional_b = self._definition(self.category_b, "Beta")
        for user in (self.staff_a, self.staff_a2, self.test_staff, self.ordinary_staff):
            StaffAssignment.objects.create(user=user, category=self.category_a)
        StaffAssignment.objects.create(user=self.manager_b, category=self.category_b)

    def _definition(self, category, prefix):
        definition = ChecklistDefinition.objects.create(
            name=f"{prefix} checklist", category=category, shift=self.shift
        )
        section = ChecklistSection.objects.create(
            definition=definition, name=f"{prefix} section", sort_order=10
        )
        regular = TaskDefinition.objects.create(
            section=section, label=f"{prefix} regular", sort_order=10
        )
        optional = TaskDefinition.objects.create(
            section=section, label=f"{prefix} optional", sort_order=20, allow_na=True
        )
        return definition, regular, optional

    def _report(self, **filters):
        period = ReportPeriod(
            "daily", self.operational_date, self.operational_date, "fixture"
        )
        return build_report(program=self.program_a, period=period, filters=filters)


class ReportCalculationTests(ReportingFixtureMixin, TestCase):
    def test_fully_completed_shared_checklist(self):
        instance = resolve_checklist(self.definition_a, self.operational_date)
        for item in instance.items.all():
            change_item_state(item_id=item.pk, staff=self.staff_a, new_state=TaskState.COMPLETED)
        row = self._report()["rows"][0]
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["completed_count"], 2)
        self.assertEqual(row["completion_percentage"], 100.0)

    def test_partial_multiple_staff_and_unfinished_contributors(self):
        instance = resolve_checklist(self.definition_a, self.operational_date)
        regular = instance.items.get(source_task=self.regular_a)
        optional = instance.items.get(source_task=self.optional_a)
        change_item_state(item_id=regular.pk, staff=self.staff_a, new_state=TaskState.COMPLETED)
        change_item_state(item_id=optional.pk, staff=self.staff_a2, new_state=TaskState.COMPLETED)
        change_item_state(item_id=optional.pk, staff=self.staff_a2, new_state=TaskState.PENDING)
        row = self._report()["rows"][0]
        self.assertEqual(row["status"], "incomplete")
        self.assertEqual(row["completed_count"], 1)
        self.assertEqual(row["pending_count"], 1)
        self.assertCountEqual(row["contributors"], ["Production Staff", "Second Staff"])
        self.assertEqual(len(row["tasks"][1]["contributions"]), 2)

    def test_na_is_excluded_from_denominator_and_attributed(self):
        instance = resolve_checklist(self.definition_a, self.operational_date)
        regular = instance.items.get(source_task=self.regular_a)
        optional = instance.items.get(source_task=self.optional_a)
        change_item_state(item_id=regular.pk, staff=self.staff_a, new_state=TaskState.COMPLETED)
        change_item_state(item_id=optional.pk, staff=self.staff_a2, new_state=TaskState.NOT_APPLICABLE)
        row = self._report()["rows"][0]
        self.assertEqual(row["applicable_count"], 1)
        self.assertEqual(row["completed_count"], 1)
        self.assertEqual(row["na_count"], 1)
        self.assertEqual(row["completion_percentage"], 100.0)
        na_task = next(task for task in row["tasks"] if task["state"] == TaskState.NOT_APPLICABLE)
        self.assertEqual(na_task["contributor"], "Second Staff")

    def test_na_not_allowed_is_rejected_and_reported_pending(self):
        instance = resolve_checklist(self.definition_a, self.operational_date)
        item = instance.items.get(source_task=self.regular_a)
        with self.assertRaises(ValidationError):
            change_item_state(item_id=item.pk, staff=self.staff_a, new_state=TaskState.NOT_APPLICABLE)
        row = self._report()["rows"][0]
        self.assertEqual(row["pending_count"], 2)

    def test_no_contributors(self):
        resolve_checklist(self.definition_a, self.operational_date)
        row = self._report()["rows"][0]
        self.assertEqual(row["contributors"], [])
        self.assertEqual(row["pending_count"], 2)

    def test_test_staff_only_activity_cannot_contaminate_production(self):
        instance = resolve_checklist(self.definition_a, self.operational_date)
        for item in instance.items.all():
            change_item_state(item_id=item.pk, staff=self.test_staff, new_state=TaskState.COMPLETED)
        membership = ProgramMembership.objects.get(
            user=self.test_staff, program=self.program_a
        )
        membership.is_active = False
        membership.save(update_fields=("is_active",))
        row = self._report()["rows"][0]
        self.assertEqual(row["completed_count"], 0)
        self.assertEqual(row["pending_count"], 2)
        self.assertEqual(row["contributors"], [])

    def test_mixed_test_and_production_uses_latest_production_history(self):
        instance = resolve_checklist(self.definition_a, self.operational_date)
        item = instance.items.get(source_task=self.regular_a)
        change_item_state(item_id=item.pk, staff=self.test_staff, new_state=TaskState.COMPLETED)
        change_item_state(item_id=item.pk, staff=self.staff_a, new_state=TaskState.PENDING)
        change_item_state(item_id=item.pk, staff=self.test_staff, new_state=TaskState.COMPLETED)
        task = self._report()["rows"][0]["tasks"][0]
        self.assertEqual(task["state"], TaskState.PENDING)
        self.assertEqual(task["contributor"], "Production Staff")
        self.assertEqual(len(task["contributions"]), 1)

    def test_late_historical_entry_changes_live_report(self):
        instance = resolve_checklist(self.definition_a, self.operational_date)
        item = instance.items.get(source_task=self.regular_a)
        self.assertEqual(self._report()["totals"]["completed"], 0)
        change_item_state(item_id=item.pk, staff=self.staff_a, new_state=TaskState.COMPLETED)
        self.assertEqual(self._report()["totals"]["completed"], 1)

    def test_multiple_programs_with_overlapping_identity_are_isolated(self):
        resolve_checklist(self.definition_a, self.operational_date)
        beta = resolve_checklist(self.definition_b, self.operational_date)
        item = beta.items.get(source_task=self.regular_b)
        change_item_state(item_id=item.pk, staff=self.manager_b, new_state=TaskState.COMPLETED)
        report = self._report()
        self.assertEqual(len(report["rows"]), 1)
        self.assertNotIn("Beta", str(report["rows"]))


class ReportPeriodTests(TestCase):
    def test_daily_weekly_month_and_rolling_boundaries(self):
        self.assertEqual(
            period_from_params({"period": "daily", "date": "2026-09-15"}).start,
            date(2026, 9, 15),
        )
        weekly = period_from_params({"period": "weekly", "date": "2026-09-16"})
        self.assertEqual((weekly.start, weekly.end), (date(2026, 9, 14), date(2026, 9, 20)))
        monthly = period_from_params({"period": "monthly", "month": "2024-02"})
        self.assertEqual((monthly.start, monthly.end), (date(2024, 2, 1), date(2024, 2, 29)))
        rolling = period_from_params({"period": "rolling365", "date": "2024-03-01"})
        self.assertEqual((rolling.end - rolling.start).days, 364)

    def test_calendar_year_and_previous_calendar_year_schedule(self):
        annual = period_from_params({"period": "annual", "year": "2024"})
        self.assertEqual((annual.start, annual.end), (date(2024, 1, 1), date(2024, 12, 31)))
        due = dict(scheduled_periods(date(2027, 1, 1)))
        self.assertEqual((due["annual"].start, due["annual"].end), (date(2026, 1, 1), date(2026, 12, 31)))
        self.assertEqual((due["monthly"].start, due["monthly"].end), (date(2026, 12, 1), date(2026, 12, 31)))


class ReportSecurityAndExportTests(ReportingFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        alpha = resolve_checklist(self.definition_a, self.operational_date)
        beta = resolve_checklist(self.definition_b, self.operational_date)
        change_item_state(
            item_id=alpha.items.get(source_task=self.regular_a).pk,
            staff=self.staff_a,
            new_state=TaskState.COMPLETED,
        )
        change_item_state(
            item_id=beta.items.get(source_task=self.regular_b).pk,
            staff=self.manager_b,
            new_state=TaskState.COMPLETED,
        )

    def test_manager_cannot_select_or_drill_into_other_program(self):
        self.client.force_login(self.manager_a)
        response = self.client.get(reverse("reports"), {"program": self.program_b.pk})
        self.assertEqual(response.status_code, 403)
        beta_instance = ChecklistInstance.objects.get(program=self.program_b)
        response = self.client.get(
            reverse("report-detail", args=(beta_instance.pk,)),
            {"date": self.operational_date.isoformat()},
        )
        self.assertEqual(response.status_code, 403)

    def test_cross_program_category_and_shift_query_cannot_leak(self):
        self.client.force_login(self.manager_a)
        response = self.client.get(
            reverse("reports"),
            {"date": self.operational_date.isoformat(), "category": self.category_b.pk},
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Beta regular")
        self.assertEqual(response.context["rows"], [])

    def test_csv_is_program_scoped_and_honours_filters(self):
        alpha = ChecklistInstance.objects.get(program=self.program_a)
        change_item_state(
            item_id=alpha.items.get(source_task=self.optional_a).pk,
            staff=self.test_staff,
            new_state=TaskState.COMPLETED,
        )
        self.client.force_login(self.manager_a)
        response = self.client.get(
            reverse("report-csv"),
            {"date": self.operational_date.isoformat(), "task_state": "completed"},
        )
        content = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn("Alpha regular", content)
        self.assertNotIn("Alpha optional", content)
        self.assertNotIn("Beta regular", content)

    def test_staff_denied_manager_allowed_admin_allowed(self):
        self.client.force_login(self.ordinary_staff)
        self.assertEqual(self.client.get(reverse("reports")).status_code, 403)
        self.client.force_login(self.manager_a)
        self.assertEqual(self.client.get(reverse("reports")).status_code, 200)
        self.client.force_login(self.admin)
        self.assertEqual(
            self.client.get(reverse("reports"), {"program": self.program_a.pk}).status_code,
            200,
        )

    def test_inactive_configuration_history_reports_but_cannot_be_reopened(self):
        self.definition_a.is_active = False
        self.definition_a.save(update_fields=("is_active",))
        self.client.force_login(self.manager_a)
        response = self.client.get(reverse("reports"), {"date": self.operational_date.isoformat()})
        self.assertEqual(len(response.context["rows"]), 1)
        with self.assertRaises(PermissionDenied):
            resolve_checklist(self.definition_a, self.operational_date + timedelta(days=1))

    def test_admin_can_explicitly_inspect_test_staff_activity(self):
        alpha = ChecklistInstance.objects.get(program=self.program_a)
        change_item_state(
            item_id=alpha.items.get(source_task=self.optional_a).pk,
            staff=self.test_staff,
            new_state=TaskState.COMPLETED,
        )
        self.client.force_login(self.admin)
        normal = self.client.get(
            reverse("reports"),
            {"program": self.program_a.pk, "date": self.operational_date.isoformat()},
        )
        inspection = self.client.get(
            reverse("reports"),
            {
                "program": self.program_a.pk,
                "date": self.operational_date.isoformat(),
                "include_test": "1",
            },
        )
        self.assertEqual(normal.context["totals"]["completed"], 1)
        self.assertEqual(inspection.context["totals"]["completed"], 2)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class ScheduledReportTests(ReportingFixtureMixin, TestCase):
    def test_multiple_enabled_managers_receive_the_same_program_report(self):
        second_manager = get_user_model().objects.create_user(
            "manager-a-second", email="manager-a-second@example.com", password="password"
        )
        ProgramMembership.objects.create(
            user=second_manager,
            program=self.program_a,
            role=ProgramRole.MANAGER,
            receive_scheduled_reports=True,
        )
        call_command("send_scheduled_reports", at="2026-09-16T08:00:00", verbosity=0)
        recipients = [address for message in mail.outbox for address in message.to]
        self.assertIn("manager-a@example.com", recipients)
        self.assertIn("manager-a-second@example.com", recipients)

    def test_email_routing_is_program_scoped_idempotent_and_excludes_test_staff(self):
        instance = resolve_checklist(self.definition_a, date(2026, 9, 20))
        item = instance.items.get(source_task=self.regular_a)
        change_item_state(item_id=item.pk, staff=self.test_staff, new_state=TaskState.COMPLETED)
        call_command("send_scheduled_reports", at="2026-09-21T08:00:00", verbosity=0)
        self.assertEqual(len(mail.outbox), 4)  # daily and weekly for each Program's Manager
        alpha_messages = [message for message in mail.outbox if message.to == ["manager-a@example.com"]]
        beta_messages = [message for message in mail.outbox if message.to == ["manager-b@example.com"]]
        self.assertEqual(len(alpha_messages), 2)
        self.assertEqual(len(beta_messages), 2)
        self.assertTrue(all("Program Beta" not in message.body for message in alpha_messages))
        self.assertTrue(all("Program Alpha" not in message.body for message in beta_messages))
        self.assertEqual(ScheduledReportDelivery.objects.filter(program=self.program_a).count(), 2)
        daily = ScheduledReportDelivery.objects.get(
            program=self.program_a, cadence=ReportCadence.DAILY
        )
        self.assertEqual(daily.snapshot["totals"]["completed"], 0)
        self.assertEqual(daily.snapshot["totals"]["pending"], 2)
        call_command("send_scheduled_reports", at="2026-09-21T09:00:00", verbosity=0)
        self.assertEqual(len(mail.outbox), 4)

    def test_admins_are_not_automatic_recipients(self):
        call_command("send_scheduled_reports", at="2026-09-16T08:00:00", verbosity=0)
        recipients = [address for message in mail.outbox for address in message.to]
        self.assertNotIn("admin@example.com", recipients)
        self.assertIn("manager-b@example.com", recipients)

    def test_sent_snapshot_does_not_change_after_late_entry(self):
        instance = resolve_checklist(self.definition_a, date(2026, 9, 15))
        item = instance.items.get(source_task=self.regular_a)
        call_command("send_scheduled_reports", at="2026-09-16T08:00:00", verbosity=0)
        delivery = ScheduledReportDelivery.objects.get(program=self.program_a)
        original_snapshot = delivery.snapshot
        change_item_state(item_id=item.pk, staff=self.staff_a, new_state=TaskState.COMPLETED)
        live = build_report(
            program=self.program_a,
            period=ReportPeriod("daily", date(2026, 9, 15), date(2026, 9, 15), ""),
        )
        self.assertEqual(live["totals"]["completed"], 1)
        delivery.refresh_from_db()
        self.assertEqual(delivery.snapshot, original_snapshot)


class RetentionTests(ReportingFixtureMixin, TestCase):
    def test_seven_calendar_year_boundary_is_retained_and_older_is_purged(self):
        boundary = resolve_checklist(self.definition_a, date(2019, 9, 16))
        older = resolve_checklist(self.definition_a, date(2019, 9, 15))
        change_item_state(
            item_id=older.items.get(source_task=self.regular_a).pk,
            staff=self.staff_a,
            new_state=TaskState.COMPLETED,
        )
        call_command("purge_operational_data", as_of="2026-09-16", verbosity=0)
        self.assertTrue(ChecklistInstance.objects.filter(pk=boundary.pk).exists())
        self.assertFalse(ChecklistInstance.objects.filter(pk=older.pk).exists())
        self.assertTrue(Program.objects.filter(pk=self.program_a.pk).exists())
        self.assertTrue(TaskDefinition.objects.filter(pk=self.regular_a.pk).exists())

    def test_dry_run_does_not_delete(self):
        older = resolve_checklist(self.definition_a, date(2018, 1, 1))
        output = StringIO()
        call_command(
            "purge_operational_data",
            as_of="2026-09-16",
            dry_run=True,
            stdout=output,
            verbosity=0,
        )
        self.assertTrue(ChecklistInstance.objects.filter(pk=older.pk).exists())
        self.assertIn("Dry run", output.getvalue())


class SeedReportingStabilityTests(TestCase):
    def test_seed_rerun_preserves_operational_snapshot_and_reporting_history(self):
        call_command("seed_development", verbosity=0)
        program = Program.objects.get(slug="sonder-house")
        definition = ChecklistDefinition.objects.filter(category__program=program).first()
        production_staff = get_user_model().objects.create_user("seed-history-staff")
        StaffAssignment.objects.create(user=production_staff, category=definition.category)
        operational_date = date(2026, 9, 16)
        instance = resolve_checklist(definition, operational_date)
        item = instance.items.first()
        original_label = item.task_label_snapshot
        change_item_state(
            item_id=item.pk,
            staff=production_staff,
            new_state=TaskState.COMPLETED,
        )

        call_command("seed_development", verbosity=0)

        item.refresh_from_db()
        report = build_report(
            program=program,
            period=ReportPeriod("daily", operational_date, operational_date, ""),
        )
        self.assertEqual(item.task_label_snapshot, original_label)
        self.assertEqual(item.contributions.count(), 1)
        self.assertEqual(report["totals"]["completed"], 1)
