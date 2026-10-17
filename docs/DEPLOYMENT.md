# Chore production deployment

Production and ChoreTest are Unraid template-managed Docker containers. Unraid does not
need or maintain a Git checkout, and deployment does not use Docker Compose. GitHub
Actions builds the same Dockerfile for both environments; the Git branch determines the
published GHCR tag. Production and test use separate writable appdata directories and
separate template-configured host ports.

## Layout

| Deployment | Image | Container | Writable appdata |
| --- | --- | --- | --- |
| Production | `ghcr.io/kevinatlee/chore:latest` | `Chore` | `/mnt/user/appdata/Chore/` |
| Test | `ghcr.io/kevinatlee/chore:test` | `ChoreTest` | `/mnt/user/appdata/ChoreTest/` |

Production stores its database at `/mnt/user/appdata/Chore/db.sqlite3` and daily backups
under `/mnt/user/appdata/Chore/backups/`. ChoreTest stores its independent database at
`/mnt/user/appdata/ChoreTest/db.sqlite3`. Its read-only `/production-backups` container
mount is the only production data it can see.

The container runs as UID/GID 1000. Ensure both appdata directories are writable by that
identity before starting the containers.

## Initial preparation

Configure both containers through Unraid's Docker templates. The templates pull their
images directly from GHCR; no repository checkout or Compose file belongs on the server.
If GHCR package access is private, configure registry credentials in Unraid before pulling
the images.

Create or select the persistent appdata paths in the templates:

- Chore: `/mnt/user/appdata/Chore` mounted read/write at `/app/data`.
- ChoreTest: `/mnt/user/appdata/ChoreTest` mounted read/write at `/app/data`.
- ChoreTest production backup source: `/mnt/user/appdata/Chore/backups` mounted read-only
  at `/production-backups`.

The container runs as UID/GID 1000, so those paths must be writable by that identity.
Supply environment variables through the Unraid template and use different long random
`DJANGO_SECRET_KEY` values for production and test.

For the existing deployment, retain the current production appdata directory. A completely
new empty installation migrates an empty database automatically but still needs the
authoritative configuration created once. Only for that fresh-install case, open the Chore
container console in Unraid and run `python manage.py seed_development`; this is not a
normal update step.

## Unraid user template

The versioned production template source is `unraid/my-Chore.xml`, and its published raw
URL is recorded in the template metadata. Import or maintain the template through Unraid;
do not clone the repository onto the server.

In the Unraid interface, open **Docker → Add Container** and select the Chore user
template. Its defaults create `Chore` on the bridge network, map host port `4523` to
container port `8000`, persist `/mnt/user/appdata/Chore` at `/app/data`, use the
`ghcr.io/kevinatlee/chore:latest` image, restart unless stopped, and rotate three 10 MB
Docker JSON log files. Supply `APP_FQDN`, a long random `DJANGO_SECRET_KEY`, Gmail SMTP
values, and `DEFAULT_FROM_EMAIL` before applying the template.

The template's WebUI button opens the canonical production URL, `https://chore.cc`, and
its versioned icon is `assets/chore-icon.png`. Production access remains HTTPS through
Cloudflare Tunnel; port `4523` is the tunnel origin, not a direct application WebUI. Do
not disable HTTPS redirects or secure cookies to make `http://UNRAID-IP:4523` an
application access path.

## Required environment

Production Unraid template variables:

- `APP_FQDN`: production hostname only, without `https://`, a path, or a port.
- `DJANGO_DEBUG=false`.
- `DJANGO_SECRET_KEY`: long random secret, supplied only at deployment time.
- `EMAIL_ENABLED=true`.
- `EMAIL_HOST=smtp.gmail.com`.
- `EMAIL_PORT=587`.
- `EMAIL_USE_TLS=true`.
- `EMAIL_HOST_USER`: Gmail address used to authenticate.
- `EMAIL_HOST_PASSWORD`: Gmail App Password, not the normal account password.
- `DEFAULT_FROM_EMAIL`: visible sender address.

ChoreTest Unraid template variables:

- `APP_FQDN`: separate test hostname.
- `DJANGO_DEBUG=false`.
- `DJANGO_SECRET_KEY`: a separate long random secret.
- `EMAIL_ENABLED=false`.

`EMAIL_ENABLED=false` selects a suppressing Django email backend and the container also
refuses to start ChoreTest if the value is not false. Do not add SMTP credentials to the
ChoreTest template.

`APP_FQDN` automatically configures Django's allowed host and `https://` CSRF trusted
origin. Optional comma-separated `DJANGO_ALLOWED_HOSTS` and
`DJANGO_CSRF_TRUSTED_ORIGINS` values add entries; they do not replace the FQDN. Production
secure cookies and HTTPS redirects are enabled by default. `DJANGO_SECURE_HSTS_SECONDS`
is intentionally explicit: use `0` while first validating the hostname and TLS route,
then use the example's `31536000` only when the hostname is permanently HTTPS-only.

The application timezone and container timezone are fixed to `America/Vancouver`. They
are not user-configurable.

## Deploy and update through Unraid

For a normal production update:

1. Merge the approved code to `main`.
2. Confirm the GitHub Actions build for `ghcr.io/kevinatlee/chore:latest` completed
   successfully.
3. In Unraid, use **Force Update** on the Chore container.
4. Confirm the container becomes healthy and review its logs.

Force Update pulls the new image and recreates the container from the saved template.
Persistent `/mnt/user/appdata/Chore/db.sqlite3` is retained; no database reset is required.

For ChoreTest staging:

1. Advance the `test` branch to the desired commit.
2. Confirm the GitHub Actions build for `ghcr.io/kevinatlee/chore:test` completed
   successfully.
3. In Unraid, use **Force Update** on ChoreTest.
4. Confirm the container becomes healthy and review its logs.

At production startup, the entrypoint backs up the existing database when one is present,
runs `python manage.py migrate --noinput`, collects static files, and starts the production
scheduler plus Gunicorn. On a brand-new empty appdata directory, migrations create the
database and the scheduler creates the first valid backup after startup.

At ChoreTest startup, the entrypoint uses the existing persistent
`/mnt/user/appdata/ChoreTest/db.sqlite3`, applies candidate migrations, collects static
files, and starts Gunicorn. An ordinary restart or Force Update never replaces the test
database. Automatic restoration was removed because container restarts must not silently
discard test-only data.

## Production-safe configuration updates

Configuration revisions are carried forward by normal Django migrations. Do not reset or
replace the production database: historical Chore Lists, Task Entries, programming notes,
Comments for Incomplete Tasks, and report deliveries remain intact. Life Skills uses one
reusable Morning task list, while the Shift Assignment is available Monday–Friday only;
weekend Life Skills history remains stored but is excluded from normal reporting.

Normal Force Update startup creates a safe backup and applies migrations without deleting
the database. Migration 0009 performs the active configuration corrections in place, so
`seed_development` is not required after a normal container update. Use that command only
for a new empty installation or an intentional administrative reconciliation. Use
`--reset-passwords` only when intentionally applying the configured operational password.

For this release, migrations 0010–0011 add feedback and per-staff completion storage,
snapshot the completion requirement, backfill existing completed items, and update only
six exact stable-key/label task matches to require two staff. Audit the reviewed mapping
before or after staging deployment with:

```sh
docker exec ChoreTest python manage.py configure_multistaff_tasks
```

The command is read-only unless `--apply` is supplied, reports every exact match, and
refuses a partial or ambiguous mapping. Normal migration already applies the reviewed
values; `--apply` is only an idempotent recovery/verification option. Never run
`seed_development` to apply this production update.

Before a future production deployment, verify a current daily backup exists and copy it to
an additional immutable/pre-upgrade location. Review `python manage.py migrate --plan` on
ChoreTest first. Database rollback means stopping Chore, restoring that verified
pre-upgrade SQLite snapshot (including safe WAL/SHM handling below), and starting the
matching prior image digest. Do not reverse these migrations against the live database:
reversing 0010 would discard new feedback/completion tables and columns.

## Cloudflare Tunnel

Create two public hostnames in Cloudflare Tunnel, each targeting the host port configured
in its Unraid template. The production template currently defaults to host port `4523` for
container port `8000`; ChoreTest uses its separately configured host port.

The external connection must be HTTPS. Preserve the public Host header (or explicitly set
the HTTP Host header to the corresponding `APP_FQDN`) and pass
`X-Forwarded-Proto: https`. The application trusts that standard proxy header, keeps CSRF
and host validation enabled, and issues secure cookies. Do not configure Cloudflare to
rewrite both services to the same hostname.

## Gmail SMTP

Enable two-step verification on the production Google account, create an App Password,
and place that App Password only in production's `EMAIL_HOST_PASSWORD`. Restart `Chore`
after changing email configuration. The internal production scheduler invokes the
idempotent Manager-report command at 08:00 Vancouver time. Only active Managers with
**Receive scheduled reports** enabled and a non-empty email address are recipients.

## Backups and explicit ChoreTest refresh

The production scheduler runs at 02:00 and 08:00 Vancouver time. The backup command is
idempotent and creates at most one `chore-YYYY-MM-DD.sqlite3` file per local day using
Python's SQLite online backup API. Every completed snapshot is integrity-checked and
atomically installed. Only the newest seven dated application backups are retained.

Refreshing ChoreTest is a deliberate stopped-container operation. The helper refuses to
run unless deployment mode is `test`, email is disabled, the operator confirms
`ChoreTest`, and `--application-stopped` is supplied. It validates the source with SQLite
`quick_check`, creates a timestamped online recovery snapshot under
`/mnt/user/appdata/ChoreTest/refresh-backups/`, removes stale WAL/SHM/journal sidecars,
copies through a temporary file, preserves destination mode and ownership where the
platform permits, atomically replaces the database, and validates the result. The source
backup is opened read-only and is never changed.

Use the newest valid dated production backup:

```sh
docker stop ChoreTest
docker run --rm --name ChoreTestRefresh \
  --user 1000:1000 \
  -e CHORE_DEPLOYMENT_MODE=test -e EMAIL_ENABLED=false \
  -v /mnt/user/appdata/ChoreTest:/app/data \
  -v /mnt/user/appdata/Chore/backups:/production-backups:ro \
  --entrypoint python ghcr.io/kevinatlee/chore:test \
  -m docker.refresh_test_database --confirm ChoreTest --application-stopped
docker start ChoreTest
docker exec ChoreTest python manage.py migrate --plan
docker exec ChoreTest python manage.py check
```

Select a particular dated or manually named snapshot by adding an absolute container path,
for example `--source /production-backups/chore-2026-10-15.sqlite3`. The selected file
must be inside `/production-backups` and pass validation; its filename does not need to
match the daily naming convention.

If refresh fails before atomic replacement, the existing database remains in place. If
post-replacement validation fails, the helper restores the pre-refresh recovery snapshot.
To manually restore a prior ChoreTest database, keep ChoreTest stopped and copy the chosen
validated `refresh-backups/choretest-before-refresh-*.sqlite3` to a temporary file alongside
`db.sqlite3`, set ownership to `1000:1000`, remove `db.sqlite3-wal`, `db.sqlite3-shm`, and
`db.sqlite3-journal`, then rename the temporary file atomically to `db.sqlite3`. Start
ChoreTest and verify `/health/`, `python manage.py check`, and the expected data in the UI.

Do not run the helper while ChoreTest is running or point `--database` at
`/mnt/user/appdata/Chore/db.sqlite3`. `EMAIL_ENABLED=false` remains mandatory after every
refresh, so copied production configuration cannot enable outbound test email.

## Restore production

Choose a backup from `/mnt/user/appdata/Chore/backups/`, then use the Unraid interface to
stop Chore before replacing its database. From an Unraid terminal:

```sh
cp /mnt/user/appdata/Chore/db.sqlite3 /mnt/user/appdata/Chore/db.sqlite3.before-restore
cp /mnt/user/appdata/Chore/backups/chore-YYYY-MM-DD.sqlite3 /mnt/user/appdata/Chore/db.sqlite3.restore
chown 1000:1000 /mnt/user/appdata/Chore/db.sqlite3.restore
mv /mnt/user/appdata/Chore/db.sqlite3.restore /mnt/user/appdata/Chore/db.sqlite3
```

Start Chore again from the Unraid interface. Keep `db.sqlite3.before-restore` until the
restored application has been verified. Startup applies migrations needed by the selected
image. Restarting ChoreTest afterward preserves its existing test database. Run the
explicit refresh procedure only if a new production snapshot is intentionally required.

## Health and troubleshooting

Docker checks `GET /health/` inside each container every 30 seconds with a five-second
timeout, three retries, and a 40-second startup grace period. The endpoint returns only
`{"status":"ok"}` after `SELECT 1` succeeds. Check state and bounded container logs with:

```sh
docker inspect --format '{{json .State.Health}}' Chore
docker inspect --format '{{json .State.Health}}' ChoreTest
docker logs --tail 200 Chore
docker logs --tail 200 ChoreTest
```

Common failures:

- `DisallowedHost` or CSRF rejection: verify `APP_FQDN`, the Cloudflare public hostname,
  preserved Host header, and forwarded HTTPS header agree.
- Redirect loop: Cloudflare is not sending `X-Forwarded-Proto: https`.
- ChoreTest refresh failure: confirm the container is stopped, the source exists in the
  read-only backup mount, and both source and destination pass SQLite integrity checks.
- SQLite write failure: appdata ownership is not UID/GID 1000 or the Unraid appdata share
  is not writable.
- Gmail authentication failure: use a current App Password and verify the configured
  Gmail username. Secrets are never printed intentionally.

Docker's `json-file` logs rotate at 10 MB with three files. No external monitoring or
auto-healing service is installed.

## Image updates and rollback

Pushes to `main` publish `ghcr.io/kevinatlee/chore:latest`. Pushes to the dedicated `test`
branch publish `ghcr.io/kevinatlee/chore:test`. GitHub Actions uses the repository's
`GITHUB_TOKEN`; no registry password is committed. After the appropriate workflow succeeds,
the operator deploys that tag with **Force Update** on the corresponding Unraid container.

Promote and verify a candidate on ChoreTest before merging that commit to `main`. Record
the tested image digest before promotion. For an application-image rollback, edit the
Unraid template repository field to the known-good immutable digest and apply/Force Update
the container. If the rollback image cannot use the current schema, first follow the
stopped-container database restore procedure with the matching pre-upgrade backup. Never
run reverse migrations against the live database without a verified backup.

Remaining manual operations are Cloudflare hostname creation, Gmail App Password creation,
Unraid template variable provisioning, appdata permissions, GHCR access when private,
candidate promotion, Force Update, and image/database rollback selection.
