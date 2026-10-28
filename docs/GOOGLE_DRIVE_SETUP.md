# Google Drive setup for the institution website

The portal is available at **http://127.0.0.1:8010/website/**. Your designated folder ID is configured in the local `.env`. A folder link does not grant the application permission to upload. Until you connect Google, documents remain in the private local staging queue and are clearly marked **Pending**, not **Stored**.

## 1. Create the Google OAuth client

1. Open [Google Cloud Console](https://console.cloud.google.com/), select or create a project, and enable **Google Drive API** under **APIs & Services → Library**.
2. Open **Google Auth Platform** (or **APIs & Services → OAuth consent screen**). Configure the app name, support email and developer contact. For a personal Google account, select an external audience. While testing, add the Google account you will connect as a test user.
3. In **Data Access**, add `https://www.googleapis.com/auth/drive`. This is a restricted scope. This implementation uses it to access the existing folder you supplied; Google does not offer a folder-only OAuth scope. The server itself uses only the configured folder and its stored document IDs. Use a dedicated organizational storage account where possible. Production external publication may require Google verification. The alternative `drive.file` scope requires a Google Picker or app-created folder workflow, which this integration does not implement.
4. Open **Clients → Create client → Web application**. Add this exact **Authorized redirect URI**:

   ```text
   http://127.0.0.1:8010/website/api/drive/callback
   ```

5. Download the OAuth client JSON and save it at:

   ```text
   C:\Users\satvikk\OneDrive\Documents\Anumati\.runtime\google-oauth-client.json
   ```

   Keep the filename exactly as shown. Do not paste the client secret into chat, source code or a browser form. This path is ignored by Git. The file must contain Google's `web` client configuration, not a service-account key. If stored elsewhere, set `GOOGLE_DRIVE_CLIENT_FILE` in `.env` and restart the web/worker processes.

## 2. Connect the designated folder

1. Sign in at `/website/` as **bureau** (password in `.runtime/DEMO-CREDENTIALS.txt`).
2. Open **Document storage**. It shows the configured folder and redirect URI.
3. Select **Connect Google Drive**. Sign in with a Google account that has upload access to that folder, review Google's permissions, and authorize access.
4. The callback verifies the folder exists and that the account can add documents before saving the offline refresh credential. You return to Document storage.
5. With Celery worker and beat running, queued uploads are picked up every 20 seconds. Use **Refresh** to check. A completed document changes to **Stored** and gains a **Download** action in its submission.

Existing folder permissions are inherited. The application does not create public links or grant sharing permissions. Ensure this folder is not publicly shared if it will hold private institutional documents. Portal access checks cannot override access somebody already has directly in Google Drive.

## 3. Verify the full flow

1. Sign in as **institution**, create a complaint or grievance, optionally choose an existing approval application, and attach a small DOCX, DOC, PDF or TXT file.
2. Record its `GR-…` reference. Wait for the document to show **Stored**.
3. Sign in as **bureau**, open the same reference in **Administration inbox**, download the document and record a response.
4. Sign in as **institution2**: the other institution's submission and document must not be visible.
5. In Drive, find a filename beginning with the `GR-…` reference. The document also carries institution/submission identifiers as app properties. File bytes are kept unchanged; Word documents are not converted to Google Docs.

## Credentials, retries and operations

- Refresh credentials are stored at `.runtime/google-drive-token.json`, ignored by Git. Access tokens stay server-side and are never sent to the React client. Protect both credential files with local filesystem permissions. This workspace is inside OneDrive: for production, place credential files outside synchronized folders using the two environment settings and use a dedicated service OS account.
- A personal My Drive account and an authorized Workspace account can both use this OAuth flow. Shared-drive requests include `supportsAllDrives=true`. Service-account authentication is not implemented; service accounts do not own ordinary My Drive storage quota.
- Google external OAuth apps in testing can have short-lived refresh tokens. If uploads report an authorization error, reconnect. Account revocation, missing files, quota and folder access errors are reported without exposing provider response bodies or credentials.
- Celery retries with backoff, stops after eight attempts, and exposes **Retry** to bureau/operations. Pending documents survive application/broker restarts. Expired worker leases are recoverable. PostgreSQL stores a preallocated Drive ID before upload to prevent duplicate files after uncertain network outcomes.
- Drive checksums and sizes are verified before marking a document stored. The temporary local copy is then deleted. Downloads check the folder, size, MD5 and SHA-256, and stream as an attachment through Django after authorization.
- Supported documents: PDF, DOC, DOCX and UTF-8 TXT, up to 5 MB each. Five files can accompany the initial submission; further uploads are capped at 20 files per submission. Header/package validation is **not antivirus scanning**. Install an approved scanning process before using this prototype for real untrusted documents; there is no claim that uploaded files are malware-free.
- Production hosting needs HTTPS and an exact HTTPS OAuth redirect URI. Keep staging storage private, configure request/body limits at the ingress, and ensure web/worker instances share the credential/staging paths. Never serve `private-media` directly as public static files.

References: [Google web-server OAuth](https://developers.google.com/identity/protocols/oauth2/web-server), [Drive scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth), [Drive uploads](https://developers.google.com/workspace/drive/api/guides/manage-uploads), [pre-generated file IDs](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/generateIds).
