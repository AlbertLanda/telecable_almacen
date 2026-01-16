from django.db import transaction
from django.core.exceptions import ValidationError, PermissionDenied

from inventario.models import (
    DocumentoInventario,
    TipoDocumento,
    EstadoDocumento,
    UserProfile,
)


def _require_roles(user, *roles):
    profile = getattr(user, "profile", None)
    if not profile:
        raise PermissionDenied("Usuario sin perfil (UserProfile).")
    if profile.rol not in roles:
        raise PermissionDenied("No tienes permisos para esta acción.")
    return profile


def _sede_operativa(user):
    profile = getattr(user, "profile", None)
    if not profile:
        raise ValidationError("Usuario sin perfil (UserProfile).")
    sede = profile.get_sede_operativa()
    if not sede:
        raise ValidationError("No tienes sede operativa asignada.")
    return sede


@transaction.atomic
def req_to_sal(*, user, req: DocumentoInventario, responsable=None, ubicacion=None) -> DocumentoInventario:
    """
    ✅ Genera SAL (BORRADOR) desde un REQ (PENDIENTE).
    - Solo ALMACÉN/JEFA
    - Respeta sede: almacén solo atiende REQ de su sede
    - sede_salida = sede operativa del almacén (ej. Jauja si es central)
    - ubicacion es opcional (solo informativa)
    """
    profile = _require_roles(user, UserProfile.Rol.ALMACEN, UserProfile.Rol.JEFA)

    if req.tipo != TipoDocumento.REQ:
        raise ValidationError("Solo un REQ puede convertirse en SAL.")

    if req.estado == EstadoDocumento.ANULADO:
        raise ValidationError("No puedes convertir un REQ anulado.")

    # ✅ flujo correcto: solo desde REQ_PENDIENTE
    if req.estado != EstadoDocumento.REQ_PENDIENTE:
        raise ValidationError("Solo un REQ en estado PENDIENTE puede convertirse en SAL.")

    if not req.items.exists():
        raise ValidationError("No puedes convertir a SAL: el REQ no tiene ítems.")

    sede_salida = _sede_operativa(user)

    # ✅ regla por sede: almacén atiende REQ de su misma sede (JEFA puede todo)
    if profile.rol != UserProfile.Rol.JEFA and req.sede_id != sede_salida.id:
        raise PermissionDenied("No puedes atender REQ de otra sede.")

    # ✅ coherencia sede-ubicacion si mandas ubicación
    if ubicacion and ubicacion.sede_id != sede_salida.id:
        raise ValidationError("La ubicación seleccionada no pertenece a la sede de salida.")

    sal = req.generar_salida_desde_req(
        responsable=responsable or user,
        sede_salida=sede_salida,
        ubicacion=ubicacion,
    )
    return sal
