from inventario.repositories.producto_repo import get_producto_por_codigo
from inventario.repositories.stock_repo import get_stocks_por_producto


def buscar_producto_y_stock(code: str):
    """
    Caso de uso: Escanear código y obtener producto + stocks por ubicación
    Retorna: (producto, stocks, error_msg)
    """
    code = (code or "").strip()
    if not code:
        return None, [], None

    producto = get_producto_por_codigo(code)
    if not producto:
        return None, [], f"No se encontró producto con código: {code}"

    stocks = get_stocks_por_producto(producto)
    return producto, stocks, None
