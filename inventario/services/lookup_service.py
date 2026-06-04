from __future__ import annotations
from django.db.models import Q
from inventario.models import Producto

def buscar_producto_por_code(code: str) -> Producto | None:
    """
    Busca un producto por su barcode o su código interno.
    Retorna el objeto Producto o None si no existe.
    """
    code = (code or "").strip().upper()
    if not code:
        return None

    # 1. Buscamos coincidencia exacta de barcode
    p = Producto.objects.filter(barcode__iexact=code).first()
    if p:
        return p

    # 2. Buscamos coincidencia exacta de código interno
    p = Producto.objects.filter(codigo_interno__iexact=code).first()
    if p:
        return p

    return None