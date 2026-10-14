"""Selección explícita y control de versiones de las mochilas por sede de origen."""
from django.core import signing
from django.core.exceptions import ValidationError

from inventario.models import StockTecnico
from inventario.services.seriales_service import equipos_de_mochila, ids_unicos

SALT = 'operaciones.liquidacion.mochilas.v1'


def version_fila(item, equipos):
    return [item.sede_id, item.producto_id, item.cantidad,
            item.actualizado_en.isoformat(), item.producto.es_serializado, item.producto.es_activo,
            [[e.id, e.actualizado_en.isoformat()] for e in equipos]]


def preparar_mochila(tecnico, usuario, sede):
    filas = list(StockTecnico.objects.filter(tecnico=tecnico, cantidad__gt=0)
                 .select_related('producto', 'sede').order_by('sede_id', 'producto__nombre', 'id'))
    versiones = {}
    for item in filas:
        item.equipos = list(equipos_de_mochila(tecnico, item.sede, item.producto).order_by('id')) if item.sede_id and item.producto.es_serializado else []
        item.requiere_revision = not item.sede_id or (item.producto.es_serializado and len(item.equipos) != item.cantidad)
        versiones[str(item.id)] = version_fila(item, item.equipos)
    token = signing.dumps({'usuario': usuario.pk, 'tecnico': tecnico.pk, 'receptor': sede.pk,
                           'filas': versiones}, salt=SALT, compress=True)
    return filas, token


def bloquear_seleccion(post, tecnico, usuario, sede):
    """Dentro de atomic. Un formulario viejo nunca liquida una carga nueva."""
    try:
        snapshot = signing.loads(post.get('mochila_version', ''), salt=SALT, max_age=43200)
    except signing.BadSignature:
        raise ValidationError('La pantalla venció o cambió. Actualiza y vuelve a seleccionar las filas.')
    if (snapshot['usuario'], snapshot['tecnico'], snapshot['receptor']) != (usuario.pk, tecnico.pk, sede.pk):
        raise ValidationError('La selección corresponde a otro usuario, técnico o almacén receptor.')
    ids = ids_unicos(post.getlist('filas'))
    if not ids or not all(str(pk) in snapshot['filas'] for pk in ids):
        raise ValidationError('Selecciona las filas que deseas liquidar.')
    filas = list(StockTecnico.objects.select_for_update().filter(tecnico=tecnico, pk__in=ids).order_by('id'))
    if len(filas) != len(ids):
        raise ValidationError('La mochila cambió. Actualiza la página.')
    for item in filas:
        equipos = list(equipos_de_mochila(tecnico, item.sede, item.producto).select_for_update().order_by('id')) if item.sede_id and item.producto.es_serializado else []
        if not item.sede_id or item.cantidad <= 0 or version_fila(item, equipos) != snapshot['filas'][str(item.id)]:
            raise ValidationError('Esta carga ya fue liquidada o cambió desde que abriste la página. Actualiza para ver los pendientes.')
    return filas
