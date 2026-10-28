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
from .services import command, create_case, DomainError, advance, make_deadline, review_evidence, security_review_evidence, permitted_audit_events, resolve_exception, migrate_policy, get_obligation_lifecycle
from .tasks import upload_pending_evidence
from .aicte_adapter import AICTEAdapter, AICTEIntegrationError
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


@override_settings(DEMO_MODE=True,EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class AuditConfidentialityTests(Fixtures, TestCase):
    def setUp(self):
        self.build_fixtures()
        self.general_g = AuditEvent.objects.create(
            action='website_submitted',
            scope_type='grievance',
            institution=self.i1,
            application=None,
            actor=self.users['institution'],
            reason='General grievance submission'
        )
        self.linked_g = AuditEvent.objects.create(
            action='website_submitted',
            scope_type='grievance',
            institution=self.i1,
            application=self.case,
            actor=self.users['institution'],
            reason='Linked grievance submission'
        )
        self.app_event = AuditEvent.objects.create(
            action='submit',
            scope_type='application',
            institution=self.i1,
            application=self.case,
            actor=self.users['institution'],
            reason='Application submitted'
        )

    def test_grievance_audit_isolated_from_non_grievance_roles(self):
        for role in ['policy', 'operator', 'reviewer', 'committee', 'evc']:
            qs = permitted_audit_events(self.users[role])
            self.assertFalse(qs.filter(scope_type='grievance').exists(), f"Role {role} should not see grievance audit events")

    def test_cross_institution_isolation(self):
        other_qs = permitted_audit_events(self.other)
        self.assertFalse(other_qs.filter(institution=self.i1).exists())
        self.assertNotIn(self.general_g, other_qs)
        self.assertNotIn(self.linked_g, other_qs)

    def test_regional_isolation(self):
        north_qs = permitted_audit_events(self.users['regional'])
        self.assertIn(self.general_g, north_qs)
        self.assertIn(self.linked_g, north_qs)

        south_user = User.objects.create_user('south_reg', password='test-password')
        Profile.objects.create(user=south_user, role='regional', region='South')
        south_qs = permitted_audit_events(south_user)
        self.assertNotIn(self.general_g, south_qs)
        self.assertNotIn(self.linked_g, south_qs)

    def test_bureau_and_auditor_access(self):
        for role in ['bureau', 'auditor']:
            qs = permitted_audit_events(self.users[role])
            self.assertIn(self.general_g, qs)
            self.assertIn(self.linked_g, qs)

    def test_institution_own_grievance_audit(self):
        inst_qs = permitted_audit_events(self.users['institution'])
        self.assertIn(self.general_g, inst_qs)
        self.assertIn(self.linked_g, inst_qs)

    def test_audit_api_endpoint_scoping(self):
        self.client.force_login(self.users['reviewer'])
        res = self.client.get('/api/audit')
        self.assertEqual(res.status_code, 200)
        items = res.json()['items']
        item_ids = [item['id'] for item in items]
        self.assertNotIn(self.general_g.pk, item_ids)
        self.assertNotIn(self.linked_g.pk, item_ids)


@override_settings(DEMO_MODE=True,EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class DeadlineResponsibilityTests(Fixtures, TestCase):
    def setUp(self):
        self.build_fixtures()
        self.cmd('institution', 'submit')

    def test_deadline_ownership_transfer_on_return_and_resubmit(self):
        self.case.refresh_from_db()
        stage_dl = self.case.deadlines.get(kind='stage', active=True)
        self.assertEqual(stage_dl.owner, self.users['reviewer'])

        outbox_alert = Outbox.objects.create(
            application=self.case,
            recipient=self.users['reviewer'],
            identity=f'reminder-{uuid.uuid4()}',
            title='Approaching deadline',
            body='Reviewer deadline alert',
            deadline=stage_dl,
            revision=stage_dl.revision,
            state='pending'
        )

        self.cmd('reviewer', 'return', requirement='CLAUSE-12A')
        self.case.refresh_from_db()
        self.assertEqual(self.case.stage, 'RETURNED')
        self.assertEqual(self.case.assigned_to, self.users['institution'])

        stage_dl.refresh_from_db()
        self.assertEqual(stage_dl.owner, self.users['institution'])
        self.assertEqual(stage_dl.owner, self.users['institution'])

        outbox_alert.refresh_from_db()
        self.assertEqual(outbox_alert.state, 'superseded')
        self.assertEqual(deliver_event(str(outbox_alert.pk)), 'superseded')

        inst_alert = Outbox.objects.create(
            application=self.case,
            recipient=self.users['institution'],
            identity=f'reminder-{uuid.uuid4()}',
            title='Approaching deadline',
            body='Institution deadline alert',
            deadline=stage_dl,
            revision=stage_dl.revision,
            state='pending'
        )

        self.cmd('institution', 'resubmit', reason='Clarification and revised documents provided')
        self.case.refresh_from_db()
        self.assertEqual(self.case.stage, 'SCRUTINY')
        self.assertEqual(self.case.assigned_to, self.users['reviewer'])

        stage_dl.refresh_from_db()
        self.assertEqual(stage_dl.owner, self.users['reviewer'])
        self.assertEqual(stage_dl.owner, self.users['reviewer'])

        inst_alert.refresh_from_db()
        self.assertEqual(inst_alert.state, 'superseded')
        self.assertEqual(deliver_event(str(inst_alert.pk)), 'superseded')

    def test_reassignment_transfers_deadline_owner_and_supersedes_outbox(self):
        new_reviewer = User.objects.create_user('reviewer2', password='test-password')
        Profile.objects.create(user=new_reviewer, role='reviewer')

        self.case.refresh_from_db()
        stage_dl = self.case.deadlines.get(kind='stage', active=True)
        outbox_alert = Outbox.objects.create(
            application=self.case,
            recipient=self.users['reviewer'],
            identity=f'reminder-{uuid.uuid4()}',
            title='Approaching deadline',
            body='Reviewer deadline alert',
            deadline=stage_dl,
            revision=stage_dl.revision,
            state='pending'
        )

        self.cmd('regional', 'reassign', user_id=new_reviewer.pk)
        self.case.refresh_from_db()
        self.assertEqual(self.case.assigned_to, new_reviewer)
        stage_dl.refresh_from_db()
        self.assertEqual(stage_dl.owner, new_reviewer)

        outbox_alert.refresh_from_db()
        self.assertEqual(outbox_alert.state, 'superseded')


@override_settings(DEMO_MODE=True,EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class ConfigurableRoutingAndGovernanceTests(Fixtures, TestCase):
    def setUp(self):
        self.build_fixtures()
        self.policy.rules['routes']['with_sac'] = ['SCRUTINY', 'EVC', 'SAC', 'DECISION', 'RATIFICATION']
        self.policy.rules['routes']['scrutiny_only'] = ['SCRUTINY', 'DECISION', 'RATIFICATION']
        self.policy.save()

    def test_route_with_sac_progression(self):
        case = create_case(self.users['institution'], {'category': 'with_sac', 'external_id': 'SAC-TEST-1', 'academic_year': '2026-2027'})
        self.cmd('institution', 'submit', case=case)
        case.refresh_from_db()
        self.assertEqual(case.stage, 'SCRUTINY')
        self.cmd('reviewer', 'complete_stage', case=case)
        case.refresh_from_db()
        self.assertEqual(case.stage, 'EVC')

        start = timezone.now() + timedelta(days=1)
        end = start + timedelta(hours=2)
        self.cmd('evc', 'schedule_evc', case=case, starts_at=start.isoformat(), ends_at=end.isoformat())
        case.visits.update(starts_at=timezone.now() - timedelta(hours=1))
        self.cmd('evc', 'conduct_evc', case=case, representatives='Reps present')
        self.cmd('evc', 'report_evc', case=case, report='Inspection conducted according to approved criteria.')
        self.cmd('evc', 'complete_stage', case=case)

        case.refresh_from_db()
        self.assertEqual(case.stage, 'SAC')
        self.assertEqual(case.assigned_to.profile.role, 'committee')

        self.cmd('committee', 'complete_stage', case=case)
        case.refresh_from_db()
        self.assertEqual(case.stage, 'DECISION')
        self.assertEqual(case.assigned_to.profile.role, 'executive')

    def test_exception_recovery_workflow(self):
        case = create_case(self.users['institution'], {'category': 'unsupported_cat', 'external_id': 'EXC-1', 'academic_year': '2026-2027'})
        self.cmd('institution', 'submit', case=case)
        case.refresh_from_db()
        self.assertEqual(case.stage, 'EXCEPTION')
        self.assertEqual(case.assigned_to, self.users['regional'])

        resolve_exception(self.users['regional'], case.pk, {'category': 'name_change', 'reason': 'Administrative review corrected category'})
        case.refresh_from_db()
        self.assertEqual(case.stage, 'SCRUTINY')
        self.assertEqual(case.category, 'name_change')
        self.assertTrue(case.audit.filter(action='exception_resolved').exists())

    def test_audited_policy_migration(self):
        self.cmd('institution', 'submit')
        self.case.refresh_from_db()
        self.assertEqual(self.case.policy, self.policy)
        new_pol = Policy.objects.create(
            name='Test Policy v2',
            version=2,
            state='active',
            source='Updated regulations',
            rules={'routes': {'name_change': ['SCRUTINY', 'DECISION', 'RATIFICATION']}, 'stage_days': 10, 'reminder_days': 4}
        )
        migrate_policy(self.users['bureau'], self.case.pk, {'policy_id': new_pol.pk, 'reason': 'Council approved migration to v2'})
        self.case.refresh_from_db()
        self.assertEqual(self.case.policy, new_pol)
        self.assertTrue(self.case.audit.filter(action='policy_migrated').exists())


@override_settings(DEMO_MODE=True,EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class EVCLifecycleAndDeadlinesTests(Fixtures, TestCase):
    def setUp(self):
        self.build_fixtures()
        self.case.category = 'evc_only'
        self.case.save()
        self.cmd('institution', 'submit')
        self.case.refresh_from_db()

    def test_evc_deadlines_and_late_scheduled_detection(self):
        self.assertTrue(self.case.deadlines.filter(kind='evc_scheduling', active=True).exists())

        start = timezone.now() + timedelta(days=20)
        end = start + timedelta(hours=2)
        self.cmd('evc', 'schedule_evc', starts_at=start.isoformat(), ends_at=end.isoformat())
        self.case.refresh_from_db()
        visit = self.case.visits.get(status='confirmed')
        self.assertTrue(visit.late_scheduled)
        self.assertFalse(self.case.deadlines.filter(kind='evc_scheduling', active=True).exists())
        self.assertTrue(self.case.deadlines.filter(kind='evc_visit', active=True).exists())

    def test_evc_reschedule_and_cancellation(self):
        start = timezone.now() + timedelta(days=2)
        end = start + timedelta(hours=2)
        self.cmd('evc', 'schedule_evc', starts_at=start.isoformat(), ends_at=end.isoformat())

        new_start = timezone.now() + timedelta(days=3)
        new_end = new_start + timedelta(hours=2)
        self.cmd('evc', 'reschedule_evc', starts_at=new_start.isoformat(), ends_at=new_end.isoformat(), reason='Severe weather alert')
        visit = self.case.visits.get(status='confirmed')
        self.assertEqual(visit.starts_at, new_start)

        self.cmd('evc', 'cancel_evc', reason='Emergency institutional shutdown')
        visit.refresh_from_db()
        self.assertEqual(visit.status, 'cancelled')
        self.assertEqual(visit.cancellation_reason, 'Emergency institutional shutdown')

    def test_evc_conduct_and_report_lifecycle(self):
        start = timezone.now() + timedelta(days=1)
        end = start + timedelta(hours=2)
        self.cmd('evc', 'schedule_evc', starts_at=start.isoformat(), ends_at=end.isoformat())
        self.case.visits.update(starts_at=timezone.now() - timedelta(hours=1))
        self.cmd('evc', 'conduct_evc', representatives='Principal and Registrar attended')
        visit = self.case.visits.get()
        self.assertEqual(visit.status, 'conducted')
        self.assertTrue(self.case.deadlines.filter(kind='evc_report', active=True).exists())

        self.cmd('evc', 'report_evc', report='All infrastructural requirements verified on site.')
        visit.refresh_from_db()
        self.assertIsNotNone(visit.report_submitted_at)
        self.assertIn('All infrastructural', visit.report)


@override_settings(DEMO_MODE=True,EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class SecurityReviewAndEvidenceStorageTests(Fixtures, TestCase):
    def setUp(self):
        self.build_fixtures()
        self.client.force_login(self.users['institution'])
        self.obligation = Obligation.objects.create(
            application=self.case, title='Safety Doc', kind='undertaking', source='rule', verifier=self.users['reviewer']
        )
        make_deadline(self.case, 'Evidence due', 'submission', timezone.now() + timedelta(days=1), self.users['institution'], self.obligation)

    def test_pdf_quarantine_manual_security_review_and_acceptance(self):
        self.cmd('institution', 'submit')
        self.case.refresh_from_db()
        res = self.client.post(
            f'/api/applications/{self.case.pk}/evidence',
            {'file': SimpleUploadedFile('safety.pdf', b'%PDF-1.5 test document'), 'obligation_id': self.obligation.pk, 'expected_version': self.case.version}
        )
        self.assertEqual(res.status_code, 201)
        ev_id = res.json()['evidence']['id']
        ev = Evidence.objects.get(pk=ev_id)
        self.addCleanup(lambda: ev.file.delete(save=False))

        scan_file(str(ev.pk))
        ev.refresh_from_db()
        self.assertEqual(ev.safety, 'quarantine')

        with self.assertRaises(DomainError):
            review_evidence(self.users['reviewer'], ev.pk, {'outcome': 'accepted', 'expected_version': self.case.version, 'reason': 'Premature review'})

        with self.assertRaises(DomainError):
            security_review_evidence(self.users['institution'], ev.pk, {'decision': 'release', 'reason': 'Self review'})

        security_review_evidence(self.users['reviewer'], ev.pk, {'decision': 'release', 'reason': 'Manual inspection and hash check clean'})
        ev.refresh_from_db()
        self.assertEqual(ev.safety, 'security_reviewed')
        self.assertEqual(ev.security_reviewer, self.users['reviewer'])
        self.assertIsNotNone(ev.security_reviewed_at)

        self.case.refresh_from_db()
        review_evidence(self.users['reviewer'], ev.pk, {'outcome': 'accepted', 'expected_version': self.case.version, 'reason': 'Substantive compliance verified'})
        self.obligation.refresh_from_db()
        self.assertEqual(self.obligation.status, 'fulfilled')

    def test_security_review_rejection(self):
        res = self.client.post(
            f'/api/applications/{self.case.pk}/evidence',
            {'file': SimpleUploadedFile('unsafe.docx', b'fake docx bytes'), 'obligation_id': self.obligation.pk, 'expected_version': self.case.version}
        )
        ev_id = res.json()['evidence']['id']
        ev = Evidence.objects.get(pk=ev_id)
        self.addCleanup(lambda: ev.file.delete(save=False))

        scan_file(str(ev.pk))
        security_review_evidence(self.users['bureau'], ev.pk, {'decision': 'reject', 'reason': 'Suspicious macro payload detected'})
        ev.refresh_from_db()
        self.assertEqual(ev.safety, 'security_rejected')
        self.obligation.refresh_from_db()
        self.assertEqual(self.obligation.status, 'security_rejected')

    def test_drive_upload_evidence_task(self):
        ev = Evidence.objects.create(
            application=self.case,
            obligation=self.obligation,
            file='test.txt',
            name='test.txt',
            checksum='b'*64,
            size=10,
            uploaded_by=self.users['institution'],
            safety='clean',
            storage_state='pending'
        )
        with patch('website.drive.connected', return_value=True), patch('website.drive.upload_file', return_value=('drive-123', 'md5-123')):
            upload_pending_evidence()
        ev.refresh_from_db()
        self.assertEqual(ev.storage_state, 'stored')
        self.assertEqual(ev.drive_file_id, 'drive-123')
        self.assertEqual(ev.drive_md5, 'md5-123')


@override_settings(DEMO_MODE=True,EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class ObligationLifecycleTests(Fixtures, TestCase):
    def setUp(self):
        self.build_fixtures()

    def test_obligation_lifecycle_states(self):
        o = Obligation.objects.create(
            application=self.case, title='Lease', kind='lease', source='rules', verifier=self.users['reviewer']
        )
        self.assertEqual(get_obligation_lifecycle(o), 'pending_submission')

        o.status = 'fulfilled'
        o.valid_until = timezone.localdate() + timedelta(days=100)
        o.save()
        self.assertEqual(get_obligation_lifecycle(o), 'fulfilled')

        o.valid_until = timezone.localdate() + timedelta(days=15)
        o.save()
        self.assertEqual(get_obligation_lifecycle(o), 'renewal_required')

        o.valid_until = timezone.localdate() - timedelta(days=1)
        o.save()
        self.assertEqual(get_obligation_lifecycle(o), 'expired')

        e_init = Evidence.objects.create(
            application=self.case, obligation=o, file='init.txt', name='init.txt', checksum='a'*64, size=10,
            uploaded_by=self.users['institution'], safety='clean', review='accepted'
        )
        o.accepted_evidence = e_init
        o.save()
        e = Evidence.objects.create(
            application=self.case, obligation=o, file='dummy', name='d.txt', checksum='c'*64, size=10,
            uploaded_by=self.users['institution'], safety='demo_validated', review='pending'
        )
        self.assertEqual(get_obligation_lifecycle(o), 'renewal_pending_verification')


@override_settings(DEMO_MODE=True,EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class AnalyticsReportsAndAdapterTests(Fixtures, TestCase):
    def setUp(self):
        self.build_fixtures()
        self.client.force_login(self.users['bureau'])

    def test_dashboard_rich_analytics(self):
        res = self.client.get('/api/dashboard')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        for key in ['by_stage', 'stage_ageing', 'workload', 'pendency', 'evc_summary', 'validity_summary', 'regional_performance']:
            self.assertIn(key, data, f"Key {key} missing from dashboard analytics")

    def test_all_report_types_export_safe_csv(self):
        report_types = ['applications', 'evc', 'compliance', 'validity', 'escalations', 'regional_performance', 'audit']
        for rtype in report_types:
            res = self.client.get(f'/api/reports?type={rtype}')
            self.assertEqual(res.status_code, 200, f"Report type {rtype} failed with {res.status_code}")
            self.assertEqual(res['Content-Type'], 'text/csv; charset=utf-8')
            content = res.content.decode('utf-8')
            self.assertTrue(len(content) > 0)

    def test_csv_formula_injection_protection(self):
        from core.views import safe_csv
        self.assertEqual(safe_csv('=1+1'), "'=1+1")
        self.assertEqual(safe_csv('+cmd'), "'+cmd")
        self.assertEqual(safe_csv('-cmd'), "'-cmd")
        self.assertEqual(safe_csv('@mention'), "'@mention")
        self.assertEqual(safe_csv('normal'), 'normal')

    def test_aicte_adapter_contract(self):
        adapter = AICTEAdapter
        valid_payload = {
            'source': 'aicte_nsws',
            'institution_code': 'AICTE-001',
            'category': 'new_institution',
            'academic_year': '2026-2027',
            'external_id': 'EXT-12345'
        }
        res = adapter.validate_intake_payload(valid_payload)
        self.assertEqual(res.institution_code, 'AICTE-001')

        invalid_payload = {'institution_code': 'AICTE-001'}
        with self.assertRaises(AICTEIntegrationError):
            adapter.validate_intake_payload(invalid_payload)

        secret = 'test-secret'
        body = b'{"event":"payment_received","transaction_id":"TXN123"}'
        import hmac, hashlib
        sig = hmac.new(secret.encode('utf-8'), body, hashlib.sha256).hexdigest()
        self.assertTrue(adapter.verify_payment_webhook(body, sig, secret))
        self.assertFalse(adapter.verify_payment_webhook(body, 'invalid_sig', secret))
