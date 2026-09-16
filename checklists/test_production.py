import sqlite3
from contextlib import closing
from datetime import date, timedelta
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core import mail
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.mail import send_mail
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from chore.environment import fqdn_configuration, parse_bool
from chore.sqlite_backups import (
    BackupError,
    create_daily_backup,
    refresh_database_from_backup,
    validate_chore_database,
)


class RuntimeConfigurationTests(SimpleTestCase):
    def test_fqdn_derives_allowed_host_and_https_csrf_origin(self):
        configuration = fqdn_configuration(
            "Chore.Example.com.",
            "internal.example,localhost",
            "https://extra.example",
        )

        self.assertEqual(configuration["fqdn"], "chore.example.com")
        self.assertEqual(
            configuration["allowed_hosts"],
            ["chore.example.com", "internal.example", "localhost"],
        )
        self.assertEqual(
            configuration["trusted_origins"],
            ["https://chore.example.com", "https://extra.example"],
        )

    def test_fqdn_rejects_a_url_or_port(self):
        for invalid in ("https://chore.example.com", "chore.example.com:8443"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ImproperlyConfigured):
                    fqdn_configuration(invalid)

    def test_boolean_parser_fails_closed_on_unknown_values(self):
        with self.assertRaises(ImproperlyConfigured):
            parse_bool("sometimes", name="EMAIL_ENABLED")


class HealthEndpointTests(TestCase):
    def test_health_is_public_and_checks_database(self):
        response = self.client.get(reverse("health"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})


class EmailSafetyTests(TestCase):
    @override_settings(
        EMAIL_ENABLED=False,
        EMAIL_BACKEND="checklists.email_backends.DisabledEmailBackend",
    )
    def test_disabled_backend_suppresses_all_django_email(self):
        with self.assertLogs("checklists.email_backends", level="WARNING"):
            sent = send_mail(
                "Subject",
                "Body",
                "from@example.com",
                ["manager@example.com"],
            )

        self.assertEqual(sent, 0)

    @override_settings(
        EMAIL_ENABLED=False,
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    )
    def test_scheduled_reports_stop_before_attempting_delivery(self):
        output = StringIO()

        call_command("send_scheduled_reports", stdout=output)

        self.assertIn("scheduled reports were suppressed", output.getvalue())
        self.assertEqual(len(mail.outbox), 0)


class SQLiteBackupTests(SimpleTestCase):
    def setUp(self):
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.database = self.root / "source.sqlite3"
        self.backups = self.root / "backups"
        self._create_database(self.database, "initial")

    def _create_database(self, path, marker):
        with closing(sqlite3.connect(path)) as connection:
            connection.execute(
                "CREATE TABLE django_migrations "
                "(id integer PRIMARY KEY, app varchar(255), name varchar(255), applied datetime)"
            )
            connection.execute("CREATE TABLE sample (value text)")
            connection.execute("INSERT INTO sample (value) VALUES (?)", (marker,))
            connection.commit()

    def _marker(self, path):
        with closing(sqlite3.connect(path)) as connection:
            return connection.execute("SELECT value FROM sample").fetchone()[0]

    def test_safe_daily_backup_is_valid_idempotent_and_retains_seven(self):
        first_day = date(2026, 9, 1)
        first_path = None
        for offset in range(9):
            path, created, _removed = create_daily_backup(
                self.database,
                self.backups,
                backup_date=first_day + timedelta(days=offset),
                retention=7,
            )
            if offset == 0:
                first_path = path
            self.assertTrue(created)
            self.assertTrue(validate_chore_database(path))

        self.assertFalse(first_path.exists())
        self.assertEqual(len(list(self.backups.glob("chore-*.sqlite3"))), 7)

        newest_date = first_day + timedelta(days=8)
        newest, created, removed = create_daily_backup(
            self.database,
            self.backups,
            backup_date=newest_date,
            retention=7,
        )
        self.assertFalse(created)
        self.assertEqual(removed, [])
        self.assertEqual(self._marker(newest), "initial")

    def test_test_refresh_uses_newest_valid_backup_and_writable_copy(self):
        valid, _created, _removed = create_daily_backup(
            self.database,
            self.backups,
            backup_date=date(2026, 9, 15),
        )
        corrupt = self.backups / "chore-2026-09-16.sqlite3"
        corrupt.write_bytes(b"not a sqlite database")
        destination = self.root / "test-data" / "db.sqlite3"

        source, copied = refresh_database_from_backup(self.backups, destination)

        self.assertEqual(source, valid)
        self.assertEqual(copied, destination)
        self.assertEqual(self._marker(destination), "initial")
        with closing(sqlite3.connect(destination)) as connection:
            connection.execute("UPDATE sample SET value = 'test-only'")
            connection.commit()
        self.assertEqual(self._marker(destination), "test-only")
        self.assertEqual(self._marker(valid), "initial")

    def test_test_refresh_fails_when_no_valid_backup_exists(self):
        self.backups.mkdir()
        (self.backups / "chore-2026-09-16.sqlite3").write_bytes(b"corrupt")

        with self.assertRaisesRegex(BackupError, "No valid production backup"):
            refresh_database_from_backup(
                self.backups, self.root / "test-data" / "db.sqlite3"
            )
