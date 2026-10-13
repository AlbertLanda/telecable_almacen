"""Base aislada para CI. Nunca carga la conexión de producción."""
from .dev import *

if os.getenv("TEST_POSTGRES") == "1":
    DATABASES = {"default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "telecable_test",
        "USER": "postgres",
        "PASSWORD": "postgres",
        "HOST": "127.0.0.1",
        "PORT": "5432",
    }}
