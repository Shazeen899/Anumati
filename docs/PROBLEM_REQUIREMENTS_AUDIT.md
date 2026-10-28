# Problem statement 26262 — implementation verification

Reviewed on 28 October 2026 against commit `aaa5f1e` and the current local configuration.

## Verdict

**The current solution does not fulfil all demands.** It is a working integrated prototype with substantial coverage of the core approval/compliance journey. It has specific functional gaps, one reproduced confidentiality defect, and integrations that are not operational. It must not be represented as fully APH-compliant or production-ready.

The original problem statement is the acceptance baseline. The newly supplied explanation is supporting interpretation; its fictional examples are not official requirements or timelines. The grievance website and Google Drive storage are additional user requirements, not replacements for the original approval/compliance requirements. MFA, live SMS, autonomous expert selection and optional AI are not separately mandated by the supplied statement; their absence is not counted as an explicit PS failure.

## Verification performed

- Read both supplied text documents and inspected implementation, configured demo routes, scheduled tasks, access rules, reports and existing tests.
- Re-ran `manage.py test core website --noinput -v 1`: **39 tests passed**, in 13.169 seconds. These establish the tested behaviors, not complete requirement coverage.
- Executed four additional isolated PostgreSQL probes. All reproduced the gaps described below. The probes assert the current defective/limited behavior; their successful run is not a feature acceptance pass.
- Checked configuration without exposing credentials: OAuth client configured **false**, Google authorization saved **false**, website documents pending **1**, documents stored in Drive **0**.
- The four probes use a temporary test database, which was removed. No live records or application source code were changed during this review.
- No current APH legal interpretation, live government integration, live Google upload or production security certification was performed.

## Confirmed priority findings

### P1 — general grievance contents leak through the global audit endpoint

An institution can submit a general grievance with no linked approval application. Its audit event has `application=None`. `core/views.py:275` allows policy and operations accounts to read global events with no application. A policy account denied the grievance detail endpoint can nevertheless retrieve the grievance subject/reference/identifier through `/api/audit`; subsequent responses are also recorded in the event reason.

**Reproduced:** the policy account received 404 for the grievance detail and received its private subject in the audit feed. The shared audit reader must enforce the grievance scope for website events instead of treating every null-application event as globally visible. This affects the explicit confidentiality and role-based access requirement.

### P1 — returned-case deadline reminders target the previous reviewer

`core/services.py:118`, in the `return` action, changes the case assignee to the institution and preserves the deadline. It does not transfer the existing deadline owner. `core/tasks.py:13` sends approaching-deadline reminders to that owner.

**Reproduced:** the institution owned the returned case, but its upcoming deadline notification was addressed to the reviewer. The institution does receive the separate return notification; the defect is the responsibility of subsequent deadline reminders. Preserve the original clock while explicitly modeling which party currently owes the action.

### P1 — the route configurator cannot express the stated EVC + SAC example

`core/services.py:82` permits only SCRUTINY, EVC, SHC, DECISION and RATIFICATION in configured routes. SAC is hardcoded into a separate rejected-case appeal path. All configured routes must end with DECISION and RATIFICATION.

**Reproduced:** a route `EVC → SAC → DECISION → RATIFICATION` is rejected as an unsupported stage. SHC is configurable, but no seeded route demonstrates it. This is not the complete configurable routing capability described by the problem statement. The required official route/authority catalogue still needs domain validation; the presence of a hardcoded appeal does not prove all prescribed SAC routes are supported.

### P2 — EVC scheduling and completion do not have distinct deadline lifecycles

The system queues EVC-stage applications and records confirmed, conducted and reported visits. It guards overlap and requires a report before advancing. However, `core/services.py:118` creates no visit scheduling/completion/report deadline when managing the visit; reminders use the general stage deadline.

**Reproduced:** a visit could be scheduled after the current stage due date, and the only active deadline remained `stage`. A late visit is not necessarily prohibited by the PS, but separate scheduling/completion tracking and appropriate late-state alerts are needed. The present generic EVC-stage alert does not demonstrate the full timing requirement.

## Demand-by-demand matrix

| Demand | Assessment | What works | Gap preventing full fulfilment |
|---|---|---|---|
| A: Applicable automatic routing | Partial | Policy-pinned routes, guarded transitions, assigned human completion | Restricted route vocabulary; SAC example unsupported; no validated complete APH catalogue |
| A: Return/re-scrutiny with reasons | Substantially implemented, defect found | Findings, institution responses, reviewer resolution and original deadline preserved | Deadline reminder recipient stays with prior reviewer; re-review catalogue remains limited |
| A: Routine administration and case summaries | Partial | Automatic assignment, structured case detail, outbox notifications | No full standardized communication/speaking-order templates or generated formal case-summary package |
| B: Consolidated role dashboards | Implemented for demo scope | Institution/region/assignment scopes; stage/region counts, case ownership and overdue filters | Polls every 30 seconds rather than pushing updates; production timeliness not measured |
| B: Ageing, workload, bottlenecks | Partial | Overall case age, stage history, stage counts and overdue cases | No officer/committee workload dashboard or owner filter; no dedicated stage-age/turnaround analytics; no region-wise performance metrics |
| B/F: Complete audit trail | Partial, security defect | Append-only database audit events, reasons, actors, versions, decisions, deadlines, file reviews and downloads | General-grievance audit leakage; external delivery receipt audit absent; full completeness not established |
| C: EVC queue/status/alerts | Partial | EVC route queue, scheduling/rescheduling/cancellation, attendance, report and stage alerts | No distinct scheduling/completion/report deadlines or complete dedicated EVC report |
| D: Undertakings and compliance | Implemented for synthetic happy path; practical document gap | Sourced obligations after approval, due dates, receipts, independent human verification, accepted/returned status | Core PDF evidence stays quarantined indefinitely; no approved scanning-to-release path; real evidence journey is not complete |
| E: Lease/document validity | Partial | Accepted validity dates, renewal evidence versions, upcoming/overdue validity deadlines, separate verification | Demo lease duration check only; official eligibility rules unvalidated; status can remain fulfilled after expiry although expiry is separately flagged |
| F: Configurable notifications/escalations | Partial | Durable Celery/Redis scheduling, outbox retries, deduplication, one upcoming alert and one overdue escalation per revision | Global stage/reminder offsets, fixed bureau escalation owner, no configurable multilevel hierarchy or per-event schedules; overdue alert goes only to escalation owner |
| F: Consolidated reporting | Partial | Application, compliance/validity and audit CSV exports with access checks | No dedicated full EVC report, reminder/escalation report or regional processing-performance report |
| Single integrated platform | Implemented structurally | Shared PostgreSQL identities and foreign keys connect applications, obligations, visits, files, deadlines and website grievances | Website grievance documents are separate from core compliance evidence; attachment to a grievance does not satisfy an approval obligation |
| Configurable APH-aligned rules/timelines | Partial / conformity unverified | Policy version/source, draft/preview/validation lifecycle and pinning | Demo assumptions, hardcoded authority mapping and appeal window, no working-day calendar or full official rules catalogue |
| Secure documents and confidentiality | Partial, not acceptable as complete | Private storage, authorized download endpoints, sessions, CSRF, login throttling, scope tests | Audit leakage; no malware scanning pipeline; website format checks are not an antivirus scan; production configuration and independent security review outstanding |
| Existing authorized AICTE integration where feasible | Not demonstrated | Simulated intake/source identity and simulated payment prerequisite | No authorized live adapter or feasibility evidence. Do not claim live AICTE integration; the statement qualifies this requirement with “wherever feasible” |
| Human oversight | Implemented for demonstrated workflow | Assigned reviewers verify, executive/council roles decide, deadlines generate alerts instead of automatic rejection | Correct legal authority mappings and every regulatory exception remain unvalidated |
| Measurable expected outcomes | Not established | Automation plausibly reduces repeated follow-up in sample cases | No before/after workload, processing-time, deadline-miss or institutional pilot measurements |

## Additional user requirements: website and Google Drive

- **Separate website module: implemented.** Independent Django module and React build, institutional submission/tracking, administrator response workflow and optional application linkage.
- **Database bridge: implemented as shared PostgreSQL with separate module tables and real foreign keys**, not two separate physical databases. Institution/application data is not duplicated.
- **Google Drive integration code: implemented; operational storage: incomplete.** OAuth client and authorization are missing. Uploaded website documents are staged privately, not currently stored in Drive.
- **Administration document access: implemented as an authorized proxy download after Drive storage succeeds**, but the live Drive download has not been verified and the current pending document cannot yet be downloaded through that endpoint.
- **Core approval/compliance files are not integrated with Drive.** `core.Evidence` uses local private storage and its own validation workflow. If the additional Drive requirement means every institutional upload, including approval/compliance evidence, that broader requirement is not fulfilled.
- DOC/DOCX support exists in the grievance website. Core evidence uploads accept PDF/TXT only, with PDF files intentionally quarantined and only synthetic TXT files usable in demo mode.

## Recommended completion order

1. Repair audit confidentiality and add a regression test for general grievances and every audit consumer.
2. Correct returned-case deadline responsibility without resetting time; cover resubmission and reassignment.
3. Extend the route model and get the applicable route/authority/timeline catalogue validated; add route-specific acceptance tests.
4. Implement separate EVC scheduling/completion/report deadlines and missing pendency/workload/report views.
5. Complete real document validation/release and clarify whether all core evidence must use Drive; authorize and verify the actual Drive integration.
6. Add configurable reminder/escalation policies and complete notification delivery/closure audit behavior.
7. Demonstrate the full prototype checklist with realistic synthetic documents, then evaluate operational security, authorized-system integration feasibility and measurable outcomes for a pilot.

The grievance module adds useful functionality, but it does not close the outstanding workflow, compliance and alert-management requirements. A polished interface and a passing existing test suite should not be presented as proof of full PS fulfilment.
