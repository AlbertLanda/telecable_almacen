from __future__ import annotations

from django.db import transaction
from django.utils import timezone
from django.core.exceptions import ValidationError, PermissionDenied

from inventario.models import (
    DocumentoInventario,
    DocumentoItem,
    TipoDocumento,
    EstadoDocumento,
    Producto,
    Ubicacion,
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
def get_or_create_req_borrador(
    *,
    user,
    ubicacion: Ubicacion | None = None,
    centro_costo: str = "",
) -> DocumentoInventario:
    """
    ✅ Un usuario solo debe tener 1 REQ en BORRADOR por SEDE (carrito).
    Ubicación es solo informativa (opcional).
    """
    profile = _require_roles(user, UserProfile.Rol.SOLICITANTE, UserProfile.Rol.JEFA)
    sede = _sede_operativa(user)

    if ubicacion and profile.rol != UserProfile.Rol.JEFA and ubicacion.sede_id != sede.id:
        raise ValidationError("La ubicación no pertenece a tu sede operativa.")

    doc = (
        DocumentoInventario.objects
        .select_for_update()
        .filter(
            tipo=TipoDocumento.REQ,
            estado=EstadoDocumento.REQ_BORRADOR,
            responsable=user,
            sede=sede,
        )
        .order_by("-fecha")
        .first()
    )
    if doc:
        # Si te mandan ubicación, la guardamos como informativa para UI
        if ubicacion and doc.ubicacion_id != ubicacion.id:
            doc.ubicacion = ubicacion
            doc.save(update_fields=["ubicacion"])
        return doc

    return DocumentoInventario.objects.create(
        tipo=TipoDocumento.REQ,
        fecha=timezone.now(),
        sede=sede,
        ubicacion=ubicacion,  # informativo
        centro_costo=centro_costo,
        responsable=user,
        estado=EstadoDocumento.REQ_BORRADOR,
    )


@transaction.atomic
def add_item_to_req(
    *,
    user,
    req: DocumentoInventario,
    producto: Producto,
    cantidad: int = 1,
    observacion: str = "",
) -> DocumentoItem:
    _require_roles(user, UserProfile.Rol.SOLICITANTE, UserProfile.Rol.JEFA)

    if req.tipo != TipoDocumento.REQ or req.estado != EstadoDocumento.REQ_BORRADOR:
        raise ValidationError("Solo puedes agregar ítems a un REQ en BORRADOR.")

    if req.responsable_id != user.id:
        raise PermissionDenied("No puedes modificar un REQ que no es tuyo.")

    if cantidad <= 0:
        raise ValidationError("La cantidad debe ser mayor a 0.")

    item, created = DocumentoItem.objects.select_for_update().get_or_create(
        documento=req,
        producto=producto,
        defaults={"cantidad": cantidad, "observacion": observacion},
    )

    if not created:
        item.cantidad += cantidad

        fields = ["cantidad"]
        if observacion:
            item.observacion = observacion
            fields.append("observacion")

        item.save(update_fields=fields)

    return item


@transaction.atomic
def remove_item_from_req(*, user, req: DocumentoInventario, producto: Producto) -> None:
    _require_roles(user, UserProfile.Rol.SOLICITANTE, UserProfile.Rol.JEFA)

    if req.tipo != TipoDocumento.REQ or req.estado != EstadoDocumento.REQ_BORRADOR:
        raise ValidationError("Solo puedes quitar ítems de un REQ en BORRADOR.")

    if req.responsable_id != user.id:
        raise PermissionDenied("No puedes modificar un REQ que no es tuyo.")

    DocumentoItem.objects.filter(documento=req, producto=producto).delete()


@transaction.atomic
def set_item_qty(*, user, req: DocumentoInventario, producto: Producto, cantidad: int) -> DocumentoItem:
    _require_roles(user, UserProfile.Rol.SOLICITANTE, UserProfile.Rol.JEFA)

    if req.tipo != TipoDocumento.REQ or req.estado != EstadoDocumento.REQ_BORRADOR:
        raise ValidationError("Solo puedes editar cantidades en un REQ en BORRADOR.")

    if req.responsable_id != user.id:
        raise PermissionDenied("No puedes modificar un REQ que no es tuyo.")

    if cantidad <= 0:
        raise ValidationError("La cantidad debe ser mayor a 0.")

    try:
        item = DocumentoItem.objects.select_for_update().get(documento=req, producto=producto)
    except DocumentoItem.DoesNotExist:
        raise ValidationError("Ese producto no está en el REQ.")

    item.cantidad = cantidad
    item.save(update_fields=["cantidad"])
    return item
