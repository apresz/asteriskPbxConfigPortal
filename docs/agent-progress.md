# Agent Progress

## Current Progress

- Task: Fix acceptance audit findings B4, I2-I9, selected test gaps, and security/operations risks
- Date: 2026-06-06
- Status: completed

## Summary

- Hardened config archive authority so only admins can create, download, deploy, and rollback generated PBX config archives.
- Added a real emergency trunk FK, extension-DID outbound caller ID dialplan lookup, provider static-IP ACL generation, local HTMX asset, bundled recording retention script, deployment root validation, header-only PBX agent WebSocket credentials, and production-safe SQLite fallback behavior.
- Expanded tests for permissions, generated dialplan, provider ACLs, retention script export/deployment, agent credential handling, local HTMX asset use, location creation authority, deployment path safety, and golden export/runtime bundles.

## Files Changed

- `core/models.py`, `core/forms.py`, `core/migrations/0019_location_emergency_trunk_ref.py`: add and validate the linked emergency trunk record.
- `core/views.py`, `templates/core/partials/location_list.html`, `templates/core/partials/location_detail.html`: enforce admin-only config archive/deploy actions and update UI flags/display.
- `core/config_export.py`, `core/config_archive.py`, `core/file_permissions.py`, `core/deployments.py`: fix extension-DID caller ID generation, provider ACLs, retention script bundling, executable archive modes, script deployment, and deployment root validation.
- `core/agent_ws.py`: remove query-string agent credential support.
- `templates/base.html`, `static/js/htmx.min.js`: vendor HTMX locally.
- `portal/settings.py`, `.env.example`, `README.md`: document and configure SQLite fallback and deployment allowed roots.
- `core/tests.py`, `core/testdata/*`: add regression coverage and update golden files.

## Verification

- `python manage.py test`: passed - 267 tests.
- `python manage.py makemigrations --check --dry-run`: passed - no changes detected.

## Assumptions and Follow-ups

- The requested "fork" was implemented as a local branch: `codex/fix-acceptance-gaps`.
- Real external acceptance tests that require physical phones, live provider trunks, real SSH targets, or Docker runtime boot remain represented by safe local harness/golden tests in this repository.

## Change Log

- 2026-06-06: Fixed requested acceptance gaps and added regression coverage; full Django test suite passed and migration drift check showed no changes.
