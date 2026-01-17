# inventario/signals.py
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.contrib.auth import get_user_model

from .models import MovimientoInventario, UserProfile, Sede

User = get_user_model()


@receiver(post_save, sender=MovimientoInventario)
def aplicar_movimiento_al_guardar(sender, instance, created, **kwargs):
    if kwargs.get("raw") or not created:
        return
    instance.aplicar()



@receiver(post_save, sender=User)
def crear_profile_usuario(sender, instance, created, **kwargs):
    if kwargs.get("raw") or not created:
        return

    sede_default = Sede.objects.filter(activo=True).order_by("id").first()
    if not sede_default:
        sede_default = Sede.objects.create(nombre="SEDE-DEFAULT")

    profile, _ = UserProfile.objects.get_or_create(
        user=instance,
        defaults={
            "rol": UserProfile.Rol.ALMACEN,
            "sede_principal": sede_default,
        },
    )

