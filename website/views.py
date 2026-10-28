import io
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

from django.conf import settings
from django.db.models import Count, Q
from django.http import FileResponse, HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.utils import timezone

from core.models import Application
from core.services import profile, require
from core.views import body, endpoint
from . import drive, services
from .models import Document


def document_json(doc):
    return {'id': str(doc.pk), 'name': doc.name, 'size': doc.size, 'sha256': doc.sha256,
            'state': doc.state, 'received_at': doc.received_at, 'stored_at': doc.stored_at,
            'last_error': doc.last_error, 'download_url': f'/website/api/documents/{doc.pk}/download' if doc.state == 'stored' else None}


def item_json(item, detail=False):
    result = {'id': str(item.pk), 'reference': item.reference, 'subject': item.subject,
              'kind': item.kind, 'category': item.category, 'institution': item.institution.name,
              'institution_code': item.institution.code, 'region': item.institution.region,
              'application_id': str(item.application_id) if item.application_id else None,
              'application_reference': item.application.reference if item.application_id else None,
              'status': item.status, 'version': item.version, 'created_at': item.created_at, 'updated_at': item.updated_at}
    if detail:
        result.update(description=item.description,
            documents=[document_json(doc) for doc in item.documents.order_by('received_at')],
            messages=[{'id': m.pk, 'text': m.text, 'status': m.status,
                       'actor': m.actor.get_full_name() or m.actor.username, 'created_at': m.created_at}
                      for m in item.messages.select_related('actor').order_by('created_at', 'id')])
    return result


@endpoint()
def metadata(request):
    p = profile(request.user)
    require(p.role in ['institution', 'bureau', 'regional', 'auditor', 'operator'], 'This portal is not available to your role.', 403)
    applications = Application.objects.filter(institution_id=p.institution_id).order_by('-created_at') if p.role == 'institution' and p.institution_id else Application.objects.none()
    return JsonResponse({'categories': services.CATEGORIES,
        'institution': p.institution.name if p.institution_id else None,
        'applications': list(applications.values('id', 'reference', 'category', 'stage')),
        'drive_connected': drive.connected(), 'can_manage_drive': p.role in ['bureau', 'operator'],
        'can_respond': p.role in services.ADMIN_ROLES or p.role == 'institution'})


@endpoint(('GET', 'POST'))
def submissions(request):
    if request.method == 'POST':
        item, created = services.create(request.user, request.POST, request.FILES.getlist('documents'))
        return JsonResponse(item_json(item, True), status=201 if created else 200)
    qs = services.scoped(request.user)
    counts = dict(qs.values_list('status').annotate(total=Count('id')))
    search = request.GET.get('search', '').strip()[:200]
    if search:
        qs = qs.filter(Q(reference__icontains=search) | Q(subject__icontains=search) | Q(institution__name__icontains=search))
    if request.GET.get('status'):
        qs = qs.filter(status=request.GET['status'])
    if request.GET.get('application'):
        qs = qs.filter(application_id=request.GET['application'])
    page = max(1, int(request.GET.get('page', 1)))
    return JsonResponse({'items': [item_json(i) for i in qs.order_by('-updated_at')[(page-1)*20:page*20]],
                         'total': qs.count(), 'counts': counts, 'page': page})


@endpoint(('GET', 'POST'))
def detail(request, pk):
    item = services.respond(request.user, pk, body(request)) if request.method == 'POST' else services.permitted(request.user, pk)
    return JsonResponse(item_json(item, True))


@endpoint(('POST',))
def upload(request, pk):
    doc = services.add_document(request.user, pk, request.FILES.get('document'))
    return JsonResponse(document_json(doc), status=201)


@endpoint()
def download(request, pk):
    doc = Document.objects.filter(pk=pk, grievance__in=services.scoped(request.user)).first()
    require(doc, 'Document not found in your authorised scope.', 404)
    require(doc.state == 'stored', 'This document is awaiting Google Drive storage.', 409)
    try:
        content = drive.DriveClient().download(doc)
    except drive.DriveError as error:
        return JsonResponse({'error': str(error)}, status=503)
    services.record(doc.grievance, request.user, 'document_downloaded', 'Authorised document download.', document_id=str(doc.pk))
    response = FileResponse(io.BytesIO(content), as_attachment=True, filename=doc.name, content_type='application/octet-stream')
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    return response


def drive_admin(request):
    require(profile(request.user).role in ['bureau', 'operator'], 'Drive setup is restricted to bureau and operations.', 403)


@endpoint(('GET', 'POST'))
def storage(request):
    drive_admin(request)
    if request.method == 'POST':
        doc = Document.objects.filter(pk=body(request).get('document_id'), state__in=['pending', 'failed']).first()
        require(doc, 'Choose a pending or failed document.', 404)
        Document.objects.filter(pk=doc.pk, state__in=['pending', 'failed']).update(state='pending', attempts=0, available_at=timezone.now(), last_error='')
        services.record(doc.grievance, request.user, 'storage_retry', 'Administrator requested Drive upload retry.', document_id=str(doc.pk))
    return JsonResponse({'configured': drive.configured(), 'connected': drive.connected(),
        'folder_url': 'https://drive.google.com/drive/folders/' + settings.GOOGLE_DRIVE_FOLDER_ID,
        'redirect_uri': settings.GOOGLE_DRIVE_REDIRECT_URI,
        'pending': [{'id': str(d.pk), 'reference': d.grievance.reference, 'name': d.name, 'state': d.state,
                     'last_error': d.last_error} for d in Document.objects.exclude(state='stored').select_related('grievance').order_by('received_at')[:100]]})


@endpoint(('POST',))
def oauth_start(request):
    drive_admin(request)
    try:
        client, _ = drive.client_config()
    except drive.DriveError as error:
        return JsonResponse({'error': str(error)}, status=503)
    state = secrets.token_urlsafe(32)
    request.session['drive_oauth'] = {'state': state, 'created': time.time(), 'user': request.user.pk}
    return JsonResponse({'url': 'https://accounts.google.com/o/oauth2/v2/auth?' + urlencode({
        'client_id': client, 'redirect_uri': settings.GOOGLE_DRIVE_REDIRECT_URI, 'response_type': 'code',
        'scope': 'https://www.googleapis.com/auth/drive', 'access_type': 'offline', 'prompt': 'consent', 'state': state})})


@endpoint()
def oauth_callback(request):
    drive_admin(request)
    saved = request.session.pop('drive_oauth', {})
    require(saved.get('user') == request.user.pk and time.time() - saved.get('created', 0) < 600
            and secrets.compare_digest(saved.get('state', ''), request.GET.get('state', '')),
            'Google connection expired or the verification state did not match. Start again.', 400)
    require(request.GET.get('code') and not request.GET.get('error'), 'Google authorization was not completed.')
    try:
        drive.exchange_code(request.GET['code'])
    except drive.DriveError as error:
        return JsonResponse({'error': str(error)}, status=503)
    from core.services import audit
    audit(None, request.user, 'website_drive_connected', 'Administrator authorized the configured Google Drive folder.')
    return redirect('/website/?storage=1')


def frontend(request, path=''):
    root = (settings.BASE_DIR / 'website' / 'frontend' / 'dist').resolve()
    target = (root / path).resolve()
    if path.startswith('assets/') and target.is_relative_to(root) and target.is_file():
        import mimetypes
        return FileResponse(target.open('rb'), content_type=mimetypes.guess_type(target)[0] or 'application/octet-stream')
    if path.startswith('api/'):
        return JsonResponse({'error': 'Endpoint not found.'}, status=404)
    if not (root / 'index.html').exists():
        return HttpResponse('Build the institution portal: cd website/frontend && npm run build', status=503)
    return FileResponse((root / 'index.html').open('rb'), content_type='text/html')
