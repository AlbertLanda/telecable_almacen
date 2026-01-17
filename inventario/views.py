from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from inventario.services.scan_service import buscar_producto_y_stock


@login_required
def scan_view(request):
    code = (request.GET.get("q") or "").strip()
    producto, stocks, error = buscar_producto_y_stock(code)

    return render(request, "inventario/scan.html", {
        "q": code,
        "producto": producto,
        "stocks": stocks,
        "error": error,
    })
