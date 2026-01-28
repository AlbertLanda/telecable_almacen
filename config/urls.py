from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
# Importamos una vista para el root (ej. redirigir al login o dashboard)
from inventario.views.dashboard import dashboard_redirect 

urlpatterns = [
    path("admin/", admin.site.urls),
    
    # Redirección raíz ("/") -> dashboard
    path("", dashboard_redirect, name="root"),

    # RUTAS DE CADA APP
    # 1. Core (Inventario base, Auth, REQ/SAL generales)
    path("", include("inventario.urls")),

    # 2. Operaciones (Técnicos, Liquidaciones semanales)
    path("operaciones/", include("operaciones.urls")),

    # 3. Proyectos (Expansión)
    path("proyectos/", include("proyectos.urls")),

] + static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)