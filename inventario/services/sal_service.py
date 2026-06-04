from django.db import transaction
from django.core.exceptions import ValidationError, PermissionDenied

from inventario.models import (
    DocumentoInventario,
    DocumentoItem,
    TipoDocumento,
    EstadoDocumento,
    UserProfile,
    Ubicacion,
    Sede,
)

def _require_roles(user, *roles):
    profile = getattr(user, "profile", None)
    if not profile:
        raise PermissionDenied("Usuario sin perfil (UserProfile).")
    if profile.rol not in roles:
        raise PermissionDenied("No tienes permisos para esta acción.")
    return profile

def _sede_operativa(user) -> Sede:
    profile = getattr(user, "profile", None)
    if not profile:
        raise ValidationError("Usuario sin perfil (UserProfile).")
    sede = profile.get_sede_operativa()
    if not sede:
        raise ValidationError("No tienes sede operativa asignada.")
    return sede

@transaction.atomic
def req_to_sal(*, user, req: DocumentoInventario, responsable=None, ubicacion: Ubicacion | None = None) -> DocumentoInventario:
    """
    Genera SAL (BORRADOR) desde un REQ (PENDIENTE).
    Evita select_for_update con outer joins (causa del NotSupportedError).
    """
    profile = _require_roles(user, UserProfile.Rol.ALMACEN, UserProfile.Rol.JEFA)

    # Re-lee y BLOQUEA el REQ sin select_related (sin joins)
    req_locked = (
        DocumentoInventario.objects
        .select_for_update()
        .get(id=req.id)
    )

    if req_locked.tipo != TipoDocumento.REQ:
        raise ValidationError("Solo un REQ puede convertirse en SAL.")

    if req_locked.estado == EstadoDocumento.ANULADO:
        raise ValidationError("No puedes convertir un REQ anulado.")

    if req_locked.estado != EstadoDocumento.REQ_PENDIENTE:
        raise ValidationError("Solo un REQ en estado PENDIENTE puede convertirse en SAL.")

    # Bloquea items aparte (producto NO es nullable, ok)
    items = list(
        DocumentoItem.objects
        .select_for_update()
        .select_related("producto")
        .filter(documento=req_locked)
    )
    if not items:
        raise ValidationError("No puedes convertir a SAL: el REQ no tiene ítems.")

    sede_salida = _sede_operativa(user)

    if profile.rol != UserProfile.Rol.JEFA and req_locked.sede_id != sede_salida.id:
        raise PermissionDenied("No puedes atender REQ de otra sede.")

    if ubicacion and ubicacion.sede_id != sede_salida.id:
        raise ValidationError("La ubicación seleccionada no pertenece a la sede de salida.")

    responsable_final = responsable or user

    sal = req_locked.generar_salida_desde_req(
        responsable=responsable_final,
        sede_salida=sede_salida,
        ubicacion=ubicacion,
    )

    return sal
