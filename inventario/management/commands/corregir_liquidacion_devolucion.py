from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from inventario.models import (
    DocumentoInventario,
    DocumentoItem,
    Producto,
    ItemSerializado,
    MovimientoInventario,
    Ubicacion,
    TipoDocumento,
)


class Command(BaseCommand):
    help = (
        "Corrige una liquidación de técnico YA CONFIRMADA: mueve unidades que "
        "quedaron registradas como 'Consumo/Instalado' a 'Devuelto', para el "
        "caso en que el equipo sí regresó al almacén (ej: un técnico le "
        "prestó el equipo a otro en campo y almacén no tenía forma de "
        "marcarlo como devuelto en el momento de liquidar). "
        "Caso real: python manage.py corregir_liquidacion_devolucion "
        "--documento ING-0000000139 --producto TC-ALM-000072 "
        "--codigos 291,292,306,307,308"
    )

    def add_arguments(self, parser):
        parser.add_argument("--documento", required=True, help="Número del documento ING, ej: ING-0000000139")
        parser.add_argument("--producto", required=True, help="Código interno del producto, ej: TC-ALM-000072")
        parser.add_argument(
            "--codigos", required=True,
            help="Códigos de trazabilidad (el número pintado) separados por coma, ej: 291,292,306,307,308",
        )
        parser.add_argument("--dry-run", action="store_true", help="Solo muestra qué haría, sin guardar nada.")

    def handle(self, *args, **options):
        numero = options["documento"].strip()
        codigo_producto = options["producto"].strip()
        codigos = [c.strip() for c in options["codigos"].split(",") if c.strip()]
        dry_run = options["dry_run"]

        try:
            doc = DocumentoInventario.objects.get(numero=numero, tipo=TipoDocumento.ING)
        except DocumentoInventario.DoesNotExist:
            raise CommandError(f"No existe un documento ING con número {numero}.")

        try:
            producto = Producto.objects.get(codigo_interno=codigo_producto)
        except Producto.DoesNotExist:
            raise CommandError(f"No existe un producto con código {codigo_producto}.")

        try:
            item_doc = DocumentoItem.objects.get(documento=doc, producto=producto)
        except DocumentoItem.DoesNotExist:
            raise CommandError(f"El documento {numero} no tiene una línea para {producto.nombre}.")

        equipos = list(
            ItemSerializado.objects.filter(producto=producto, codigo_trazabilidad__in=codigos)
        )
        encontrados = {e.codigo_trazabilidad for e in equipos}
        faltantes = [c for c in codigos if c not in encontrados]
        if faltantes:
            raise CommandError(
                f"No se encontraron equipos de {producto.nombre} con código(s) de "
                f"trazabilidad: {', '.join(faltantes)}."
            )

        cantidad = len(equipos)

        if cantidad > item_doc.cantidad_usada:
            raise CommandError(
                f"Pides devolver {cantidad} unidades, pero el documento {numero} solo "
                f"tiene {item_doc.cantidad_usada} marcadas como Consumo/Instalado para "
                f"{producto.nombre}."
            )

        self.stdout.write(
            f"Documento {doc.numero} ({doc.fecha:%d/%m/%Y}) - {producto.nombre}:\n"
            f"  Devuelto: {item_doc.cantidad} -> {item_doc.cantidad + cantidad}\n"
            f"  Consumo:  {item_doc.cantidad_usada} -> {item_doc.cantidad_usada - cantidad}\n"
            f"  Equipos a liberar a EN_ALMACEN: {', '.join(sorted(encontrados))}"
        )

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry-run: no se guardó nada."))
            return

        with transaction.atomic():
            item_doc.cantidad += cantidad
            item_doc.cantidad_usada -= cantidad
            item_doc.save(update_fields=["cantidad", "cantidad_usada"])

            ubicacion_retorno = Ubicacion.objects.filter(sede=doc.sede).first()
            ItemSerializado.objects.filter(id__in=[e.id for e in equipos]).update(
                estado=ItemSerializado.Estado.EN_ALMACEN,
                asignado_a=None,
                ubicacion=ubicacion_retorno,
            )

            MovimientoInventario.objects.create(
                producto=producto,
                sede=doc.sede,
                tipo=MovimientoInventario.TIPO_IN,
                qty=cantidad,
                referencia=doc.numero,
                usuario=doc.responsable,
                nota=(
                    f"Corrección devolución tardía de liquidación {doc.numero} "
                    f"({doc.fecha:%d/%m/%Y}). Códigos: {', '.join(sorted(encontrados))}."
                ),
            ).aplicar()

        self.stdout.write(self.style.SUCCESS("Corrección aplicada correctamente."))
