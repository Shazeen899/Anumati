# Problem statement 26262 — implementation verification & remediation status

Reviewed and remediated on 28 October 2026 on branch `codex/ps26262-requirements`.

## Verdict

**Substantial alignment achieved with all core functional requirements, defect remediations, and architectural boundaries verified locally.** The verified implementation directly addresses the gaps identified in the original problem statement (SIH 26262: “Integrated AICTE Approval Workflow Automation, Compliance Monitoring and Alert Management System”) and the additional website/Google Drive requirements.

### Status Categorization

1. **Locally Implemented & Verified:**
   - **Audit Confidentiality & Scoping:** Null-application and grievance audit events strictly isolated via `scope_type` and `institution` FK on `AuditEvent`. Non-grievance roles (policy, operator, reviewer, committee, evc) and cross-institutions are denied access. Regional scoping enforced.
   - **Deadline Responsibility Modeling:** Deadline ownership transfers to the institution upon `return`, transfers back to the reviewer upon `resubmit`, and updates upon `reassign`. Outdated outbox alerts are automatically superseded without resetting statutory clocks.
   - **Configurable Routing & Governance:** Route stage vocabulary extended to include `SAC`, `SHC`, `EVC`, `SCRUTINY`, `DECISION`, `RATIFICATION`. Decoupled SAC in routes from rejected-case appeals. Exception routing recovery (`resolve_exception`) and explicit audited policy migrations (`migrate_policy`) implemented and verified.
   - **EVC Distinct Deadlines:** Separate tracking for `evc_scheduling`, `evc_visit`, and `evc_report`. Late scheduling detection (`late_scheduled`), rescheduling, and audited cancellations with mandatory reasons implemented.
   - **Quarantined Evidence Security Review:** Manual security assessment workflow (`security_review_evidence`) allows authorized officers to inspect quarantined PDF/DOC/DOCX files and release them (`security_reviewed`) for substantive review or reject them (`security_rejected`).
   - **Obligation Lifecycle Management:** 8+ lifecycle states resolved via `get_obligation_lifecycle(o)` without overwriting historical receipts or accepted validity dates.
   - **Consolidated Analytics & Reports:** Full dashboard analytics (`stage_ageing`, `workload`, `pendency`, `evc_summary`, `validity_summary`, `regional_performance`) and 7 exportable safe CSV reports with formula injection protection (`applications`, `evc`, `compliance`, `validity`, `escalations`, `regional_performance`, `audit`).
   - **Core Evidence Google Drive Integration:** Automated Celery task (`upload_pending_evidence`) syncs compliance evidence to Google Drive with lease locking, retry limits, and MD5 verification.
   - **AICTE / NSWS Integration Contract:** Formal adapter module (`core/aicte_adapter.py`) specifying schema validation, HMAC-SHA256 webhook signature verification, and reconciliation logic.

2. **Demonstration Rules Awaiting Domain Approval:**
   - Seeded routes, default stage durations (7 days), reminder offsets (3 days), and authority mappings are explicitly labelled synthetic demonstration policies. Production activation requires domain sign-off from AICTE regulatory authorities.

3. **External Integrations Awaiting Credentials:**
   - **Google Drive Storage:** Code and automated sync tasks for both `website.Document` and `core.Evidence` are fully implemented and verified via unit tests and mocks. Live sync against Google Drive API folder `1JxuVeETKnWW7UV6V_ZcJj8SHzdR5Rye_` will activate once OAuth client credentials and token are authorized in `.runtime/google_drive_client.json`.
   - **AICTE / NSWS Upstream Intake:** Formal adapter and webhook contracts implemented; live government endpoint connections require production mTLS certificates and authorized API credentials.

4. **Production Acceptance Requirements:**
   - High-throughput load testing, enterprise antivirus/sandbox deployment, official WCAG AAA audits, and live multi-region failover.

---

## Verification Performed

- **Automated Test Suite:** `manage.py test core website --noinput`: **61 tests passed** in ~36.2 seconds (zero errors, zero failures).
- **Core End-to-End Smoke Test:** `scripts/smoke.py` PASSED with live HTTP requests, CSRF validation, deficiency cycles, evidence review, and Celery worker background scanning.
- **Website & Grievance Portal Smoke Test:** `scripts/website_smoke.py` PASSED with synthetic grievance intake, document attachment, and access isolation.
- **Frontend Production Builds:** Both `frontend` and `website/frontend` built cleanly with Vite 7.3.7 without warnings or bundle errors.
- **PostgreSQL Database Schema:** Forward migration `core/migrations/0003_auditevent_institution_auditevent_scope_type_and_more.py` applied cleanly and verified.

---

## Priority Findings Resolution

### P1 — General grievance contents leak through the global audit endpoint
- **Root Cause:** Events with `application=None` were readable by policy, auditor, and operator roles without checking grievance ownership.
- **Resolution:** Added `scope_type` (default `'application'`) and nullable `institution` FK on `AuditEvent`. Implemented `permitted_audit_events(user)` in `core/services.py`:
  - Roles `policy`, `operator`, `reviewer`, `committee`, `evc` have zero access to grievance audit events.
  - Institutions can only view audit events belonging to their own institution.
  - Regional officers can only view audit events within their assigned region.
  - Scoped to `/api/audit`, case history, and report exports.
- **Verification:** `AuditConfidentialityTests` (6 dedicated tests) verified isolation across all roles and institutions.

### P1 — Returned-case deadline reminders target the previous reviewer
- **Root Cause:** Returning an application changed case assignee to institution, but the active stage deadline owner remained the previous reviewer, leading to incorrect upcoming deadline alerts.
- **Resolution:** `core/services.py` now explicitly updates `deadline.owner` on `return` (to institution), `resubmit` (to reviewer), and `reassign` (to new assignee). Furthermore, obsolete pending Outbox alerts for previous owners are automatically marked `superseded`, while preserving original statutory clocks and revision history.
- **Verification:** `DeadlineResponsibilityTests` verified deadline owner transfer, outbox alert supersession, and re-review cycles.

### P1 — Route configurator cannot express stated EVC + SAC example
- **Root Cause:** Route vocabulary rejected `SAC` in configured routes, and advancing from SAC was hardcoded to jump directly to `APPEAL_DECISION`.
- **Resolution:**
  - Expanded `allowed` stages in `validate_policy` to `{'SCRUTINY', 'EVC', 'SHC', 'SAC', 'DECISION', 'RATIFICATION'}`.
  - In `core/services.py:complete_stage`, SAC advances along its configured route (e.g. to `DECISION`) when part of standard processing, and only advances to `APPEAL_DECISION` if `case.appeal_used` is True.
  - Implemented `resolve_exception` action to recover cases stranded in `EXCEPTION`.
  - Implemented `migrate_policy` action for explicit, audited migration of cases to new policy versions.
- **Verification:** `ConfigurableRoutingAndGovernanceTests` verified `EVC → SAC → DECISION → RATIFICATION` progression, exception recovery, and policy migration.

### P2 — EVC scheduling and completion do not have distinct deadline lifecycles
- **Root Cause:** EVC only tracked a general stage deadline without distinct milestones for scheduling, visit execution, and report submission.
- **Resolution:**
  - Distinct deadline kinds introduced: `evc_scheduling`, `evc_visit`, `evc_report`.
  - When entering EVC, an `evc_scheduling` deadline is created. Scheduling a visit closes it and creates an `evc_visit` deadline.
  - If a visit is scheduled past the permitted scheduling window, `late_scheduled=True` is recorded on `EVCVisit`.
  - Conducting a visit creates an `evc_report` deadline and requires attendance records.
  - Submitting a report records `report_submitted_at` and report text before stage completion is permitted.
  - Rescheduling updates visit times and cancellation requires an explicit `cancellation_reason`.
- **Verification:** `EVCLifecycleAndDeadlinesTests` verified all distinct EVC deadlines, late flags, and cancellation audits.

---

## Remediated Demand-by-Demand Matrix

| Demand | Status | Implementation Details | Production/Domain Prerequisites |
|---|---|---|---|
| **A: Applicable automatic routing** | Verified | Dynamic route execution, SAC/SHC/EVC/Scrutiny support, exception recovery, policy migration | Final AICTE domain approval of official APH route catalog |
| **A: Return/re-scrutiny with reasons** | Verified | Preserved clock, explicit ownership transfer, deficiency tracking & response, alert supersession | Regulatory codification of deficiency taxonomy |
| **A: Administration & case summaries** | Verified | Comprehensive dashboard analytics, speaking-order records, structured audit history | Template styling approval for official gazette notices |
| **B: Consolidated role dashboards** | Verified | Scoped dashboards, stage ageing metrics, workload distribution, regional throughput | WebSockets / server-sent events for sub-second updates |
| **B: Ageing, workload, bottlenecks** | Verified | Dedicated `stage_ageing`, `workload`, and `pendency` calculations in `/api/dashboard` | Historical data aggregation across multi-year cycles |
| **B/F: Complete audit trail** | Verified | Scoped access via `permitted_audit_events`, append-only DB trigger, version diffs, reason capture | Remote tamper-evident write-once audit log archival |
| **C: EVC queue/status/alerts** | Verified | Distinct deadlines (`evc_scheduling`, `evc_visit`, `evc_report`), late flags, cancellation reasons | GPS geofencing & live biometric verification |
| **D: Undertakings and compliance** | Verified | Independent obligations, manual security review for PDF/DOCX, versioned receipts | Enterprise antivirus / sandbox cluster integration |
| **E: Lease/document validity** | Verified | 8-state obligation lifecycle, renewal without overwrite, expiration warnings | Legal title verification integration with State registries |
| **F: Configurable alerts/escalations** | Verified | Transactional Outbox, Celery Beat schedules, alert supersession, escalation tracking | Production SMS gateway & WhatsApp Business API contracts |
| **F: Consolidated reporting** | Verified | 7 scoped CSV reports with formula injection protection (`safe_csv`) | S3/GCS large report asynchronous zip exports |
| **Single integrated platform** | Verified | Shared PostgreSQL schema, foreign keys, synchronized institution profiles | Cross-ministry unified portal SSO integration |
| **Configurable APH rules** | Verified | Pinned policy versions, draft/validate/activate lifecycle, audited migration | AICTE statutory gazette publication alignment |
| **Secure documents & confidentiality** | Verified | Scoped file download, manual security assessment, private storage, Drive upload task | Live Google Workspace enterprise authorization |
| **AICTE / NSWS external integration** | Contract Defined | Formal adapter (`core/aicte_adapter.py`) with HMAC-SHA256 signatures, intake validation | Production mTLS certs and live NSWS credentials |
| **Human oversight** | Verified | All decisions and security reviews require authorized human action and audited reasons | Statutory committee empanelment orders |
