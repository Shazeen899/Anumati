# Verification record

Local verification date: **27 October 2026**. Tests use synthetic data only.

| Check | Result |
| --- | --- |
| Django system checks | Passed, zero issues |
| PostgreSQL integration suite | 25 tests passed in 13.262 seconds |
| 100 concurrent authority commands | One committed outcome; 99 HTTP-style 409 domain conflicts; one authority audit event |
| React production build | Passed with Vite 7.3.7 |
| Actual HTTP session/CSRF journey | Passed on `127.0.0.1:8010` |
| Actual Redis/Celery evidence validation | Passed as part of the HTTP journey |
| Cross-institution HTTP access | Denied with 404 |
| Receipt preservation | Upload received-at retained after background validation and human review |
| Desktop/mobile browser | Sign-in, overview and case detail inspected at narrow and desktop widths |

The live HTTP journey created synthetic case `AN-2026-2C7737`: intake → scrutiny → deficiency return → institution response → original deficiency resolution → scrutiny completion → executive approval → Council ratification → undertaking creation → evidence upload → Celery validation → verifier acceptance. It ended approved with its undertaking fulfilled, and 12 recorded audit events before the subsequent download audit. Verification timestamp: `2026-10-27T16:36:34Z`.

The automated suite also covers stale requests, same-key replay, changed-payload conflicts, rollback on persistence failure, EVC overlap and completion guards, malicious synthetic file rejection, PDF quarantine, renewal validity preservation, policy dry-run, missed-deadline catch-up, logical alert deduplication, expired leases and local mail failure recovery.

Run `python scripts/smoke.py` to repeat the live journey while the local services run. Each run creates a new clearly synthetic case and evidence file. Test logs and live results are stored under `.runtime/`, excluded from Git. The test suite itself uses a disposable PostgreSQL test database.

These results do not establish production load capacity, malware scanning quality, formal WCAG conformance, independent security certification, all possible APH workflows, real provider delivery, or disaster-recovery objectives. GitHub CI is configured; a local pass is not a claim that a remote CI run has completed.
