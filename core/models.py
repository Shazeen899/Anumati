import uuid
from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

class Institution(models.Model):
    name = models.CharField(max_length=240)
    code = models.CharField(max_length=40, unique=True)
    region = models.CharField(max_length=80)
    kind = models.CharField(max_length=30, default='Technical institution')
    city = models.CharField(max_length=100, blank=True)

class Profile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    role = models.CharField(max_length=30, choices=[(x, x) for x in ['institution', 'reviewer', 'evc', 'committee', 'regional', 'bureau', 'executive', 'council', 'policy', 'auditor', 'operator']])
    institution = models.ForeignKey(Institution, null=True, blank=True, on_delete=models.PROTECT)
    region = models.CharField(max_length=80, blank=True)

class Policy(models.Model):
    name = models.CharField(max_length=120)
    version = models.PositiveIntegerField()
    state = models.CharField(max_length=20, default='draft')
    source = models.TextField()
    rules = models.JSONField(default=dict)
    effective_at = models.DateTimeField(default=timezone.now)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    demo = models.BooleanField(default=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['name', 'version'], name='policy_version_unique')]

class Application(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(max_length=50, unique=True)
    institution = models.ForeignKey(Institution, on_delete=models.PROTECT)
    category = models.CharField(max_length=50)
    academic_year = models.CharField(max_length=9)
    source = models.CharField(max_length=40, default='Simulated intake')
    external_id = models.CharField(max_length=100)
    payment_verified = models.BooleanField(default=False)
    policy = models.ForeignKey(Policy, null=True, on_delete=models.PROTECT)
    route = models.JSONField(default=list)
    stage_index = models.PositiveIntegerField(default=0)
    stage = models.CharField(max_length=40, default='DRAFT', db_index=True)
    version = models.PositiveIntegerField(default=1)
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, related_name='assigned_cases', on_delete=models.PROTECT)
    blocker = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    submitted_at = models.DateTimeField(null=True)
    updated_at = models.DateTimeField(auto_now=True)
    dependency = models.ForeignKey('self', null=True, blank=True, on_delete=models.PROTECT)
    published_at = models.DateTimeField(null=True)
    appeal_used = models.BooleanField(default=False)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['source', 'external_id'], name='unique_source_event')]

class StageRecord(models.Model):
    application = models.ForeignKey(Application, related_name='stages', on_delete=models.PROTECT)
    stage = models.CharField(max_length=40)
    entered_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True)
    finding = models.TextField(blank=True)

class Deficiency(models.Model):
    application = models.ForeignKey(Application, related_name='deficiencies', on_delete=models.PROTECT)
    finding = models.TextField()
    requirement = models.CharField(max_length=240)
    evidence = models.ForeignKey('Evidence', null=True, on_delete=models.PROTECT)
    response = models.TextField(blank=True)
    resolved = models.BooleanField(default=False)
    resolution = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)

class EVCVisit(models.Model):
    application = models.ForeignKey(Application, related_name='visits', on_delete=models.PROTECT)
    coordinator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    status = models.CharField(max_length=25, default='confirmed')
    report = models.TextField(blank=True)
    representatives = models.TextField(blank=True)
    video_url = models.URLField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)

class Decision(models.Model):
    application = models.ForeignKey(Application, related_name='decisions', on_delete=models.PROTECT)
    kind = models.CharField(max_length=30)
    outcome = models.CharField(max_length=30)
    reason = models.TextField()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(default=timezone.now)
    case_version = models.PositiveIntegerField()
    class Meta:
        constraints = [models.UniqueConstraint(fields=['application', 'case_version'], name='decision_version_unique')]

class Obligation(models.Model):
    application = models.ForeignKey(Application, related_name='obligations', on_delete=models.PROTECT)
    decision = models.ForeignKey(Decision, null=True, on_delete=models.PROTECT)
    title = models.CharField(max_length=240)
    kind = models.CharField(max_length=30, default='undertaking')
    source = models.TextField()
    status = models.CharField(max_length=30, default='open')
    verifier = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    accepted_evidence = models.ForeignKey('Evidence', null=True, related_name='+', on_delete=models.PROTECT)
    valid_from = models.DateField(null=True)
    valid_until = models.DateField(null=True)
    course_years = models.PositiveIntegerField(default=4)
    finding = models.TextField(blank=True)

def evidence_path(instance, filename):
    return f'evidence/{instance.id}/{uuid.uuid4().hex}'

class Evidence(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(Application, related_name='evidence', on_delete=models.PROTECT)
    obligation = models.ForeignKey(Obligation, null=True, related_name='submissions', on_delete=models.PROTECT)
    file = models.FileField(upload_to=evidence_path)
    name = models.CharField(max_length=240)
    checksum = models.CharField(max_length=64)
    size = models.PositiveIntegerField()
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    received_at = models.DateTimeField(default=timezone.now)
    safety = models.CharField(max_length=25, default='quarantine')
    scan_note = models.TextField(blank=True)
    review = models.CharField(max_length=20, default='pending')
    finding = models.TextField(blank=True)
    reviewed_at = models.DateTimeField(null=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, related_name='+', on_delete=models.PROTECT)
    valid_from = models.DateField(null=True)
    valid_until = models.DateField(null=True)

class Deadline(models.Model):
    application = models.ForeignKey(Application, related_name='deadlines', on_delete=models.PROTECT)
    obligation = models.ForeignKey(Obligation, null=True, related_name='deadlines', on_delete=models.PROTECT)
    kind = models.CharField(max_length=30)
    title = models.CharField(max_length=240)
    due_at = models.DateTimeField(db_index=True)
    revision = models.PositiveIntegerField(default=1)
    active = models.BooleanField(default=True)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='+', on_delete=models.PROTECT)
    escalation_owner = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='+', on_delete=models.PROTECT)
    source = models.CharField(max_length=240, default='Demonstration calendar: elapsed days, Asia/Kolkata')

class Outbox(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(Application, null=True, on_delete=models.PROTECT)
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    identity = models.CharField(max_length=240, unique=True)
    title = models.CharField(max_length=240)
    body = models.TextField()
    deadline = models.ForeignKey(Deadline, null=True, on_delete=models.PROTECT)
    revision = models.PositiveIntegerField(null=True)
    state = models.CharField(max_length=25, default='pending', db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    available_at = models.DateTimeField(default=timezone.now)
    lease_until = models.DateTimeField(null=True)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)

class Notification(models.Model):
    event = models.OneToOneField(Outbox, on_delete=models.PROTECT)
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    read_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(default=timezone.now)

class Escalation(models.Model):
    deadline = models.ForeignKey(Deadline, on_delete=models.PROTECT)
    revision = models.PositiveIntegerField()
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    status = models.CharField(max_length=20, default='open')
    reason = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['deadline', 'revision'], name='escalation_revision_unique')]

class AuditEvent(models.Model):
    application = models.ForeignKey(Application, null=True, related_name='audit', on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    action = models.CharField(max_length=80)
    reason = models.TextField()
    before_version = models.PositiveIntegerField(null=True)
    after_version = models.PositiveIntegerField(null=True)
    details = models.JSONField(default=dict)
    correlation_id = models.UUIDField(default=uuid.uuid4)
    created_at = models.DateTimeField(default=timezone.now)

class Command(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    key = models.CharField(max_length=100)
    fingerprint = models.CharField(max_length=64)
    response = models.JSONField()
    class Meta:
        constraints = [models.UniqueConstraint(fields=['actor', 'key'], name='command_actor_key_unique')]

class LoginAttempt(models.Model):
    identity = models.CharField(max_length=64, unique=True)
    failures = models.PositiveIntegerField(default=0)
    window_start = models.DateTimeField(default=timezone.now)
