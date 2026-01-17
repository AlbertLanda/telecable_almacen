from django.core.exceptions import PermissionDenied
from .exceptions import DomainError  # si ya tienes una
from inventario.models import UserProfile, TipoDocumento, EstadoDocumento, DocumentoInventario, Stock

def require_role(user, *roles):
    if not hasattr(user, "profile"):
        raise PermissionDenied("Usuario sin perfil.")
    if user.profile.rol not in roles:
        raise PermissionDenied("No tienes permisos para esta acción.")

def sede_operativa(user):
    # tu helper ya existe en profile: get_sede_operativa()
    return user.profile.get_sede_operativa()

def docs_queryset_for_user(user):
    rol = user.profile.rol
    sede = sede_operativa(user)

    qs = DocumentoInventario.objects.all()

    if rol == UserProfile.Rol.SOLICITANTE:
        return qs.filter(responsable=user)

    if rol == UserProfile.Rol.ALMACEN:
        return qs.filter(sede=sede)

    if rol == UserProfile.Rol.ADMIN:
        return qs  # o solo catálogo si deseas

    if rol == UserProfile.Rol.JEFA:
        # si usas sedes_permitidas
        sedes = user.profile.sedes_permitidas.all()
        return qs.filter(sede__in=sedes) if sedes.exists() else qs

    return qs.none()
