# Verification record

Local verification date: **28 October 2026** on branch `codex/ps26262-requirements`. Tests use synthetic demonstration data only.

| Check | Result | Details |
| --- | --- | --- |
| Django system checks | **Passed** | Zero issues, zero silenced warnings |
| PostgreSQL test suite | **61 tests passed** | Ran in 36.227 seconds against local PostgreSQL 16 on port 55432 |
| 100 concurrent authority commands | **Passed** | Exactly one winner committed; 99 HTTP-style 409 domain conflicts; one authority audit event |
| Main React frontend build | **Passed** | Vite 7.3.7 production bundle generated (`dist/assets/index-DUNiGCvH.js`: 277 kB) |
| Website React frontend build | **Passed** | Vite 7.3.7 production bundle generated (`website/frontend/dist/assets/index-DOGwzrVS.js`: 247 kB) |
| Actual HTTP session/CSRF journey | **Passed** | End-to-end live flow on `127.0.0.1:8010` via `scripts/smoke.py` |
| Celery background validation | **Passed** | Celery worker processed file scanning and evidence validation in real-time |
| Website & Grievance HTTP journey | **Passed** | Live flow on `127.0.0.1:8010/website/` via `scripts/website_smoke.py` |
| Audit confidentiality isolation | **Passed** | Policy, operator, reviewer, committee, and cross-institutions blocked from grievance audit events |
| Deadline responsibility transfer | **Passed** | Ownership transferred upon return/resubmit/reassign; outdated outbox alerts superseded |
| Configurable routing & SAC support | **Passed** | Scrutiny → EVC → SAC → Decision → Ratification progression verified |
| Distinct EVC deadlines | **Passed** | Scheduling, visit, and report deadlines tracked independently; late scheduling flagged |
| Quarantined evidence security review | **Passed** | PDF/DOCX quarantined; authorized officer released to `security_reviewed`; substantive review accepted |
| Obligation lifecycle states | **Passed** | 8 distinct states handled without overwriting accepted evidence receipts or validity |
| Safe CSV reports & analytics | **Passed** | 7 report types exported; formula injection protection (`safe_csv`) verified |
| AICTE adapter contract | **Passed** | Schema validation and HMAC-SHA256 signature verification verified |

## Live Smoke Journey Summaries

### Core Approval & Compliance Platform (`scripts/smoke.py`)
- Verified synthetic case `AN-2026-FBB91C`:
  1. Intake draft created by institution.
  2. Submitted to Scrutiny stage.
  3. Reviewer recorded deficiency and returned case to institution (deadline transferred to institution).
  4. Institution resubmitted with explanation (deadline transferred back to reviewer).
  5. Reviewer resolved deficiency and completed Scrutiny stage.
  6. Executive approved; Council ratified to `APPROVED`.
  7. Post-approval compliance undertaking created.
  8. Institution uploaded evidence file.
  9. Celery worker performed background scan and marked `demo_validated`.
  10. Reviewer accepted evidence version.
  11. Final stage: `APPROVED`, obligation status: `fulfilled`, audit events: 12.
- Verified isolation: Cross-institution access was denied with HTTP 404.

### Grievance Website & Document Staging (`scripts/website_smoke.py`)
- Verified synthetic grievance `GR-2026-E688A7AF3FC2`:
  1. Submitted via institution portal and linked to `AN-2026-FBB91C`.
  2. Attached synthetic document in pending storage state.
  3. Regional and bureau officers reviewed submission.
  4. Cross-institution access denied (0 search results, 404 detail access).

---

## Repeat Verification Instructions

To execute the automated verification locally:

```powershell
# 1. Run all unit and integration tests:
.venv\Scripts\python.exe manage.py test core website --noinput

# 2. Rebuild React frontends:
npm --prefix frontend run build
npm --prefix website/frontend run build

# 3. Run core smoke test against local running stack:
$env:DEMO_PASSWORD='nTu-8RpHiqT8VuW9uaNq'; .venv\Scripts\python.exe scripts\smoke.py

# 4. Run website grievance smoke test:
$env:DEMO_PASSWORD='nTu-8RpHiqT8VuW9uaNq'; .venv\Scripts\python.exe scripts\website_smoke.py
```
