"""Retornos documentados de almacén secundario a central, sin mochila técnica.

Un borrador estable identifica cada envío. La salida descuenta origen una vez;
la recepción existente crea el ING y aumenta destino al recibir físicamente.
"""
import hashlib
import hmac
import json

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from inventario.models import (
    DocumentoInventario, DocumentoItem, DocumentoItemSerializado, EstadoDocumento,
    ItemSerializado, Producto, Sede, Stock, TipoDocumento, TipoRequerimiento, UserProfile,
)
from inventario.services.seriales_service import ids_unicos


def _perfil(user, *, bloquear=False):
    if not user or not user.is_authenticated or not user.is_active:
        raise PermissionDenied("Debes ser un almacenero activo para gestionar retornos.")
    qs = UserProfile.objects
    if bloquear:
        qs = qs.select_for_update()
    try:
        perfil = qs.get(user_id=user.pk)
    except UserProfile.DoesNotExist as exc:
        raise PermissionDenied("El usuario no tiene un perfil de almacén.") from exc
    if perfil.rol != UserProfile.Rol.ALMACEN or not perfil.user.is_active:
        raise PermissionDenied("Solo un almacenero activo puede gestionar retornos.")
    return perfil


def _contexto(perfil):
    origen = perfil.get_sede_operativa()
    if not origen or not origen.activo or origen.tipo != Sede.SECUNDARIO:
        raise PermissionDenied("El retorno debe salir de tu almacén secundario activo.")
    destinos = list(Sede.objects.filter(tipo=Sede.CENTRAL, activo=True).order_by("pk")[:2])
    if len(destinos) != 1:
        raise ValidationError("Debe existir exactamente un almacén central activo.")
    if destinos[0].pk == origen.pk:
        raise ValidationError("El origen y el destino deben ser diferentes.")
    return {"sede_origen": origen, "sede_destino": destinos[0]}


def obtener_contexto_retorno(user):
    return _contexto(_perfil(user))


def _borradores(user, contexto):
    return DocumentoInventario.objects.filter(
        tipo=TipoDocumento.SAL, tipo_requerimiento=TipoRequerimiento.RETORNO_CENTRAL,
        estado=EstadoDocumento.BORRADOR, responsable=user,
        sede=contexto["sede_origen"],
    ).order_by("pk")


def _crear_o_recuperar(user, contexto):
    borradores = list(_borradores(user, contexto).select_for_update()[:2])
    if len(borradores) > 1:
        raise ValidationError("Hay varios borradores de retorno. Revisa los existentes antes de crear otro.")
    if borradores:
        return borradores[0]
    return DocumentoInventario.objects.create(
        tipo=TipoDocumento.SAL, tipo_requerimiento=TipoRequerimiento.RETORNO_CENTRAL,
        estado=EstadoDocumento.BORRADOR, responsable=user, solicitante=user,
        sede=contexto["sede_origen"], sede_origen=contexto["sede_origen"],
        sede_destino=contexto["sede_destino"], referencia="RETORNO A CENTRAL",
    )


@transaction.atomic
def crear_borrador_retorno(*, user):
    """Inicializa una sola SAL vacía por operador/sede; todavía no mueve stock."""
    contexto = _contexto(_perfil(user, bloquear=True))
    return _crear_o_recuperar(user, contexto)


def _validar_cabecera(doc, user, contexto):
    if doc.responsable_id != user.pk or doc.sede_id != contexto["sede_origen"].pk:
        raise PermissionDenied("Este retorno pertenece a otro almacenero o sede.")
    if (
        doc.tipo != TipoDocumento.SAL
        or doc.tipo_requerimiento != TipoRequerimiento.RETORNO_CENTRAL
        or doc.sede_origen_id != doc.sede_id
        or doc.sede_destino_id != contexto["sede_destino"].pk
        or doc.solicitante_id != doc.responsable_id
        or doc.origen_id is not None
    ):
        raise ValidationError("La cabecera no corresponde a un retorno de tu almacén a central.")


def _entero_positivo(valor, etiqueta):
    try:
        if isinstance(valor, bool):
            raise ValueError
        numero = int(str(valor))
        if numero <= 0:
            raise ValueError
        return numero
    except (TypeError, ValueError):
        raise ValidationError(f"{etiqueta}: indica un número entero mayor que cero.")


def _validar_producto(producto, cantidad, seriales_ids, sede):
    if not producto.activo or not producto.unidad.strip():
        raise ValidationError("El producto debe estar activo y tener una unidad de medida.")
    disponible = Stock.objects.filter(producto=producto, sede=sede).values_list("cantidad", flat=True).first() or 0
    if cantidad > disponible:
        raise ValidationError(f"Stock insuficiente de {producto.nombre} en {sede.nombre}: {disponible} {producto.unidad}.")
    if producto.es_serializado:
        if len(seriales_ids) != cantidad:
            raise ValidationError(f"Selecciona exactamente {cantidad} equipos de {producto.nombre}.")
        disponibles = ItemSerializado.objects.filter(
            pk__in=seriales_ids, producto=producto, estado=ItemSerializado.Estado.EN_ALMACEN,
            ubicacion__sede=sede, asignado_a__isnull=True, proyecto__isnull=True,
        ).count()
        if disponibles != cantidad:
            raise ValidationError("La selección contiene equipos no disponibles en el almacén de origen.")
    elif seriales_ids:
        raise ValidationError("Un producto no serializado no debe incluir equipos seleccionados.")


@transaction.atomic
def guardar_borrador_retorno(*, user, documento_id, items, observaciones):
    contexto = _contexto(_perfil(user, bloquear=True))
    try:
        doc = DocumentoInventario.objects.select_for_update().get(pk=documento_id)
    except DocumentoInventario.DoesNotExist as exc:
        raise ValidationError("El borrador de retorno no existe.") from exc
    _validar_cabecera(doc, user, contexto)
    if doc.estado != EstadoDocumento.BORRADOR or doc.recibido:
        raise ValidationError("Solo puedes editar un retorno en borrador.")
    if doc.firmas.exists() or doc.derivados.exists():
        raise ValidationError("El borrador tiene firmas o documentos derivados y no puede editarse.")
    motivo = str(observaciones or "").strip()
    if not motivo or len(motivo) > 2000:
        raise ValidationError("Indica el motivo del retorno, con un máximo de 2000 caracteres.")
    if not isinstance(items, list) or not items:
        raise ValidationError("Incluye al menos un producto para devolver.")
    lineas = []
    productos_vistos = set()
    for entrada in items:
        if not isinstance(entrada, dict):
            raise ValidationError("La lista de productos no es válida.")
        producto_id = _entero_positivo(entrada.get("producto_id"), "Producto")
        cantidad = _entero_positivo(entrada.get("cantidad"), "Cantidad")
        if producto_id in productos_vistos:
            raise ValidationError("Cada producto debe aparecer una sola vez en el retorno.")
        productos_vistos.add(producto_id)
        try:
            producto = Producto.objects.get(pk=producto_id)
        except Producto.DoesNotExist as exc:
            raise ValidationError("Uno de los productos no existe.") from exc
        seriales = ids_unicos(entrada.get("seriales_ids", []))
        _validar_producto(producto, cantidad, seriales, contexto["sede_origen"])
        lineas.append((producto, cantidad, seriales))
    # Sustituir únicamente líneas del borrador; no crea ni aplica movimientos.
    doc.items.all().delete()
    for producto, cantidad, seriales in sorted(lineas, key=lambda linea: linea[0].pk):
        item = DocumentoItem.objects.create(documento=doc, producto=producto, cantidad=cantidad)
        DocumentoItemSerializado.objects.bulk_create([
            DocumentoItemSerializado(documento_item=item, item_serializado_id=pk)
            for pk in sorted(seriales)
        ])
    doc.observaciones = motivo
    doc.save(update_fields=["observaciones"])
    return doc


def huella_retorno(documento, *, items=None):
    """Detecta cambios entre la revisión en pantalla y el primer envío."""
    if items is None:
        items = list(documento.items.select_related("producto").prefetch_related(
            "seriales_seleccionados__item_serializado",
        ))
    lineas = []
    for item in sorted(items, key=lambda item: (item.producto_id, item.pk)):
        seriales = sorted(item.seriales_seleccionados.all(), key=lambda vinculo: vinculo.item_serializado_id)
        lineas.append({
            "producto": item.producto_id, "cantidad": item.cantidad,
            "codigo": item.producto.codigo_interno, "nombre": item.producto.nombre,
            "unidad": item.producto.unidad,
            "seriales": [(
                vinculo.item_serializado_id, vinculo.item_serializado.serial,
                vinculo.item_serializado.codigo_trazabilidad, vinculo.item_serializado.mac_address,
                vinculo.item_serializado.serial_secundario,
            ) for vinculo in seriales],
        })
    datos = {
        "id": documento.pk, "origen": documento.sede_id, "destino": documento.sede_destino_id,
        "responsable": documento.responsable_id, "solicitante": documento.solicitante_id,
        "origen_nombre": documento.sede.nombre, "destino_nombre": documento.sede_destino.nombre,
        "motivo": documento.observaciones, "lineas": lineas,
    }
    return hashlib.sha256(json.dumps(datos, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validar_retorno_para_confirmar(doc, items):
    """Guarda del modelo, incluso si otra ruta llama confirmar() directamente."""
    contexto = obtener_contexto_retorno(doc.responsable)
    _validar_cabecera(doc, doc.responsable, contexto)
    if doc.estado != EstadoDocumento.BORRADOR or doc.recibido or doc.recibido_por_id or doc.recibido_en:
        raise ValidationError("El retorno no está pendiente de envío.")
    if doc.entregado_por_id not in (None, doc.responsable_id):
        raise ValidationError("El remitente del retorno no coincide con su responsable.")
    if not doc.observaciones.strip() or len(doc.observaciones) > 2000:
        raise ValidationError("Indica un motivo válido para devolver el material.")
    if doc.firmas.exists() or doc.derivados.exists():
        raise ValidationError("El retorno ya tiene firmas o un documento derivado. Revisa su estado.")
    if not items:
        raise ValidationError("El retorno no contiene productos.")
    for item in items:
        cantidad = _entero_positivo(item.cantidad, "Cantidad")
        if item.cantidad_devuelta or item.cantidad_merma or item.cantidad_usada:
            raise ValidationError("Las líneas del retorno no pueden contener liquidaciones.")
        seriales = list(item.seriales_seleccionados.values_list("item_serializado_id", flat=True))
        _validar_producto(item.producto, cantidad, seriales, contexto["sede_origen"])


@transaction.atomic
def confirmar_retorno(*, user, documento_id, revision=None):
    contexto = _contexto(_perfil(user, bloquear=True))
    try:
        doc = DocumentoInventario.objects.select_for_update().get(pk=documento_id)
    except DocumentoInventario.DoesNotExist as exc:
        raise ValidationError("El retorno no existe.") from exc
    _validar_cabecera(doc, user, contexto)
    if doc.estado == EstadoDocumento.CONFIRMADO:
        return doc
    if doc.estado != EstadoDocumento.BORRADOR:
        raise ValidationError("Solo puedes enviar un retorno en borrador.")
    if (
        not isinstance(revision, str) or len(revision) != 64 or not revision.isascii()
        or not hmac.compare_digest(revision, huella_retorno(doc))
    ):
        raise ValidationError("El retorno cambió o falta su revisión. Vuelve a revisar el resumen antes de enviarlo.")
    doc.confirmar(entregado_por=user, actualizar_mochila_tecnico=False)
    return doc
