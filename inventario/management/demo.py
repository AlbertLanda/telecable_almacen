"""Autorización explícita de datos de demostración en desarrollo."""
from django.conf import settings
from django.core.management.base import CommandError


def require_demo_data():
    """Debe ejecutarse antes de abrir transacciones o consultar modelos."""
    if settings.DEBUG is not True or getattr(settings, "ALLOW_DEMO_DATA", False) is not True:
        raise CommandError(
            "Los datos de demostración requieren DEBUG=True y ALLOW_DEMO_DATA=True. "
            "En desarrollo, habilita ALLOW_DEMO_DATA=1 solo para una base de pruebas."
        )
