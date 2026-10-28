from django.urls import path, re_path
from . import views

urlpatterns = [
    path('api/metadata', views.metadata),
    path('api/submissions', views.submissions),
    path('api/submissions/<uuid:pk>', views.detail),
    path('api/submissions/<uuid:pk>/documents', views.upload),
    path('api/documents/<uuid:pk>/download', views.download),
    path('api/storage', views.storage),
    path('api/drive/connect', views.oauth_start),
    path('api/drive/callback', views.oauth_callback),
    re_path(r'^(?P<path>.*)$', views.frontend),
]
