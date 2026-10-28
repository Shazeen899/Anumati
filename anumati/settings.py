import os
from pathlib import Path
BASE_DIR = Path(__file__).resolve().parent.parent
# Small explicit .env loader: environment values always take precedence.
if (BASE_DIR / '.env').exists():
    for line in (BASE_DIR / '.env').read_text().splitlines():
        if line.strip() and not line.lstrip().startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
SECRET_KEY = os.environ['DJANGO_SECRET_KEY']
DEBUG = os.getenv('DJANGO_DEBUG', '0') == '1'
DEMO_MODE = os.getenv('DEMO_MODE', '0') == '1'
ALLOWED_HOSTS = os.getenv('ALLOWED_HOSTS', '127.0.0.1,localhost,testserver').split(',')
INSTALLED_APPS = ['django.contrib.auth', 'django.contrib.contenttypes', 'django.contrib.sessions', 'django.contrib.staticfiles', 'core']
MIDDLEWARE = ['django.middleware.security.SecurityMiddleware', 'django.contrib.sessions.middleware.SessionMiddleware', 'django.middleware.common.CommonMiddleware', 'django.middleware.csrf.CsrfViewMiddleware', 'django.contrib.auth.middleware.AuthenticationMiddleware', 'django.middleware.clickjacking.XFrameOptionsMiddleware']
ROOT_URLCONF = 'anumati.urls'
WSGI_APPLICATION = 'anumati.wsgi.application'
DATABASES = {'default': {'ENGINE': 'django.db.backends.postgresql', 'NAME': os.getenv('PGDATABASE', 'anumati'), 'USER': os.getenv('PGUSER', 'anumati'), 'PASSWORD': os.getenv('PGPASSWORD', ''), 'HOST': os.getenv('PGHOST', '127.0.0.1'), 'PORT': os.getenv('PGPORT', '5432'), 'CONN_MAX_AGE': 60}}
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
AUTH_PASSWORD_VALIDATORS = [{'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'}]
TIME_ZONE = 'Asia/Kolkata'
USE_TZ = True
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
MEDIA_ROOT = BASE_DIR / 'private-media'
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'DENY'
DATA_UPLOAD_MAX_MEMORY_SIZE = 12 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 1024 * 1024
CELERY_BROKER_URL = os.getenv('REDIS_URL', 'redis://127.0.0.1:6379/0')
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_BROKER_TRANSPORT_OPTIONS = {'visibility_timeout': 300}
CELERY_BEAT_SCHEDULE = {'deadlines': {'task': 'core.tasks.scan_deadlines', 'schedule': 30.0}, 'outbox-recovery': {'task': 'core.tasks.dispatch_outbox', 'schedule': 15.0}, 'scan-recovery': {'task': 'core.tasks.scan_pending_files', 'schedule': 30.0}}
EMAIL_BACKEND = 'django.core.mail.backends.filebased.EmailBackend'
EMAIL_FILE_PATH = BASE_DIR / '.runtime' / 'mail'
DEFAULT_FROM_EMAIL = 'notifications@anumati.local'
