from django.db.models.signals import post_save
from django.dispatch import receiver
from django.contrib.auth import get_user_model

# Importamos solo lo que existe en el models.py limpio de 'inventario'
from .models import MovimientoInventario, UserProfile, Sede

User = get_user_model()

@receiver(post_save, sender=User)
def crear_profile_usuario(sender, instance, created, **kwargs):
    """
    Cada vez que se crea un Usuario, se le crea un perfil (UserProfile)
    automáticamente con rol ALMACEN por defecto.
    """
    if kwargs.get("raw") or not created:
        return

    # Buscamos una sede por defecto para no romper la creación
    sede_default = Sede.objects.filter(activo=True).order_by("id").first()
    
    # Si no hay sedes, creamos una dummy (para evitar error en setup inicial)
    if not sede_default:
        try:
            sede_default = Sede.objects.create(nombre="SEDE-DEFAULT", tipo=Sede.SECUNDARIO)
        except Exception:
            pass # Si falla (ej. migración pendiente), dejamos null

    # Usamos get_or_create para evitar duplicados
    UserProfile.objects.get_or_create(
        user=instance,
        defaults={
            "rol": UserProfile.Rol.ALMACEN,
            "sede_principal": sede_default,
        },
    )