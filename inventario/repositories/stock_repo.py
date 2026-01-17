from inventario.models import Stock


def get_stocks_por_producto(producto):
    if not producto:
        return []
    return list(
        Stock.objects
        .select_related("ubicacion")
        .filter(producto=producto)
        .order_by("ubicacion__nombre")
    )
