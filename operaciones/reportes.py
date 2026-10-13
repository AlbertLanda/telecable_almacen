"""Detalle de equipos según el resultado guardado en cada liquidación."""
from django.db.models import Prefetch

from inventario.models import DocumentoItemSerializado


def items_con_trazabilidad(documento):
    items = list(documento.items.select_related('producto').prefetch_related(
        Prefetch('seriales_seleccionados', queryset=DocumentoItemSerializado.objects
                 .select_related('item_serializado').order_by('item_serializado__codigo_trazabilidad', 'id'),
                 to_attr='seriales_del_documento')
    ).order_by('producto__nombre', 'id'))
    for item in items:
        item.detalle_equipos = None
        if not item.producto.es_serializado:
            continue
        grupos = {estado: [] for estado in ('EN_ALMACEN', 'INSTALADO', 'MERMA', 'SIN_RESULTADO')}
        for registro in item.seriales_del_documento:
            equipo = registro.item_serializado
            # El estado actual cambia en posteriores despachos. No reconstruye
            # lo sucedido en este documento y no debe usarse como alternativa.
            estado = registro.estado_resultante
            if estado not in grupos or equipo.producto_id != item.producto_id:
                estado = 'SIN_RESULTADO'
            grupos[estado].append(equipo)
        item.detalle_equipos = [
            {'titulo': 'Devueltas al almacén', 'equipos': grupos['EN_ALMACEN'], 'cantidad': item.cantidad},
            {'titulo': 'Consumo registrado / no devueltas', 'equipos': grupos['INSTALADO'], 'cantidad': item.cantidad_usada},
            {'titulo': 'Devueltas dañadas (merma)', 'equipos': grupos['MERMA'], 'cantidad': item.cantidad_merma},
        ]
        item.equipos_sin_resultado = grupos['SIN_RESULTADO']
        item.sin_trazabilidad = not item.seriales_del_documento
        item.trazabilidad_incompleta = bool(grupos['SIN_RESULTADO']) or any(
            len(g['equipos']) != g['cantidad'] for g in item.detalle_equipos
        )
    return items
