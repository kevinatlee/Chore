# Chore

Chore is a shared operational checklist application for Sonder House. It separates authenticated application accounts from the operational staff roster, provides lazily created shared checklists, records per-task Pending/Completed/N/A state and Staff Contribution history, and supplies Program-scoped operational reporting.

The important identity rule is enforced in the database: one operational checklist exists for each Program, date, position, and shift. Selecting another staff name changes attribution, never checklist identity.

## Technology

- Python 3.12+
- Django 5.2 LTS
- SQLite for local development
- Server-rendered HTML and responsive CSS
- Django's built-in session authentication and administration

## Local setup

PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python manage.py migrate
$env:CHORE_OPERATIONAL_PASSWORD = "choose-a-local-password"
python manage.py seed_development --reset-passwords
python manage.py runserver
```

If Python is not on `PATH`, use an installed Python 3.12+ executable for the same commands.

Open `http://127.0.0.1:8000/` for Chore and `http://127.0.0.1:8000/admin/` for configuration. The seed creates one shared Program authentication account:

- `sonderhouse`, displayed as **Sonder House Operations**, with operational-entry access

Passwords are never stored in the repository. The seed reads `CHORE_OPERATIONAL_PASSWORD`; if absent when the account is first created, the account receives an unusable password. Set the variable and rerun with `--reset-passwords` to enable sign-in. The active shared operational account is exempt from Django's composition, similarity, common-password, and minimum-length checks so its password can be communicated to the team; it is still hashed and authenticated normally. Administrators, Managers, staff/admin users, and accounts with Manager access retain the normal validators.

The development seed is repeatable. Seed-managed staff, shifts, definitions, sections, and tasks have immutable seed keys, so rerunning restores the canonical configuration instead of creating replacements. The command does not create or modify personal Administrator/Manager accounts.

## Seeded configuration

- Front Desk: Morning, Evening, and Night
- Support: Morning and Evening
- Awake Night: Night
- Life Skills: Morning

Morning, Evening, and Night retain 07:00–15:00, 15:00–23:00, and 23:00–07:00 as secondary boundary metadata. Life Skills source-document time blocks do not define shifts; Life Skills uses Morning. The active seed contains 40 Life Skills tasks and 190 tasks overall. Tasks that are conditional or explicitly say “as needed” are selectively configured to allow N/A.

For a shift that crosses midnight, the operational date is the date on which the shift starts. For example, staff working the 23:00–07:00 shift after midnight select the previous calendar date.

## Domain design

Authentication answers who may enter Chore. `ProgramMembership` grants either operational-entry or Manager reporting access. `StaffMember` is a separate Program-owned operational roster record with no username, password, or permanent position assignment. Staff select their name for each checklist session; inactive roster records disappear from new-work selection while historical attribution remains.

The first request for a valid date/category/shift lazily creates a `ChecklistInstance`. Its database uniqueness constraint prevents duplicates. SQLite atomic writes begin in `IMMEDIATE` mode and retry transient lock errors within a small bound, so concurrent creation and state changes serialize safely in the Phase 1 deployment. In the same transaction, `ChecklistItem` rows snapshot the category, shift, section, task label, ordering, N/A permission, weekday, and schedule times. Later configuration edits therefore do not rewrite historical operational meaning. A definition's category and shift become immutable after its first operational checklist, and each instance validates that its definition/category/shift identity agrees.

Every meaningful item state change updates the current state and appends a `StaffContribution` in one transaction. Contributions reference the selected operational `StaffMember`; the authenticated account is retained separately as optional audit metadata. The service rechecks operational authorization, selected Staff status and Program, active configuration, and per-task N/A permission.

## Administration

The Django admin keeps Authentication Users separate from Operational Staff. Administrators can add, rename, deactivate, and reactivate roster records; delete is disabled so history is preserved. It also manages Program memberships, categories, shifts, definitions, tasks, and delivery records.

## Verification

```powershell
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test
python -m compileall chore checklists
git diff --check main..HEAD
```

## Reporting and operations

Phase 2 adds Program-scoped Manager reporting without changing shared-checklist ownership. Configure Manager `ProgramMembership` records in Django admin; a Manager can report only on authorized Programs and receives scheduled email only when **Receive scheduled reports** is enabled. Report Staff filters and attribution use operational `StaffMember` records.

The report UI is available at `/reports/` and uses one Date control for daily, Monday–Sunday weekly, containing-month, containing-calendar-year, and rolling-365-day periods. It includes Position, valid Shift, Staff, Completion, and Section filtering, plus CSV and print/browser-PDF output. Task wording is normalized at display time, preserving stored configuration and historical snapshots. Web reports are live; sent email delivery rows preserve their generated HTML and JSON snapshot.

Generate deterministic development reporting history with the real roster, without creating authentication accounts:

```powershell
python manage.py generate_mock_data --days 365
python manage.py generate_mock_data --clear
```

Cleanup removes only ChecklistInstances explicitly marked as mock-generated.

Invoke the scheduler command from the eventual host scheduler at or after 08:00 local time. It determines which daily/weekly/monthly/annual periods are due and uses unique delivery records to avoid repeat sends:

```powershell
python manage.py send_scheduled_reports
```

Email uses Django settings backed by `DJANGO_EMAIL_BACKEND`, `DJANGO_EMAIL_HOST`, `DJANGO_EMAIL_PORT`, `DJANGO_EMAIL_HOST_USER`, `DJANGO_EMAIL_HOST_PASSWORD`, `DJANGO_EMAIL_USE_TLS`, and `DJANGO_DEFAULT_FROM_EMAIL`. No SMTP credential is stored in the repository.

Purge only operational checklists strictly older than the seven-calendar-year boundary with:

```powershell
python manage.py purge_operational_data --dry-run
python manage.py purge_operational_data
```

Configuration, inactive identities, and sent report snapshots are not removed by retention. Phase 2 intentionally does not include realtime synchronization, advanced analytics, staff scoring, server-side PDF rendering, infrastructure queues, or deployment automation.
