# website — separate institution services module

This Django app and independent React build implement institution complaints/grievances and administrative responses. The existing approval workflow remains in `core` and `frontend`; the new module lives entirely under `website` except shared project registration, navigation and configuration.

## Run

```powershell
.\.venv\Scripts\python.exe manage.py migrate
cd website/frontend
npm.cmd ci
npm.cmd run build
cd ../..
.\.venv\Scripts\python.exe scripts/runtime.py start
```

Open **http://127.0.0.1:8010/website/**. Uses existing Anumati accounts/sessions. No Docker or new application framework is required. The independent Vite development server is on port 5174 and proxies both APIs to Django on port 8010.

## Database bridge

Both modules use the existing **PostgreSQL database**, with distinct `website_*` and `core_*` tables. We deliberately do not create two unsynchronized copies of institutions/users or a second physical database: PostgreSQL foreign keys and atomic transactions connect the modules reliably.

- `website_grievance.institution_id` → `core_institution.id` (required).
- `website_grievance.application_id` → `core_application.id` (optional, same institution enforced at intake).
- `website_document.grievance_id` → `website_grievance.id`.
- `website_message.grievance_id` → `website_grievance.id`.
- User references point to existing `auth_user`; role/region/institution come from existing `core_profile`.
- Uploaded bytes are staged privately then stored in Google Drive; PostgreSQL retains checksums, receipt time, Drive IDs and upload state. Documents remain attached to their grievance and hence the correct institution/application.

Database rows are protected from cascading deletion. The existing immutable audit ledger records submissions, responses, storage completion, authorized downloads and retries; notifications use the existing durable outbox. Initial submissions accept a client-generated UUID for idempotency, while responses use locked rows and optimistic versions.

## Permissions and workflow

| Account | Access |
|---|---|
| Institution | Submit, see only its institution's records, add documents, respond while open |
| Bureau | All submissions and documents, request information, resolve/reopen, Drive setup and retries |
| Regional | Same operations on its own region, excluding Drive setup |
| Auditor | Read and download within the grievance module; cannot change status |
| Operator | Storage connection/queue management; cannot decide grievances |
| Other roles | No grievance record or document access |

Workflow: **Submitted → In review / Awaiting institution → Resolved**. An institution response to a request returns the record to In review. Only bureau/regional staff can resolve or reopen, with a recorded reason. General grievances without an approval application are supported. The portal does not create or decide approval applications; those remain in the linked approval workspace.

Admin access to documents is through a Django download endpoint after per-request scope checks. No OAuth token, Drive file ID or public file URL is exposed to institutions. No Google Drive sharing permissions are added automatically. Preexisting direct Drive access is controlled by the folder owner.

See [Google Drive setup](../docs/GOOGLE_DRIVE_SETUP.md). Live Drive upload/download requires the user to supply a Google OAuth client and authorize access. Tests use a mocked Drive boundary; they do not claim a successful live Google upload.

## Verification

```powershell
.\.venv\Scripts\python.exe manage.py test core website --noinput
```

Website tests cover foreign-institution access, forged linkage, role boundaries, CSRF, stale updates, submission idempotency, attachments and validation, transactional cleanup, durable retry/lease recovery, Drive ID reuse, integrity checks, authorized downloads, and OAuth state/replay protection.
