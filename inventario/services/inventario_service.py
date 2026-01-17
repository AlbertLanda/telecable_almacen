from django.db import transaction
from django.utils import timezone
from django.core.exceptions import ValidationError
from decimal import Decimal

from inventario.models import (
    DocumentoInventario, LineaDocumento, LineaSerial,
    Stock, MovimientoInventario, ItemSerializado
)


@transaction.atomic
def aplicar_documento(documento_id: int):
    doc = DocumentoInventario.objects.select_for_update().get(id=documento_id)

    if doc.cerrado:
        return  # ya aplicado

    for linea in doc.lineas.select_for_update().select_related("producto"):
        prod = linea.producto

        # Validaciones para serializados
        if prod.es_serializado:
            seriales = list(linea.seriales.select_related("item"))
            if len(seriales) != linea.qty:
                raise ValidationError(f"{prod.nombre}: qty={linea.qty} pero seriales={len(seriales)}")

            # cada serial debe pertenecer al mismo producto y estar en sede/ubic correcta
            for ls in seriales:
                item = ls.item
                if item.producto_id != prod.id:
                    raise ValidationError(f"Serial {item.serial} no corresponde a {prod.nombre}")
                if item.sede_id != doc.sede_id or item.ubicacion_id != doc.ubicacion_id:
                    raise ValidationError(f"Serial {item.serial} no está en esa sede/ubicación")

                if doc.tipo == DocumentoInventario.Tipo.OUT:
                    if item.estado != ItemSerializado.Estado.EN_ALMACEN:
                        raise ValidationError(f"Serial {item.serial} no está disponible (estado: {item.estado})")
                    item.estado = ItemSerializado.Estado.ASIGNADO
                    item.asignado_a = doc.responsable
                    item.save(update_fields=["estado", "asignado_a", "actualizado_en"])

                else:  # IN
                    # Puede regresar como EN_ALMACEN o MERMA según doc.motivo
                    if doc.motivo == DocumentoInventario.Motivo.MERMA:
                        item.estado = ItemSerializado.Estado.MERMA
                    else:
                        item.estado = ItemSerializado.Estado.EN_ALMACEN
                        item.asignado_a = None

                    item.save(update_fields=["estado", "asignado_a", "actualizado_en"])

            # Stock para serializados: se controla como qty también (contable)
            _aplicar_stock_y_kardex(doc, prod, linea.qty)

        else:
            # Consumible normal
            _aplicar_stock_y_kardex(doc, prod, linea.qty)

    doc.cerrado = True
    doc.save(update_fields=["cerrado", "actualizado_en"])


def _aplicar_stock_y_kardex(doc, producto, qty: int):
    stock, _ = Stock.objects.select_for_update().get_or_create(
        producto=producto, ubicacion=doc.ubicacion, defaults={"cantidad": 0}
    )

    if doc.tipo == DocumentoInventario.Tipo.OUT:
        if stock.cantidad < qty:
            raise ValidationError(f"Stock insuficiente de {producto.nombre}. Disponible: {stock.cantidad}, pedido: {qty}")
        stock.cantidad -= qty
        tipo_mov = MovimientoInventario.TIPO_OUT
    else:
        stock.cantidad += qty
        tipo_mov = MovimientoInventario.TIPO_IN

    stock.actualizado_en_operacion = timezone.now()
    stock.save(update_fields=["cantidad", "actualizado_en_operacion", "actualizado_en"])

    mov = MovimientoInventario(
        producto=producto,
        ubicacion=doc.ubicacion,
        tipo=tipo_mov,
        qty=qty,
        referencia=doc.referencia,
        nota=f"{doc.get_motivo_display()} - Resp: {doc.responsable.username}",
    )
    mov.save()
