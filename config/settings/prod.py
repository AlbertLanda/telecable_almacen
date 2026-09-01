from .base import *
from pathlib import Path
import dj_database_url
import os

DEBUG = False

# 1. SEGURIDAD DE HOSTS
# En Azure, ALLOWED_HOSTS vendrá de la variable de entorno, ej: "tu-app.azurewebsites.net"
if not ALLOWED_HOSTS:
    # Fallback por si acaso, pero idealmente se llena con env
    ALLOWED_HOSTS = ["*"]

# 2. SEGURIDAD SSL (HTTPS)
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_HTTPONLY = False   # <--- CORREGIDO: En False permite que AJAX (scan.js) lea el token CSRF
SESSION_COOKIE_HTTPONLY = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = True

# 3. BASE DE DATOS (Conexión Nube)
# Reemplaza la config local con la URL de conexión que nos dará Azure
DATABASES["default"] = dj_database_url.config(
    default=os.getenv("DATABASE_URL"),
    conn_max_age=600,
    ssl_require=True
)

# 4. ARCHIVOS ESTÁTICOS (CSS/JS) - Whitenoise
# Insertamos Whitenoise justo después de SecurityMiddleware
try:
    index = MIDDLEWARE.index("django.middleware.security.SecurityMiddleware")
    MIDDLEWARE.insert(index + 1, "whitenoise.middleware.WhiteNoiseMiddleware")
except ValueError:
    # Si no encuentra SecurityMiddleware, lo pone al inicio (backup)
    MIDDLEWARE.insert(0, "whitenoise.middleware.WhiteNoiseMiddleware")

STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

# 5. ARCHIVOS MEDIA (Planos, Fotos) - Azure Blob Storage
# Solo se activa si configuras las claves en Azure
if os.getenv("AZURE_ACCOUNT_NAME"):
    INSTALLED_APPS.append("storages")
    DEFAULT_FILE_STORAGE = "storages.backends.azure_storage.AzureStorage"
    
    AZURE_ACCOUNT_NAME = os.getenv("AZURE_ACCOUNT_NAME")
    AZURE_ACCOUNT_KEY = os.getenv("AZURE_ACCOUNT_KEY")
    AZURE_CONTAINER = os.getenv("AZURE_CONTAINER", "media")
    AZURE_URL_EXPIRATION_SECS = None  # Para que los links no caduquen (públicos)
    AZURE_CUSTOM_DOMAIN = f"{AZURE_ACCOUNT_NAME}.blob.core.windows.net"
    MEDIA_URL = f"https://{AZURE_CUSTOM_DOMAIN}/{AZURE_CONTAINER}/"
else:
    # Sin Blob Storage configurado: usamos el disco persistente de Azure
    # App Service (/home) en vez de la carpeta del proyecto. Todo lo que
    # está dentro de /home/site/wwwroot se reemplaza en cada deploy, pero
    # /home en sí es un disco compartido (Azure Files) que sobrevive a
    # despliegues y reinicios. Requiere WEBSITES_ENABLE_APP_SERVICE_STORAGE=true
    # en las Application Settings del App Service.
    MEDIA_ROOT = Path(os.getenv("MEDIA_ROOT_PATH", "/home/media"))

# 6. CSRF
# Importante: Agrega tu dominio de Azure aquí cuando lo tengas
CSRF_TRUSTED_ORIGINS = [
    "https://" + h.strip() for h in os.getenv("DJANGO_ALLOWED_HOSTS", "").split(",") if h.strip()
]
