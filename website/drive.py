"""Google Drive REST adapter. No credentials or Drive IDs are sent to institutions."""
import json
import os
import re
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings

API = 'https://www.googleapis.com/drive/v3/files'
TOKEN_URL = 'https://oauth2.googleapis.com/token'


class DriveError(Exception):
    pass


def client_config():
    try:
        value = json.loads(Path(settings.GOOGLE_DRIVE_CLIENT_FILE).read_text())['web']
        return value['client_id'], value['client_secret']
    except (OSError, ValueError, KeyError):
        raise DriveError('Google OAuth client configuration is missing. Ask the administrator to complete Drive setup.')


def configured():
    try:
        client_config()
        return bool(settings.GOOGLE_DRIVE_FOLDER_ID)
    except DriveError:
        return False


def connected():
    return configured() and Path(settings.GOOGLE_DRIVE_TOKEN_FILE).is_file()


def token_request(values):
    request = Request(TOKEN_URL, data=urlencode(values).encode(), headers={'Content-Type': 'application/x-www-form-urlencoded'})
    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    except (HTTPError, URLError, TimeoutError, ValueError):
        raise DriveError('Google authorization failed or expired. Reconnect Drive from administration.') from None


def exchange_code(code):
    client, secret = client_config()
    result = token_request({'client_id': client, 'client_secret': secret, 'code': code,
                           'redirect_uri': settings.GOOGLE_DRIVE_REDIRECT_URI, 'grant_type': 'authorization_code'})
    if not result.get('refresh_token'):
        raise DriveError('Google did not return offline access. Reconnect and grant consent.')
    adapter = DriveClient(result['access_token'])
    adapter.check_folder()
    path = Path(settings.GOOGLE_DRIVE_TOKEN_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps({'refresh_token': result['refresh_token']}))
    os.chmod(temp, 0o600)
    temp.replace(path)


def access_token():
    client, secret = client_config()
    try:
        token = json.loads(Path(settings.GOOGLE_DRIVE_TOKEN_FILE).read_text())['refresh_token']
    except (OSError, ValueError, KeyError):
        raise DriveError('Google Drive is not connected. Documents remain queued securely.')
    return token_request({'client_id': client, 'client_secret': secret,
                          'refresh_token': token, 'grant_type': 'refresh_token'})['access_token']


def safe_id(value):
    if not re.fullmatch(r'[A-Za-z0-9_-]{5,200}', value or ''):
        raise DriveError('Invalid Drive identifier in configuration.')
    return value


class DriveClient:
    def __init__(self, token=None):
        self.token = token or access_token()

    def request(self, method, url, data=None, content_type='application/json', binary=False):
        request = Request(url, data=data, method=method,
                          headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': content_type})
        try:
            with urlopen(request, timeout=60) as response:
                # All accepted uploads are capped at 5 MB. Bound downloads too.
                content = response.read(5 * 1024 * 1024 + 1024 if binary else 1024 * 1024)
                return content if binary else json.loads(content)
        except HTTPError as error:
            if error.code == 404:
                return None
            raise DriveError(f'Google Drive returned HTTP {error.code}. Check connection, folder access and storage quota.') from None
        except (URLError, TimeoutError, ValueError):
            raise DriveError('Google Drive is temporarily unavailable. The document will be retried.') from None

    def metadata(self, file_id):
        return self.request('GET', API + '/' + safe_id(file_id) + '?' + urlencode({
            'supportsAllDrives': 'true', 'fields': 'id,size,md5Checksum,parents,trashed,mimeType,capabilities(canAddChildren)'}))

    def check_folder(self):
        folder = self.metadata(settings.GOOGLE_DRIVE_FOLDER_ID)
        if not folder or folder.get('trashed') or folder.get('mimeType') != 'application/vnd.google-apps.folder' or not folder.get('capabilities', {}).get('canAddChildren'):
            raise DriveError('The configured Drive folder is missing or this Google account cannot upload to it.')

    def reserve_id(self):
        result = self.request('GET', API + '/generateIds?count=1&space=drive&type=files')
        return safe_id(result['ids'][0])

    def upload(self, doc):
        # A preallocated ID is saved in PostgreSQL before upload. A retry after a
        # network timeout observes the same file instead of creating duplicates.
        existing = self.metadata(doc.drive_file_id)
        if existing:
            self.verify(doc, existing)
            return
        boundary = 'anumati_' + uuid.uuid4().hex
        metadata = {'id': doc.drive_file_id,
                    'name': f'{doc.grievance.reference}__{doc.pk.hex[:8]}__{doc.name}',
                    'parents': [safe_id(doc.drive_folder_id)],
                    'appProperties': {'document_id': str(doc.pk), 'grievance_id': str(doc.grievance_id),
                                      'institution_id': str(doc.grievance.institution_id)},
                    'description': f'{doc.grievance.reference}; SHA-256: {doc.sha256}'}
        with doc.staged_file.open('rb') as file:
            content = file.read()
        payload = (f'--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n'.encode()
                   + json.dumps(metadata).encode() + f'\r\n--{boundary}\r\nContent-Type: {doc.mime_type}\r\n\r\n'.encode()
                   + content + f'\r\n--{boundary}--\r\n'.encode())
        result = self.request('POST', 'https://www.googleapis.com/upload/drive/v3/files?' + urlencode({
            'uploadType': 'multipart', 'supportsAllDrives': 'true', 'fields': 'id,size,md5Checksum,parents,trashed'}),
            payload, 'multipart/related; boundary=' + boundary)
        self.verify(doc, result)

    @staticmethod
    def verify(doc, metadata):
        if not metadata or metadata.get('trashed') or metadata.get('md5Checksum') != doc.md5 or int(metadata.get('size', -1)) != doc.size or doc.drive_folder_id not in metadata.get('parents', []):
            raise DriveError('Drive file integrity or folder verification failed. Contact the administrator.')

    def download(self, doc):
        self.verify(doc, self.metadata(doc.drive_file_id))
        content = self.request('GET', API + '/' + safe_id(doc.drive_file_id) + '?alt=media&supportsAllDrives=true', binary=True)
        import hashlib
        if content is None or len(content) != doc.size or hashlib.sha256(content).hexdigest() != doc.sha256:
            raise DriveError('Document integrity verification failed.')
        return content


def upload_file(file_field, name, mime_type, file_id=None, app_properties=None):
    client = DriveClient()
    target_id = file_id or client.reserve_id()
    existing = client.metadata(target_id)
    if existing:
        return target_id, existing.get('md5Checksum', '')
    boundary = 'anumati_' + uuid.uuid4().hex
    folder_id = safe_id(settings.GOOGLE_DRIVE_FOLDER_ID)
    meta = {
        'id': target_id,
        'name': name,
        'parents': [folder_id],
        'appProperties': app_properties or {},
    }
    with file_field.open('rb') as f:
        content = f.read()
    payload = (f'--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n'.encode()
               + json.dumps(meta).encode() + f'\r\n--{boundary}\r\nContent-Type: {mime_type}\r\n\r\n'.encode()
               + content + f'\r\n--{boundary}--\r\n'.encode())
    result = client.request('POST', 'https://www.googleapis.com/upload/drive/v3/files?' + urlencode({
        'uploadType': 'multipart', 'supportsAllDrives': 'true', 'fields': 'id,size,md5Checksum,parents,trashed'}),
        payload, 'multipart/related; boundary=' + boundary)
    if not result or result.get('trashed') or folder_id not in result.get('parents', []):
        raise DriveError('Drive upload verification failed.')
    return result['id'], result.get('md5Checksum', '')


def download_stream(doc_or_evidence):
    client = DriveClient()
    file_id = getattr(doc_or_evidence, 'drive_file_id', None)
    if not file_id:
        raise DriveError('Document has no Google Drive file ID.')
    content = client.request('GET', API + '/' + safe_id(file_id) + '?alt=media&supportsAllDrives=true', binary=True)
    if content is None:
        raise DriveError('Document could not be retrieved from Google Drive.')
    import io
    return io.BytesIO(content)
