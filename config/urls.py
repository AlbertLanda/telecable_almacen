"""
URL configuration for config project.
"""

from django.contrib import admin
from django.urls import path, include
from django.shortcuts import redirect
from django.conf import settings
from django.conf.urls.static import static

def root_redirect(request):
    """
    Redirección raíz del sistema.
    - Si está logueado → /dashboard/
    - Si no → /login/
    """
    if request.user.is_authenticated:
        return redirect("dashboard")
    return redirect("login")

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", root_redirect, name="root"),
    path("", include("inventario.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
