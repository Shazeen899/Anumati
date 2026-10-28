# Operations

Run `scripts/start.ps1` and inspect `.runtime/processes.json` plus `web.log`, `worker.log`, `beat.log`, `redis.log` and `postgres.log`. The PID ledger is local-only. `scripts/stop.ps1` checks command ownership before stopping processes, then cleanly shuts down this PostgreSQL cluster. Never stop it during tests, migrations or backups.

Web uses port 8010 because another application occupies 8000. `scripts/runtime.py status` checks configured ports; `/api/health` also checks PostgreSQL. Worker logs and the Operations screen show actual task progress.

- Broker outage: business commands retain outbox intent. Restore Redis and workers; the dispatcher republishes eligible IDs.
- Worker failure: expired leases make unfinished work eligible again.
- Scheduler outage: restart one Beat. All outstanding eligible deadlines are scanned, including missed intervals.
- Mail sink failure: in-app effects remain available; five attempts lead to a visible failure queue. Replay preserves logical identity and records a reason.
- Scanner unavailable: files stay quarantined. PDF quarantine is intentional here.
- Uncertain command outcome: retry the same payload and idempotency key; do not bypass a version conflict.

Back up PostgreSQL with `pg_dump` together with private evidence versions and protected configuration under a coordinated maintenance window. Restore first into a separate disposable database/media directory and verify checksums, decisions and audit history. No measured RPO/RTO or completed disaster-recovery drill is claimed. Redis persistence is not a database backup.

For a real pilot, use official native Linux PostgreSQL/Redis, least-privilege service/migration roles, authenticated private broker networking, Celery workers and one scheduler. Replace Django's development server with an approved production WSGI/TLS deployment. Hosting components beyond the requested stack were not installed. Consolidate policy, authority, identity, scanner, provider, retention and incident-response requirements before real records are used.
