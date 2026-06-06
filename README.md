# Asterisk PBX Config Portal

Foundation Django + HTMX portal for LAN/WARP-only PBX configuration workflows.

## Local Docker Stack

```bash
docker compose up --build
```

The web app listens on `http://localhost:8000`. PostgreSQL is provided by the `db` service and wired through `DATABASE_URL`.
Production access should remain limited to trusted LAN or WARP client networks.

## Environment

Copy `.env.example` to `.env` for local overrides.

Key settings:

- `DATABASE_URL`: PostgreSQL connection string for Django.
- `DJANGO_ALLOW_SQLITE_FALLBACK`: allow SQLite when `DATABASE_URL` is unset. Keep false outside local development/tests.
- `DJANGO_SECRET_KEY`: Django secret key.
- `DJANGO_ALLOWED_HOSTS`: comma-separated hostnames.
- `DJANGO_CSRF_TRUSTED_ORIGINS`: comma-separated trusted origins with scheme.
- `PORTAL_ENFORCE_CLIENT_CIDR`: when true, restricts requests to configured LAN/WARP CIDRs.
- `PORTAL_ALLOWED_CLIENT_CIDRS`: comma-separated CIDR list for client IP allowlisting.
- `PBX_AGENT_PORTAL_URL`: WARP-reachable portal URL used in exported PBX agent WebSocket config.
- `PBX_ACTIVE_CONFIG_MARKER`: deployed marker path the PBX agent reads for active version reporting.
- `PBX_DEPLOYMENT_ALLOWED_ROOTS`: comma-separated absolute remote path prefixes allowed for SSH deployment paths.
- `PBX_RUNTIME_IMAGE_TAG_POLICY`: `warn` allows tag-only custom PBX runtime images with export warnings; `block` requires immutable digests before export.

## Routes

- `/` portal overview
- `/extensions/`
- `/trunks/`
- `/dial-plan/`
- `/settings/` Admin backups and portal settings
- `/health/`

## Admin Backups

Admins can generate and download full portal backups from `/settings/`.
The ZIP archive includes database data, uploaded media/audio, export metadata, generated configuration data, and audit logs.
PostgreSQL deployments use `pg_dump` when the command is available; local SQLite/test runs use a Django fixture dump fallback.

Downloaded backups are suitable for off-host storage.
Treat each archive as sensitive because PostgreSQL records, generated configs, and exported ZIPs can contain plaintext SIP, AMI, SMTP, deployment SSH, and PBX agent secrets.
Retained copies should be stored outside the application host on encrypted storage with administrator-only access.

## Security and Operations Checklist

- Keep portal access restricted to LAN/WARP client CIDRs with `PORTAL_ENFORCE_CLIENT_CIDR=true` in production.
- Store local SQLite databases, uploaded media, generated backups, exported ZIPs, expanded export directories, SSH private keys, and deployment staging directories with owner-only permissions where the platform supports POSIX modes.
- Review deployment hosts for `0700` staging directories and owner-only generated config files before enabling live deploy/rollback operations.
- Keep SSH deployment paths under the configured `PBX_DEPLOYMENT_ALLOWED_ROOTS`; dangerous broad roots such as `/` and `/tmp` are refused before deployment commands run.
- PBX agent WebSocket authentication uses headers or bearer authorization. Do not place agent tokens or secrets in URLs.
- HTMX is vendored under `static/js/htmx.min.js`; avoid reintroducing runtime CDN dependencies for the internal portal shell.
- Treat PostgreSQL dumps and all exported configuration archives as plaintext telecom secret material.
- Validate emergency calling routes, emergency caller ID, and upstream trunk behavior for each location before deployment.
- Confirm call recording consent, retention, and playback access obligations for the deployment jurisdiction before enabling recording policies.

## Validation

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python manage.py test
python manage.py makemigrations --check --dry-run
```
