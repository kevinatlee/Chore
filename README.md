# Chore

Chore is a shared operational checklist application for Sonder House. It separates authenticated application accounts from the operational staff roster, provides lazily created shared checklists, records per-task Pending/Completed/N/A state and Staff Contribution history, and supplies Program-scoped operational reporting.

The important identity rule is enforced in the database: one operational checklist exists for each Program, date, position, and shift. Selecting another staff name changes attribution, never checklist identity.

## Technology

- Python 3.12+
- Django 5.2 LTS
- SQLite for local development and the production deployment
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

Phase 3 adds plain-language field labels, section descriptions, active/inactive guidance, report-routing explanations, and focused confirmation prompts for deactivation and removal of future N/A eligibility. Deactivation prevents future operational use while preserving historical Chore Lists, snapshots, Staff Contributions, and sent-report records. Destructive bulk deletion is unavailable on the clarified configuration screens; Django's protected relationships and confirmation page continue to guard individual deletion. The legacy special-case roster identity and all associated flags, filtering, seed behavior, and migration handling have been removed; operational staff now follow one uniform model.

## Shared Chore List synchronization

Staff working the same Program + Position + Shift + operational date continue to use one shared `ChecklistInstance`. The staff page checks a Program-authorized JSON state endpoint every seven seconds while the page is visible and performs an immediate check when a background tab becomes visible. When the server revision has not changed, the endpoint returns only the unchanged revision, keeping polling payloads small.

Task actions update the interface immediately, submit only the selected item state, and then replace the visible checklist state with the canonical server response. Coworker updates reconcile task state, N/A state, Staff Contribution attribution, timestamps, progress, and the recent-contribution list without a manual refresh. Polling pauses while the page is hidden and while a local mutation is in flight, so actively submitted controls are not replaced by a background refresh.

Concurrency remains deliberately small and database-backed: each mutation rechecks Program authorization, roster status, configuration identity, and task-level N/A eligibility inside the existing atomic write. The target item is locked where supported; SQLite uses `IMMEDIATE` transactions with bounded lock retries. Each meaningful state transition and its append-only Staff Contribution are committed together. Clients never submit a whole checklist snapshot, so stale browser state cannot overwrite unrelated newer task changes.

## Verification

```powershell
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test
python -m compileall chore checklists
git diff --check main..HEAD
```

## Production deployment

Phase 4 provides production and test Docker Compose deployments, GHCR `:latest` and
`:test` image publishing, runtime FQDN/proxy security configuration, global test-email
suppression, a database-aware healthcheck, and SQLite-safe daily backups. See
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for initial Unraid setup, Cloudflare Tunnel and
Gmail configuration, ChoreTest refresh behavior, updates, restores, and rollback.

## Reporting and operations

Phase 2 adds Program-scoped Manager reporting without changing shared-checklist ownership. Configure Manager `ProgramMembership` records in Django admin; a Manager can report only on authorized Programs and receives scheduled email only when **Receive scheduled reports** is enabled. Report Staff filters and attribution use operational `StaffMember` records.

The report UI is available at `/reports/` and uses one Date control for daily, Monday–Sunday weekly, containing-month, containing-calendar-year, and rolling-365-day periods. It includes Position, valid Shift, Staff, Completion, and Section filtering, plus CSV and print/browser-PDF output. Task wording is normalized at display time, preserving stored configuration and historical snapshots. Web reports are live; sent email delivery rows preserve their generated HTML and JSON snapshot.

Generate deterministic synthetic operational history for reporting and testing with the normal operational roster:

```powershell
python manage.py generate_mock_data --days 365
```

Generated history uses the normal `ChecklistInstance`, `ChecklistItem`, and `StaffContribution` models. Its contributions reference ordinary `StaffMember` roster records, and its instances are identified internally by `ChecklistInstance.is_mock_data=True`. The marker is not a staff-facing field. Mock generation creates no authentication account and does not depend on any special staff identity.

Delete only generated mock operational history with:

```powershell
python manage.py generate_mock_data --clear
```

Cleanup removes only ChecklistInstances explicitly marked as mock-generated, plus their Items and Staff Contributions. Real operational history, configuration, roster records, users, and Program memberships remain untouched.

The production container invokes the scheduler automatically at 08:00 Vancouver time. For local or manual operation, the idempotent report command determines which daily/weekly/monthly/annual periods are due and uses unique delivery records to avoid repeat sends:

```powershell
python manage.py send_scheduled_reports
```

Production email uses `EMAIL_ENABLED`, `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS`, and `DEFAULT_FROM_EMAIL`. The earlier `DJANGO_EMAIL_*` names remain accepted for compatibility. `EMAIL_ENABLED=false` globally suppresses Django email and scheduled-report delivery. No SMTP credential is stored in the repository.

Purge only real, non-mock operational checklists strictly older than the seven-calendar-year boundary with:

```powershell
python manage.py purge_operational_data --dry-run
python manage.py purge_operational_data
```

For the one-time transition from development/testing to live use on the same database, first inspect and then remove all real runtime operational history while preserving generated mock reporting history with:

```powershell
python manage.py purge_operational_data --fresh-start --dry-run
python manage.py purge_operational_data --fresh-start
```

Fresh-start mode removes all non-mock Chore List instances, their item snapshots and Staff Contributions, plus scheduled report delivery history. Generated mock Chore Lists, items, and Staff Contributions remain available for reporting; `python manage.py generate_mock_data --clear` is the explicit way to remove them. Programs, active/inactive configuration, authentication users, Program memberships, and the operational staff roster are also preserved. The purge contains no named or special-case staff cleanup logic.

Normal retention also leaves generated mock history untouched and does not remove configuration, inactive identities, or sent report snapshots. The application intentionally uses short polling rather than websocket or message-bus infrastructure. It does not add advanced analytics, staff scoring, a live Manager dashboard, server-side PDF rendering, infrastructure queues, or Phase 5 functionality.
