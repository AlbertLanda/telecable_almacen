from django.db.models import Q
from inventario.models import Producto


def get_producto_por_codigo(code: str):
    code = (code or "").strip()
    if not code:
        return None

    return (
        Producto.objects
        .select_related("categoria")
        .filter(Q(barcode=code) | Q(codigo_interno__iexact=code))
        .first()
    )
