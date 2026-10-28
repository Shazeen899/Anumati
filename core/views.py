import csv
import hashlib
import io
import json
import uuid
from datetime import timedelta, date
from functools import wraps
from pathlib import Path
from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.db import transaction, IntegrityError
from django.db.models import Count, Q
from django.http import JsonResponse, FileResponse, HttpResponse
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET
from .models import *
from .services import *

def endpoint(methods=('GET',), public=False):
    def decorate(fn):
        @wraps(fn)
        def wrapped(request, *args, **kwargs):
            if request.method not in methods:
                return JsonResponse({'error': 'Method not allowed.'}, status=405)
            if not public and not request.user.is_authenticated:
                return JsonResponse({'error': 'Sign in to continue.'}, status=401)
            try:
                return fn(request, *args, **kwargs)
            except DomainError as e:
                return JsonResponse({'error': e.message}, status=e.status)
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                return JsonResponse({'error': 'Invalid input. Check the required fields and formats.'}, status=400)
            except IntegrityError:
                return JsonResponse({'error': 'This record conflicts with an existing operation. Refresh and retry.'}, status=409)
        return wrapped
    return decorate

def body(request):
    value = json.loads(request.body or b'{}')
    require(isinstance(value, dict), 'Expected a JSON object.')
    return value

def actor_json(user):
    p = profile(user)
    return {'id': user.pk, 'name': user.get_full_name() or user.username, 'username': user.username, 'role': p.role, 'region': p.region, 'institution_id': p.institution_id}

@ensure_csrf_cookie
@endpoint(public=True)
def session(request):
    return JsonResponse({'user': actor_json(request.user) if request.user.is_authenticated else None, 'demo': settings.DEMO_MODE})

@endpoint(('POST',), public=True)
def sign_in(request):
    data = body(request)
    username = str(data.get('username', ''))[:150]
    identity = hashlib.sha256((request.META.get('REMOTE_ADDR', '') + ':' + username.lower()).encode()).hexdigest()
    with transaction.atomic():
        attempt, _ = LoginAttempt.objects.get_or_create(identity=identity)
        attempt = LoginAttempt.objects.select_for_update().get(pk=attempt.pk)
        if attempt.window_start < timezone.now() - timedelta(minutes=15):
            attempt.failures, attempt.window_start = 0, timezone.now()
        if attempt.failures >= 10:
            return JsonResponse({'error': 'Too many sign-in attempts. Try again in 15 minutes.'}, status=429)
        user = authenticate(request, username=username, password=data.get('password', ''))
        if user is None:
            attempt.failures += 1
            attempt.save()
            return JsonResponse({'error': 'Username or password is incorrect.'}, status=401)
        attempt.failures = 0
        attempt.save()
    profile(user)
    login(request, user)
    return JsonResponse({'user': actor_json(user)})

@endpoint(('POST',))
def sign_out(request):
    logout(request)
    return JsonResponse({'ok': True})

def case_json(c):
    d = c.deadlines.filter(active=True, kind='stage').order_by('due_at').first()
    return {'id': str(c.pk), 'reference': c.reference, 'institution': c.institution.name, 'institution_id': c.institution_id, 'region': c.institution.region, 'city': c.institution.city, 'category': c.category, 'academic_year': c.academic_year, 'stage': c.stage, 'version': c.version, 'owner': c.assigned_to.get_full_name() if c.assigned_to else 'Unassigned', 'owner_id': c.assigned_to_id, 'due_at': d.due_at.isoformat() if d else None, 'overdue': bool(d and d.due_at < timezone.now()), 'age_days': (timezone.now() - c.created_at).days, 'blocker': c.blocker, 'created_at': c.created_at.isoformat(), 'payment_verified': c.payment_verified, 'source': c.source}

def filtered(request):
    qs = scope(request.user)
    search = request.GET.get('search', '').strip()[:200]
    if search:
        qs = qs.filter(Q(reference__icontains=search) | Q(institution__name__icontains=search) | Q(external_id__icontains=search))
    if request.GET.get('stage'):
        qs = qs.filter(stage=request.GET['stage'])
    if request.GET.get('region'):
        qs = qs.filter(institution__region=request.GET['region'])
    if request.GET.get('overdue') == '1':
        qs = qs.filter(deadlines__active=True, deadlines__due_at__lt=timezone.now()).distinct()
    return qs.order_by('-updated_at')

@endpoint(('GET', 'POST'))
def applications(request):
    if request.method == 'POST':
        return JsonResponse(case_json(create_case(request.user, body(request))), status=201)
    qs = filtered(request)
    page = max(1, int(request.GET.get('page', 1)))
    return JsonResponse({'items': [case_json(c) for c in qs[(page-1)*30:page*30]], 'total': qs.count(), 'page': page})

def evidence_json(e):
    return {'id': str(e.pk), 'name': e.name, 'size': e.size, 'checksum': e.checksum, 'received_at': e.received_at.isoformat(), 'safety': e.safety, 'scan_note': e.scan_note, 'review': e.review, 'finding': e.finding, 'reviewed_at': e.reviewed_at.isoformat() if e.reviewed_at else None, 'obligation_id': e.obligation_id, 'valid_from': e.valid_from, 'valid_until': e.valid_until}

def obligation_json(o):
    d = o.deadlines.filter(kind='submission').order_by('-id').first()
    latest = o.submissions.order_by('-received_at').first()
    return {'id': o.pk, 'application_id': str(o.application_id), 'reference': o.application.reference, 'institution': o.application.institution.name, 'title': o.title, 'kind': o.kind, 'status': o.status, 'source': o.source, 'due_at': d.due_at if d else None, 'overdue': bool(d and d.active and d.due_at < timezone.now()), 'submitted_at': latest.received_at if latest else None, 'submitted_on_time': (latest.received_at <= d.due_at) if latest and d else None, 'valid_from': o.valid_from, 'valid_until': o.valid_until, 'expired': bool(o.valid_until and o.valid_until < timezone.localdate()), 'accepted_evidence_id': str(o.accepted_evidence_id) if o.accepted_evidence_id else None, 'verifier_id': o.verifier_id, 'verifier': o.verifier.get_full_name(), 'finding': o.finding, 'submissions': [evidence_json(e) for e in o.submissions.order_by('-received_at')]}

@endpoint()
def application_detail(request, pk):
    c = permitted_case(request.user, pk)
    result = case_json(c)
    result.update({'route': c.route, 'stage_index': c.stage_index, 'policy': {'name': c.policy.name, 'version': c.policy.version, 'source': c.policy.source, 'demo': c.policy.demo} if c.policy else None, 'external_id': c.external_id, 'appeal_used': c.appeal_used, 'published_at': c.published_at, 'deficiencies': list(c.deficiencies.order_by('id').values('id', 'finding', 'requirement', 'response', 'resolved', 'resolution', 'evidence_id')), 'stages': list(c.stages.order_by('id').values('stage', 'entered_at', 'completed_at', 'finding')), 'evidence': [evidence_json(e) for e in c.evidence.order_by('-received_at')], 'obligations': [obligation_json(o) for o in c.obligations.select_related('application__institution', 'verifier')], 'visits': list(c.visits.order_by('-id').values('id', 'starts_at', 'ends_at', 'status', 'report', 'representatives', 'video_url')), 'decisions': list(c.decisions.order_by('id').values('kind', 'outcome', 'reason', 'actor__first_name', 'created_at', 'case_version')), 'deadlines': list(c.deadlines.order_by('-active', 'due_at').values('id', 'title', 'kind', 'due_at', 'active', 'revision', 'source')), 'audit': list(c.audit.order_by('-created_at')[:100].values('id', 'action', 'reason', 'actor__username', 'created_at', 'before_version', 'after_version', 'details', 'correlation_id'))})
    return JsonResponse(result)

@endpoint(('POST',))
def case_command(request, pk):
    return JsonResponse(command(request.user, pk, body(request), request.headers.get('Idempotency-Key')))

@endpoint()
def dashboard(request):
    qs = scope(request.user)
    now = timezone.now()
    active = qs.exclude(stage__in=['APPROVED', 'REJECTED', 'WITHDRAWN', 'DRAFT'])
    deadlines = Deadline.objects.filter(application__in=qs, active=True)
    obligations = Obligation.objects.filter(application__in=qs)
    upcoming = deadlines.order_by('due_at')[:8]
    return JsonResponse({'as_of': now, 'total': qs.count(), 'active': active.count(), 'approved': qs.filter(stage='APPROVED').count(), 'overdue': deadlines.filter(due_at__lt=now).values('application').distinct().count(), 'evc_pending': qs.filter(stage='EVC').count(), 'open_obligations': obligations.exclude(status='fulfilled').count(), 'by_stage': list(qs.values('stage').annotate(count=Count('id')).order_by('stage')), 'by_region': list(qs.values('institution__region').annotate(count=Count('id')).order_by('institution__region')), 'deadlines': [{'id': d.pk, 'title': d.title, 'due_at': d.due_at, 'reference': d.application.reference, 'application_id': str(d.application_id), 'institution': d.application.institution.name, 'overdue': d.due_at < now, 'owner': d.owner.get_full_name()} for d in upcoming], 'recent': [case_json(c) for c in qs.order_by('-updated_at')[:6]], 'audit': list(AuditEvent.objects.filter(application__in=qs).order_by('-created_at')[:6].values('action', 'reason', 'application__reference', 'created_at', 'actor__username'))})

@endpoint()
def metadata(request):
    p = profile(request.user)
    institutions = Institution.objects.all()
    if p.role == 'institution':
        institutions = institutions.filter(pk=p.institution_id)
    elif p.region:
        institutions = institutions.filter(region=p.region)
    users = Profile.objects.filter(user__is_active=True)
    if p.region:
        users = users.filter(Q(region=p.region) | Q(region=''))
    return JsonResponse({'institutions': list(institutions.values('id', 'name', 'region', 'code')), 'regions': list(scope(request.user).values_list('institution__region', flat=True).distinct()), 'categories': ['new_institution', 'new_programme', 'name_change', 'closure', 'self_disclosure', 'evc_only'], 'assignees': [{'id': x.user_id, 'name': x.user.get_full_name(), 'role': x.role} for x in users] if p.role in ['regional', 'bureau'] else []})

@endpoint()
def compliance(request):
    return JsonResponse({'items': [obligation_json(o) for o in Obligation.objects.filter(application__in=scope(request.user)).select_related('application__institution', 'verifier').order_by('id')]})

@endpoint(('POST',))
def upload(request, pk):
    case = permitted_case(request.user, pk)
    require(profile(request.user).role == 'institution', 'Only the institution may submit evidence.', 403)
    file = request.FILES.get('file')
    require(file and 0 < file.size <= 10*1024*1024, 'Choose a nonempty file up to 10 MB.')
    require(Path(file.name).suffix.lower() in ['.pdf', '.txt'], 'Only PDF or UTF-8 text files are accepted.')
    obligation = None
    if request.POST.get('obligation_id'):
        obligation = case.obligations.filter(pk=request.POST['obligation_id']).first()
        require(obligation is not None, 'This requirement does not belong to the case.')
    digest = hashlib.sha256()
    for chunk in file.chunks():
        digest.update(chunk)
    file.seek(0)
    with transaction.atomic():
        case = Application.objects.select_for_update().get(pk=pk)
        require(int(request.POST.get('expected_version', 0)) == case.version, 'Case changed. Refresh before uploading.', 409)
        e = Evidence(application=case, obligation=obligation, file=file, name=Path(file.name).name[:240], checksum=digest.hexdigest(), size=file.size, uploaded_by=request.user)
        for field in ['valid_from', 'valid_until']:
            if request.POST.get(field):
                setattr(e, field, date.fromisoformat(request.POST[field]))
        require(not e.valid_from or not e.valid_until or e.valid_until > e.valid_from, 'End of validity must follow its start.')
        e.save()
        if obligation:
            obligation.status = 'submitted'
            obligation.save()
            # Validity remains active until the renewed evidence is accepted.
            obligation.deadlines.filter(kind='submission', active=True).update(active=False)
            if not obligation.deadlines.filter(kind='verification', active=True).exists():
                make_deadline(case, f'Verify {obligation.title}', 'verification', timezone.now()+timedelta(days=7), obligation.verifier, obligation)
        before = case.version
        case.version += 1
        case.save()
        audit(case, request.user, 'evidence_received', e.name, before, {'evidence_id': str(e.pk), 'checksum': e.checksum, 'received_at': e.received_at.isoformat()})
    return JsonResponse({'evidence': evidence_json(e), 'message': 'Receipt saved. Evidence is quarantined pending validation.'}, status=201)

@endpoint(('POST',))
def evidence_review(request, pk):
    return JsonResponse(review_evidence(request.user, pk, body(request)))

@endpoint()
def download(request, pk):
    e = Evidence.objects.filter(pk=pk).first()
    require(e, 'Evidence not found.', 404)
    permitted_case(request.user, e.application_id)
    require(e.safety == 'clean' or (settings.DEMO_MODE and e.safety == 'demo_validated'), 'Evidence remains quarantined or was rejected.', 409)
    response = FileResponse(e.file.open('rb'), as_attachment=True, filename=e.name, content_type='application/octet-stream')
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    audit(e.application, request.user, 'evidence_downloaded', 'Authorised attachment download', details={'evidence_id': str(e.pk)})
    return response

@endpoint(('GET', 'POST'))
def notifications(request):
    qs = Notification.objects.filter(recipient=request.user)
    if request.method == 'POST':
        data = body(request)
        qs.filter(pk=data.get('id')).update(read_at=timezone.now())
    return JsonResponse({'items': [{'id': n.pk, 'title': n.event.title, 'body': n.event.body, 'read': bool(n.read_at), 'application_id': str(n.event.application_id) if n.event.application_id else None, 'created_at': n.created_at} for n in qs.select_related('event').order_by('-created_at')[:100]], 'unread': qs.filter(read_at=None).count()})

@endpoint()
def escalations(request):
    qs = Escalation.objects.filter(deadline__application__in=scope(request.user)).select_related('deadline__application__institution', 'owner')
    return JsonResponse({'items': [{'id': e.pk, 'status': e.status, 'owner': e.owner.get_full_name(), 'owner_id': e.owner_id, 'reason': e.reason, 'title': e.deadline.title, 'due_at': e.deadline.due_at, 'application_id': str(e.deadline.application_id), 'reference': e.deadline.application.reference, 'institution': e.deadline.application.institution.name} for e in qs.order_by('-created_at')]})

@endpoint(('GET', 'POST'))
def policies(request):
    p = profile(request.user)
    require(p.role in ['policy', 'auditor', 'bureau'], 'Policy registry is restricted.', 403)
    if request.method == 'POST':
        require(p.role == 'policy', 'Only policy administrators may configure policies.', 403)
        data = body(request)
        action = data.get('action')
        reason = str(data.get('reason', '')).strip()
        require(len(reason) >= 5, 'Record the reason for this policy change.')
        with transaction.atomic():
            User.objects.select_for_update().get(pk=request.user.pk)
            if action == 'draft':
                rules = data.get('rules', {})
                validate_policy(rules)
                pol = Policy.objects.create(name=data.get('name', 'Anumati demo'), version=int(data['version']), source=data['source'], rules=rules, demo=True)
            else:
                pol = Policy.objects.select_for_update().get(pk=data['id'])
                validate_policy(pol.rules)
                if action == 'validate':
                    require(pol.state == 'draft', 'Only a draft can be validated.', 409)
                    pol.state = 'validated'
                elif action == 'activate':
                    require(settings.DEMO_MODE and pol.demo and pol.state == 'validated', 'Only a validated demonstration policy can be activated here.', 409)
                    # Serialise activation independently of actor to prevent overlaps.
                    with connection.cursor() as cursor:
                        cursor.execute('SELECT pg_advisory_xact_lock(%s)', [8126263])
                    for other in Policy.objects.filter(state='active').exclude(pk=pol.pk):
                        require(not set(other.rules['routes']) & set(pol.rules['routes']), 'Retire the conflicting active policy before activating this version.', 409)
                    pol.state, pol.approved_by = 'active', request.user
                elif action == 'retire':
                    require(pol.state == 'active', 'Only active policies can be retired.', 409)
                    pol.state = 'retired'
                elif action == 'preview':
                    return JsonResponse({'routes': pol.rules['routes'], 'affected_new_cases_only': True, 'existing_cases_pinned': Application.objects.filter(policy=pol).count(), 'message': 'Validation passed. Preview made no writes.'})
                else:
                    raise DomainError('Unknown policy action.')
                pol.save()
            audit(None, request.user, 'policy_' + action, reason, details={'policy': pol.pk})
    return JsonResponse({'items': list(Policy.objects.order_by('-version').values('id', 'name', 'version', 'state', 'source', 'rules', 'demo', 'effective_at'))})

@endpoint(('GET', 'POST'))
def operations(request):
    require(profile(request.user).role in ['operator', 'bureau'], 'Operations access is restricted.', 403)
    if request.method == 'POST':
        data = body(request)
        require(len(str(data.get('reason', ''))) >= 5, 'A replay reason is required.')
        with transaction.atomic():
            event = Outbox.objects.select_for_update().filter(pk=data.get('id'), state='failed').first()
            require(event, 'Choose a failed event to replay.')
            event.state, event.attempts, event.available_at, event.lease_until = 'pending', 0, timezone.now(), None
            event.save()
            audit(None, request.user, 'outbox_replay', data['reason'], details={'event_id': str(event.pk)})
    return JsonResponse({'counts': list(Outbox.objects.values('state').annotate(count=Count('id'))), 'items': list(Outbox.objects.exclude(state='completed').order_by('created_at')[:100].values('id', 'state', 'attempts', 'last_error', 'created_at', 'available_at')), 'quarantined_files': Evidence.objects.filter(safety='quarantine').count(), 'channel': 'In-app and local email sink; no external provider configured'})

@endpoint()
def audit_log(request):
    qs = AuditEvent.objects.filter(application__in=scope(request.user))
    if profile(request.user).role in ['auditor', 'bureau', 'policy', 'operator']:
        qs = AuditEvent.objects.filter(Q(application__in=scope(request.user)) | Q(application=None))
    return JsonResponse({'items': list(qs.order_by('-created_at')[:200].values('id', 'action', 'reason', 'actor__username', 'application_id', 'application__reference', 'created_at', 'before_version', 'after_version', 'details', 'correlation_id'))})

def safe_csv(value):
    value = '' if value is None else str(value)
    return "'" + value if value.startswith(('=', '+', '-', '@', '\t', '\r')) else value

@endpoint()
def report(request):
    qs = filtered(request)
    report_type = request.GET.get('type', 'applications')
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['Anumati demonstration report', 'As of', timezone.now().isoformat(), 'Timezone', 'Asia/Kolkata'])
    if report_type == 'compliance':
        writer.writerow(['Reference', 'Institution', 'Requirement', 'Status', 'Validity end', 'Verifier'])
        rows = ((o.application.reference, o.application.institution.name, o.title, o.status, o.valid_until, o.verifier.get_full_name()) for o in Obligation.objects.filter(application__in=qs).select_related('application__institution', 'verifier'))
    elif report_type == 'audit':
        writer.writerow(['Reference', 'Actor', 'Action', 'Reason', 'Recorded at', 'Before version', 'After version'])
        rows = ((e.application.reference, e.actor.username if e.actor else 'scheduler', e.action, e.reason, e.created_at, e.before_version, e.after_version) for e in AuditEvent.objects.filter(application__in=qs).select_related('application', 'actor').order_by('created_at'))
    else:
        writer.writerow(['Reference', 'Institution', 'Region', 'Category', 'Stage', 'Owner', 'Age days'])
        rows = ((c.reference, c.institution.name, c.institution.region, c.category, c.stage, c.assigned_to.get_full_name() if c.assigned_to else '', (timezone.now()-c.created_at).days) for c in qs)
    for row in rows:
        writer.writerow([safe_csv(v) for v in row])
    audit(None, request.user, 'report_export', f'{report_type} report', details={'filters': dict(request.GET.items())})
    response = HttpResponse(buf.getvalue(), content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="anumati-{report_type}.csv"'
    response['Cache-Control'] = 'private, no-store'
    return response

@endpoint(public=True)
def health(request):
    from django.db import connection
    with connection.cursor() as cursor:
        cursor.execute('SELECT 1')
    return JsonResponse({'status': 'ok', 'database': 'postgresql'})

def frontend(request, path=''):
    root = settings.BASE_DIR / 'frontend' / 'dist'
    target = (root / path).resolve()
    if path and target.is_relative_to(root.resolve()) and target.is_file():
        import mimetypes
        return FileResponse(target.open('rb'), content_type=mimetypes.guess_type(target)[0] or 'application/octet-stream')
    if path.startswith('api/'):
        return JsonResponse({'error': 'Endpoint not found.'}, status=404)
    if not (root / 'index.html').exists():
        return HttpResponse('Build React first: cd frontend && npm run build', status=503)
    return FileResponse((root / 'index.html').open('rb'), content_type='text/html')
