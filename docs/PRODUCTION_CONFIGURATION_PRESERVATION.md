# Production configuration preservation report

## Source and limitation

The private fixture `chore-production-config-20261016T053115Z.json` was validated against
the supplied SHA-256 value `9f14ef2cc6e0d2323ea7d62e8d6e8fb99d98b3eb4454004ae5c26627a410d99e`.
It is reference input only and is not committed or imported into production. The running
production image digest/revision was not available from this development environment, so
runtime source parity cannot be independently proven from the configuration fixture.

## Export inventory

- 1 Program
- 4 Staff Categories
- 3 Shifts
- 7 active Checklist Definitions (the request narrative says six genuine checklists;
  the fixture itself contains seven, including Life Skills Morning)
- 33 Checklist Sections
- 118 Task Definitions, including 38 allowing N/A and 2 inactive tasks
- 43 Assignment/Section Memberships
- 171 Section/Task Memberships
- 0 scheduled task windows in this snapshot

The export deliberately contains configuration rather than users, credentials, staff
identities, operational checklist history, or delivery history.

## Differences from repository development seed

The current repository seed creates 115 tasks. Production contains all 115 seed tasks plus
three records that the current seed does not recreate: inactive historical tasks
`Print needed forms` and `Greet / buzz in tenants`, and the active Admin-created task
`Kitchen Prep` (no seed key). `Kitchen Prep` is placed at order 30 in **Other Cleaning /
Closing Tasks**; the following `Make coffee at 05:00 AM` is consequently order 40 rather
than the seed's order 30.

Production also differs from seed defaults in ways that must be preserved:

- N/A is enabled in production for `Empty garbage in office and Med Room`,
  `Sweep / mop / vacuum front entrance and lobby`, `Sweep / mop office and Med Room`, and
  `Vacuum Common Room carpet`; the repository seed currently sets those four to false.
- Production has 23 task-order differences across reusable **Tenant / Site** and **Office**
  sections. These are deliberate 10-point order shifts involving visitor sign-in/sign-out,
  messages/mail, pharmacy deliveries, WISH documentation, camera monitoring, wellness
  checks, printer paper, Facebook, Ban Book review, phone/intercom, canteen, and coin tasks.
- The stable Checklist Definition and Checklist Section sets match the seed. All 43
  assignment/section relationships and their ordering match. Production has one additional
  section/task relationship: the Admin-created `Kitchen Prep` placement described above.

No migration or startup path in this change invokes `seed_development`, so these production
differences are not reconciled back to obsolete repository defaults.

## Reviewed multi-staff mapping

| Export task ID | Stable key suffix | Exact label | Applicable position/shift relationships | Allow N/A | New count |
| --- | --- | --- | --- | --- | --- |
| 2 | `0ea65e10607f1b3945a0` | Review IR binder, Communication Book, WISH, Inclusion, schedule, and DNA list | Front Desk Morning/Evening/Night; Support Morning/Evening; Awake Night Night | No | 2 |
| 39 | `03a8dcc33316f2ca4e41` | Review Heat Hut | Support Morning/Evening | Yes | 2 |
| 3 | `ca72368919b93134d763` | Check and reply to emails | Front Desk Morning/Evening/Night; Support Morning/Evening; Awake Night Night; Life Skills Morning | No | 2 |
| 4 | `08179a09b70ca6748b49` | Complete leave requests and maintenance requests | Front Desk Morning/Evening/Night; Support Morning/Evening | Yes | 2 |
| 71 | `e07e816fcb3b44ca0aad` | Submit leave requests | Awake Night Night | Yes | 2 |
| 5 | `856c28710c15e5859166` | Complete Citation training | Front Desk Morning/Evening/Night; Support Morning/Evening; Awake Night Night; Life Skills Morning | Yes | 2 |

No mapping is ambiguous. “Assist tenants with Heat Hut and label items” (task 47) is a
different task and is intentionally excluded. Activation matches both the complete stable
key and exact label, refuses partial/ambiguous seeded configurations, and changes only
`required_staff_completions`.

## Migration safety

The schema changes are additive. Existing definitions receive a default required count of
one; existing checklist items receive a snapshot count of one, so historical completion
meaning does not change. Existing completed checklist items are backfilled with one active
completion using their existing `current_staff` and timestamp while append-only Task Entry
history remains untouched. The six reviewed current definitions then receive count two;
old checklist snapshots are not updated.

Migration regression tests compare task labels, N/A permissions, completion-note flags,
active states, schedules, section and task ordering, memberships, and assignment/category/
shift relationships before and after the activation migration. Only the six reviewed new
count values may differ. No seed command runs during migration or container startup.

The supplied 380-object fixture was also loaded into a disposable database at migration
0009 and migrated through 0011. Normalized before/after values for all fields and
relationships listed above were identical. The result retained 118 tasks, 38 N/A flags,
33 sections, 171 section/task memberships, and 43 assignment/section memberships; exactly
six reviewed tasks became two-completion definitions and the other 112 remained at one.
This is representative local verification, not a claim that the migration has run on the
live production database.

