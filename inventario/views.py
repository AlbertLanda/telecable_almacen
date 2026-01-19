from django.shortcuts import render
from django.contrib.auth.decorators import login_required
# Importa tus modelos. ¡IMPORTANTE importar Categoria!
from inventario.models import Producto, Stock, Sede, Categoria

# Si tienes un servicio, úsalo, si no, usa la lógica manual
try:
    from inventario.services.scan_service import buscar_producto_y_stock
except ImportError:
    buscar_producto_y_stock = None

@login_required
def scan_view(request):
    # 1. Obtener código buscado (Mayúsculas y sin espacios)
    q = request.GET.get("q", "").strip().upper()
    
    producto = None
    stocks = []
    error = None

    # 2. Lógica de Búsqueda
    if buscar_producto_y_stock:
        try:
            producto, stocks, error = buscar_producto_y_stock(q)
        except Exception as e:
            # Fallback si el servicio falla
            if q:
                try:
                    # Buscamos por codigo_interno (según tu modelo)
                    producto = Producto.objects.get(codigo_interno=q)
                    stocks = Stock.objects.filter(producto=producto).select_related('sede')
                except Producto.DoesNotExist:
                    error = f"No se encontró producto con código: {q}"
    else:
        # Lógica manual si no hay servicio
        if q:
            try:
                producto = Producto.objects.get(codigo_interno=q)
                stocks = Stock.objects.filter(producto=producto).select_related('sede')
            except Producto.DoesNotExist:
                error = f"No se encontró producto con código: {q}"

    # 3. DATOS PARA LOS COMBOS DEL MODAL (ESTO ES LA CLAVE)
    # -----------------------------------------------------
    
    # Lista de Sedes (Para elegir destino)
    todas_sedes = Sede.objects.all().order_by('id')
    
    # Lista de Categorías (Para que salgan los nombres "Routers", "Cables", etc.)
    todas_categorias = Categoria.objects.all().order_by('nombre')

    # Sede del usuario actual (para pre-seleccionar)
    sede_usuario = None
    if hasattr(request.user, 'profile'):
        sede_usuario = request.user.profile.get_sede_operativa()

    # 4. Renderizar con todo el contexto
    return render(request, "inventario/scan.html", {
        "q": q,
        "producto": producto,
        "stocks": stocks,
        "error": error,
        
        # Enviamos las listas al HTML
        "lista_sedes": todas_sedes,
        "lista_categorias": todas_categorias, # <--- ¡ESTO FALTABA!
        "sede": sede_usuario,
        "user": request.user
    })