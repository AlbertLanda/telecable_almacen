from __future__ import annotations

from inventario.models import Producto

def buscar_producto_por_code(code: str) -> Producto | None:
    """
    Busca por:
    - barcode (alfanum ONU / EAN)
    - codigo_interno (TC-ALM-000001)
    """
    code = (code or "").strip().upper()
    if not code:
        return None

    # 1) barcode exacto
    p = Producto.objects.filter(barcode__iexact=code).first()
    if p:
        return p

    # 2) codigo_interno exacto
    p = Producto.objects.filter(codigo_interno__iexact=code).first()
    if p:
        return p

    return None
