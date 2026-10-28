from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .drive import DriveClient, DriveError, connected
from .models import Document
from .services import record


@shared_task
def dispatch_documents():
    # The database is the durable queue. No broker availability is required at intake.
    if not connected():
        return 0
    now = timezone.now()
    ids = Document.objects.filter(Q(state='pending', available_at__lte=now) |
                                  Q(state='uploading', lease_until__lt=now)).values_list('pk', flat=True)[:50]
    for pk in ids:
        upload_document.delay(str(pk))
    cleanup_staging()


@shared_task
def upload_document(pk):
    now = timezone.now()
    with transaction.atomic():
        doc = Document.objects.select_for_update().filter(pk=pk).first()
        if not doc or doc.state == 'stored' or doc.state == 'failed':
            return
        if doc.state == 'uploading' and doc.lease_until and doc.lease_until > now:
            return
        if doc.available_at > now:
            return
        doc.state = 'uploading'
        doc.attempts += 1
        doc.lease_until = now + timedelta(minutes=10)
        doc.save()
    try:
        client = DriveClient()
        if not doc.drive_file_id:
            client.check_folder()
            doc.drive_file_id = client.reserve_id()
            doc.drive_folder_id = settings.GOOGLE_DRIVE_FOLDER_ID
            doc.save(update_fields=['drive_file_id', 'drive_folder_id'])
        client.upload(doc)
        with transaction.atomic():
            doc = Document.objects.select_for_update().get(pk=pk)
            doc.state, doc.stored_at, doc.lease_until, doc.last_error = 'stored', timezone.now(), None, ''
            doc.save()
            record(doc.grievance, None, 'document_stored', 'Document verified in Google Drive.', document_id=str(doc.pk))
        cleanup_document(doc)
    except Exception as error:
        # Do not store provider responses, secrets, paths or document bytes in logs.
        Document.objects.filter(pk=pk).exclude(state='stored').update(
            state='failed' if doc.attempts >= 8 else 'pending', lease_until=None,
            available_at=timezone.now() + timedelta(seconds=min(3600, 30 * 2 ** doc.attempts)),
            last_error=str(error)[:240] if isinstance(error, DriveError) else 'Upload could not complete. Administrator retry is available.')


def cleanup_document(doc):
    if doc.staged_file:
        doc.staged_file.storage.delete(doc.staged_file.name)
        Document.objects.filter(pk=doc.pk, state='stored').update(staged_file='')


def cleanup_staging():
    for doc in Document.objects.filter(state='stored').exclude(staged_file='')[:100]:
        cleanup_document(doc)
