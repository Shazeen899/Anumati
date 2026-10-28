from django.urls import path, re_path
from core import views
urlpatterns = [
    path('api/session', views.session), path('api/login', views.sign_in), path('api/logout', views.sign_out),
    path('api/dashboard', views.dashboard), path('api/metadata', views.metadata),
    path('api/applications', views.applications), path('api/applications/<uuid:pk>', views.application_detail),
    path('api/applications/<uuid:pk>/commands', views.case_command), path('api/applications/<uuid:pk>/evidence', views.upload),
    path('api/evidence/<uuid:pk>/review', views.evidence_review), path('api/evidence/<uuid:pk>/download', views.download),
    path('api/compliance', views.compliance), path('api/notifications', views.notifications), path('api/escalations', views.escalations),
    path('api/policies', views.policies), path('api/operations', views.operations), path('api/audit', views.audit_log),
    path('api/reports', views.report), path('api/health', views.health), re_path(r'^(?P<path>.*)$', views.frontend),
]
