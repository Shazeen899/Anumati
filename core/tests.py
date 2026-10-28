import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch
from django.test import TestCase, TransactionTestCase, Client, override_settings
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection, connections, close_old_connections, transaction, DatabaseError
from django.utils import timezone
from .models import *
from .services import command, create_case, DomainError, advance, make_deadline, review_evidence
from .tasks import scan_deadlines, deliver_event, scan_file

class Fixtures:
    def build_fixtures(self):
        self.i1=Institution.objects.create(name='Test A',code='A',region='North')
        self.i2=Institution.objects.create(name='Test B',code='B',region='South')
        self.users={}
        for role in ['institution','reviewer','evc','committee','regional','bureau','executive','council','policy','auditor','operator']:
            u=User.objects.create_user(role,password='Long-test-password',email=role+'@example.test')
            Profile.objects.create(user=u,role=role,institution=self.i1 if role=='institution' else None,region='North' if role=='regional' else '')
            self.users[role]=u
        self.other=User.objects.create_user('other',password='Long-test-password')
        Profile.objects.create(user=self.other,role='institution',institution=self.i2,region='South')
        self.policy=Policy.objects.create(name='Test',version=1,state='active',source='Synthetic test',rules={'routes':{'name_change':['SCRUTINY','DECISION','RATIFICATION'],'new_institution':['SCRUTINY','EVC','DECISION','RATIFICATION'],'evc_only':['EVC','DECISION','RATIFICATION']},'stage_days':7,'reminder_days':3})
        self.case=create_case(self.users['institution'],{'category':'name_change','external_id':'TEST-1','academic_year':'2026-2027'})

    def cmd(self,role,action,case=None,**extra):
        c=case or self.case
        c.refresh_from_db()
        return command(self.users[role],c.pk,{'action':action,'expected_version':c.version,'reason':'Substantive test finding and reason',**extra},str(uuid.uuid4()))

@override_settings(DEMO_MODE=True,EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class WorkflowTests(Fixtures,TestCase):
    def setUp(self):self.build_fixtures()

    def test_full_route_decision_and_ratification(self):
        self.cmd('institution','submit')
        self.assertEqual(Application.objects.get(pk=self.case.pk).stage,'SCRUTINY')
        self.cmd('reviewer','complete_stage')
        self.cmd('executive','approve')
        self.assertEqual(Application.objects.get(pk=self.case.pk).stage,'RATIFICATION')
        self.cmd('council','ratify')
        self.case.refresh_from_db()
        self.assertEqual(self.case.stage,'APPROVED')
        self.assertEqual(self.case.decisions.count(),3)
        self.assertEqual(self.case.audit.filter(action='ratify').count(),1)
        self.assertFalse(self.case.deadlines.filter(kind='stage',active=True).exists())
        self.assertTrue(Outbox.objects.filter(application=self.case).exists())

    def test_cannot_skip_stages_or_use_wrong_role(self):
        self.cmd('institution','submit')
        for role,action in [('executive','approve'),('institution','complete_stage'),('operator','approve')]:
            with self.assertRaises(DomainError):self.cmd(role,action)
        self.case.refresh_from_db()
        self.assertEqual(self.case.stage,'SCRUTINY')
        self.assertFalse(self.case.decisions.exists())

    def test_duplicate_and_changed_payload_idempotency(self):
        data={'action':'submit','expected_version':1,'reason':'Submit synthetic application'}
        one=command(self.users['institution'],self.case.pk,data,'same-key')
        two=command(self.users['institution'],self.case.pk,data,'same-key')
        self.assertEqual(one,two)
        self.assertEqual(self.case.audit.filter(action='submit').count(),1)
        with self.assertRaises(DomainError) as e:command(self.users['institution'],self.case.pk,{**data,'reason':'different reason'},'same-key')
        self.assertEqual(e.exception.status,409)

    def test_stale_version_conflicts(self):
        self.cmd('institution','submit')
        with self.assertRaises(DomainError) as e:
            command(self.users['reviewer'],self.case.pk,{'action':'complete_stage','expected_version':1,'reason':'Old browser tab'},'stale')
        self.assertEqual(e.exception.status,409)

    def test_transaction_failure_rolls_back_everything(self):
        self.cmd('institution','submit')
        self.case.refresh_from_db();version=self.case.version;events=self.case.audit.count();outbox=Outbox.objects.count()
        with patch('core.services.notify',side_effect=RuntimeError('simulated persistence failure')):
            with self.assertRaises(RuntimeError):self.cmd('reviewer','complete_stage')
        self.case.refresh_from_db()
        self.assertEqual(self.case.version,version)
        self.assertEqual(self.case.stage,'SCRUTINY')
        self.assertEqual(self.case.audit.count(),events)
        self.assertEqual(Outbox.objects.count(),outbox)
        self.assertFalse(self.case.decisions.exists())

    def test_repeated_deficiency_cycles_preserve_originals_and_clock(self):
        self.cmd('institution','submit')
        deadline=self.case.deadlines.get(active=True).due_at
        for n in range(2):
            self.cmd('reviewer','return',requirement=f'CLAUSE-{n}')
            self.cmd('institution','resubmit',reason=f'Evidence corrected for original finding {n}')
            with self.assertRaises(DomainError):self.cmd('reviewer','complete_stage')
            d=self.case.deficiencies.get(resolved=False)
            self.cmd('reviewer','resolve_deficiency',deficiency_id=d.pk)
        self.assertEqual(self.case.deficiencies.count(),2)
        self.assertEqual(self.case.deadlines.get(active=True).due_at,deadline)
        self.cmd('reviewer','complete_stage')

    def test_new_institution_prerequisite(self):
        self.case.category='new_institution';self.case.save()
        with self.assertRaises(DomainError):self.cmd('institution','submit')
        self.cmd('regional','verify_payment')
        self.cmd('institution','submit')
        self.assertEqual(Application.objects.get(pk=self.case.pk).route,['SCRUTINY','EVC','DECISION','RATIFICATION'])

    def test_missing_policy_becomes_owned_exception(self):
        self.case.category='unknown_category';self.case.save()
        self.cmd('institution','submit')
        self.case.refresh_from_db()
        self.assertEqual(self.case.stage,'EXCEPTION')
        self.assertEqual(self.case.assigned_to,self.users['regional'])

    def test_import_replay_and_payload_conflict(self):
        same=create_case(self.users['institution'],{'category':'name_change','external_id':'TEST-1','academic_year':'2026-2027'})
        self.assertEqual(same.pk,self.case.pk)
        with self.assertRaises(DomainError):create_case(self.users['institution'],{'category':'closure','external_id':'TEST-1','academic_year':'2026-2027'})

    def test_appeal_is_separate_human_process(self):
        self.cmd('institution','submit');self.cmd('reviewer','complete_stage');self.cmd('executive','reject');self.cmd('council','ratify')
        self.cmd('institution','appeal');self.cmd('committee','complete_stage');self.cmd('council','appeal_approve')
        self.case.refresh_from_db()
        self.assertEqual(self.case.stage,'APPROVED');self.assertTrue(self.case.appeal_used)
        self.assertTrue(self.case.decisions.filter(kind='executive',outcome='rejected').exists())
        with self.assertRaises(DomainError):self.cmd('institution','appeal')

    def test_evc_conflict_report_and_completion_guard(self):
        self.case.category='evc_only';self.case.save();self.cmd('institution','submit')
        start=timezone.now()+timedelta(days=1);end=start+timedelta(hours=2)
        self.cmd('evc','schedule_evc',starts_at=start.isoformat(),ends_at=end.isoformat())
        with self.assertRaises(DomainError):self.cmd('evc','complete_stage')
        with self.assertRaises(DomainError):self.cmd('evc','conduct_evc',representatives='Two authorised representatives attended')
        other=create_case(self.users['institution'],{'category':'evc_only','external_id':'EVC-2','academic_year':'2026-2027'})
        self.cmd('institution','submit',case=other)
        with self.assertRaises(DomainError):self.cmd('evc','schedule_evc',case=other,starts_at=start.isoformat(),ends_at=end.isoformat())
        self.case.visits.update(starts_at=timezone.now()-timedelta(hours=1))
        self.cmd('evc','conduct_evc',representatives='Two authorised applicant representatives attended')
        self.cmd('evc','report_evc',report='The synthetic facilities were reviewed against approved intake. All demonstration checks completed.')
        self.cmd('evc','complete_stage')
        self.case.refresh_from_db();self.assertEqual(self.case.stage,'DECISION')

    def test_audit_update_and_delete_denied_by_database(self):
        event=self.case.audit.first()
        for operation in [lambda:AuditEvent.objects.filter(pk=event.pk).update(reason='rewrite'),lambda:AuditEvent.objects.filter(pk=event.pk).delete()]:
            with self.assertRaises(DatabaseError):
                with transaction.atomic():operation()

@override_settings(DEMO_MODE=True,EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class AccessAndEvidenceTests(Fixtures,TestCase):
    def setUp(self):self.build_fixtures();self.client.force_login(self.users['institution'])
    def test_institution_isolation_list_detail_search_export_and_file(self):
        other=create_case(self.other,{'category':'name_change','external_id':'PRIVATE-B','academic_year':'2026-2027'})
        e=Evidence.objects.create(application=other,file='not-real',name='secret.txt',checksum='a'*64,size=3,uploaded_by=self.other,safety='demo_validated')
        self.assertEqual(self.client.get(f'/api/applications/{other.pk}').status_code,404)
        self.assertEqual(self.client.get(f'/api/evidence/{e.pk}/download').status_code,404)
        result=self.client.get('/api/applications?search=PRIVATE-B').json()
        self.assertEqual(result['total'],0)
        self.assertNotIn('PRIVATE-B',self.client.get('/api/reports').content.decode())
        self.assertNotIn('Test B',self.client.get('/api/reports').content.decode())

    def test_unassigned_reviewer_and_forged_role_denied(self):
        self.cmd('institution','submit')
        u=User.objects.create_user('unassigned');Profile.objects.create(user=u,role='reviewer')
        self.client.force_login(u)
        self.assertEqual(self.client.get(f'/api/applications/{self.case.pk}').status_code,404)
        self.client.force_login(self.users['institution'])
        result=self.client.post(f'/api/applications/{self.case.pk}/commands',data=json.dumps({'action':'approve','expected_version':2,'reason':'Forged executive action','role':'executive'}),content_type='application/json',HTTP_IDEMPOTENCY_KEY='forged')
        self.assertEqual(result.status_code,403)

    def test_sessions_require_csrf(self):
        client=Client(enforce_csrf_checks=True)
        self.assertEqual(client.post('/api/login',data='{}',content_type='application/json').status_code,403)
        self.assertEqual(client.get('/api/applications').status_code,401)
        client.get('/api/session');self.assertIn('csrftoken',client.cookies)

    def test_login_rate_limit(self):
        self.client.logout()
        for _ in range(10):self.assertEqual(self.client.post('/api/login',data=json.dumps({'username':'bad','password':'wrong'}),content_type='application/json').status_code,401)
        self.assertEqual(self.client.post('/api/login',data=json.dumps({'username':'bad','password':'wrong'}),content_type='application/json').status_code,429)

    def obligation(self,kind='undertaking'):
        o=Obligation.objects.create(application=self.case,title='Evidence requirement',kind=kind,source='test',verifier=self.users['reviewer'])
        make_deadline(self.case,'Evidence due','submission',timezone.now()+timedelta(days=1),self.users['institution'],o)
        return o

    def upload(self,o,content=b'Synthetic evidence for verification.',name='evidence.txt',**fields):
        self.case.refresh_from_db()
        response=self.client.post(f'/api/applications/{self.case.pk}/evidence',{'file':SimpleUploadedFile(name,content),'obligation_id':o.pk,'expected_version':self.case.version,**fields})
        self.assertEqual(response.status_code,201,response.content)
        e=Evidence.objects.get(pk=response.json()['evidence']['id'])
        self.addCleanup(lambda:e.file.delete(save=False))
        return e

    def test_receipt_scan_review_and_independent_lifecycle(self):
        o=self.obligation();e=self.upload(o);received=e.received_at
        self.assertEqual(self.client.get(f'/api/evidence/{e.pk}/download').status_code,409)
        o.refresh_from_db();self.assertEqual(o.status,'submitted')
        scan_file(str(e.pk));e.refresh_from_db();self.assertEqual(e.safety,'demo_validated')
        self.assertEqual(e.received_at,received)
        self.case.refresh_from_db()
        review_evidence(self.users['reviewer'],e.pk,{'outcome':'accepted','expected_version':self.case.version,'reason':'Reviewed exact synthetic evidence version'})
        o.refresh_from_db();self.assertEqual(o.status,'fulfilled');self.assertEqual(o.accepted_evidence_id,e.pk)
        self.case.refresh_from_db();self.assertEqual(self.case.stage,'DRAFT')
        response=self.client.get(f'/api/evidence/{e.pk}/download')
        self.assertEqual(response.status_code,200)
        self.assertEqual(b''.join(response.streaming_content), b'Synthetic evidence for verification.')

    def test_malicious_and_pdf_files_fail_closed(self):
        o=self.obligation();e=self.upload(o,b'EICAR-STANDARD-ANTIVIRUS-TEST-FILE')
        scan_file(str(e.pk));e.refresh_from_db();self.assertEqual(e.safety,'rejected')
        self.assertEqual(self.client.get(f'/api/evidence/{e.pk}/download').status_code,409)
        pdf=self.upload(o,b'%PDF-1.4 demo',name='evidence.pdf')
        scan_file(str(pdf.pk));pdf.refresh_from_db();self.assertEqual(pdf.safety,'quarantine')

    def test_renewal_submission_does_not_replace_accepted_validity(self):
        o=self.obligation('lease');o.valid_until=timezone.localdate()-timedelta(days=2);o.status='fulfilled';o.save()
        e=self.upload(o,valid_from='2026-01-01',valid_until='2027-01-01')
        scan_file(str(e.pk));o.refresh_from_db();self.assertLess(o.valid_until,timezone.localdate())
        self.case.refresh_from_db()
        with self.assertRaises(DomainError):review_evidence(self.users['reviewer'],e.pk,{'outcome':'accepted','expected_version':self.case.version,'reason':'Checking insufficient remaining lease term'})
        o.refresh_from_db();self.assertLess(o.valid_until,timezone.localdate())

    def test_policy_preview_read_only_and_invalid_activation(self):
        self.client.force_login(self.users['policy']);n=AuditEvent.objects.count()
        r=self.client.post('/api/policies',data=json.dumps({'id':self.policy.pk,'action':'preview','reason':'Test dry run'}),content_type='application/json')
        self.assertEqual(r.status_code,200);self.assertEqual(n,AuditEvent.objects.count())
        r=self.client.post('/api/policies',data=json.dumps({'action':'draft','version':2,'source':'test','reason':'Invalid policy test','rules':{'routes':{'bad':['APPROVED']},'stage_days':7,'reminder_days':3}}),content_type='application/json')
        self.assertEqual(r.status_code,400)

@override_settings(DEMO_MODE=True,EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class BackgroundTests(Fixtures,TestCase):
    def setUp(self):self.build_fixtures();self.cmd('institution','submit')
    def test_scheduler_catches_up_and_deduplicates(self):
        self.case.deadlines.update(due_at=timezone.now()-timedelta(days=2))
        scan_deadlines();scan_deadlines()
        self.assertEqual(Escalation.objects.count(),1)
        self.assertEqual(Outbox.objects.filter(deadline__isnull=False).count(),1)
        event=Outbox.objects.filter(deadline__isnull=False).first()
        deliver_event(str(event.pk));deliver_event(str(event.pk))
        self.assertEqual(Notification.objects.filter(event=event).count(),1)

    def test_extended_deadline_supersedes_queued_alert(self):
        d=self.case.deadlines.get(active=True);d.due_at=timezone.now()-timedelta(days=1);d.save()
        scan_deadlines();event=Outbox.objects.get(deadline=d)
        self.cmd('bureau','extend_deadline',deadline_id=d.pk,due_at=(timezone.now()+timedelta(days=20)).isoformat())
        self.assertEqual(deliver_event(str(event.pk)),'superseded')
        self.assertFalse(Notification.objects.filter(event=event).exists())

    def test_mail_failure_does_not_erase_decision_and_retries_same_identity(self):
        self.cmd('reviewer','complete_stage');self.cmd('executive','approve')
        event=Outbox.objects.order_by('-created_at').first()
        with patch('core.tasks.send_mail',side_effect=OSError('sink unavailable')):
            deliver_event(str(event.pk))
        event.refresh_from_db();self.assertEqual(event.state,'pending');self.assertEqual(event.attempts,1)
        event.available_at=timezone.now();event.save();deliver_event(str(event.pk))
        self.assertEqual(Notification.objects.filter(event=event).count(),1)
        self.assertTrue(self.case.decisions.filter(kind='executive',outcome='approved').exists())

    def test_expired_worker_lease_is_recoverable(self):
        event=Outbox.objects.first();event.state='processing';event.lease_until=timezone.now()-timedelta(minutes=1);event.save()
        self.assertEqual(deliver_event(str(event.pk)),'completed')

@override_settings(DEMO_MODE=True,EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class ConcurrencyTests(Fixtures,TransactionTestCase):
    def setUp(self):self.build_fixtures()
    def test_100_concurrent_final_decisions_have_one_winner(self):
        self.cmd('institution','submit');self.cmd('reviewer','complete_stage');self.case.refresh_from_db()
        case_id,version,user_id=self.case.pk,self.case.version,self.users['executive'].pk
        def run(i):
            close_old_connections()
            try:
                u=User.objects.get(pk=user_id)
                return command(u,case_id,{'action':'approve' if i%2 else 'reject','expected_version':version,'reason':'Concurrent authority command test'},f'concurrent-{i}')['stage']
            except DomainError as e:return e.status
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=10) as pool:results=list(pool.map(run,range(100)))
        self.assertEqual(results.count('RATIFICATION'),1)
        self.assertEqual(results.count(409),99)
        self.assertEqual(Decision.objects.filter(application_id=case_id,kind='executive').count(),1)
        self.assertEqual(AuditEvent.objects.filter(application_id=case_id,action__in=['approve','reject']).count(),1)
