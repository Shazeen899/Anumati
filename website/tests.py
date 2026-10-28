import hashlib
import io
import json
import tempfile
import uuid
import zipfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from core.models import Application, AuditEvent, Institution, Profile
from core.services import DomainError
from . import drive, services
from .models import Document, Grievance
from .tasks import upload_document


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class WebsiteTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings_override = override_settings(MEDIA_ROOT=self.temp.name,
            GOOGLE_DRIVE_FOLDER_ID='folder_example', GOOGLE_DRIVE_CLIENT_FILE=Path(self.temp.name)/'client.json',
            GOOGLE_DRIVE_TOKEN_FILE=Path(self.temp.name)/'token.json')
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.institution = Institution.objects.create(name='North Institute', code='NI', region='North')
        self.other_institution = Institution.objects.create(name='South Institute', code='SI', region='South')
        self.institute = self.user('institution', self.institution)
        self.other = self.user('institution', self.other_institution, username='other')
        self.bureau = self.user('bureau')
        self.regional = self.user('regional', region='North')
        self.south = self.user('regional', region='South', username='south')
        self.auditor = self.user('auditor')
        self.reviewer = self.user('reviewer')
        self.application = Application.objects.create(reference='AN-NORTH', institution=self.institution,
            category='name_change', academic_year='2026-2027', external_id='north')
        self.other_application = Application.objects.create(reference='AN-SOUTH', institution=self.other_institution,
            category='name_change', academic_year='2026-2027', external_id='south')

    def user(self, role, institution=None, region='', username=None):
        user = User.objects.create_user(username or role, password='test-password')
        Profile.objects.create(user=user, role=role, institution=institution, region=region)
        return user

    def payload(self, **changes):
        return {'kind':'grievance', 'category':'approval_delay', 'subject':'Please review the pending application',
                'description':'The institution requests an update on the pending approval application.',
                'submission_key':str(uuid.uuid4()), 'application_id':str(self.application.pk), **changes}

    def file(self):
        return SimpleUploadedFile('support.txt', b'Synthetic institution support document.')

    def create(self, files=None):
        return services.create(self.institute, self.payload(), [self.file()] if files is None else files)[0]

    def test_submission_bridge_receipt_and_duplicate_delivery(self):
        payload = self.payload()
        item, created = services.create(self.institute, payload, [self.file()])
        self.assertTrue(created)
        self.assertEqual(item.application, self.application)
        self.assertEqual(item.institution, self.application.institution)
        doc = item.documents.get()
        self.assertEqual(doc.state, 'pending')
        self.assertEqual(doc.size, len(b'Synthetic institution support document.'))
        same, created = services.create(self.institute, payload, [self.file()])
        self.assertFalse(created)
        self.assertEqual(same.pk, item.pk)
        self.assertEqual(Document.objects.count(), 1)
        with self.assertRaises(DomainError):
            services.create(self.institute, {**payload, 'subject':'Changed content using same key'}, [self.file()])
        self.assertTrue(AuditEvent.objects.filter(application=self.application, action='website_submitted').exists())

    def test_foreign_application_and_forged_institution_rejected(self):
        with self.assertRaises(DomainError):
            services.create(self.institute, self.payload(application_id=str(self.other_application.pk)), [])
        item, _ = services.create(self.institute, self.payload(institution_id=self.other_institution.pk), [])
        self.assertEqual(item.institution, self.institution)
        with self.assertRaises(DomainError):
            services.create(self.bureau, self.payload(), [])

    def test_list_detail_search_metadata_and_document_isolation(self):
        item = self.create()
        doc = item.documents.get()
        for actor in [self.other, self.south, self.reviewer]:
            self.client.force_login(actor)
            self.assertEqual(self.client.get('/website/api/submissions', {'search':item.reference}).json()['total'], 0)
            self.assertEqual(self.client.get(f'/website/api/submissions/{item.pk}').status_code, 404)
            self.assertEqual(self.client.get(f'/website/api/documents/{doc.pk}/download').status_code, 404)
        self.client.force_login(self.other)
        choices = self.client.get('/website/api/metadata').json()['applications']
        self.assertEqual([a['id'] for a in choices], [str(self.other_application.pk)])
        for actor in [self.bureau, self.regional, self.auditor, self.institute]:
            self.client.force_login(actor)
            self.assertEqual(self.client.get(f'/website/api/submissions/{item.pk}').status_code, 200)

    def test_grievance_response_resolution_and_reopening(self):
        item = self.create([])
        item = services.respond(self.bureau, item.pk, {'expected_version':1, 'status':'awaiting_institution', 'message':'Please provide a receipt.'})
        item = services.respond(self.institute, item.pk, {'expected_version':2, 'status':'awaiting_institution', 'message':'The receipt is now attached.'})
        self.assertEqual(item.status, 'in_review')
        item = services.respond(self.regional, item.pk, {'expected_version':3, 'status':'resolved', 'message':'The approval delay has been investigated and resolved.'})
        with self.assertRaises(DomainError):
            services.respond(self.institute, item.pk, {'expected_version':4, 'message':'Attempt to reopen by institution'})
        item = services.respond(self.bureau, item.pk, {'expected_version':4, 'status':'in_review', 'message':'Reopened following additional evidence.'})
        self.assertEqual(item.version, 5)
        self.assertEqual(item.messages.count(), 5)

    def test_stale_and_unauthorized_updates_do_not_change_record(self):
        item = self.create([])
        for user, data in [(self.bureau, {'expected_version':0, 'status':'resolved'}),
                           (self.institute, {'expected_version':1, 'status':'resolved'}),
                           (self.auditor, {'expected_version':1, 'status':'in_review'})]:
            with self.assertRaises(DomainError):
                services.respond(user, item.pk, {**data,'message':'Invalid update attempted'})
        item.refresh_from_db()
        self.assertEqual(item.status, 'submitted')
        self.assertEqual(item.messages.count(), 1)

    def test_file_formats_size_limits_and_no_partial_submission(self):
        for file in [SimpleUploadedFile('bad.exe', b'not a document'), SimpleUploadedFile('bad.pdf', b'not pdf'),
                     SimpleUploadedFile('bad.doc', b'not doc'), SimpleUploadedFile('bad.docx', b'not zip'),
                     SimpleUploadedFile('bad.txt', b'\x00'), SimpleUploadedFile('big.txt', b'x'*(5*1024*1024+1))]:
            with self.assertRaises(DomainError):
                services.create(self.institute, self.payload(), [self.file(),file])
        self.assertEqual(Grievance.objects.count(), 0)
        self.assertEqual(Document.objects.count(), 0)
        content = io.BytesIO()
        with zipfile.ZipFile(content,'w') as z:
            z.writestr('[Content_Types].xml','<Types/>')
            z.writestr('word/document.xml','<document/>')
        docx = SimpleUploadedFile('support.docx',content.getvalue())
        item = self.create([docx,SimpleUploadedFile('support.pdf',b'%PDF-1.4\nsynthetic fixture')])
        self.assertEqual(item.documents.count(), 2)

    def test_failure_after_staging_rolls_back_and_cleans_files(self):
        with patch('website.services.record', side_effect=RuntimeError('forced rollback')):
            with self.assertRaises(RuntimeError):
                self.create()
        self.assertEqual(Document.objects.count(), 0)
        self.assertEqual(Grievance.objects.count(), 0)
        self.assertFalse(any(p.is_file() for p in Path(self.temp.name).rglob('*')))

    def test_document_addition_requires_ownership_and_open_submission(self):
        item = self.create([])
        with self.assertRaises(DomainError): services.add_document(self.other,item.pk,self.file())
        with self.assertRaises(DomainError): services.add_document(self.bureau,item.pk,self.file())
        doc = services.add_document(self.institute,item.pk,self.file())
        item.refresh_from_db()
        self.assertEqual(doc.grievance,item)
        self.assertEqual(item.version,2)
        services.respond(self.bureau,item.pk,{'expected_version':2,'status':'resolved','message':'Resolved with the supplied document.'})
        with self.assertRaises(DomainError): services.add_document(self.institute,item.pk,self.file())

    def test_upload_failure_retries_same_drive_id_and_preserves_receipt(self):
        doc = self.create().documents.get()
        received = doc.received_at
        path = Path(doc.staged_file.path)
        with patch('website.tasks.DriveClient') as adapter:
            adapter.return_value.reserve_id.return_value = 'generated_file_123'
            adapter.return_value.upload.side_effect = drive.DriveError('Temporary failure')
            upload_document(str(doc.pk))
            doc.refresh_from_db()
            self.assertEqual(doc.state,'pending')
            self.assertEqual(doc.drive_file_id,'generated_file_123')
            self.assertTrue(path.exists())
            self.assertEqual(doc.received_at,received)
            adapter.return_value.upload.side_effect = None
            Document.objects.filter(pk=doc.pk).update(available_at=timezone.now()-timedelta(seconds=1))
            upload_document(str(doc.pk))
            doc.refresh_from_db()
            self.assertEqual(doc.state,'stored')
            self.assertFalse(path.exists())
            self.assertEqual(doc.staged_file.name,'')
            self.assertEqual(adapter.return_value.reserve_id.call_count,1)
            upload_document(str(doc.pk))
            self.assertEqual(adapter.return_value.upload.call_count,2)

    def test_live_lease_skipped_expired_lease_recovered_and_failed_job_manual_retry(self):
        doc = self.create().documents.get()
        Document.objects.filter(pk=doc.pk).update(state='uploading',lease_until=timezone.now()+timedelta(minutes=1))
        with patch('website.tasks.DriveClient') as adapter:
            upload_document(str(doc.pk));adapter.assert_not_called()
            Document.objects.filter(pk=doc.pk).update(lease_until=timezone.now()-timedelta(seconds=1),attempts=7)
            adapter.side_effect=drive.DriveError('Unavailable')
            upload_document(str(doc.pk))
        doc.refresh_from_db();self.assertEqual(doc.state,'failed')
        self.client.force_login(self.bureau)
        self.assertEqual(self.client.post('/website/api/storage',data=json.dumps({'document_id':str(doc.pk)}),content_type='application/json').status_code,200)
        doc.refresh_from_db();self.assertEqual(doc.state,'pending');self.assertEqual(doc.attempts,0)

    def test_download_from_drive_is_authorized_audited_and_attachment_only(self):
        doc = self.create().documents.get()
        self.client.force_login(self.bureau)
        url=f'/website/api/documents/{doc.pk}/download'
        self.assertEqual(self.client.get(url).status_code,409)
        Document.objects.filter(pk=doc.pk).update(state='stored',drive_file_id='drive_example')
        with patch('website.views.drive.DriveClient') as adapter:
            adapter.return_value.download.return_value=b'Synthetic institution support document.'
            result=self.client.get(url)
            self.assertEqual(result.status_code,200)
            self.assertIn('attachment;',result['Content-Disposition'])
            self.assertEqual(b''.join(result.streaming_content),b'Synthetic institution support document.')
        self.assertTrue(AuditEvent.objects.filter(action='website_document_downloaded').exists())

    def test_drive_adapter_deduplicates_and_checks_integrity(self):
        doc = self.create().documents.get()
        doc.drive_file_id='file_existing';doc.drive_folder_id='folder_example'
        adapter=drive.DriveClient('synthetic-token')
        meta={'id':doc.drive_file_id,'size':str(doc.size),'md5Checksum':doc.md5,'parents':[doc.drive_folder_id]}
        with patch.object(adapter,'request',return_value=meta) as call:
            adapter.upload(doc)
            self.assertEqual(call.call_count,1)
            self.assertEqual(call.call_args.args[0],'GET')
        for changed in [{**meta,'parents':['wrong_folder']},{**meta,'md5Checksum':'tampered'},{**meta,'trashed':True}]:
            with self.assertRaises(drive.DriveError): adapter.verify(doc,changed)
        with patch.object(adapter,'metadata',return_value=meta),patch.object(adapter,'request',return_value=b'tampered'):
            with self.assertRaises(drive.DriveError): adapter.download(doc)

    def test_anonymous_csrf_and_storage_permissions(self):
        self.assertEqual(self.client.get('/website/api/submissions').status_code,401)
        client=Client(enforce_csrf_checks=True);client.force_login(self.institute)
        self.assertEqual(client.post('/website/api/submissions',self.payload()).status_code,403)
        self.client.force_login(self.institute)
        self.assertEqual(self.client.get('/website/api/storage').status_code,403)
        self.assertEqual(self.client.post('/website/api/drive/connect').status_code,403)

    def test_oauth_state_validation_and_no_credentials_exposed(self):
        self.client.force_login(self.bureau)
        self.assertEqual(self.client.post('/website/api/drive/connect').status_code,503)
        Path(self.temp.name,'client.json').write_text(json.dumps({'web':{'client_id':'public-client','client_secret':'private-secret'}}))
        result=self.client.post('/website/api/drive/connect')
        self.assertEqual(result.status_code,200)
        self.assertNotIn('private-secret',result.content.decode())
        self.assertEqual(self.client.get('/website/api/drive/callback',{'code':'fake','state':'forged'}).status_code,400)
        self.client.post('/website/api/drive/connect')
        state=self.client.session['drive_oauth']['state']
        with patch('website.views.drive.exchange_code') as exchange:
            result=self.client.get('/website/api/drive/callback',{'code':'fake','state':state})
            self.assertEqual(result.status_code,302)
            exchange.assert_called_once_with('fake')
            self.assertEqual(self.client.get('/website/api/drive/callback',{'code':'fake','state':state}).status_code,400)
