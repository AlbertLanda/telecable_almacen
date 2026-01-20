from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from inventario.models import Producto, Stock, Sede, Categoria

@login_required
def scan_view(request):
    # 1. Obtener código buscado
    q = request.GET.get("q", "").strip().upper()
    
    producto = None
    stocks = []
    error = None

    # 2. Lógica de Búsqueda (Esto MANTIENE la visibilidad global)
    if q:
        try:
            # Buscamos el producto
            producto = Producto.objects.get(codigo_interno=q)
            
            # Buscamos el stock en TODAS las sedes (para que la tabla de disponibilidad muestre todo)
            stocks = Stock.objects.filter(producto=producto).select_related('sede').order_by('sede__id')
            
        except Producto.DoesNotExist:
            error = f"No se encontró producto con código: {q}"

    # 3. LÓGICA DE PERMISOS PARA EL INPUT (Esto RESTRINGE el selector)
    # ---------------------------------------------------------------
    sedes_para_input = Sede.objects.none()
    
    if hasattr(request.user, 'profile'):
        profile = request.user.profile
        
        # Obtenemos solo las sedes permitidas configuradas en el Admin
        sedes_para_input = profile.sedes_permitidas.all().order_by('id')
        
        # Si no tiene sedes permitidas explícitas, usamos su sede operativa por defecto
        if not sedes_para_input.exists():
            sede_actual = profile.get_sede_operativa()
            if sede_actual:
                sedes_para_input = Sede.objects.filter(id=sede_actual.id)
    else:
        # Fallback para superusuarios sin perfil (ven todo)
        sedes_para_input = Sede.objects.all().order_by('id')

    # Datos para categorías
    todas_categorias = Categoria.objects.all().order_by('nombre')

    # Sede actual para pre-seleccionar en el combo
    sede_usuario = None
    if hasattr(request.user, 'profile'):
        sede_usuario = request.user.profile.get_sede_operativa()

    # 4. Renderizar
    return render(request, "inventario/scan.html", {
        "q": q,
        "producto": producto,
        "stocks": stocks, # Muestra stock de TODAS las sedes (Informativo)
        "error": error,
        
        # Aquí está el filtro: El combo box solo tendrá las sedes permitidas
        "lista_sedes": sedes_para_input, 
        
        "lista_categorias": todas_categorias,
        "sede": sede_usuario,
        "user": request.user
    })