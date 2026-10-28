import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class Grievance(models.Model):
    """Independent intake records bridged to core through real PostgreSQL FKs."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(max_length=40, unique=True)
    institution = models.ForeignKey('core.Institution', on_delete=models.PROTECT)
    application = models.ForeignKey('core.Application', null=True, blank=True,
                                    related_name='grievances', on_delete=models.PROTECT)
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    kind = models.CharField(max_length=20, choices=[('grievance', 'Grievance'), ('complaint', 'Complaint')])
    subject = models.CharField(max_length=200)
    description = models.TextField()
    category = models.CharField(max_length=40)
    status = models.CharField(max_length=30, default='submitted', db_index=True)
    version = models.PositiveIntegerField(default=1)
    submission_key = models.UUIDField()
    fingerprint = models.CharField(max_length=64)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['submitted_by', 'submission_key'], name='website_submission_unique')]


def staging_path(instance, filename):
    return f'website-staging/{instance.pk.hex}'


class Document(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    grievance = models.ForeignKey(Grievance, related_name='documents', on_delete=models.PROTECT)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    name = models.CharField(max_length=240)
    mime_type = models.CharField(max_length=120)
    size = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)
    md5 = models.CharField(max_length=32)
    staged_file = models.FileField(upload_to=staging_path, blank=True)
    state = models.CharField(max_length=20, default='pending', db_index=True)
    drive_file_id = models.CharField(max_length=200, blank=True)
    drive_folder_id = models.CharField(max_length=200, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    available_at = models.DateTimeField(default=timezone.now)
    lease_until = models.DateTimeField(null=True)
    last_error = models.CharField(max_length=240, blank=True)
    received_at = models.DateTimeField(default=timezone.now)
    stored_at = models.DateTimeField(null=True)


class Message(models.Model):
    grievance = models.ForeignKey(Grievance, related_name='messages', on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    text = models.TextField()
    status = models.CharField(max_length=30)
    created_at = models.DateTimeField(default=timezone.now)
