import hashlib
import io
import json
import uuid
import zipfile
from pathlib import Path

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.models import Application, Profile
from core.services import audit, notify, profile, require
from .models import Document, Grievance, Message

CATEGORIES = ['approval_delay', 'document_issue', 'portal_issue', 'payment_issue', 'conduct', 'other']
ADMIN_ROLES = ['bureau', 'regional']


def scoped(user):
    p = profile(user)
    qs = Grievance.objects.select_related('institution', 'application', 'submitted_by')
    if p.role == 'institution' and p.institution_id:
        return qs.filter(institution_id=p.institution_id)
    if p.role in ['bureau', 'auditor']:
        return qs
    if p.role == 'regional' and p.region:
        return qs.filter(institution__region=p.region)
    return qs.none()


def permitted(user, pk):
    item = scoped(user).filter(pk=pk).first()
    require(item, 'Submission not found in your authorised scope.', 404)
    return item


def text_field(data, key, minimum, maximum):
    value = data.get(key, '')
    require(isinstance(value, str), f'{key} must be text.')
    value = value.strip()
    require(minimum <= len(value) <= maximum, f'{key.replace("_", " ").capitalize()} must contain {minimum}–{maximum} characters.')
    return value


def validate_file(file):
    require(0 < file.size <= 5 * 1024 * 1024, 'Each document must be nonempty and no larger than 5 MB.')
    name = Path(file.name.replace('\\', '/')).name[:240]
    suffix = Path(name).suffix.lower()
    mime = {'.pdf': 'application/pdf', '.doc': 'application/msword',
            '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', '.txt': 'text/plain'}
    require(suffix in mime, 'Upload PDF, DOC, DOCX or UTF-8 TXT documents.')
    content = file.read()
    file.seek(0)
    require(b'EICAR-STANDARD-ANTIVIRUS-TEST-FILE' not in content, 'This document contains a rejected test signature.')
    if suffix == '.pdf':
        require(content.startswith(b'%PDF-'), 'The document does not have a PDF header.')
    elif suffix == '.doc':
        require(content.startswith(bytes.fromhex('d0cf11e0a1b11ae1')), 'The document does not have a DOC header.')
    elif suffix == '.docx':
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                names = archive.namelist()
                require('[Content_Types].xml' in names and 'word/document.xml' in names, 'Invalid DOCX document.')
                require(not any('vbaproject' in n.lower() for n in names), 'Macro-enabled documents are not accepted.')
                require(sum(x.file_size for x in archive.infolist()) <= 50 * 1024 * 1024, 'The expanded document exceeds 50 MB.')
        except zipfile.BadZipFile:
            require(False, 'Invalid DOCX document.')
    else:
        try:
            content.decode('utf-8')
        except UnicodeDecodeError:
            require(False, 'Text documents must use UTF-8.')
        require(b'\x00' not in content, 'Invalid text document.')
    return {'name': name, 'mime_type': mime[suffix], 'size': len(content),
            'sha256': hashlib.sha256(content).hexdigest(), 'md5': hashlib.md5(content).hexdigest()}


def record(item, user, action, reason, **details):
    audit(item.application, user, 'website_' + action, reason,
          details={'grievance_id': str(item.pk), 'reference': item.reference, **details},
          scope_type='grievance', institution=item.institution)


def recipients(item):
    return Profile.objects.filter(user__is_active=True).filter(
        Q(role='bureau') | Q(role='regional', region=item.institution.region)).select_related('user')


def send_notice(item, user, text):
    notify(item.application, user, f'website:{item.pk}:{item.version}:{user.pk}',
           f'{item.reference}: {item.subject}', text + ' Open the Institution portal → My submissions / Administration inbox.')


def create(user, data, files):
    p = profile(user)
    require(p.role == 'institution' and p.institution_id, 'Only registered institutions can submit.', 403)
    subject = text_field(data, 'subject', 5, 200)
    description = text_field(data, 'description', 20, 10000)
    kind, category = data.get('kind'), data.get('category')
    require(kind in ['complaint', 'grievance'] and category in CATEGORIES, 'Choose a valid type and category.')
    key = uuid.UUID(str(data.get('submission_key', '')))
    application = None
    if data.get('application_id'):
        application = Application.objects.filter(pk=data['application_id'], institution_id=p.institution_id).first()
        require(application, 'Choose an application belonging to your institution.', 404)
    require(len(files) <= 5, 'Attach at most five documents per submission.')
    checked = [(file, validate_file(file)) for file in files]
    canonical = {'subject': subject, 'description': description, 'kind': kind, 'category': category,
                 'application': str(application.pk) if application else None, 'files': [m for _, m in checked]}
    fingerprint = hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()
    saved = []
    try:
        with transaction.atomic():
            User.objects.select_for_update().get(pk=user.pk)
            existing = Grievance.objects.filter(submitted_by=user, submission_key=key).first()
            if existing:
                require(existing.fingerprint == fingerprint, 'Submission key already used for different content.', 409)
                return existing, False
            item = Grievance.objects.create(reference=f'GR-{timezone.localdate().year}-{uuid.uuid4().hex[:12].upper()}',
                institution_id=p.institution_id, application=application, submitted_by=user, subject=subject,
                description=description, kind=kind, category=category, submission_key=key, fingerprint=fingerprint)
            for file, metadata in checked:
                doc = Document(grievance=item, uploaded_by=user, **metadata)
                doc.staged_file.save(metadata['name'], file, save=False)
                saved.append(doc.staged_file)
                doc.save()
            Message.objects.create(grievance=item, actor=user, text='Submission received by administration.', status=item.status)
            record(item, user, 'submitted', subject)
            for officer in recipients(item):
                send_notice(item, officer.user, description[:300])
            return item, True
    except Exception:
        for file in saved:
            file.storage.delete(file.name)
        raise


def respond(user, pk, data):
    item = permitted(user, pk)
    p = profile(user)
    text = text_field(data, 'message', 5, 5000)
    with transaction.atomic():
        item = Grievance.objects.select_for_update().get(pk=item.pk)
        require(int(data.get('expected_version', 0)) == item.version, 'Submission changed. Refresh before responding.', 409)
        target = data.get('status', item.status)
        if p.role == 'institution':
            require(item.status != 'resolved', 'This submission is resolved. Create a new submission for a further issue.', 409)
            require(target in [item.status, 'in_review'] and (target == item.status or item.status == 'awaiting_institution'), 'Institutions cannot decide a submission.', 403)
            target = 'in_review' if item.status == 'awaiting_institution' else item.status
        else:
            require(p.role in ADMIN_ROLES, 'Your account has read-only access.', 403)
            allowed = {'submitted': ['in_review', 'awaiting_institution', 'resolved'],
                       'in_review': ['in_review', 'awaiting_institution', 'resolved'],
                       'awaiting_institution': ['awaiting_institution', 'in_review', 'resolved'], 'resolved': ['in_review']}
            require(target in allowed.get(item.status, []), 'Choose a valid next status.', 409)
        item.status = target
        item.version += 1
        item.save()
        Message.objects.create(grievance=item, actor=user, text=text, status=target)
        record(item, user, 'response', text, status=target)
        if p.role == 'institution':
            for officer in recipients(item):
                send_notice(item, officer.user, text[:300])
        else:
            send_notice(item, item.submitted_by, text[:300])
    return item


def add_document(user, pk, file):
    item = permitted(user, pk)
    require(profile(user).role == 'institution', 'Only the institution can attach documents.', 403)
    require(file is not None, 'Choose a document.')
    metadata = validate_file(file)
    doc = None
    try:
        with transaction.atomic():
            item = Grievance.objects.select_for_update().get(pk=item.pk)
            require(item.status != 'resolved', 'Documents cannot be added to a resolved submission.', 409)
            require(item.documents.count() < 20, 'A submission can hold at most 20 documents.')
            doc = Document(grievance=item, uploaded_by=user, **metadata)
            doc.staged_file.save(metadata['name'], file, save=False)
            doc.save()
            item.version += 1
            item.save()
            record(item, user, 'document_received', doc.name, document_id=str(doc.pk))
        return doc
    except Exception:
        if doc and doc.staged_file:
            doc.staged_file.storage.delete(doc.staged_file.name)
        raise
