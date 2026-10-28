from datetime import timedelta
import io
from pathlib import Path
import random
import zipfile
from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from .models import Deadline, Outbox, Notification, Escalation, Evidence, AuditEvent
from .services import notify

@shared_task
def scan_deadlines():
    now = timezone.now()
    created = 0
    for pk in Deadline.objects.filter(active=True).values_list('pk', flat=True).iterator():
        with transaction.atomic():
            d = Deadline.objects.select_for_update(of=('self',)).select_related('application__policy').get(pk=pk)
            if not d.active:
                continue
            rules = d.application.policy.rules if d.application.policy else {}
            milestone = 'overdue' if d.due_at < now else 'upcoming'
            reminder_offset = rules.get('reminder_offsets', {}).get(d.kind, rules.get('reminder_days', 3))
            if milestone == 'upcoming' and d.due_at > now + timedelta(days=reminder_offset):
                continue
            recipient = d.escalation_owner if milestone == 'overdue' else d.owner
            identity = f'deadline:{d.pk}:{d.revision}:{milestone}:{recipient.pk}'
            before = Outbox.objects.filter(identity=identity).exists()
            notify(d.application, recipient, identity, f'{milestone.title()}: {d.title}', f'{d.application.reference} • Due {d.due_at.isoformat()}. Review the case and record the required action.', deadline=d, revision=d.revision)
            if not before:
                created += 1
                AuditEvent.objects.create(application=d.application, action='deadline_' + milestone, reason='Scheduled demonstration deadline policy', details={'deadline': d.pk, 'revision': d.revision})
            if milestone == 'overdue':
                Escalation.objects.get_or_create(deadline=d, revision=d.revision, defaults={'owner': d.escalation_owner})
    return created

@shared_task
def dispatch_outbox():
    now = timezone.now()
    ids = list(Outbox.objects.filter(state__in=['pending', 'processing'], available_at__lte=now).filter(Q(lease_until=None) | Q(lease_until__lt=now)).values_list('pk', flat=True)[:100])
    for pk in ids:
        # Publishing does not change durable completion. Duplicate publication is safe.
        deliver_event.delay(str(pk))
    return len(ids)

@shared_task
def deliver_event(event_id):
    now = timezone.now()
    with transaction.atomic():
        event = Outbox.objects.select_for_update(of=('self',)).select_related('deadline', 'recipient').get(pk=event_id)
        if event.state in ['completed', 'superseded', 'failed'] or event.available_at > now:
            return event.state
        if event.lease_until and event.lease_until > now:
            return 'leased'
        if event.deadline_id:
            d = Deadline.objects.select_for_update().get(pk=event.deadline_id)
            if not d.active or d.revision != event.revision:
                event.state = 'superseded'
                event.save()
                return event.state
            if ':upcoming:' in event.identity and event.recipient_id != d.owner_id:
                event.state = 'superseded'
                event.save()
                return event.state
        event.state, event.lease_until = 'processing', now + timedelta(minutes=2)
        event.attempts += 1
        event.save()
        # The in-app effect is atomic and exactly one per outbox identity.
        Notification.objects.get_or_create(event=event, defaults={'recipient': event.recipient})
    try:
        # Local mail sink only. No external transmission or false delivery receipt.
        send_mail(event.title, event.body, settings.DEFAULT_FROM_EMAIL, [event.recipient.email or f'{event.recipient.username}@example.test'], fail_silently=False)
    except Exception:
        with transaction.atomic():
            event = Outbox.objects.select_for_update().get(pk=event_id)
            event.state = 'failed' if event.attempts >= 5 else 'pending'
            event.last_error = 'Test mail sink failed; in-app notification persists. Retrying may create a duplicate mail file.'
            event.lease_until = None
            event.available_at = timezone.now() + timedelta(seconds=min(300, 2 ** event.attempts * 5) + random.randint(0, 5))
            event.save()
        return event.state
    Outbox.objects.filter(pk=event_id).update(state='completed', lease_until=None, last_error='')
    return 'completed'

@shared_task
def scan_pending_files():
    for pk in Evidence.objects.filter(safety='quarantine').values_list('pk', flat=True)[:100]:
        scan_file.delay(str(pk))

@shared_task
def scan_file(evidence_id):
    with transaction.atomic():
        e = Evidence.objects.select_for_update().get(pk=evidence_id)
        if e.safety != 'quarantine':
            return e.safety
        suffix = Path(e.name).suffix.lower()
        try:
            with e.file.open('rb') as stream:
                raw = stream.read()
            if b'EICAR-STANDARD-ANTIVIRUS-TEST-FILE' in raw:
                raise ValueError('Rejected malware test signature')
            if suffix == '.txt':
                content = raw.decode('utf-8')
                if '\x00' in content or '<script' in content.lower():
                    raise ValueError('Unsafe script or null characters in text document')
                if settings.DEMO_MODE:
                    e.safety = 'demo_validated'
                    e.scan_note = 'Synthetic UTF-8 text validation only; not an antivirus result. Downloaded as an attachment.'
                else:
                    e.scan_note = 'Format check passed; awaiting manual security review or approved scanner.'
            elif suffix == '.pdf':
                if not raw.startswith(b'%PDF-'):
                    raise ValueError('Invalid PDF header')
                e.scan_note = 'Valid PDF header confirmed; awaiting manual security review or approved scanner.'
            elif suffix == '.doc':
                if not raw.startswith(bytes.fromhex('d0cf11e0a1b11ae1')):
                    raise ValueError('Invalid DOC header')
                e.scan_note = 'Valid DOC header confirmed; awaiting manual security review or approved scanner.'
            elif suffix == '.docx':
                with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                    names = archive.namelist()
                    if '[Content_Types].xml' not in names or 'word/document.xml' not in names:
                        raise ValueError('Invalid DOCX document')
                    if any('vbaproject' in n.lower() for n in names):
                        raise ValueError('Macro-enabled documents are not permitted')
                e.scan_note = 'Valid DOCX package confirmed without macros; awaiting manual security review or approved scanner.'
            else:
                raise ValueError(f'Unsupported file type {suffix}')
        except Exception as exc:
            e.safety = 'rejected'
            e.scan_note = f'Validation failed: {str(exc)}'
        e.save()
        AuditEvent.objects.create(application=e.application, action='evidence_validation', reason=e.scan_note, details={'evidence_id': str(e.pk), 'safety': e.safety})
        return e.safety

@shared_task
def upload_pending_evidence():
    try:
        from website import drive
        if not drive.connected():
            return 0
    except Exception:
        return 0

    now = timezone.now()
    ids = list(Evidence.objects.filter(storage_state__in=['pending', 'failed']).filter(Q(upload_lease_until=None) | Q(upload_lease_until__lt=now)).values_list('pk', flat=True)[:20])
    uploaded = 0
    for pk in ids:
        with transaction.atomic():
            e = Evidence.objects.select_for_update().get(pk=pk)
            if e.storage_state == 'stored':
                continue
            e.storage_state = 'uploading'
            e.upload_lease_until = now + timedelta(minutes=3)
            e.upload_attempts += 1
            e.save()
        try:
            drive_id, md5 = drive.upload_file(e.file, e.name, e.mime_type or 'application/octet-stream', e.drive_file_id)
            with transaction.atomic():
                e = Evidence.objects.select_for_update().get(pk=pk)
                e.storage_state = 'stored'
                e.drive_file_id = drive_id
                e.drive_md5 = md5
                e.upload_lease_until = None
                e.upload_error = ''
                e.save()
                AuditEvent.objects.create(application=e.application, action='evidence_stored_in_drive', reason='Evidence synchronized to designated Google Drive folder.', details={'evidence_id': str(e.pk), 'drive_file_id': drive_id})
            uploaded += 1
        except Exception as exc:
            with transaction.atomic():
                e = Evidence.objects.select_for_update().get(pk=pk)
                e.storage_state = 'failed' if e.upload_attempts >= 8 else 'pending'
                e.upload_error = str(exc)
                e.upload_lease_until = None
                e.save()
    return uploaded
