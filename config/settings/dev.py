from .base import *

DEBUG = True

# Dev: si no hay ALLOWED_HOSTS en env, usa localhost
if not ALLOWED_HOSTS:
    ALLOWED_HOSTS = ["127.0.0.1", "localhost"]

CSRF_TRUSTED_ORIGINS = [
    "http://127.0.0.1:8000",
    "http://localhost:8000",
    "http://127.0.0.1:49878",
    "http://localhost:49878",
]

CSRF_TRUSTED_ORIGINS.extend([f"http://127.0.0.1:{p}" for p in range(8000, 8010)])
CSRF_TRUSTED_ORIGINS.extend([f"http://localhost:{p}" for p in range(8000, 8010)])
CSRF_TRUSTED_ORIGINS.extend([f"http://127.0.0.1:{p}" for p in range(49870, 49880)])
CSRF_TRUSTED_ORIGINS.extend([f"http://localhost:{p}" for p in range(49870, 49880)])

CSRF_COOKIE_SECURE = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_HTTPONLY = False
SESSION_COOKIE_HTTPONLY = False
SECURE_SSL_REDIRECT = False
