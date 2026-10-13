"""Operaciones de seriales que conservan la sede responsable de la mochila."""
import json

from django.core.exceptions import ValidationError
from django.db.models import Q

from inventario.models import (
    DocumentoItemSerializado, ItemSerializado,
)


def equipos_de_mochila(tecnico, sede, producto=None):
    qs = ItemSerializado.objects.filter(
        asignado_a=tecnico, sede_asignacion=sede,
        estado=ItemSerializado.Estado.ASIGNADO, proyecto__isnull=True,
    )
    return qs.filter(producto=producto) if producto is not None else qs


def ids_unicos(valores):
    try:
        if not isinstance(valores, list) or any(isinstance(v, bool) for v in valores):
            raise ValueError
        ids = [int(str(v)) for v in valores]
        if any(v <= 0 for v in ids) or len(ids) != len(set(ids)):
            raise ValueError
    except (ValueError, TypeError):
        raise ValidationError("La selección contiene identificadores inválidos o repetidos.")
    return ids


def seleccionar_despacho(item, sede, post, cantidad):
    """Se llama dentro de atomic; todos los equipos deben seguir disponibles."""
    inicio, fin = post.get(f"rango_inicio_{item.id}"), post.get(f"rango_fin_{item.id}")
    raw = post.get(f"detalles_json_{item.id}")
    base = ItemSerializado.objects.select_for_update().filter(
        producto=item.producto, ubicacion__sede=sede,
        estado=ItemSerializado.Estado.EN_ALMACEN,
        asignado_a__isnull=True, proyecto__isnull=True,
    ).order_by("id")
    if inicio or fin:
        if raw and raw != "[]":
            raise ValidationError("Selecciona equipos o un rango, no ambos.")
        try:
            inicio, fin = int(inicio), int(fin)
        except (ValueError, TypeError):
            raise ValidationError("Rango de códigos inválido.")
        if inicio < 1 or fin < inicio or fin - inicio + 1 != cantidad:
            raise ValidationError("El rango debe coincidir con la cantidad solicitada.")
        codigos = [str(n) for n in range(inicio, fin + 1)]
        equipos = list(base.filter(codigo_trazabilidad__in=codigos))
        if {e.codigo_trazabilidad for e in equipos} != set(codigos):
            raise ValidationError("Faltan equipos del rango en la sede de despacho.")
    else:
        try:
            ids = ids_unicos(json.loads(raw or "[]"))
        except (ValueError, TypeError):
            raise ValidationError("Selección de equipos inválida.")
        if len(ids) != cantidad:
            raise ValidationError("Debes seleccionar exactamente la cantidad de equipos solicitada.")
        equipos = list(base.filter(pk__in=ids))
    if len(equipos) != cantidad:
        raise ValidationError("Uno o más equipos no están disponibles en esta sede. Revisa el despacho.")
    preseleccion = set(item.seriales_seleccionados.values_list("item_serializado_id", flat=True))
    if preseleccion and preseleccion != {e.id for e in equipos}:
        raise ValidationError("La selección no coincide con los equipos escaneados en el requerimiento.")
    return equipos


def registrar_seriales(linea, equipos):
    DocumentoItemSerializado.objects.bulk_create([
        DocumentoItemSerializado(documento_item=linea, item_serializado=e, estado_resultante=e.estado)
        for e in equipos
    ])


def validar_seleccion_liquidacion(stock, devueltos, mermas):
    """Bloquea y valida la custodia completa antes de cambiar estados o saldos."""
    equipos = list(equipos_de_mochila(
        stock.tecnico, stock.sede, stock.producto,
    ).select_for_update().order_by("id"))
    if len(equipos) != stock.cantidad:
        raise ValidationError(
            f"{stock.producto.nombre}: saldo {stock.cantidad}, pero hay {len(equipos)} "
            "equipos identificados para esta sede. Concilia los seriales antes de liquidar."
        )
    devueltos, mermas = ids_unicos(devueltos), ids_unicos(mermas)
    seleccion = set(devueltos) | set(mermas)
    if set(devueltos) & set(mermas):
        raise ValidationError("Un equipo no puede ser devuelto y merma a la vez.")
    if not seleccion.issubset({e.id for e in equipos}):
        raise ValidationError("La selección incluye equipos de otra sede, producto o responsable.")
    return equipos, devueltos, mermas


def seleccionar_devolucion(stock, raw, cantidad):
    try:
        datos = json.loads(raw or "[]")
        if not isinstance(datos, list) or len(datos) != cantidad:
            raise ValueError
        codigos = [d["sn"].strip().upper() for d in datos]
        if any(not c for c in codigos) or len(set(codigos)) != cantidad:
            raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ValidationError("Debes identificar cada equipo devuelto, sin repetir códigos.")
    equipos = []
    for codigo in codigos:
        encontrados = list(equipos_de_mochila(stock.tecnico, stock.sede, stock.producto)
            .select_for_update().filter(Q(serial=codigo) | Q(codigo_trazabilidad=codigo)))
        if len(encontrados) != 1:
            raise ValidationError(f"Equipo {codigo}: no pertenece a esta mochila o el código es ambiguo.")
        equipos.append(encontrados[0])
    ids_unicos([e.id for e in equipos])
    return equipos
