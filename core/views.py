import csv
import hashlib
import hmac
import io
import json
import secrets
import uuid
from datetime import timedelta, date
from functools import wraps
from pathlib import Path
from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.core.mail import send_mail
from django.core.validators import validate_email, ValidationError
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

@endpoint(('POST',), public=True)
def request_signup_otp(request):
    data = body(request)
    email = str(data.get('email', '')).strip().lower()
    username = str(data.get('username', '')).strip()
    institution_name = str(data.get('institution_name', '')).strip()
    region = str(data.get('region', '')).strip()
    city = str(data.get('city', '')).strip()

    if not email:
        return JsonResponse({'error': 'Email address is required.'}, status=400)
    try:
        validate_email(email)
    except ValidationError:
        return JsonResponse({'error': 'Please enter a valid email address.'}, status=400)

    if not username or len(username) < 3 or len(username) > 150:
        return JsonResponse({'error': 'Username must be between 3 and 150 characters.'}, status=400)
    if not username.replace('_', '').replace('.', '').replace('-', '').isalnum():
        return JsonResponse({'error': 'Username may only contain letters, numbers, underscores, dots, and hyphens.'}, status=400)

    if not institution_name or len(institution_name) < 3 or len(institution_name) > 240:
        return JsonResponse({'error': 'Institution name must be between 3 and 240 characters.'}, status=400)

    valid_regions = ['North', 'South', 'East', 'West', 'Central', 'North-West', 'South-West']
    if region not in valid_regions:
        return JsonResponse({'error': f'Select a valid AICTE region: {", ".join(valid_regions)}.'}, status=400)

    # Check for duplicate user
    if User.objects.filter(username__iexact=username).exists():
        return JsonResponse({'error': f'The username "{username}" is already taken.'}, status=409)
    if User.objects.filter(email__iexact=email).exists():
        return JsonResponse({'error': f'An account with the email "{email}" already exists.'}, status=409)

    # Rate limiting: max 3 requests per 15 minutes per email
    cutoff = timezone.now() - timedelta(minutes=15)
    recent_requests = SignupOTP.objects.filter(email=email, created_at__gte=cutoff).count()
    if recent_requests >= 3:
        return JsonResponse({'error': 'Too many verification code requests for this email. Please try again after 15 minutes.'}, status=429)

    # Generate 6-digit OTP
    otp = f"{secrets.randbelow(900000) + 100000}"
    salt = settings.SECRET_KEY[:16]
    otp_hash = hashlib.sha256((salt + otp).encode('utf-8')).hexdigest()
    expires_at = timezone.now() + timedelta(minutes=10)

    SignupOTP.objects.create(
        email=email,
        username=username,
        institution_name=institution_name,
        region=region,
        otp_hash=otp_hash,
        expires_at=expires_at,
    )

    subject = 'AICTE Anumati - Institution Registration Verification Code'
    message = (
        f"Greetings from AICTE Anumati Portal,\n\n"
        f"Your 6-digit verification code (OTP) for registering the institution \"{institution_name}\" is:\n\n"
        f"    {otp}\n\n"
        f"This code is valid for 10 minutes. Please enter this code along with your chosen password on the registration page to activate your account.\n\n"
        f"If you did not initiate this registration request, please disregard this email.\n\n"
        f"AICTE Anumati Portal Administration\n"
    )

    try:
        send_mail(
            subject,
            message,
            getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@anumati.aicte.gov.in'),
            [email],
            fail_silently=False,
        )
    except Exception as e:
        if not settings.DEMO_MODE:
            return JsonResponse({'error': f'Failed to dispatch verification email: {str(e)}'}, status=500)

    response_data = {
        'message': f'A 6-digit verification code has been dispatched to {email}. Valid for 10 minutes.',
        'email': email,
        'expires_in_minutes': 10,
        'demo_otp': otp if settings.DEMO_MODE else None,
    }
    return JsonResponse(response_data)

@endpoint(('POST',), public=True)
def verify_signup_otp(request):
    data = body(request)
    email = str(data.get('email', '')).strip().lower()
    otp = str(data.get('otp', '')).strip()
    password = str(data.get('password', ''))
    password_confirm = str(data.get('password_confirm', ''))
    city = str(data.get('city', '')).strip()

    if not email or not otp:
        return JsonResponse({'error': 'Email address and verification code are required.'}, status=400)
    if not password or len(password) < 8:
        return JsonResponse({'error': 'Password must be at least 8 characters long.'}, status=400)
    if password_confirm and password != password_confirm:
        return JsonResponse({'error': 'Passwords do not match.'}, status=400)

    signup_otp = SignupOTP.objects.filter(email=email, verified=False).order_by('-created_at').first()
    if not signup_otp or signup_otp.expires_at < timezone.now():
        return JsonResponse({'error': 'Verification code has expired or is invalid. Please request a new OTP.'}, status=400)

    if signup_otp.attempts >= 5:
        return JsonResponse({'error': 'Maximum verification attempts exceeded for this code. Please request a new OTP.'}, status=429)

    salt = settings.SECRET_KEY[:16]
    actual_hash = hashlib.sha256((salt + otp).encode('utf-8')).hexdigest()
    if not hmac.compare_digest(actual_hash, signup_otp.otp_hash):
        signup_otp.attempts += 1
        signup_otp.save(update_fields=['attempts'])
        remaining = max(0, 5 - signup_otp.attempts)
        return JsonResponse({'error': f'Invalid verification code. {remaining} attempt(s) remaining.'}, status=400)

    if User.objects.filter(username__iexact=signup_otp.username).exists():
        return JsonResponse({'error': f'The username "{signup_otp.username}" was taken in the meantime. Please register with a different username.'}, status=409)
    if User.objects.filter(email__iexact=signup_otp.email).exists():
        return JsonResponse({'error': f'An account with the email "{signup_otp.email}" already exists.'}, status=409)

    with transaction.atomic():
        inst_code = f"INST-{uuid.uuid4().hex[:6].upper()}"
        institution = Institution.objects.create(
            name=signup_otp.institution_name,
            code=inst_code,
            region=signup_otp.region,
            city=city or 'Headquarters',
            kind='Technical institution',
        )
        user = User.objects.create_user(
            username=signup_otp.username,
            email=signup_otp.email,
            password=password,
        )
        Profile.objects.create(
            user=user,
            role='institution',
            institution=institution,
            region=institution.region,
        )
        signup_otp.verified = True
        signup_otp.save(update_fields=['verified'])
        audit(
            None,
            user,
            'institution_registered',
            f'Institution {institution.name} ({institution.code}) successfully registered and verified via email OTP.',
            scope_type='operations',
            institution=institution,
            details={
                'username': user.username,
                'email': user.email,
                'institution_code': institution.code,
                'region': institution.region,
            }
        )

    profile(user)
    login(request, user)
    return JsonResponse({
        'message': f'Institution account successfully registered and activated.',
        'user': actor_json(user),
    })

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
    return {
        'id': str(e.pk), 'name': e.name, 'size': e.size, 'checksum': e.checksum,
        'received_at': e.received_at.isoformat(), 'safety': e.safety, 'scan_note': e.scan_note,
        'review': e.review, 'finding': e.finding,
        'reviewed_at': e.reviewed_at.isoformat() if e.reviewed_at else None,
        'obligation_id': e.obligation_id, 'valid_from': e.valid_from, 'valid_until': e.valid_until,
        'storage_state': getattr(e, 'storage_state', 'local'),
        'drive_file_id': getattr(e, 'drive_file_id', ''),
        'mime_type': getattr(e, 'mime_type', 'application/octet-stream'),
        'security_reviewer': e.security_reviewer.get_full_name() if getattr(e, 'security_reviewer', None) else None,
        'security_reviewed_at': e.security_reviewed_at.isoformat() if getattr(e, 'security_reviewed_at', None) else None
    }

def obligation_json(o):
    d = o.deadlines.filter(kind='submission').order_by('-id').first()
    latest = o.submissions.order_by('-received_at').first()
    lifecycle = get_obligation_lifecycle(o)
    return {
        'id': o.pk, 'application_id': str(o.application_id), 'reference': o.application.reference,
        'institution': o.application.institution.name, 'title': o.title, 'kind': o.kind,
        'status': o.status, 'lifecycle': lifecycle, 'source': o.source, 'due_at': d.due_at if d else None,
        'overdue': bool(d and d.active and d.due_at < timezone.now()),
        'submitted_at': latest.received_at if latest else None,
        'submitted_on_time': (latest.received_at <= d.due_at) if latest and d else None,
        'valid_from': o.valid_from, 'valid_until': o.valid_until,
        'expired': bool(o.valid_until and o.valid_until < timezone.localdate()),
        'accepted_evidence_id': str(o.accepted_evidence_id) if o.accepted_evidence_id else None,
        'verifier_id': o.verifier_id, 'verifier': o.verifier.get_full_name(), 'finding': o.finding,
        'submissions': [evidence_json(e) for e in o.submissions.order_by('-received_at')]
    }

@endpoint()
def application_detail(request, pk):
    c = permitted_case(request.user, pk)
    result = case_json(c)
    permitted_events = permitted_audit_events(request.user).filter(application=c)
    result.update({
        'route': c.route, 'stage_index': c.stage_index,
        'policy': {'name': c.policy.name, 'version': c.policy.version, 'source': c.policy.source, 'demo': c.policy.demo} if c.policy else None,
        'external_id': c.external_id, 'appeal_used': c.appeal_used, 'published_at': c.published_at,
        'deficiencies': list(c.deficiencies.order_by('id').values('id', 'finding', 'requirement', 'response', 'resolved', 'resolution', 'evidence_id')),
        'stages': list(c.stages.order_by('id').values('stage', 'entered_at', 'completed_at', 'finding')),
        'evidence': [evidence_json(e) for e in c.evidence.order_by('-received_at')],
        'obligations': [obligation_json(o) for o in c.obligations.select_related('application__institution', 'verifier')],
        'visits': list(c.visits.order_by('-id').values('id', 'starts_at', 'ends_at', 'status', 'report', 'representatives', 'video_url', 'cancellation_reason', 'late_scheduled', 'report_submitted_at')),
        'decisions': list(c.decisions.order_by('id').values('kind', 'outcome', 'reason', 'actor__first_name', 'created_at', 'case_version')),
        'deadlines': list(c.deadlines.order_by('-active', 'due_at').values('id', 'title', 'kind', 'due_at', 'active', 'revision', 'source', 'owner__first_name')),
        'audit': list(permitted_events.order_by('-created_at')[:100].values('id', 'action', 'reason', 'actor__username', 'created_at', 'before_version', 'after_version', 'details', 'correlation_id'))
    })
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
    obligations = Obligation.objects.filter(application__in=qs).select_related('application__institution', 'verifier')
    upcoming = deadlines.order_by('due_at')[:8]

    stage_ageing = []
    for stage_data in qs.exclude(stage__in=['APPROVED', 'REJECTED', 'WITHDRAWN']).values('stage').annotate(count=Count('id')).order_by('stage'):
        st = stage_data['stage']
        cases_in_st = list(qs.filter(stage=st))
        ages = [(now - c.created_at).days for c in cases_in_st]
        avg_age = round(sum(ages) / len(ages), 1) if ages else 0
        max_age = max(ages) if ages else 0
        stage_ageing.append({'stage': st, 'count': stage_data['count'], 'avg_age_days': avg_age, 'max_age_days': max_age})

    workload = []
    for user_data in active.filter(assigned_to__isnull=False).values('assigned_to__id', 'assigned_to__first_name', 'assigned_to__last_name', 'assigned_to__profile__role').annotate(count=Count('id')).order_by('-count')[:10]:
        name = f"{user_data['assigned_to__first_name']} {user_data['assigned_to__last_name']}".strip() or 'Officer'
        workload.append({'user_id': user_data['assigned_to__id'], 'name': name, 'role': user_data['assigned_to__profile__role'], 'count': user_data['count']})

    institution_pendency = qs.filter(stage='RETURNED').count()
    authority_pendency = active.exclude(stage='RETURNED').count()

    evc_qs = qs.filter(stage='EVC')
    evc_summary = {
        'scheduling_pending': evc_qs.exclude(visits__status__in=['confirmed', 'conducted', 'reported']).count(),
        'visit_pending': EVCVisit.objects.filter(application__in=qs, status='confirmed').count(),
        'report_pending': EVCVisit.objects.filter(application__in=qs, status='conducted').count(),
        'reported': EVCVisit.objects.filter(application__in=qs, status='reported').count()
    }

    validity_summary = {'total': obligations.count(), 'fulfilled': 0, 'expiring_soon': 0, 'expired': 0, 'renewal_pending': 0, 'pending_submission': 0}
    for o in obligations:
        lc = get_obligation_lifecycle(o)
        if lc in validity_summary:
            validity_summary[lc] += 1
        elif lc == 'renewal_required':
            validity_summary['expiring_soon'] += 1

    regional_perf = []
    regions = set(qs.values_list('institution__region', flat=True).distinct())
    for r in sorted(regions):
        rqs = qs.filter(institution__region=r)
        tot = rqs.count()
        appr = rqs.filter(stage='APPROVED').count()
        rej = rqs.filter(stage='REJECTED').count()
        avg_days = round(sum((now - c.created_at).days for c in rqs) / tot, 1) if tot else 0
        regional_perf.append({'region': r or 'Other', 'total': tot, 'approved': appr, 'rejected': rej, 'active': tot - appr - rej, 'avg_days': avg_days})

    return JsonResponse({
        'as_of': now, 'total': qs.count(), 'active': active.count(),
        'approved': qs.filter(stage='APPROVED').count(), 'rejected': qs.filter(stage='REJECTED').count(),
        'overdue': deadlines.filter(due_at__lt=now).values('application').distinct().count(),
        'evc_pending': evc_qs.count(), 'open_obligations': obligations.exclude(status='fulfilled').count(),
        'by_stage': list(qs.values('stage').annotate(count=Count('id')).order_by('stage')),
        'by_region': list(qs.values('institution__region').annotate(count=Count('id')).order_by('institution__region')),
        'stage_ageing': stage_ageing, 'workload': workload,
        'pendency': {'institution': institution_pendency, 'authority': authority_pendency},
        'evc_summary': evc_summary, 'validity_summary': validity_summary, 'regional_performance': regional_perf,
        'deadlines': [{'id': d.pk, 'title': d.title, 'kind': d.kind, 'due_at': d.due_at, 'reference': d.application.reference, 'application_id': str(d.application_id), 'institution': d.application.institution.name, 'overdue': d.due_at < now, 'owner': d.owner.get_full_name()} for d in upcoming],
        'recent': [case_json(c) for c in qs.order_by('-updated_at')[:6]],
        'audit': list(permitted_audit_events(request.user).filter(application__in=qs).order_by('-created_at')[:6].values('action', 'reason', 'application__reference', 'created_at', 'actor__username'))
    })

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
    suffix = Path(file.name).suffix.lower()
    mime_map = {'.pdf': 'application/pdf', '.doc': 'application/msword', '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', '.txt': 'text/plain'}
    require(suffix in mime_map, 'Upload PDF, DOC, DOCX or UTF-8 text files.')
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
        e = Evidence(
            application=case, obligation=obligation, file=file,
            name=Path(file.name).name[:240], checksum=digest.hexdigest(), size=file.size,
            uploaded_by=request.user, storage_state='pending', mime_type=mime_map.get(suffix, 'application/octet-stream')
        )
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

@endpoint(('POST',))
def security_review(request, pk):
    return JsonResponse(security_review_evidence(request.user, pk, body(request)))

@endpoint()
def download(request, pk):
    e = Evidence.objects.filter(pk=pk).first()
    require(e, 'Evidence not found.', 404)
    permitted_case(request.user, e.application_id)
    require(e.safety in ['clean', 'demo_validated', 'security_reviewed'], 'Evidence remains quarantined or was rejected.', 409)
    if e.drive_file_id and getattr(e, 'storage_state', '') == 'stored':
        try:
            from website import drive
            if drive.connected():
                stream = drive.download_stream(e)
                response = FileResponse(stream, as_attachment=True, filename=e.name, content_type=e.mime_type or 'application/octet-stream')
                response['Cache-Control'] = 'private, no-store'
                response['X-Content-Type-Options'] = 'nosniff'
                audit(e.application, request.user, 'evidence_downloaded', 'Authorised Drive attachment download', details={'evidence_id': str(e.pk), 'drive_file_id': e.drive_file_id})
                return response
        except Exception:
            pass
    response = FileResponse(e.file.open('rb'), as_attachment=True, filename=e.name, content_type=getattr(e, 'mime_type', 'application/octet-stream') or 'application/octet-stream')
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
    qs = permitted_audit_events(request.user)
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
        writer.writerow(['Reference', 'Institution', 'Requirement', 'Kind', 'Status', 'Lifecycle', 'Validity start', 'Validity end', 'Verifier'])
        rows = ((o.application.reference, o.application.institution.name, o.title, o.kind, o.status, get_obligation_lifecycle(o), o.valid_from, o.valid_until, o.verifier.get_full_name()) for o in Obligation.objects.filter(application__in=qs).select_related('application__institution', 'verifier'))
    elif report_type == 'validity':
        writer.writerow(['Reference', 'Institution', 'Requirement', 'Kind', 'Valid from', 'Valid until', 'Days remaining', 'Lifecycle', 'Verifier'])
        today = timezone.localdate()
        rows = ((o.application.reference, o.application.institution.name, o.title, o.kind, o.valid_from, o.valid_until, (o.valid_until - today).days if o.valid_until else '', get_obligation_lifecycle(o), o.verifier.get_full_name()) for o in Obligation.objects.filter(application__in=qs, valid_until__isnull=False).select_related('application__institution', 'verifier'))
    elif report_type == 'evc':
        writer.writerow(['Reference', 'Institution', 'Region', 'Coordinator', 'Visit start', 'Visit end', 'Status', 'Late scheduled', 'Report submitted at'])
        rows = ((v.application.reference, v.application.institution.name, v.application.institution.region, v.coordinator.get_full_name(), v.starts_at.isoformat(), v.ends_at.isoformat(), v.status, v.late_scheduled, v.report_submitted_at.isoformat() if v.report_submitted_at else '') for v in EVCVisit.objects.filter(application__in=qs).select_related('application__institution', 'coordinator'))
    elif report_type == 'escalations':
        writer.writerow(['Reference', 'Institution', 'Title', 'Kind', 'Due at', 'Revision', 'Owner', 'Escalation owner', 'Status', 'Reason'])
        rows = ((e.deadline.application.reference, e.deadline.application.institution.name, e.deadline.title, e.deadline.kind, e.deadline.due_at.isoformat(), e.revision, e.deadline.owner.get_full_name(), e.owner.get_full_name(), e.status, e.reason) for e in Escalation.objects.filter(deadline__application__in=qs).select_related('deadline__application__institution', 'deadline__owner', 'owner'))
    elif report_type == 'regional_performance':
        writer.writerow(['Region', 'Total cases', 'Approved', 'Rejected', 'In progress', 'Avg age days'])
        regions = set(qs.values_list('institution__region', flat=True).distinct())
        reg_rows = []
        now = timezone.now()
        for r in sorted(regions):
            rqs = qs.filter(institution__region=r)
            tot = rqs.count()
            appr = rqs.filter(stage='APPROVED').count()
            rej = rqs.filter(stage='REJECTED').count()
            inp = tot - appr - rej
            avg_age = sum((now - c.created_at).days for c in rqs) / tot if tot else 0
            reg_rows.append((r or 'Unassigned', tot, appr, rej, inp, round(avg_age, 1)))
        rows = reg_rows
    elif report_type == 'audit':
        writer.writerow(['Reference', 'Actor', 'Action', 'Reason', 'Recorded at', 'Before version', 'After version'])
        audit_events = permitted_audit_events(request.user).filter(application__in=qs).select_related('application', 'actor').order_by('created_at')
        rows = ((e.application.reference if e.application else '', e.actor.username if e.actor else 'scheduler', e.action, e.reason, e.created_at.isoformat(), e.before_version, e.after_version) for e in audit_events)
    else:
        writer.writerow(['Reference', 'Institution', 'Region', 'Category', 'Stage', 'Owner', 'Age days', 'Created at'])
        rows = ((c.reference, c.institution.name, c.institution.region, c.category, c.stage, c.assigned_to.get_full_name() if c.assigned_to else '', (timezone.now()-c.created_at).days, c.created_at.isoformat()) for c in qs)
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
