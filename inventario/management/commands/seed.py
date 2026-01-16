from django.core.management.base import BaseCommand
from inventario.models import Categoria, Ubicacion, Producto, Stock


class Command(BaseCommand):
    help = "Crea data demo inicial (categorías, ubicaciones, productos y stock)."

    def handle(self, *args, **options):
        cat, _ = Categoria.objects.get_or_create(nombre="Accesorios")
        u1, _ = Ubicacion.objects.get_or_create(nombre="Almacén Principal")
        u2, _ = Ubicacion.objects.get_or_create(nombre="Mostrador")

        p1, _ = Producto.objects.get_or_create(
            codigo_interno="TC-ALM-000001",
            defaults={"nombre": "Conector RJ45", "barcode": "750000000001", "categoria": cat}
        )

        Stock.objects.get_or_create(producto=p1, ubicacion=u1, defaults={"cantidad": 100})
        Stock.objects.get_or_create(producto=p1, ubicacion=u2, defaults={"cantidad": 20})

        self.stdout.write(self.style.SUCCESS("✅ Seed listo. Ya puedes probar /scan/?q=TC-ALM-000001"))
        