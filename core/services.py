import hashlib
import json
import uuid
from datetime import timedelta, datetime, date
from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction, connection
from django.db.models import Q
from django.utils import timezone
from .models import (Application, Profile, Policy, Institution, StageRecord, Deficiency, EVCVisit, Decision, Obligation, Evidence, Deadline, Outbox, Escalation, AuditEvent, Command)

class DomainError(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status

def require(condition, message, status=400):
    if not condition:
        raise DomainError(message, status)

def profile(user):
    try:
        return user.profile
    except Profile.DoesNotExist:
        raise DomainError('No authorised role is assigned to this account.', 403)

def scope(user):
    p = profile(user)
    qs = Application.objects.select_related('institution', 'assigned_to', 'policy')
    if p.role == 'institution':
        return qs.filter(institution_id=p.institution_id) if p.institution_id else qs.none()
    if p.role == 'regional':
        return qs.filter(institution__region=p.region) if p.region else qs.none()
    if p.role in ['reviewer', 'evc', 'committee']:
        return qs.filter(Q(assigned_to=user) | Q(obligations__verifier=user)).distinct()
    if p.role in ['bureau', 'executive', 'council', 'auditor']:
        return qs
    return qs.none()

def permitted_case(user, pk):
    case = scope(user).filter(pk=pk).first()
    require(case is not None, 'Case not found in your authorised scope.', 404)
    return case

def audit(case, user, action, reason, before=None, details=None, scope_type=None, institution=None):
    inst = institution or (case.institution if case else None)
    st = scope_type or ('grievance' if action.startswith('website_') else ('policy' if action.startswith('policy_') else ('operations' if action in ['outbox_replay', 'report_export', 'website_drive_connected'] else 'application')))
    return AuditEvent.objects.create(application=case, institution=inst, scope_type=st, actor=user, action=action, reason=reason, before_version=before, after_version=case.version if case else None, details=details or {})

def permitted_audit_events(user):
    p = profile(user)
    permitted_apps = scope(user)
    app_cond = Q(scope_type='application', application__in=permitted_apps) & ~Q(action__startswith='website_') & ~Q(action__startswith='policy_') & ~Q(action__in=['outbox_replay', 'report_export', 'website_drive_connected'])
    if p.role == 'institution' and p.institution_id:
        grievance_cond = (Q(scope_type='grievance') | Q(action__startswith='website_')) & Q(institution_id=p.institution_id) & ~Q(action='website_drive_connected')
    elif p.role in ['bureau', 'auditor']:
        grievance_cond = (Q(scope_type='grievance') | Q(action__startswith='website_')) & ~Q(action='website_drive_connected')
    elif p.role == 'regional' and p.region:
        grievance_cond = (Q(scope_type='grievance') | Q(action__startswith='website_')) & Q(institution__region=p.region) & ~Q(action='website_drive_connected')
    else:
        grievance_cond = Q(pk__in=[])

    if p.role in ['policy', 'bureau', 'auditor']:
        policy_cond = Q(scope_type='policy') | Q(action__startswith='policy_')
    else:
        policy_cond = Q(pk__in=[])

    if p.role in ['operator', 'bureau', 'auditor']:
        ops_cond = Q(scope_type='operations') | Q(action__in=['outbox_replay', 'report_export', 'website_drive_connected'])
    else:
        ops_cond = Q(pk__in=[])

    return AuditEvent.objects.filter(app_cond | grievance_cond | policy_cond | ops_cond)

def owner_for(case, stage):
    role = {'SCRUTINY': 'reviewer', 'EVC': 'evc', 'SHC': 'committee', 'SAC': 'committee', 'DECISION': 'executive', 'RATIFICATION': 'council', 'APPEAL_DECISION': 'council', 'RETURNED': 'institution'}.get(stage, 'regional')
    qs = Profile.objects.filter(role=role, user__is_active=True)
    qs = qs.filter(institution=case.institution) if role == 'institution' else qs.filter(Q(region=case.institution.region) | Q(region=''))
    p = qs.order_by('id').first()
    require(p is not None, f'No {role} assignment is configured for this region. An officer must configure responsibility.', 409)
    return p.user

def notify(case, recipient, identity, title, body='', **kwargs):
    return Outbox.objects.get_or_create(identity=identity, defaults=dict(application=case, recipient=recipient, title=title, body=body or title, **kwargs))[0]

def make_deadline(case, title, kind, due, owner, obligation=None):
    superior = Profile.objects.filter(role='bureau', user__is_active=True).first()
    require(superior, 'A bureau escalation owner must be configured.', 409)
    return Deadline.objects.create(application=case, obligation=obligation, title=title, kind=kind, due_at=due, owner=owner, escalation_owner=superior.user)

def advance(case, stage, finding=''):
    now = timezone.now()
    case.stages.filter(completed_at=None).update(completed_at=now, finding=finding)
    case.deadlines.filter(active=True, obligation=None, kind__in=['stage', 'evc_scheduling', 'evc_visit', 'evc_report']).update(active=False)
    case.stage = stage
    case.blocker = ''
    if stage not in ['APPROVED', 'REJECTED', 'WITHDRAWN']:
        case.assigned_to = owner_for(case, stage)
        StageRecord.objects.create(application=case, stage=stage)
        rules = case.policy.rules if case.policy else {}
        days = rules.get('stage_deadlines', {}).get(stage, rules.get('stage_days', 7))
        make_deadline(case, f'{stage.replace("_", " ").title()} action due', 'stage', now + timedelta(days=days), case.assigned_to)
        if stage == 'EVC':
            sched_days = rules.get('stage_deadlines', {}).get('EVC_SCHEDULING', 3)
            make_deadline(case, 'EVC scheduling due', 'evc_scheduling', now + timedelta(days=sched_days), case.assigned_to)

def iso_datetime(value):
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return timezone.make_aware(dt) if timezone.is_naive(dt) else dt
    except (ValueError, TypeError, AttributeError):
        raise DomainError('A valid date and time is required.')

def validate_policy(rules):
    require(isinstance(rules, dict), 'Policy rules must be an object.')
    routes = rules.get('routes')
    require(isinstance(routes, dict) and routes, 'At least one category route is required.')
    require(isinstance(rules.get('stage_days'), int) and 1 <= rules['stage_days'] <= 365, 'Stage days must be between 1 and 365.')
    require(isinstance(rules.get('reminder_days'), int) and 0 <= rules['reminder_days'] <= 90, 'Reminder days must be between 0 and 90.')
    allowed = {'SCRUTINY', 'EVC', 'SHC', 'SAC', 'DECISION', 'RATIFICATION'}
    for category, route in routes.items():
        require(isinstance(route, list) and route and len(route) == len(set(route)), 'Routes must contain unique stages and cannot be empty.')
        require(all(s in allowed for s in route), f'Unsupported workflow stage in route {category}.')
        require(route[-2:] == ['DECISION', 'RATIFICATION'] or route[-1:] in [['DECISION'], ['RATIFICATION']], 'Every route must end in human decision or ratification.')
    return True

@transaction.atomic
def create_case(user, data):
    p = profile(user)
    require(p.role in ['institution', 'regional', 'bureau'], 'This role cannot create applications.', 403)
    institution_id = p.institution_id if p.role == 'institution' else data.get('institution_id')
    institution = Institution.objects.filter(pk=institution_id).first()
    require(institution is not None, 'Choose an institution.')
    require(p.role != 'regional' or institution.region == p.region, 'Institution is outside your region.', 403)
    category = str(data.get('category', '')).strip()
    year = str(data.get('academic_year', '2026-2027'))
    external = str(data.get('external_id', '')).strip()
    require(category and external and len(external) <= 100, 'Category and a source reference are required.')
    require(len(year) == 9 and year[:4].isdigit() and year[4] == '-' and year[5:].isdigit() and int(year[5:]) == int(year[:4])+1, 'Use an academic year such as 2026-2027.')
    User.objects.select_for_update().get(pk=user.pk)
    existing = Application.objects.filter(source='Simulated intake', external_id=external).first()
    if existing:
        require(scope(user).filter(pk=existing.pk).exists(), 'Source reference already exists.', 409)
        require(existing.category == category and existing.institution_id == institution.pk and existing.academic_year == year, 'Source reference conflicts with another payload.', 409)
        return existing
    case = Application.objects.create(reference=f'AN-{timezone.localdate().year}-{uuid.uuid4().hex[:6].upper()}', institution=institution, category=category, academic_year=year, external_id=external, assigned_to=user)
    audit(case, user, 'application_created', 'Synthetic intake recorded; payment is not verified by self-declaration.')
    return case

@transaction.atomic
def command(user, case_id, data, key):
    permitted_case(user, case_id)
    require(key and len(key) <= 100, 'An Idempotency-Key is required.')
    # Serialise command identities for each actor, including concurrent identical keys.
    User.objects.select_for_update().get(pk=user.pk)
    fingerprint = hashlib.sha256(json.dumps({'case': str(case_id), 'payload': data}, sort_keys=True).encode()).hexdigest()
    previous = Command.objects.filter(actor=user, key=key).first()
    if previous:
        require(previous.fingerprint == fingerprint, 'This request key was used for a different command.', 409)
        return previous.response
    case = Application.objects.select_for_update().get(pk=case_id)
    permitted_case(user, case_id)
    require(data.get('expected_version') == case.version, 'This case changed. Refresh it before taking another action.', 409)
    p = profile(user)
    action = data.get('action')
    reason = str(data.get('reason', '')).strip()
    require(len(reason) >= 5, 'Record a reason of at least five characters.')
    before = case.version
    official = p.role in ['regional', 'bureau']
    assigned = case.assigned_to_id == user.pk
    if action == 'verify_payment':
        require(settings.DEMO_MODE and official and case.stage == 'DRAFT', 'Only an authorised intake officer may confirm the simulated prerequisite.', 403)
        case.payment_verified = True
    elif action == 'submit':
        require(p.role == 'institution' and case.stage == 'DRAFT', 'Only an institution can submit its draft.', 403)
        policies = list(Policy.objects.filter(state='active', effective_at__lte=timezone.now(), demo=settings.DEMO_MODE))
        matches = [x for x in policies if case.category in x.rules.get('routes', {})]
        if len(matches) != 1:
            case.stage = 'EXCEPTION'
            case.blocker = 'Missing or conflicting route. Policy review is required.'
            case.assigned_to = owner_for(case, 'EXCEPTION')
        else:
            require(case.category != 'new_institution' or case.payment_verified, 'New institution processing requires an officer-verified simulated payment event.', 409)
            if case.dependency_id:
                require(case.dependency.stage == 'APPROVED', 'Linked closure must have a favourable disposition.', 409)
            case.policy = matches[0]
            case.route = matches[0].rules['routes'][case.category]
            case.stage_index = 0
            case.submitted_at = timezone.now()
            advance(case, case.route[0], reason)
    elif action == 'return':
        require(assigned and p.role in ['reviewer', 'committee'] and case.stage in ['SCRUTINY', 'SHC', 'SAC'], 'Only the assigned reviewer may return this stage.', 403)
        require(str(data.get('requirement', '')).strip(), 'A requirement reference is required.')
        evidence = case.evidence.filter(pk=data.get('evidence_id')).first() if data.get('evidence_id') else None
        require(not data.get('evidence_id') or evidence, 'Evidence must belong to this case.')
        Deficiency.objects.create(application=case, finding=reason, requirement=data['requirement'], evidence=evidence)
        # Return does not reset the original stage deadline.
        case.stage = 'RETURNED'
        case.assigned_to = owner_for(case, 'RETURNED')
        case.blocker = reason
        stage_deadlines = case.deadlines.filter(active=True, kind='stage')
        stage_deadlines.update(owner=case.assigned_to)
        Outbox.objects.filter(application=case, deadline__in=stage_deadlines, recipient=user, state__in=['pending', 'processing']).update(state='superseded')
    elif action == 'resubmit':
        require(p.role == 'institution' and case.stage == 'RETURNED', 'This case is not awaiting your response.', 403)
        case.deficiencies.filter(resolved=False).update(response=reason)
        case.stage = case.route[case.stage_index]
        case.assigned_to = owner_for(case, case.stage)
        case.blocker = 'Re-review the original unresolved deficiencies.'
        stage_deadlines = case.deadlines.filter(active=True, kind='stage')
        stage_deadlines.update(owner=case.assigned_to)
        Outbox.objects.filter(application=case, deadline__in=stage_deadlines, recipient=user, state__in=['pending', 'processing']).update(state='superseded')
    elif action == 'resolve_deficiency':
        require(assigned and p.role in ['reviewer', 'committee'] and case.stage in ['SCRUTINY', 'SHC', 'SAC'], 'Only the assigned reviewer may resolve findings.', 403)
        item = case.deficiencies.filter(pk=data.get('deficiency_id'), resolved=False).first()
        require(item and item.response, 'A response to an unresolved deficiency is required.')
        item.resolved = True
        item.resolution = reason
        item.save()
    elif action == 'complete_stage':
        roles = {'SCRUTINY': 'reviewer', 'EVC': 'evc', 'SHC': 'committee', 'SAC': 'committee'}
        require(assigned and roles.get(case.stage) == p.role, 'You cannot complete this stage.', 403)
        require(not case.deficiencies.filter(resolved=False).exists(), 'Resolve all existing deficiencies before completing review.', 409)
        if case.stage == 'EVC':
            require(case.visits.filter(status='reported').exists(), 'A conducted EVC with its completed report is required.', 409)
        Decision.objects.create(application=case, kind='recommendation', outcome='completed', reason=reason, actor=user, case_version=before+1)
        if case.stage == 'SAC' and case.appeal_used:
            advance(case, 'APPEAL_DECISION', reason)
        else:
            case.stage_index += 1
            advance(case, case.route[case.stage_index], reason)
    elif action in ['approve', 'reject']:
        require(assigned and p.role == 'executive' and case.stage == 'DECISION', 'Only the assigned Executive Committee authority may decide.', 403)
        outcome = 'approved' if action == 'approve' else 'rejected'
        Decision.objects.create(application=case, kind='executive', outcome=outcome, reason=reason, actor=user, case_version=before+1)
        advance(case, 'RATIFICATION', reason)
    elif action == 'ratify':
        require(assigned and p.role == 'council' and case.stage == 'RATIFICATION', 'Only the assigned Council authority may ratify.', 403)
        decision = case.decisions.filter(kind='executive').latest('created_at')
        Decision.objects.create(application=case, kind='ratification', outcome=decision.outcome, reason=reason, actor=user, case_version=before+1)
        advance(case, decision.outcome.upper(), reason)
        case.published_at = timezone.now()
    elif action == 'appeal':
        require(p.role == 'institution' and case.stage == 'REJECTED' and not case.appeal_used, 'One appeal is permitted for a published rejected case in this demo.', 409)
        require(case.published_at and timezone.now() <= case.published_at + timedelta(days=7), 'The demonstration appeal window has ended.', 409)
        case.appeal_used = True
        advance(case, 'SAC', reason)
    elif action in ['appeal_approve', 'appeal_reject']:
        require(assigned and p.role == 'council' and case.stage == 'APPEAL_DECISION', 'Only the assigned Council authority may determine the appeal.', 403)
        outcome = 'approved' if action == 'appeal_approve' else 'rejected'
        Decision.objects.create(application=case, kind='appeal', outcome=outcome, reason=reason, actor=user, case_version=before+1)
        advance(case, outcome.upper(), reason)
        case.published_at = timezone.now()
    elif action in ['schedule_evc', 'reschedule_evc', 'cancel_evc', 'conduct_evc', 'report_evc']:
        require(assigned and p.role == 'evc' and case.stage == 'EVC', 'Only the assigned EVC coordinator may manage this visit.', 403)
        visit = case.visits.exclude(status='cancelled').order_by('-id').first()
        if action in ['schedule_evc', 'reschedule_evc']:
            require(action != 'schedule_evc' or visit is None, 'Use reschedule for the existing visit.', 409)
            require(action != 'reschedule_evc' or (visit and visit.status == 'confirmed'), 'Only a confirmed visit can be rescheduled.', 409)
            start, end = iso_datetime(data.get('starts_at')), iso_datetime(data.get('ends_at'))
            require(end > start and start > timezone.now(), 'Choose a future visit with an end after its start.')
            # A transaction advisory lock protects interval overlaps across all writers.
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_xact_lock(%s)', [8126262])
            conflicts = EVCVisit.objects.filter(coordinator=user, status__in=['confirmed', 'conducted'], starts_at__lt=end, ends_at__gt=start)
            if visit:
                conflicts = conflicts.exclude(pk=visit.pk)
            require(not conflicts.exists(), 'This coordinator already has a visit overlapping that time.', 409)
            if visit:
                visit.status = 'cancelled'
                visit.cancellation_reason = f'Rescheduled: {reason}'
                visit.save()
            stage_dl = case.deadlines.filter(active=True, kind='stage').first()
            is_late = bool(stage_dl and start > stage_dl.due_at)
            EVCVisit.objects.create(application=case, coordinator=user, starts_at=start, ends_at=end, late_scheduled=is_late)
            case.deadlines.filter(active=True, kind='evc_scheduling').update(active=False)
            case.deadlines.filter(active=True, kind='evc_visit').update(active=False)
            make_deadline(case, 'EVC visit conduct due', 'evc_visit', end, case.assigned_to)
        elif action == 'cancel_evc':
            require(visit and visit.status == 'confirmed', 'No confirmed visit can be cancelled.')
            visit.status = 'cancelled'
            visit.cancellation_reason = reason
            visit.save()
            case.deadlines.filter(active=True, kind='evc_visit').update(active=False)
            make_deadline(case, 'EVC scheduling due', 'evc_scheduling', timezone.now() + timedelta(days=3), case.assigned_to)
        elif action == 'conduct_evc':
            require(visit and visit.status == 'confirmed', 'A confirmed visit is required.')
            require(visit.starts_at <= timezone.now(), 'The scheduled visit has not started.', 409)
            require(data.get('representatives'), 'Record authorised representatives and attendance.')
            visit.status, visit.representatives = 'conducted', data['representatives']
            visit.save()
            case.deadlines.filter(active=True, kind='evc_visit').update(active=False)
            rules = case.policy.rules if case.policy else {}
            rep_days = rules.get('stage_deadlines', {}).get('EVC_REPORT', 2)
            make_deadline(case, 'EVC report submission due', 'evc_report', timezone.now() + timedelta(days=rep_days), case.assigned_to)
        else:
            require(visit and visit.status == 'conducted', 'Record the conducted visit first.')
            require(len(str(data.get('report', ''))) >= 20, 'A substantive report of at least 20 characters is required.')
            visit.status, visit.report = 'reported', data['report']
            visit.video_url = data.get('video_url', '')
            visit.report_submitted_at = timezone.now()
            visit.save()
            case.deadlines.filter(active=True, kind='evc_report').update(active=False)
    elif action == 'reassign':
        require(official, 'Only a scoped officer can reassign cases.', 403)
        new_owner = User.objects.filter(pk=data.get('user_id'), is_active=True).first()
        require(new_owner is not None, 'Choose an active assignee.')
        expected_role = profile(owner_for(case, case.stage)).role
        np = profile(new_owner)
        require(np.role == expected_role and (not np.region or np.region == case.institution.region), 'Assignee does not hold the required role or region.')
        require(np.role != 'institution' or np.institution_id == case.institution_id, 'Institution assignment is not permitted.')
        old_owner = case.assigned_to
        case.assigned_to = new_owner
        active_dls = case.deadlines.filter(active=True, kind__in=['stage', 'evc_scheduling', 'evc_visit', 'evc_report'])
        active_dls.update(owner=new_owner)
        if old_owner:
            Outbox.objects.filter(application=case, deadline__in=active_dls, recipient=old_owner, state__in=['pending', 'processing']).update(state='superseded')
    elif action == 'resolve_exception':
        require(official and case.stage == 'EXCEPTION', 'Only a scoped officer can resolve a routing exception.', 403)
        if data.get('category'):
            case.category = data.get('category')
        policies = list(Policy.objects.filter(state='active', effective_at__lte=timezone.now(), demo=settings.DEMO_MODE))
        matches = [x for x in policies if case.category in x.rules.get('routes', {})]
        require(len(matches) == 1, 'No unique active policy found for this category.', 409)
        case.policy = matches[0]
        case.route = matches[0].rules['routes'][case.category]
        case.stage_index = 0
        case.blocker = ''
        audit(case, user, 'exception_resolved', reason, before, {'recovered_stage': case.route[0], 'category': case.category})
        advance(case, case.route[0], reason)
    elif action == 'migrate_policy':
        require(official, 'Only an authorised officer may migrate application policy.', 403)
        pol_id = data.get('policy_id')
        target_policy = Policy.objects.filter(pk=pol_id, state='active').first()
        require(target_policy, 'Target policy not found or inactive.', 404)
        require(case.category in target_policy.rules.get('routes', {}), 'Target policy does not support this category.', 409)
        old_pol = case.policy
        case.policy = target_policy
        new_route = target_policy.rules['routes'][case.category]
        if case.stage in new_route:
            case.route = new_route
            case.stage_index = new_route.index(case.stage)
        audit(case, user, 'policy_migrated', reason, before, {'old_policy_id': old_pol.pk if old_pol else None, 'new_policy_id': target_policy.pk, 'new_version': target_policy.version})
    elif action == 'promote_grievance_document':
        require(p.role == 'institution' and case.institution_id == p.institution_id, 'Only the institution may promote grievance documents.', 403)
        obligation_id = data.get('obligation_id')
        obligation = case.obligations.filter(pk=obligation_id).first() if obligation_id else None
        doc_id = data.get('document_id')
        from website.models import Document
        doc = Document.objects.filter(pk=doc_id, grievance__institution=case.institution).first()
        require(doc, 'Grievance document not found for your institution.', 404)
        evidence = Evidence.objects.create(
            application=case,
            obligation=obligation,
            name=doc.name,
            file=doc.file,
            checksum=doc.sha256,
            size=doc.size,
            uploaded_by=user,
            received_at=doc.received_at,
            safety='quarantine',
            scan_note=f'Promoted from grievance {doc.grievance.reference} (receipt: {doc.received_at.isoformat()})',
            storage_state=doc.state,
            drive_file_id=doc.drive_file_id,
            drive_md5=doc.md5,
            mime_type=doc.mime_type
        )
        audit(case, user, 'evidence_promoted_from_grievance', reason, before, {
            'evidence_id': str(evidence.pk),
            'grievance_reference': doc.grievance.reference,
            'document_id': str(doc.pk),
            'checksum': doc.sha256
        })
    elif action == 'add_obligation':
        require(official and case.stage == 'APPROVED', 'An officer may register a sourced obligation after approval.', 403)
        require(case.institution.kind != 'University', 'University conditions are outside the demo policy.', 409)
        title = str(data.get('title', '')).strip()
        kind = data.get('kind', 'undertaking')
        require(title and kind in ['undertaking', 'lease', 'certificate'], 'Choose a title and supported obligation type.')
        require(data.get('source'), 'Record the originating requirement or decision reference.')
        verifier = owner_for(case, 'SCRUTINY')
        obligation = Obligation.objects.create(application=case, decision=case.decisions.filter(kind='ratification').last(), title=title, kind=kind, source=data['source'], verifier=verifier)
        make_deadline(case, title, 'submission', iso_datetime(data.get('due_at')), owner_for(case, 'RETURNED'), obligation)
    elif action == 'extend_deadline':
        require(official, 'Only a scoped officer may extend a deadline.', 403)
        deadline = case.deadlines.select_for_update().filter(pk=data.get('deadline_id'), active=True).first()
        require(deadline, 'Choose an active deadline.')
        due = iso_datetime(data.get('due_at'))
        require(due > deadline.due_at, 'The extended deadline must be later than its current value.')
        audit(case, user, 'deadline_revision', reason, before, {'deadline': deadline.pk, 'old_due_at': deadline.due_at.isoformat(), 'new_due_at': due.isoformat(), 'old_revision': deadline.revision})
        deadline.due_at, deadline.revision = due, deadline.revision + 1
        deadline.save()
        Escalation.objects.filter(deadline=deadline, status='open').update(status='superseded', reason=reason)
    elif action == 'resolve_escalation':
        escalation = Escalation.objects.select_for_update().filter(pk=data.get('escalation_id'), deadline__application=case, owner=user, status='open').first()
        require(escalation, 'No open escalation is assigned to you.', 403)
        escalation.status, escalation.reason = 'resolved', reason
        escalation.save()
    else:
        raise DomainError('Unknown or unsupported action.')
    case.version += 1
    case.save()
    audit(case, user, action, reason, before, {'policy_id': case.policy_id, 'stage': case.stage})
    recipients = {case.assigned_to_id, owner_for(case, 'RETURNED').pk}
    for recipient_id in recipients - {None}:
        notify(case, User.objects.get(pk=recipient_id), f'command:{case.pk}:{case.version}:{recipient_id}', f'{case.reference}: {action.replace("_", " ")}', reason)
    response = {'id': str(case.pk), 'version': case.version, 'stage': case.stage, 'message': 'Action recorded successfully.'}
    Command.objects.create(actor=user, key=key, fingerprint=fingerprint, response=response)
    return response

@transaction.atomic
def review_evidence(user, evidence_id, data):
    evidence = Evidence.objects.select_related('application', 'obligation').filter(pk=evidence_id).first()
    require(evidence, 'Evidence not found.', 404)
    p = profile(user)
    # Obligations keep their verifier after case ownership moves to the authority.
    obligation_access = evidence.obligation_id and evidence.obligation.verifier_id == user.pk
    if not obligation_access:
        permitted_case(user, evidence.application_id)
    case = Application.objects.select_for_update().get(pk=evidence.application_id)
    evidence = Evidence.objects.select_for_update().get(pk=evidence_id)
    require(data.get('expected_version') == case.version, 'Case changed. Refresh before reviewing.', 409)
    require(obligation_access or (case.assigned_to_id == user.pk and p.role == 'reviewer'), 'Only the assigned verifier can review this evidence.', 403)
    require(evidence.safety in ['clean', 'demo_validated', 'security_reviewed'] and evidence.review == 'pending', 'Evidence is unavailable or already reviewed.', 409)
    reason = str(data.get('reason', '')).strip()
    require(len(reason) >= 5, 'A review finding is required.')
    accept = data.get('outcome') == 'accepted'
    require(data.get('outcome') in ['accepted', 'returned'], 'Choose accept or return.')
    evidence.review, evidence.finding = data['outcome'], reason
    evidence.reviewed_at, evidence.reviewed_by = timezone.now(), user
    if evidence.obligation_id:
        obligation = Obligation.objects.select_for_update().get(pk=evidence.obligation_id)
        if accept:
            if obligation.kind in ['lease', 'certificate']:
                require(evidence.valid_from and evidence.valid_until, 'Validity dates are required for this document.')
                require(evidence.valid_until > evidence.valid_from, 'Validity interval is invalid.')
                if obligation.kind == 'lease':
                    remaining_days = (evidence.valid_until - timezone.localdate()).days
                    require(remaining_days >= obligation.course_years * 365, 'Demo lease check: remaining term must cover the recorded course duration. Refer unclear cases for domain review.', 409)
                obligation.valid_from, obligation.valid_until = evidence.valid_from, evidence.valid_until
                obligation.deadlines.filter(active=True).update(active=False)
                make_deadline(case, f'{obligation.title} renewal', 'validity', timezone.make_aware(datetime.combine(evidence.valid_until, datetime.max.time())), owner_for(case, 'RETURNED'), obligation)
            else:
                obligation.deadlines.filter(active=True).update(active=False)
            obligation.accepted_evidence = evidence
            obligation.status = 'fulfilled'
        else:
            obligation.status = 'returned'
            obligation.deadlines.filter(kind='submission').update(active=True)
        obligation.deadlines.filter(kind='verification', active=True).update(active=False)
        obligation.finding = reason
        obligation.save()
    evidence.save()
    before = case.version
    case.version += 1
    case.save()
    audit(case, user, 'evidence_' + evidence.review, reason, before, {'evidence_id': str(evidence.pk), 'checksum': evidence.checksum})
    notify(case, owner_for(case, 'RETURNED'), f'evidence-review:{evidence.pk}', f'Evidence {evidence.review}: {evidence.name}', reason)
    return {'message': 'Evidence review recorded.', 'version': case.version}

@transaction.atomic
def security_review_evidence(user, evidence_id, data):
    evidence = Evidence.objects.select_related('application', 'obligation').filter(pk=evidence_id).first()
    require(evidence, 'Evidence not found.', 404)
    permitted_case(user, evidence.application_id)
    case = Application.objects.select_for_update().get(pk=evidence.application_id)
    evidence = Evidence.objects.select_for_update().get(pk=evidence_id)
    p = profile(user)
    official = p.role in ['bureau', 'regional']
    assigned = case.assigned_to_id == user.pk and p.role == 'reviewer'
    require(official or assigned, 'Only an authorised officer or assigned reviewer can perform security review.', 403)
    require(evidence.safety in ['quarantine', 'rejected'], 'Evidence is not pending security review.', 409)
    outcome = data.get('outcome') or data.get('decision')
    require(outcome in ['release', 'reject'], 'Outcome must be release or reject.')
    note = str(data.get('note') or data.get('reason') or '').strip()
    require(len(note) >= 5, 'A substantive security assessment of at least 5 characters is required.')

    if outcome == 'release':
        evidence.safety = 'security_reviewed'
        evidence.scan_note = f'Manual security assessment: {note}'
    else:
        evidence.safety = 'security_rejected'
        evidence.scan_note = f'Security rejection: {note}'
        if evidence.obligation:
            evidence.obligation.status = 'security_rejected'
            evidence.obligation.save()
    evidence.security_reviewer = user
    evidence.security_reviewed_at = timezone.now()
    evidence.save()

    action_name = 'evidence_security_release' if outcome == 'release' else 'evidence_security_rejected'
    audit(case, user, action_name, note, case.version, {
        'evidence_id': str(evidence.pk),
        'checksum': evidence.checksum,
        'name': evidence.name,
        'manual_assessment': True
    })
    return {'message': f'Evidence {outcome} recorded.', 'safety': evidence.safety}

def get_obligation_lifecycle(o):
    today = timezone.localdate()
    submissions = list(o.submissions.order_by('received_at'))
    latest_sub = submissions[-1] if submissions else None

    if not latest_sub:
        if o.status == 'fulfilled':
            if o.valid_until:
                if o.valid_until < today:
                    return 'expired'
                elif (o.valid_until - today).days <= 30:
                    return 'renewal_required'
            return 'fulfilled'
        return 'pending_submission'
    if latest_sub.safety == 'quarantine':
        return 'evidence_received'
    if latest_sub.safety == 'rejected':
        return 'security_rejected'
    if latest_sub.review == 'pending':
        if o.accepted_evidence_id and o.valid_until:
            return 'renewal_pending_verification'
        return 'verification_pending'
    if latest_sub.review == 'returned':
        return 'returned'
    if latest_sub.review == 'accepted':
        if o.valid_until:
            if o.valid_until < today:
                return 'expired'
            elif (o.valid_until - today).days <= 30:
                return 'renewal_required'
        return 'fulfilled'
    return o.status

def resolve_exception(user, case_id, payload):
    c = Application.objects.get(pk=case_id)
    return command(user, case_id, {'action': 'resolve_exception', 'expected_version': c.version, **payload}, str(uuid.uuid4()))

def migrate_policy(user, case_id, payload):
    c = Application.objects.get(pk=case_id)
    return command(user, case_id, {'action': 'migrate_policy', 'expected_version': c.version, **payload}, str(uuid.uuid4()))

