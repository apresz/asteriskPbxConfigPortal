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
- `/settings/`
- `/health/`

## Validation

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python manage.py test
python manage.py makemigrations --check --dry-run
```
