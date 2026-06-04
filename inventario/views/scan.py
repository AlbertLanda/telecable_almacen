from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from inventario.models import Producto, Stock, Sede, Categoria

@login_required
def scan_view(request):
    """
    Vista para la pantalla de escaneo rápido.
    Muestra el producto escaneado y su stock en todas las sedes.
    """
    q = request.GET.get("q", "").strip().upper()
    
    producto = None
    stocks = []
    error = None

    # 1. Lógica de Búsqueda
    if q:
        try:
            # Buscamos por código interno primero
            producto = Producto.objects.filter(codigo_interno=q).first()
            
            # Si no, por barcode
            if not producto:
                producto = Producto.objects.filter(barcode=q).first()

            if producto:
                # Buscamos el stock en TODAS las sedes
                stocks = Stock.objects.filter(producto=producto).select_related('sede').order_by('sede__id')
            else:
                error = f"No se encontró producto con código: {q}"
            
        except Exception as e:
            error = f"Error al buscar: {str(e)}"

    # 2. Datos para el formulario (Sedes permitidas)
    sedes_para_input = Sede.objects.none()
    if hasattr(request.user, 'profile'):
        profile = request.user.profile
        sedes_para_input = profile.sedes_permitidas.all().order_by('id')
        
        # Fallback: si no tiene permitidas, usa la operativa
        if not sedes_para_input.exists():
            sede_actual = profile.get_sede_operativa()
            if sede_actual:
                sedes_para_input = Sede.objects.filter(id=sede_actual.id)
    else:
        sedes_para_input = Sede.objects.filter(activo=True).order_by('id')

    todas_categorias = Categoria.objects.all().order_by('nombre')
    
    sede_usuario = None
    if hasattr(request.user, 'profile'):
        sede_usuario = request.user.profile.get_sede_operativa()

    return render(request, "inventario/scan.html", {
        "q": q,
        "producto": producto,
        "stocks": stocks,
        "error": error,
        "lista_sedes": sedes_para_input,
        "lista_categorias": todas_categorias,
        "sede": sede_usuario,
        "user": request.user
    })