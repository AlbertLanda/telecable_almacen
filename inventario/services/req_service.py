from __future__ import annotations

from django.db import transaction
from django.utils import timezone
from django.core.exceptions import ValidationError, PermissionDenied

from inventario.models import (
    DocumentoInventario,
    DocumentoItem,
    TipoDocumento,
    EstadoDocumento,
    TipoRequerimiento,
    Producto,
    Ubicacion,
    UserProfile,
    Sede,
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

def _get_sede_central() -> Sede | None:
    return (
        Sede.objects
        .filter(tipo=Sede.CENTRAL, activo=True)
        .order_by("nombre")
        .first()
    )

def _normalizar_req_borrador_por_rol(doc: DocumentoInventario, profile: UserProfile) -> bool:
    """
    Fuerza que el REQ BORRADOR tenga coherencia con la nueva lógica:
    - Técnico: LOCAL (sin proveedor, sin sede_destino)
    - Almacén secundario: ENTRE_SEDES (sede_destino=CENTRAL, sin proveedor)
    - Almacén CENTRAL: PROVEEDOR (sin sede_destino) -> proveedor se asigna antes de enviar
    Retorna True si hizo cambios.
    """
    changed = False

    # SOLO aplica a REQ en BORRADOR
    if doc.tipo != TipoDocumento.REQ or doc.estado != EstadoDocumento.REQ_BORRADOR:
        return False

    # Técnico (SOLICITANTE)
    if profile.rol == UserProfile.Rol.SOLICITANTE:
        if doc.tipo_requerimiento != TipoRequerimiento.LOCAL:
            doc.tipo_requerimiento = TipoRequerimiento.LOCAL
            changed = True
        if doc.sede_destino_id:
            doc.sede_destino = None
            changed = True
        if doc.proveedor_id:
            doc.proveedor = None
            changed = True

    # Almacén
    elif profile.rol == UserProfile.Rol.ALMACEN:
        if doc.sede and doc.sede.tipo == Sede.CENTRAL:
            # CENTRAL: por defecto PROVEEDOR (proveedor se setea en UI antes de enviar)
            if doc.tipo_requerimiento != TipoRequerimiento.PROVEEDOR:
                doc.tipo_requerimiento = TipoRequerimiento.PROVEEDOR
                changed = True
            if doc.sede_destino_id:
                doc.sede_destino = None
                changed = True
            # proveedor puede ser null en borrador (se exige al enviar por clean/full_clean)
        else:
            # Secundario: ENTRE_SEDES hacia CENTRAL
            if doc.tipo_requerimiento != TipoRequerimiento.ENTRE_SEDES:
                doc.tipo_requerimiento = TipoRequerimiento.ENTRE_SEDES
                changed = True

            central = _get_sede_central()
            if central and (not doc.sede_destino_id or doc.sede_destino_id != central.id):
                doc.sede_destino = central
                changed = True

            if doc.proveedor_id:
                doc.proveedor = None
                changed = True

    # JEFA / ADMIN: no forzamos, pero si quedó vacío, ponemos LOCAL por seguridad
    else:
        if not doc.tipo_requerimiento:
            doc.tipo_requerimiento = TipoRequerimiento.LOCAL
            changed = True

    if changed:
        doc.save(update_fields=["tipo_requerimiento", "sede_destino", "proveedor"])
    return changed


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
    profile = _require_roles(
        user,
        UserProfile.Rol.SOLICITANTE,
        UserProfile.Rol.ALMACEN,
        UserProfile.Rol.JEFA,
        UserProfile.Rol.ADMIN,
    )
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
        # guarda ubicación solo como informativa para UI
        if ubicacion and doc.ubicacion_id != ubicacion.id:
            doc.ubicacion = ubicacion
            doc.save(update_fields=["ubicacion"])

        # ✅ clave: normaliza tipo_requerimiento según rol
        _normalizar_req_borrador_por_rol(doc, profile)
        return doc

    # ✅ defaults correctos al CREAR
    tipo_req = TipoRequerimiento.LOCAL
    sede_destino = None
    proveedor = None

    if profile.rol == UserProfile.Rol.ALMACEN:
        if sede.tipo == Sede.CENTRAL:
            tipo_req = TipoRequerimiento.PROVEEDOR
            sede_destino = None
        else:
            tipo_req = TipoRequerimiento.ENTRE_SEDES
            sede_destino = _get_sede_central()  # puede ser None si no existe aún

    doc = DocumentoInventario.objects.create(
        tipo=TipoDocumento.REQ,
        fecha=timezone.now(),
        sede=sede,
        ubicacion=ubicacion,  # informativo
        centro_costo=centro_costo,
        responsable=user,
        estado=EstadoDocumento.REQ_BORRADOR,
        tipo_requerimiento=tipo_req,
        sede_destino=sede_destino,
        proveedor=proveedor,
    )

    return doc


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

    if hasattr(producto, "activo") and not producto.activo:
        raise ValidationError("Este producto está inactivo.")

    MAX_QTY = 9999
    if cantidad > MAX_QTY:
        raise ValidationError(f"La cantidad máxima permitida es {MAX_QTY}.")

    item, created = DocumentoItem.objects.select_for_update().get_or_create(
        documento=req,
        producto=producto,
        defaults={
            "cantidad": cantidad,
            "costo_unitario": producto.costo_unitario,
            "observacion": "",
            "cantidad_devuelta": 0,
            "cantidad_merma": 0,
        },
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

    if hasattr(producto, "activo") and not producto.activo:
        raise ValidationError("Este producto está inactivo.")

    try:
        item = DocumentoItem.objects.select_for_update().get(documento=req, producto=producto)
    except DocumentoItem.DoesNotExist:
        raise ValidationError("Ese producto no está en el REQ.")

    MAX_QTY = 9999
    if cantidad > MAX_QTY:
        raise ValidationError(f"La cantidad máxima permitida es {MAX_QTY}.")

    item.cantidad = cantidad
    item.save(update_fields=["cantidad"])
    return item
