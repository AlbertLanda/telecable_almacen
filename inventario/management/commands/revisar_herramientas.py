from django.core.management.base import BaseCommand
from django.db.models import Sum

from inventario.models import Producto, StockTecnico, ItemSerializado


PALABRAS_EQUIPO_CLIENTE = [
    "router", "onu", "ont", "repetidor", "extensor", "antena", "modem",
    "deco", "decodificador", "starlink", "mesh", "access point", "ap wifi",
    "tp-link", "tplink", "ubiquiti", "mikrotik", "cpe", "gpon",
]

PALABRAS_HERRAMIENTA = [
    "taladro", "ponchador", "pasacable", "tester", "medidor", "alicate",
    "destornillador", "escalera", "arnes", "arnés", "casco", "crimpadora",
    "peladora", "cortadora", "llave", "martillo", "pistola", "pon",
    "protector",
]


class Command(BaseCommand):
    help = (
        "Lista todos los productos marcados como Herramienta (es_activo=True) "
        "y sugiere cuáles podrían en realidad ser equipo que se instala en el "
        "cliente (y por lo tanto deberían ser es_activo=False, como Routers "
        "Starlink). Solo informa, no cambia nada."
    )

    def handle(self, *args, **options):
        productos = Producto.objects.filter(es_activo=True).order_by("nombre")

        if not productos.exists():
            self.stdout.write("No hay productos marcados como Herramienta (es_activo=True).")
            return

        for p in productos:
            nombre_lower = p.nombre.lower()

            es_sospechoso = any(pal in nombre_lower for pal in PALABRAS_EQUIPO_CLIENTE)
            es_herramienta_conocida = any(pal in nombre_lower for pal in PALABRAS_HERRAMIENTA)

            if es_sospechoso:
                etiqueta = self.style.WARNING("⚠️  REVISAR (parece equipo de cliente)")
            elif es_herramienta_conocida:
                etiqueta = self.style.SUCCESS("✅ Herramienta genuina (probable)")
            else:
                etiqueta = self.style.NOTICE("❓ Sin coincidencia, revisar manualmente")

            en_mochilas = StockTecnico.objects.filter(producto=p).aggregate(
                total=Sum("cantidad")
            )["total"] or 0

            asignados = ItemSerializado.objects.filter(
                producto=p, estado=ItemSerializado.Estado.ASIGNADO
            ).count()

            self.stdout.write(
                f"{p.codigo_interno or 'SIN-COD'} | {p.nombre} | "
                f"serializado={p.es_serializado} | "
                f"en mochilas={en_mochilas} | asignados a técnicos={asignados} | "
                f"{etiqueta}"
            )
