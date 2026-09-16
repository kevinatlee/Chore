# Chore

Chore is a shared operational checklist application for Sonder House. It provides secure staff sign-in, category and shift configuration, lazily created daily checklists, per-task Pending/Completed/N/A state, append-only Staff Contribution history, and Program-scoped operational reporting.

The important identity rule is enforced in the database: one operational checklist exists for each date, staff category, and shift. Staff do not submit individual copies. Everyone assigned to the same category works on the same checklist and sees the persisted state on reload.

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
$env:CHORE_TEST_STAFF_PASSWORD = "choose-a-local-password"
$env:CHORE_ADMIN_PASSWORD = "choose-a-different-local-password"
python manage.py seed_development --reset-passwords
python manage.py runserver
```

If Python is not on `PATH`, use an installed Python 3.12+ executable for the same commands.

Open `http://127.0.0.1:8000/` for the staff workflow and `http://127.0.0.1:8000/admin/` for configuration. The seed creates these development identities:

- `teststaff`, displayed as **Test Staff**, with normal staff access
- `choreadmin`, displayed as **Development Admin**, with Admin/Manager access

Passwords are never stored in the repository. The seed reads `CHORE_TEST_STAFF_PASSWORD` and `CHORE_ADMIN_PASSWORD`. If either variable is absent when an account is first created, that account receives an unusable password. Set the variable and rerun with `--reset-passwords` to enable sign-in.

The development seed is repeatable. Seed-managed shifts, definitions, sections, and tasks have immutable seed keys, so rerunning the command restores the canonical source-backed fields instead of creating replacement records after an administrator edits a name, time, or order. Retired source tasks are deactivated rather than deleted. Because rerunning intentionally restores seed-managed configuration, review local customizations before doing so. The command does not create Kevin Atlee as a user and does not reset an existing usable password unless `--reset-passwords` is supplied with the corresponding environment variable.

## Seeded configuration

- Front Desk: 07:00–15:00, 15:00–23:00, and 23:00–07:00
- Support: 07:00–15:00 and 15:00–23:00
- Awake Night: 23:00–07:00 as its own category
- Life Skills: eight active weekday schedule slots from 08:00 through 15:00 under an 08:00–15:00 operational shift

Front Desk, Support, and Awake Night retain the source sections and wording from the supplied Word checklists. The revised Life Skills source intentionally leaves four former rows blank; those tasks are not seeded. Its 14:45–15:45 row is represented only for the in-scope 14:45–15:00 portion, and the later row is excluded. The resulting active seed contains 40 Life Skills tasks and 190 tasks overall. Tasks that are conditional or explicitly say “as needed” are selectively configured to allow N/A; N/A is not enabled globally.

For a shift that crosses midnight, the operational date is the date on which the shift starts. For example, staff working the 23:00–07:00 shift after midnight select the previous calendar date.

## Domain design

Configuration records (`StaffCategory`, `Shift`, `ChecklistDefinition`, `ChecklistSection`, and `TaskDefinition`) are active/inactive rather than automatically deleted. `StaffAssignment` determines which categories a staff account can access; Django's `is_staff` flag represents the Admin/Manager role.

The first request for a valid date/category/shift lazily creates a `ChecklistInstance`. Its database uniqueness constraint prevents duplicates. SQLite atomic writes begin in `IMMEDIATE` mode and retry transient lock errors within a small bound, so concurrent creation and state changes serialize safely in the Phase 1 deployment. In the same transaction, `ChecklistItem` rows snapshot the category, shift, section, task label, ordering, N/A permission, weekday, and schedule times. Later configuration edits therefore do not rewrite historical operational meaning. A definition's category and shift become immutable after its first operational checklist, and each instance validates that its definition/category/shift identity agrees.

Every meaningful item state change updates the current state and appends a `StaffContribution` in one transaction. Contributions record the actor, prior state, new state, and timestamp. User foreign keys are protected, so deactivating an account preserves its attribution. The state-change service rechecks active staff assignment and all relevant active configuration inside the write transaction. The UI and service layer both enforce per-task N/A permission, with a database check as a final integrity guard.

## Administration

The Django admin can manage users and their active status, Admin/Manager access, staff category assignments, categories, shifts, checklist definitions, sections, tasks, ordering, active flags, weekday/time metadata, and per-task N/A permission. Operational checklist snapshots and Staff Contributions are inspectable but read-only through the admin.

## Verification

```powershell
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test
python -m compileall chore checklists
git diff --check main..HEAD
```

## Reporting and operations

Phase 2 adds Program-scoped Manager reporting without changing shared-checklist ownership. Configure `ProgramMembership` records in Django admin: a Manager can report only on their active Programs, and receives scheduled email only when **Receive scheduled reports** is enabled. Test Staff is marked on its Program membership; its contribution history remains inspectable by an admin but is replayed out of production calculations and scheduled reports.

The report UI is available at `/reports/` and supports daily, Monday–Sunday weekly, calendar-month, calendar-year, and rolling-365-day periods. It includes category, valid-shift, contribution, checklist-state, task-state, and section filtering, plus CSV and print/browser-PDF output. Web reports are live; sent email delivery rows preserve their generated HTML and JSON snapshot.

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
