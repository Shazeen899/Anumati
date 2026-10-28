# Anumati

An approval and compliance demonstration for SIH problem statement 26262, implemented with **React, Django, PostgreSQL, Celery and Redis**, managed with Git and GitHub. No Docker, alternative database, external AI service, or additional backend framework is used. Vite is React build tooling; psycopg is the PostgreSQL driver.

The frontend draws on the [Ministry of Education reference](https://www.education.gov.in/technical-education-9): white masthead, navy typography, pale blue panels and accessibility controls. It uses its own identity and is not an official AICTE portal.

## Run this local installation

```powershell
cd C:\Users\satvikk\OneDrive\Documents\Anumati
.\scripts\start.ps1
```

Open **http://127.0.0.1:8010**. Credentials are in **`.runtime/DEMO-CREDENTIALS.txt`**, generated locally and excluded from Git. Start with `bureau` for the overview. Do not reuse demo credentials outside the local installation.

| Account | Responsibility |
| --- | --- |
| `institution`, `institution2` | Isolated institutional cases, responses and evidence |
| `reviewer` | Assigned scrutiny and obligation verification |
| `evc` | Assigned evaluation scheduling and reports |
| `committee` | SHC/SAC recommendations |
| `regional` | North-region oversight |
| `bureau` | Cross-region oversight and escalation |
| `executive` | Executive Committee decisions |
| `council` | Council ratification and appeal decisions |
| `policy` | Demonstration policy configuration |
| `auditor` | Read-only records and reports |
| `operator` | Background work and replay |

Stop owned local services with `scripts/stop.ps1`. Do not stop PostgreSQL while tests or migrations run. Runtime files, secrets, private evidence and source attachments are excluded from Git.

## Included functionality

- Institution-linked, deduplicated synthetic intake and policy-pinned category routes.
- Explicit actions with scope checks, row locks, expected versions, reasons and idempotency keys.
- Deficiency return/resubmission history, original finding resolution and preserved deadlines.
- EVC interval-conflict protection, rescheduling, attendance, reports and completion guards.
- Separate recommendation, executive decision, Council ratification and demonstration appeal records.
- Independent obligations, evidence receipts/review, accepted validity and document renewals.
- Private immutable files, checksums, quarantine and authorised attachment downloads.
- Durable deadlines, deduplicated reminders, assigned escalation, extensions and audit history.
- Transactional outbox, Celery delivery/recovery, in-app alerts and a **local email sink**.
- Role-specific dashboards, filters, scoped CSV exports and an audit immutability trigger.
- Sessions, CSRF, login throttling, responsive layouts and keyboard-accessible dialogs.

## Fresh Windows setup

Install Python 3.12+, Node.js 22+ and Git, then:

```powershell
git clone https://github.com/satvikdixit23/Anumati.git
cd Anumati
.\scripts\bootstrap.ps1
```

The bootstrap installs locked dependencies, builds React and generates local secrets. For the portable runtime:

1. Extract [official PostgreSQL Windows binaries](https://www.postgresql.org/download/windows/) so `.runtime/postgres/pgsql/bin/postgres.exe` exists. `scripts/extract_postgres_runtime.py` optionally retrieves only the server files from a pinned official EDB archive, validating ZIP entry CRCs. The tested local server is PostgreSQL 17.
2. Extract a Redis Windows development build under `.runtime/redis/`. This local demo uses the [redis-windows community port](https://github.com/redis-windows/redis-windows); it is not an official production Windows Redis distribution. Use official Redis on Linux for a pilot.
3. Run `scripts/start.ps1`. It initialises a private cluster, applies migrations, seeds synthetic accounts/cases and starts the services without installing system services.

Portable ports: PostgreSQL **55432**, Redis **56379**, web **8010**. Set `WEB_PORT` in `.env` if needed. All bind to loopback. PostgreSQL uses password authentication; Redis uses a loopback-only development configuration. The local database owner can create test databases; use separate least-privilege roles in production. Editing `.env` does not rotate credentials in an already-initialised database.

## Native Linux development

Install PostgreSQL and Redis as native services, create a database and password-authenticated owner, and configure `.env` from `.env.example`. For synthetic data set `DEMO_MODE=1`, `DJANGO_DEBUG=1` and a `DEMO_PASSWORD` of at least 12 characters.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.lock.txt
cd frontend && npm ci && npm run build && cd ..
python manage.py migrate
python manage.py seed_demo
python manage.py runserver 127.0.0.1:8010
```

In separate terminals: `celery -A anumati worker --loglevel=info` and `celery -A anumati beat --loglevel=info`. Run one Beat instance. Windows uses `--pool=solo` only for development. React development uses `cd frontend && npm run dev` on 5173, proxying the API to 8010. The built app is served on the Django origin.

## Test

```powershell
.\scripts\check.ps1
```

Equivalent: `python manage.py check`, `python manage.py test core --noinput -v 2`, and `cd frontend && npm run build`. Tests use real PostgreSQL and create/remove `test_anumati`. The suite includes 100 concurrent authority commands, requiring one decision and 99 conflicts. GitHub Actions installs native PostgreSQL/Redis on Ubuntu with no container services.

## Demonstrate the journey

1. As `institution`, create and submit a name-change application with a unique source reference.
2. As `reviewer`, return a deficiency with its requirement reference.
3. As `institution`, respond; as `reviewer`, resolve the original deficiency and complete scrutiny.
4. As `executive`, approve; as `council`, ratify. Both authority records remain in history.
5. As `bureau`, register a sourced undertaking on the approved case.
6. As `institution`, submit a synthetic `.txt` file from the case Compliance tab. The receipt is immediate; Celery validates it asynchronously.
7. As `reviewer`, accept the exact evidence version. Obligation fulfilment is independent of approval.
8. As `evc`, use seeded `AN-2026-0002` to record attendance for its already-started synthetic visit, submit a report and complete EVC. New visits must start in the future and cannot be conducted early.
9. As `bureau`, inspect seeded overdue escalations, extend a deadline or record a resolution. Check History and scoped reports.
10. Inspect notifications and local email files in `.runtime/mail/`.

## Scope and deployment boundary

This is a runnable **demonstration vertical slice**, not a certified production AICTE service. See [coverage](docs/COVERAGE.md), [architecture](docs/ARCHITECTURE.md), [operations](docs/OPERATIONS.md) and [verification](docs/VERIFICATION.md).

PDF uploads remain quarantined because no approved malware scanner is configured. Synthetic text is explicitly labelled `demo_validated`, not antivirus-clean. Live AICTE/NSWS/payment, SSO/MFA, SMS/email providers, statutory signatures, consolidated policy rules, production TLS/hosting and formal security/accessibility/recovery acceptance are not claimed. Disabling `DEMO_MODE` alone does not make the application production-ready.
## Institution website module

The separate institution portal is available at **http://127.0.0.1:8010/website/**. Institutions can submit complaints and grievances with documents, link existing approval applications, and follow administrative responses. Administrators access the same records and documents through the portal inbox or the links in the approval workspace.

See [module architecture and permissions](website/README.md) and [Google Drive setup instructions](docs/GOOGLE_DRIVE_SETUP.md). Google authorization is required before queued documents can be stored in the designated Drive folder.
