# Asterisk PBX Config Portal

Foundation Django + HTMX portal for LAN/WARP-only PBX configuration workflows.

## Local Docker Stack

```bash
docker compose up --build
```

The web app listens on `http://localhost:8000`. PostgreSQL is provided by the `db` service and wired through `DATABASE_URL`.

## Environment

Copy `.env.example` to `.env` for local overrides.

Key settings:

- `DATABASE_URL`: PostgreSQL connection string for Django.
- `DJANGO_SECRET_KEY`: Django secret key.
- `DJANGO_ALLOWED_HOSTS`: comma-separated hostnames.
- `DJANGO_CSRF_TRUSTED_ORIGINS`: comma-separated trusted origins with scheme.
- `PORTAL_ENFORCE_CLIENT_CIDR`: when true, restricts requests to configured LAN/WARP CIDRs.
- `PORTAL_ALLOWED_CLIENT_CIDRS`: comma-separated CIDR list for client IP allowlisting.
- `PBX_AGENT_PORTAL_URL`: WARP-reachable portal URL used in exported PBX agent WebSocket config.
- `PBX_ACTIVE_CONFIG_MARKER`: deployed marker path the PBX agent reads for active version reporting.

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
Treat each archive as sensitive because it can contain PBX credentials, deployment secrets, uploaded prompts, and audit history.
Retained copies should be stored outside the application host on encrypted storage.

## Security and Operations

The portal is intended for LAN/WARP-only administration. Keep `PORTAL_ENFORCE_CLIENT_CIDR=true` in deployed environments and limit `PORTAL_ALLOWED_CLIENT_CIDRS` to trusted LAN and WARP ranges.

PostgreSQL records, SQLite files, uploaded media, admin backups, exported config ZIPs, expanded export directories, deployment staging directories, and SSH private keys can contain plaintext telecom secrets. The app writes supported local sensitive files with restrictive permissions, but operators must also restrict PostgreSQL data directories, deployment hosts, downloaded archives, and off-host storage.

Emergency calling is an administrator responsibility. Before deployment, verify emergency caller ID, emergency-capable trunks, outbound routes, provider registration, and any local test requirements.

Call recording consent, notice, and retention compliance are administrator responsibilities. Configure recording policies only after confirming the legal and operational requirements for the affected locations and users.

### Manual Security Checklist

- Confirm database data directories or SQLite files are restricted to the service account.
- Confirm `MEDIA_ROOT`, admin backup archives, exported config ZIPs, and expanded export directories are not world-readable.
- Confirm deployment SSH private keys and remote staging directories are restricted to the deployment account.
- Confirm the portal remains reachable only from trusted LAN/WARP client CIDRs.
- Confirm emergency calling and recording consent responsibilities are reviewed before live deployment.

## Validation

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python manage.py test
python manage.py makemigrations --check --dry-run
```
