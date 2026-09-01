from django.contrib import admin
from django.urls import path, re_path, include
from django.conf import settings
from django.conf.urls.static import static
from django.views.static import serve as static_serve
# 1. AGREGAMOS ESTE IMPORT 👇
from django.views.generic import RedirectView

# Importamos una vista para el root (ej. redirigir al login o dashboard)
from inventario.views.dashboard import dashboard_redirect

urlpatterns = [
    path("admin/", admin.site.urls),

    # 2. AGREGAMOS ESTA LÍNEA AQUÍ 👇 (Para callar el error 404 en consola)
    path('favicon.ico', RedirectView.as_view(url='/static/favicon.ico', permanent=True)),

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
elif settings.MEDIA_URL.startswith("/"):
    # En producción, static() de Django no registra nada porque DEBUG=False.
    # Si MEDIA_URL sigue siendo una ruta local (no una URL externa de Azure
    # Blob Storage), servimos los archivos igual para que planos, fotos, etc.
    # sean accesibles. Cuando se configure AZURE_ACCOUNT_NAME, MEDIA_URL pasa
    # a ser la URL del Blob Storage y esta ruta deja de usarse.
    urlpatterns += [
        re_path(r"^media/(?P<path>.*)$", static_serve, {"document_root": settings.MEDIA_ROOT}),
    ]