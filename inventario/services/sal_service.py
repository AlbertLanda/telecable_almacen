from __future__ import annotations

from django.db import transaction
from django.core.exceptions import ValidationError, PermissionDenied

from inventario.models import (
    DocumentoInventario,
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
def req_to_sal(
    *,
    user,
    req: DocumentoInventario,
    responsable=None,
    ubicacion: Ubicacion | None = None,
) -> DocumentoInventario:
    """
    ✅ Genera SAL (BORRADOR) desde un REQ (PENDIENTE).
    - Solo ALMACÉN/JEFA
    - Respeta sede: almacén solo atiende REQ de su sede (JEFA puede todo)
    - sede_salida = sede operativa del almacén (user)
    - ubicacion es opcional (solo informativa)
    """
    profile = _require_roles(user, UserProfile.Rol.ALMACEN, UserProfile.Rol.JEFA)

    # 🔒 Bloquear el REQ para evitar doble conversión concurrente
    req = (
        DocumentoInventario.objects
        .select_for_update()
        .select_related("sede", "origen")
        .get(pk=req.pk)
    )

    if req.tipo != TipoDocumento.REQ:
        raise ValidationError("Solo un REQ puede convertirse en SAL.")

    if req.estado == EstadoDocumento.ANULADO:
        raise ValidationError("No puedes convertir un REQ anulado.")

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

    # ✅ Si ya existe una SAL borrador creada desde este REQ, devolverla (anti-duplicados)
    sal_existente = (
        DocumentoInventario.objects
        .filter(
            tipo=TipoDocumento.SAL,
            estado=EstadoDocumento.BORRADOR,
            origen=req,
        )
        .order_by("-fecha")
        .first()
    )
    if sal_existente:
        return sal_existente

    responsable_final = responsable or user

    sal = req.generar_salida_desde_req(
        responsable=responsable_final,
        sede_salida=sede_salida,
        ubicacion=ubicacion,
    )

    # ✅ Recomendado: marcar REQ como atendido al crear la SAL (ya fue tomada por almacén)
    # Si prefieres marcarlo recién cuando CONFIRMAS la SAL, comenta esto.
    req.estado = EstadoDocumento.REQ_ATENDIDO
    req.save(update_fields=["estado"])

    return sal
