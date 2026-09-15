# SEO Portal — Backend

Django + Django REST Framework + PostgreSQL. See `../README.md` for how this
fits into the overall migration from the old Next.js/Supabase app.

## Structure

```
backend/
├── config/
│   ├── settings/
│   │   ├── base.py      # shared settings
│   │   ├── dev.py       # local dev overrides (insecure cookies allowed over http)
│   │   └── prod.py      # production overrides (HTTPS, HSTS, secure cookies)
│   ├── celery.py         # Celery app (not yet used by any task — see Phase 4)
│   └── urls.py
├── api/v1/urls.py         # the one place every app's URLs get wired in
├── apps/
│   ├── accounts/          # custom User model, register/login/refresh/logout/me
│   └── businesses/        # Account, Membership, Business — the tenant model
└── common/
    ├── permissions.py     # IsAccountMember + accounts_for_user() — tenant isolation, in one place
    ├── pagination.py
    └── exceptions.py      # the {error: {code, message, details}} envelope every error uses
```

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Copy `.env` (already present, pointed at the same Supabase Postgres instance
`../src` uses — see the comment in that file for why) and run migrations:

```bash
python manage.py migrate
python manage.py createsuperuser   # optional, for /admin/
python manage.py runserver 8000
```

## Authentication model

- `POST /api/v1/auth/register/` — creates a `User`, an `Account` they own, and
  an owner `Membership`. Returns `{ access, user }`; sets the refresh token as
  an `httpOnly` cookie (never in the response body).
- `POST /api/v1/auth/login/`
- `POST /api/v1/auth/refresh/` — reads the refresh cookie, rotates it
  (blacklists the old one, issues a new one), returns a new access token.
- `POST /api/v1/auth/logout/` — blacklists the current refresh token.
- `GET /api/v1/auth/me/` — current user + the accounts they belong to (with role).

The access token is meant to live in the frontend's memory only. Never put it
in localStorage — see `frontend/src/api/apiClient.ts` for why.

## Tenant isolation

Every model beyond `User`/`Account` itself hangs off `Business.account` (or
directly off `Account`). Two mechanisms enforce isolation, deliberately
redundant:

1. `get_queryset()` on every ViewSet filters to `accounts_for_user(request.user)`
   — a user can't even **list** another account's rows.
2. `IsAccountMember` (object-level permission) — a direct-by-id request for a
   row outside the user's accounts 404s, rather than 403ing (a 403 would
   confirm the row exists at all).

See `apps/businesses/tests.py::TestTenantIsolation` for the tests that pin
this behavior down.

## Adding a new app (Phase 4 pattern)

1. `python manage.py startapp <name> apps/<name>`
2. Fix `apps/<name>/apps.py`: `name = "apps.<name>"`, add a `label`.
3. Add `"apps.<name>"` to `INSTALLED_APPS` in `config/settings/base.py`.
4. Write models scoped to `Business` (not directly to `User`).
5. `path("", include("apps.<name>.urls"))` in `api/v1/urls.py`.
6. If it talks to an external API (Google, WordPress, ...), put that client
   in `integrations/<name>/` with **no Django imports** — see the existing
   `src/lib/google/search-console.ts` in the old app for the contract to port;
   it's already framework-agnostic.

## Tests

```bash
python -m pytest apps/ -v
```

Uses a real (temporary) Postgres test database on the same server as
`DATABASE_URL` — Django creates and migrates it automatically. `--reuse-db`
(see `pytest.ini`) keeps it around between runs for speed; run
`python manage.py test --keepdb=False` or drop `test_postgres` manually if
you need a truly clean slate.
