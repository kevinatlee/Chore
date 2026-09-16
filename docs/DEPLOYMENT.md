# Chore production deployment

This deployment keeps SQLite and the existing Chore data model. Production and test use
separate writable appdata directories and separate host ports. Both images are built from
the same Dockerfile; the Git branch determines the published tag.

## Layout

| Deployment | Image | Container | Host port | Writable appdata |
| --- | --- | --- | --- | --- |
| Production | `ghcr.io/kevinatlee/chore:latest` | `Chore` | `8000` | `/mnt/user/appdata/Chore/` |
| Test | `ghcr.io/kevinatlee/chore:test` | `ChoreTest` | `8001` | `/mnt/user/appdata/ChoreTest/` |

Production stores its database at `/mnt/user/appdata/Chore/db.sqlite3` and daily backups
under `/mnt/user/appdata/Chore/backups/`. ChoreTest stores its independent database at
`/mnt/user/appdata/ChoreTest/db.sqlite3`. Its read-only `/production-backups` container
mount is the only production data it can see.

The container runs as UID/GID 1000. Ensure both appdata directories are writable by that
identity before starting the containers.

## Initial preparation

1. Clone this repository on Unraid and sign in to GHCR if the package is private:

   ```sh
   echo 'A_GITHUB_TOKEN_WITH_READ_PACKAGES' | docker login ghcr.io -u YOUR_GITHUB_USER --password-stdin
   ```

2. Create appdata directories:

   ```sh
   mkdir -p /mnt/user/appdata/Chore/backups /mnt/user/appdata/ChoreTest
   chown -R 1000:1000 /mnt/user/appdata/Chore /mnt/user/appdata/ChoreTest
   ```

3. Preserve the intentional current demo/operational data on the first deployment by
   copying the existing repository `db.sqlite3` into production appdata before the first
   container start:

   ```sh
   cp /path/to/Chore/db.sqlite3 /mnt/user/appdata/Chore/db.sqlite3
   chown 1000:1000 /mnt/user/appdata/Chore/db.sqlite3
   ```

   Do not run a seed, purge, mock-clear, or cleanup command. Later deployments reuse this
   persistent file and apply only normal Django migrations.

4. Copy `.env.production.example` to `.env.production` and `.env.test.example` to
   `.env.test`, beside the Compose files. Both real env files are ignored by Git. Generate
   different long random `DJANGO_SECRET_KEY` values for production and test.

## Unraid user template

The versioned production user template is `unraid/my-Chore.xml`. Copy it to the Unraid
boot device, preserving the filename:

```sh
cp /path/to/Chore/unraid/my-Chore.xml \
  /boot/config/plugins/dockerMan/templates-user/my-Chore.xml
```

In the Unraid interface, open **Docker → Add Container** and select the Chore user
template. Its defaults create `Chore` on the bridge network, map host port `4523` to
container port `8000`, persist `/mnt/user/appdata/Chore` at `/app/data`, use the
`ghcr.io/kevinatlee/chore:latest` image, restart unless stopped, and rotate three 10 MB
Docker JSON log files. Supply `APP_FQDN`, a long random `DJANGO_SECRET_KEY`, Gmail SMTP
values, and `DEFAULT_FROM_EMAIL` before applying the template.

The template intentionally has no LAN HTTP WebUI link because Unraid templates cannot
substitute `APP_FQDN` into that field. Production access remains the configured
`https://APP_FQDN` through Cloudflare Tunnel. Do not disable HTTPS redirects or secure
cookies to make `http://UNRAID-IP:4523` an application access path. The existing Docker
Compose deployment below remains fully supported.

## Required environment

Production `.env.production`:

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

Test `.env.test`:

- `APP_FQDN`: separate test hostname.
- `DJANGO_DEBUG=false`.
- `DJANGO_SECRET_KEY`: a separate long random secret.
- `EMAIL_ENABLED=false`.

`EMAIL_ENABLED=false` selects a suppressing Django email backend and the container also
refuses to start ChoreTest if the value is not false. Do not add SMTP credentials to the
test env file.

`APP_FQDN` automatically configures Django's allowed host and `https://` CSRF trusted
origin. Optional comma-separated `DJANGO_ALLOWED_HOSTS` and
`DJANGO_CSRF_TRUSTED_ORIGINS` values add entries; they do not replace the FQDN. Production
secure cookies and HTTPS redirects are enabled by default. `DJANGO_SECURE_HSTS_SECONDS`
is intentionally explicit: use `0` while first validating the hostname and TLS route,
then use the example's `31536000` only when the hostname is permanently HTTPS-only.

The application timezone and container timezone are fixed to `America/Vancouver`. They
are not user-configurable.

## Start production and test

From the repository checkout:

```sh
docker compose -f compose.production.yml pull
docker compose -f compose.production.yml up -d
docker compose -f compose.test.yml pull
docker compose -f compose.test.yml up -d
```

Production startup creates or retains today's safe backup before applying migrations.
On a brand-new empty appdata directory, it migrates first and the production scheduler
creates the first backup immediately afterward. Static assets are collected on every
start and served by WhiteNoise; Gunicorn serves the application.

## Cloudflare Tunnel

Create two public hostnames in Cloudflare Tunnel:

- production FQDN to `http://UNRAID_LAN_IP:8000`
- test FQDN to `http://UNRAID_LAN_IP:8001`

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

## Backups and ChoreTest refresh

The production scheduler runs at 02:00 and 08:00 Vancouver time. The backup command is
idempotent and creates at most one `chore-YYYY-MM-DD.sqlite3` file per local day using
Python's SQLite online backup API. Every completed snapshot is integrity-checked and
atomically installed. Only the newest seven dated application backups are retained.

At every ChoreTest start or recreation:

1. Backup files are inspected newest first.
2. The newest valid SQLite/Chore backup is copied to a temporary file inside test appdata.
3. The copy is integrity-checked and atomically replaces ChoreTest's database.
4. Candidate migrations run against the test copy only.

Existing ChoreTest changes are intentionally discarded. Corrupt backup files are skipped;
startup fails clearly if no valid production backup exists. The read-only backup mount is
never modified and the live production database is not mounted into ChoreTest.

## Restore production

Choose a backup from `/mnt/user/appdata/Chore/backups/`, then stop production before
replacing its database:

```sh
docker compose -f compose.production.yml down
cp /mnt/user/appdata/Chore/db.sqlite3 /mnt/user/appdata/Chore/db.sqlite3.before-restore
cp /mnt/user/appdata/Chore/backups/chore-YYYY-MM-DD.sqlite3 /mnt/user/appdata/Chore/db.sqlite3.restore
chown 1000:1000 /mnt/user/appdata/Chore/db.sqlite3.restore
mv /mnt/user/appdata/Chore/db.sqlite3.restore /mnt/user/appdata/Chore/db.sqlite3
docker compose -f compose.production.yml up -d
```

Keep `db.sqlite3.before-restore` until the restored application has been verified. Startup
applies migrations needed by the selected image. Restarting ChoreTest afterward refreshes
it from the newest retained production backup, not directly from the just-restored live
file.

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
- ChoreTest startup failure: production has not yet produced a valid backup, or the
  read-only mount path/permissions are wrong.
- SQLite write failure: appdata ownership is not UID/GID 1000 or the Unraid appdata share
  is not writable.
- Gmail authentication failure: use a current App Password and verify the configured
  Gmail username. Secrets are never printed intentionally.

Docker's `json-file` logs rotate at 10 MB with three files. No external monitoring or
auto-healing service is installed.

## Image updates and rollback

Pushes to `main` publish `ghcr.io/kevinatlee/chore:latest`. Pushes to the dedicated `test`
branch publish `ghcr.io/kevinatlee/chore:test`. GitHub Actions uses the repository's
`GITHUB_TOKEN`; no registry password is committed. Unraid remains operator-controlled:

```sh
docker compose -f compose.test.yml pull
docker compose -f compose.test.yml up -d

docker compose -f compose.production.yml pull
docker compose -f compose.production.yml up -d
```

Promote and verify a candidate on ChoreTest before merging that commit to `main`. Record
the tested image digest (`docker image inspect`) before promotion. For an application-image
rollback, temporarily change the Compose image to that known-good immutable digest, pull,
and recreate the container. If the rollback image cannot use the current schema, first
follow the stopped-container database restore procedure with the matching pre-upgrade
backup. Never run reverse migrations against the live database without a verified backup.

Remaining manual operations are Cloudflare hostname creation, Gmail App Password creation,
real env-file provisioning, appdata permissions, initial database copy, GHCR access when
private, candidate promotion, and image/database rollback selection.
