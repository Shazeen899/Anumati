from datetime import timedelta
import random
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
            if d.due_at > now + timedelta(days=rules.get('reminder_days', 3)):
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
        # No malware scanner is claimed. Only synthetic plain-text files are
        # usable in DEMO_MODE; all other files fail closed in quarantine.
        if not settings.DEMO_MODE or not e.name.lower().endswith('.txt'):
            return 'quarantine'
        try:
            with e.file.open('rb') as stream:
                raw = stream.read()
            content = raw.decode('utf-8')
            if 'EICAR-STANDARD-ANTIVIRUS-TEST-FILE' in content or '\x00' in content or '<script' in content.lower():
                raise ValueError('Unsafe demo content')
            e.safety = 'demo_validated'
            e.scan_note = 'Synthetic UTF-8 text validation only; not an antivirus result. Downloaded as an attachment.'
        except (ValueError, UnicodeError):
            e.safety = 'rejected'
            e.scan_note = 'Synthetic text validation failed. Submit a new version.'
        e.save()
        AuditEvent.objects.create(application=e.application, action='evidence_validation', reason=e.scan_note, details={'evidence_id': str(e.pk), 'safety': e.safety})
        return e.safety
