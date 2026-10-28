# Coverage and remaining work

The supplied problem statement (SIH 26262), PRD, and seven-sheet architecture were evaluated as product inputs. All core approval, compliance monitoring, and alert management demands have been brought into alignment with verifiable local code, schema migrations, automated tests, and UI integration.

| Area | Demonstrated Locally | Remaining Pilot/Production Work |
| --- | --- | --- |
| **Access control & confidentiality** | Scoped queries via `permitted_cases`, `permitted_audit_events`, role, institution, region, and file download protection | Time-limited delegation, OAuth2 SSO / SAML integration |
| **Intake and routing** | Unique source references, pinned policies, dynamic routes (`SAC`, `SHC`, `EVC`, `SCRUTINY`, `DECISION`, `RATIFICATION`), exception recovery (`resolve_exception`) | Live upstream NSWS / AICTE ERP direct sync, complete statutory APH combination sign-off |
| **Deficiencies & returns** | Original findings, responses, reviewer resolution, preserved statutory clocks, explicit deadline owner transfer | Full procedural speaking-order legal templates |
| **Decisions & appeals** | Executive decisions, Council ratifications, SAC recommendations, Council appeal approvals/rejections | Gazette notification publishing automation |
| **Policy lifecycle** | Versioned draft/validate/preview/activate/retire lifecycle, case policy pinning, audited migration (`migrate_policy`) | Multi-year policy transition rules certified by domain authorities |
| **EVC lifecycle** | Distinct deadlines (`evc_scheduling`, `evc_visit`, `evc_report`), conflict guards, late scheduling detection, audited cancellation | GPS location geofencing, video stream verification |
| **Compliance monitoring** | Independent obligations, 8-state validity lifecycle (`get_obligation_lifecycle`), renewals without overwrite, verifier scoping | Land registry & Municipal Corporation API integration |
| **Evidence handling** | Private storage, checksums, quarantine, manual security assessment (`security_review_evidence`) releasing PDF/DOCX | Production antivirus cluster integration |
| **Deadlines & alerts** | Durable revisions, catch-up scans, deduplication, alert supersession on ownership transfer, escalation tracking | Production SMS / WhatsApp Business API contracts |
| **Consolidated reports** | 7 scoped CSV exports (`applications`, `evc`, `compliance`, `validity`, `escalations`, `regional_performance`, `audit`) with CSV formula protection | S3 / GCS large asynchronous batch exports |
| **Google Drive storage** | Two-way integration code for `website.Document` and `core.Evidence`, Celery sync task (`upload_pending_evidence`), MD5 verification | Live Google Workspace authorization via OAuth client tokens |
| **Security & operations** | CSRF protection, secure sessions, login rate-limiting, append-only DB triggers, outbox replay | Production TLS certificates, independent write-once audit log archival |

*Note: All demonstration policies and timelines are explicitly synthetic. Final statutory validity and legal authority mappings require formal adoption by AICTE.*
