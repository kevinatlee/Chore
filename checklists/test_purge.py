import os
import sqlite3
import subprocess
import sys
from datetime import date, time
from io import StringIO
from tempfile import TemporaryDirectory

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import FieldDoesNotExist
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase

from .models import (
    ChecklistDefinition,
    ChecklistInstance,
    ChecklistItem,
    ChecklistSection,
    Program,
    ProgramMembership,
    ProgramRole,
    ReportCadence,
    ScheduledReportDelivery,
    Shift,
    StaffCategory,
    StaffContribution,
    StaffMember,
    TaskDefinition,
    TaskState,
)
from .services import change_item_state, resolve_checklist


class FreshStartPurgeTests(TestCase):
    operational_date = date(2026, 9, 16)

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            "operator", email="operator@example.com", password="password"
        )
        self.program = Program.objects.create(name="Purge Program", slug="purge-program")
        self.membership = ProgramMembership.objects.create(
            user=self.user,
            program=self.program,
            role=ProgramRole.OPERATIONAL,
        )
        self.staff = StaffMember.objects.create(
            program=self.program,
            first_name="Operational",
            last_name="Staff",
        )
        self.shift = Shift.objects.create(
            name="Morning", start_time=time(7), end_time=time(15), sort_order=10
        )
        self.category = StaffCategory.objects.create(
            program=self.program,
            name="Support",
            slug="support",
            sort_order=10,
        )
        self.definition = ChecklistDefinition.objects.create(
            name="Support Morning",
            category=self.category,
            shift=self.shift,
            sort_order=10,
        )
        self.section = ChecklistSection.objects.create(
            definition=self.definition,
            name="Office",
            sort_order=10,
        )
        self.task = TaskDefinition.objects.create(
            section=self.section,
            label="Test task",
            sort_order=10,
        )

        self.instance = resolve_checklist(self.definition, self.operational_date)
        self.item = self.instance.items.get(source_task=self.task)
        change_item_state(
            item_id=self.item.pk,
            actor=None,
            staff_member=self.staff,
            new_state=TaskState.COMPLETED,
            system=True,
        )

        self.mock_instance = resolve_checklist(self.definition, date(2026, 9, 15))
        self.mock_instance.is_mock_data = True
        self.mock_instance.save(update_fields=("is_mock_data",))
        self.mock_item = self.mock_instance.items.get(source_task=self.task)
        change_item_state(
            item_id=self.mock_item.pk,
            actor=None,
            staff_member=self.staff,
            new_state=TaskState.COMPLETED,
            system=True,
        )

        self.delivery = ScheduledReportDelivery.objects.create(
            program=self.program,
            cadence=ReportCadence.DAILY,
            period_start=self.operational_date,
            period_end=self.operational_date,
            recipient=self.user,
            recipient_email=self.user.email,
            subject="Test report",
            body_html="<p>Test report</p>",
            snapshot={"test": True},
        )

    def test_fresh_start_dry_run_preserves_runtime_and_mock_data(self):
        output = StringIO()

        call_command(
            "purge_operational_data",
            fresh_start=True,
            dry_run=True,
            stdout=output,
            verbosity=0,
        )

        self.assertTrue(ChecklistInstance.objects.filter(pk=self.instance.pk).exists())
        self.assertTrue(ChecklistItem.objects.filter(pk=self.item.pk).exists())
        self.assertTrue(ChecklistInstance.objects.filter(pk=self.mock_instance.pk).exists())
        self.assertTrue(ChecklistItem.objects.filter(pk=self.mock_item.pk).exists())
        self.assertEqual(StaffContribution.objects.count(), 2)
        self.assertTrue(ScheduledReportDelivery.objects.filter(pk=self.delivery.pk).exists())
        self.assertIn("Dry run (fresh start)", output.getvalue())
        self.assertIn(
            "1 non-mock checklist(s), 1 item(s), 1 contribution(s), "
            "1 scheduled report delivery record(s)",
            output.getvalue(),
        )
        self.assertIn("preserving 1 generated mock checklist(s)", output.getvalue())

    def test_fresh_start_removes_real_runtime_history_and_preserves_mock_and_configuration(self):
        call_command("purge_operational_data", fresh_start=True, verbosity=0)

        self.assertFalse(ChecklistInstance.objects.filter(pk=self.instance.pk).exists())
        self.assertFalse(ChecklistItem.objects.filter(pk=self.item.pk).exists())
        self.assertTrue(ChecklistInstance.objects.filter(pk=self.mock_instance.pk).exists())
        self.assertTrue(ChecklistItem.objects.filter(pk=self.mock_item.pk).exists())
        self.assertEqual(ChecklistInstance.objects.count(), 1)
        self.assertEqual(ChecklistItem.objects.count(), 1)
        self.assertEqual(StaffContribution.objects.count(), 1)
        self.assertFalse(
            StaffContribution.objects.filter(item_id=self.item.pk).exists()
        )
        self.assertTrue(
            StaffContribution.objects.filter(item_id=self.mock_item.pk).exists()
        )
        self.assertEqual(ScheduledReportDelivery.objects.count(), 0)

        self.assertTrue(get_user_model().objects.filter(pk=self.user.pk).exists())
        self.assertTrue(ProgramMembership.objects.filter(pk=self.membership.pk).exists())
        self.assertTrue(StaffMember.objects.filter(pk=self.staff.pk).exists())
        self.assertTrue(Program.objects.filter(pk=self.program.pk).exists())
        self.assertTrue(Shift.objects.filter(pk=self.shift.pk).exists())
        self.assertTrue(StaffCategory.objects.filter(pk=self.category.pk).exists())
        self.assertTrue(ChecklistDefinition.objects.filter(pk=self.definition.pk).exists())
        self.assertTrue(ChecklistSection.objects.filter(pk=self.section.pk).exists())
        self.assertTrue(TaskDefinition.objects.filter(pk=self.task.pk).exists())

    def test_retention_removes_old_real_history_and_preserves_old_mock_history(self):
        old_real = resolve_checklist(self.definition, date(2018, 1, 2))
        old_real_item = old_real.items.get(source_task=self.task)
        change_item_state(
            item_id=old_real_item.pk,
            actor=None,
            staff_member=self.staff,
            new_state=TaskState.COMPLETED,
            system=True,
        )
        old_mock = resolve_checklist(self.definition, date(2018, 1, 1))
        old_mock.is_mock_data = True
        old_mock.save(update_fields=("is_mock_data",))
        old_mock_item = old_mock.items.get(source_task=self.task)
        change_item_state(
            item_id=old_mock_item.pk,
            actor=None,
            staff_member=self.staff,
            new_state=TaskState.COMPLETED,
            system=True,
        )

        call_command(
            "purge_operational_data",
            as_of="2026-09-16",
            verbosity=0,
        )

        self.assertFalse(ChecklistInstance.objects.filter(pk=old_real.pk).exists())
        self.assertFalse(ChecklistItem.objects.filter(pk=old_real_item.pk).exists())
        self.assertFalse(
            StaffContribution.objects.filter(item_id=old_real_item.pk).exists()
        )
        self.assertTrue(ChecklistInstance.objects.filter(pk=old_mock.pk).exists())
        self.assertTrue(ChecklistItem.objects.filter(pk=old_mock_item.pk).exists())
        self.assertTrue(
            StaffContribution.objects.filter(item_id=old_mock_item.pk).exists()
        )

    def test_fresh_start_rejects_retention_date(self):
        with self.assertRaisesMessage(
            CommandError, "--as-of cannot be used with --fresh-start."
        ):
            call_command(
                "purge_operational_data",
                fresh_start=True,
                as_of="2026-09-16",
                verbosity=0,
            )


class MembershipSchemaMigrationTests(SimpleTestCase):
    legacy_column = "is_test_staff"

    def _migrate(self, database_path, target=None):
        command = [sys.executable, str(settings.BASE_DIR / "manage.py"), "migrate"]
        if target:
            command.extend(("checklists", target))
        command.append("--noinput")
        environment = os.environ.copy()
        environment["CHORE_DATABASE_PATH"] = str(database_path)
        return subprocess.run(
            command,
            cwd=settings.BASE_DIR,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )

    def _membership_columns(self, database_path):
        connection = sqlite3.connect(database_path)
        try:
            return {
                row[1]
                for row in connection.execute(
                    'PRAGMA table_info("checklists_programmembership")'
                )
            }
        finally:
            connection.close()

    def test_fresh_and_phase_two_upgrade_paths_converge_without_legacy_column(self):
        with TemporaryDirectory() as directory:
            fresh_database = settings.BASE_DIR / directory / "fresh.sqlite3"
            upgrade_database = settings.BASE_DIR / directory / "upgrade.sqlite3"

            self._migrate(fresh_database)
            self.assertNotIn(
                self.legacy_column, self._membership_columns(fresh_database)
            )

            self._migrate(upgrade_database, "0003_phase_2_program_reporting")
            connection = sqlite3.connect(upgrade_database)
            try:
                connection.execute(
                    f'ALTER TABLE "checklists_programmembership" ADD COLUMN '
                    f'"{self.legacy_column}" bool NOT NULL DEFAULT 0'
                )
                connection.commit()
            finally:
                connection.close()
            self.assertIn(
                self.legacy_column, self._membership_columns(upgrade_database)
            )

            self._migrate(upgrade_database)
            self.assertNotIn(
                self.legacy_column, self._membership_columns(upgrade_database)
            )

        with self.assertRaises(FieldDoesNotExist):
            ProgramMembership._meta.get_field(self.legacy_column)
