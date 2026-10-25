# TaskPilot — API (Django REST Framework + PostgreSQL)

Authentication, workspaces and role-based access control, projects, issues and
epics, sprints, comments, attachments, activity history, notifications and reports.

- API reference: OpenAPI at `/api/schema/`, Swagger UI at `/api/docs/` (development;
  `API_DOCS_ENABLED=true` to expose in production)
- Authorization model: [`docs/authorization.md`](docs/authorization.md)
- Frontend contract notes: `../frontend/docs/`

## Local setup (Windows, PostgreSQL 16+)

```powershell
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# Creates the `taskpilot` role + database and writes .env with generated secrets
# (asks for the postgres superuser password; prints no secrets)
powershell -ExecutionPolicy Bypass -File scripts\setup-local-db.ps1

python manage.py migrate
python manage.py seed_dev --password <choose-a-demo-password>   # optional demo data
python manage.py runserver                                      # http://localhost:8000/api/
```

On other systems create a PostgreSQL role with `CREATEDB` (Django creates a test
database) and copy `.env.example` to `.env`.

`seed_dev` creates two demo workspaces (`silu@app.com` … `riley@app.com`, one
user per role). It refuses to run unless `DJANGO_ENV=development` with `DEBUG` on, only
touches its own demo workspaces, and does nothing if they exist (`--reset` recreates them).

## Configuration

All settings come from environment variables (`taskpilot/settings.py`). In
development they may live in `backend/.env` (gitignored); real environment variables
win. `DJANGO_ENV=production` refuses to start without a strong `DJANGO_SECRET_KEY`,
`DJANGO_ALLOWED_HOSTS`, `CORS_ALLOWED_ORIGINS`, `FRONTEND_URL`, a database password and a
`Secure` refresh cookie. See `.env.example` (development) and
`.env.production.example` (production).

## Tests and checks

```bash
python manage.py test tests                 # needs the PostgreSQL role (CREATEDB)
ruff check . --exclude .venv,*/migrations   # lint (ruff is a dev-only tool)
python manage.py makemigrations --check --dry-run
python manage.py spectacular --validate --file /dev/null   # OpenAPI schema, no warnings
DJANGO_ENV=production ... python manage.py check --deploy
```

The suite (`tests/`) covers authentication (cookie refresh, rotation, revocation,
throttling, password reset), the role matrix and project-role resolution, membership
and invitation rules, cross-workspace isolation and spoofed ids, private projects,
issue/comment/attachment/sprint workflows with activity and notifications, reports,
database constraints, and query counts for list endpoints.

## Apps

| App | Contents |
| --- | -------- |
| `accounts` | Email-based `User`; register, login, cookie refresh/logout, profile, password change/reset |
| `workspaces` | `Workspace`, `WorkspaceMembership` (role), `Invitation`, `Team`; **`roles.py`, `access.py`, `permissions.py`: the authorization core**; `seed_dev` command |
| `projects` | `Project` (visibility), `ProjectMembership` (project role) |
| `issues` | `Issue` (epics are issues), `Comment`, `Attachment`; move/rank, bulk edits, labels, search, signed downloads |
| `sprints` | `Sprint` (one active per project, velocity snapshots) |
| `activity` | `Activity`: issue history written server-side |
| `notifications` | `Notification`: assignment, mention, comment and status updates |
| `reports` | `GET /api/workspaces/:ws/reports/` aggregates |

## Deployment requirements

1. **Python 3.12**, `pip install -r requirements.txt`, served by a WSGI server (e.g.
   gunicorn / waitress) behind a TLS-terminating reverse proxy that sets
   `X-Forwarded-Proto` and `X-Forwarded-For` (set `DRF_NUM_PROXIES` to the number of proxies so
   throttling sees real client IPs).
2. **PostgreSQL** with SSL (`POSTGRES_SSLMODE=require` by default in production).
3. Environment variables from `.env.production.example`, supplied by the platform or a
   secrets manager (never a committed file).
4. On every deploy: `python manage.py migrate`, `python manage.py createcachetable`
   (throttle counters and the refresh-rotation window use a PostgreSQL cache table so
   all workers share them), `python manage.py collectstatic` (Django admin assets; serve
   `DJANGO_STATIC_ROOT` from the proxy).
5. **Attachments** are stored under `DJANGO_MEDIA_ROOT` (persistent volume, not web-served;
   downloads go through signed, 15-minute API links). Back it up with the database.
6. **SMTP** credentials (`EMAIL_*`) for password resets and invitations.
7. The frontend (`npm run build`) served as static files with an SPA fallback to
   `index.html`; same origin as `/api` is simplest, otherwise set `VITE_API_BASE_URL` at
   build time and add the app origin to `CORS_ALLOWED_ORIGINS`.
