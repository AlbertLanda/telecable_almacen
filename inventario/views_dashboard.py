from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import F, Sum
from django.shortcuts import redirect, render, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.http import JsonResponse

# Importamos los modelos. Asegúrate de tener Categoria importado.
from inventario.models import (
    DocumentoInventario,
    EstadoDocumento,
    MovimientoInventario,
    Stock,
    TipoDocumento,
    UserProfile,
    Sede,
    Producto,
    Categoria
)

# --------------------
# HELPERS
# --------------------
def _require_roles(user, *roles):
    """Verifica permisos de rol del usuario."""
    profile = getattr(user, "profile", None)
    if not profile:
        raise PermissionDenied("Usuario sin perfil (UserProfile).")
    if profile.rol not in roles:
        raise PermissionDenied("No tienes permisos para esta acción.")
    return profile

def _require_sede(profile: UserProfile):
    """Verifica que el usuario tenga sede asignada."""
    sede = profile.get_sede_operativa()
    if not sede:
        raise PermissionDenied("No tienes sede operativa asignada.")
    return sede

# --------------------
# VISTAS DASHBOARD (Redirección y Paneles)
# --------------------
@login_required
def dashboard_redirect(request):
    profile = _require_roles(request.user, UserProfile.Rol.SOLICITANTE, UserProfile.Rol.ALMACEN, UserProfile.Rol.JEFA, UserProfile.Rol.ADMIN)
    if profile.rol == UserProfile.Rol.SOLICITANTE: return redirect("tecnico_dashboard")
    if profile.rol == UserProfile.Rol.ALMACEN: return redirect("dash_almacen")
    return redirect("dash_admin")

@login_required
def dash_admin(request):
    profile = _require_roles(request.user, UserProfile.Rol.ADMIN, UserProfile.Rol.JEFA)
    sede = profile.get_sede_operativa()
    
    total_equipos = Stock.objects.filter(sede=sede).aggregate(total=Sum("cantidad"))["total"] or 0
    total_cables = Stock.objects.filter(sede=sede, producto__nombre__icontains="cable").aggregate(total=Sum("cantidad"))["total"] or 0
    low_stock = Stock.objects.filter(sede=sede, producto__activo=True, cantidad__lt=F("producto__stock_minimo")).count()
    ult_movs = MovimientoInventario.objects.filter(sede=sede).select_related("producto").order_by("-creado_en")[:10]
    
    return render(request, "inventario/dash_admin.html", {
        "profile": profile, "sede": sede, "total_equipos": total_equipos,
        "total_cables": total_cables, "low_stock": low_stock, "ult_movs": ult_movs, "user": request.user,
    })

@login_required
def dash_almacen(request):
    profile = _require_roles(request.user, UserProfile.Rol.ALMACEN, UserProfile.Rol.JEFA)
    sede = _require_sede(profile)
    hoy = timezone.localdate()
    
    req_pendientes = DocumentoInventario.objects.filter(tipo=TipoDocumento.REQ, estado=EstadoDocumento.REQ_PENDIENTE, sede=sede).count()
    sal_hoy = DocumentoInventario.objects.filter(tipo=TipoDocumento.SAL, estado=EstadoDocumento.CONFIRMADO, sede=sede, fecha__date=hoy).count()
    ing_pendientes = DocumentoInventario.objects.filter(tipo=TipoDocumento.ING, estado=EstadoDocumento.BORRADOR, sede=sede).count()
    stock_bajo = Stock.objects.filter(sede=sede, producto__activo=True, producto__stock_minimo__gt=0, cantidad__lt=F("producto__stock_minimo")).count()
    ult_movs = MovimientoInventario.objects.filter(sede=sede).select_related("producto").order_by("-creado_en")[:12]
    ult_reqs = DocumentoInventario.objects.filter(tipo=TipoDocumento.REQ, estado=EstadoDocumento.REQ_PENDIENTE, sede=sede).select_related("responsable").order_by("-fecha")[:10]
    
    return render(request, "inventario/dash_almacen.html", {
        "profile": profile, "sede": sede, "req_pendientes": req_pendientes, "sal_hoy": sal_hoy,
        "ing_pendientes": ing_pendientes, "stock_bajo": stock_bajo, "ult_movs": ult_movs, "ult_reqs": ult_reqs,
    })

@login_required
def dash_solicitante(request):
    profile = _require_roles(request.user, UserProfile.Rol.SOLICITANTE, UserProfile.Rol.JEFA)
    sede = _require_sede(profile)
    mis_reqs = DocumentoInventario.objects.filter(tipo=TipoDocumento.REQ, responsable=request.user).order_by("-fecha")[:12]
    return render(request, "inventario/dash_solicitante.html", {"profile": profile, "sede": sede, "mis_reqs": mis_reqs})

# --------------------
# INVENTARIO GENERAL
# --------------------

@login_required
def inventory_list(request):
    profile = _require_roles(request.user, UserProfile.Rol.ADMIN, UserProfile.Rol.JEFA, UserProfile.Rol.ALMACEN)
    
    sedes_disponibles = []
    if profile.rol in [UserProfile.Rol.ADMIN, UserProfile.Rol.JEFA]:
        sedes_disponibles = Sede.objects.all().order_by('id')
    else:
        sedes_disponibles = [profile.get_sede_operativa()]

    sede_id_param = request.GET.get('sede_id')
    sede_actual = profile.get_sede_operativa()
    
    if sede_id_param and profile.rol in [UserProfile.Rol.ADMIN, UserProfile.Rol.JEFA]:
        try:
            sede_actual = Sede.objects.get(id=sede_id_param)
        except Sede.DoesNotExist:
            pass 

    stocks = Stock.objects.filter(sede=sede_actual).select_related("producto").order_by("producto__nombre")
    query = (request.GET.get("q") or "").strip()
    if query:
        stocks = stocks.filter(producto__nombre__icontains=query)

    return render(request, "inventario/inventory_list.html", {
        "profile": profile, "sede_actual": sede_actual,
        "sedes": sedes_disponibles, "stocks": stocks, "query": query,
    })

@require_POST
@login_required
def update_stock_simple(request):
    """Edición manual desde el botón lápiz."""
    stock_id = request.POST.get('stock_id')
    cantidad = request.POST.get('cantidad')
    sede_redirect = request.POST.get('sede_id_redirect')
    
    stock = get_object_or_404(Stock, id=stock_id)
    if cantidad:
        stock.cantidad = int(cantidad)
        stock.save()
        
    return redirect(f"/dashboard/inventario/?sede_id={sede_redirect}")

# --------------------
# LÓGICA DE ESCÁNER (APIs y Creación)
# --------------------

@login_required
def get_product_by_code(request):
    """
    API JSON para buscar producto. 
    CORREGIDO: Usa 'codigo_interno' en lugar de 'codigo'.
    """
    codigo = request.GET.get('codigo', '').strip().upper()
    sede_id = request.GET.get('sede_id')
    
    if not codigo or not sede_id:
        return JsonResponse({'found': False, 'error': 'Faltan datos'})

    try:
        # 1. Buscar STOCK existente usando codigo_interno
        stock = Stock.objects.select_related('producto').get(sede_id=sede_id, producto__codigo_interno=codigo)
        return JsonResponse({
            'found': True, 'type': 'existente',
            'stock_id': stock.id,
            'nombre': stock.producto.nombre,
            'cantidad_actual': stock.cantidad,
            'medida': stock.producto.unidad # CORREGIDO: 'unidad' en vez de 'unidad_medida'
        })
    except Stock.DoesNotExist:
        try:
            # 2. Buscar PRODUCTO global usando codigo_interno
            prod = Producto.objects.get(codigo_interno=codigo)
            return JsonResponse({
                'found': True, 'type': 'nuevo_en_sede',
                'producto_id': prod.id,
                'nombre': prod.nombre,
                'cantidad_actual': 0,
                'medida': prod.unidad # CORREGIDO
            })
        except Producto.DoesNotExist:
            # 3. No existe -> Devolver para crear
            return JsonResponse({'found': False, 'code_searched': codigo})

@require_POST
@login_required
def add_stock_simple(request):
    """
    Suma stock a un producto existente en la SEDE SELECCIONADA.
    Si el stock no existe en esa sede, lo crea automáticamente.
    """
    producto_id = request.POST.get('producto_id')
    target_sede_id = request.POST.get('target_sede_id') # Sede elegida en el combo
    cantidad = int(request.POST.get('cantidad', 0))
    origen = request.POST.get('origen', 'inventory') 

    if cantidad > 0 and producto_id and target_sede_id:
        
        # 1. Obtener objetos
        prod = get_object_or_404(Producto, id=producto_id)
        sede_obj = get_object_or_404(Sede, id=target_sede_id)

        # 2. Buscar o Crear el Stock en esa Sede específica
        # get_or_create devuelve una tupla (objeto, creado)
        stock, created = Stock.objects.get_or_create(
            sede=sede_obj,
            producto=prod,
            defaults={'cantidad': 0} # Si se crea nuevo, empieza en 0 antes de sumar
        )

        # 3. Sumar la cantidad
        stock.cantidad += cantidad
        stock.save()

    # Redirección
    if origen == 'scan':
        return redirect("scan")
    else:
        # Si venía del inventario, intentamos volver a la sede que se modificó
        return redirect(f"/dashboard/inventario/?sede_id={target_sede_id}")

@require_POST
@login_required
def create_product_simple(request):
    """
    Crea un producto nuevo.
    CORREGIDO: Usa 'codigo_interno', 'unidad' y busca la Categoría por ID.
    """
    codigo = request.POST.get('codigo', '').strip().upper()
    nombre = request.POST.get('nombre', '').strip().upper()
    
    # Obtenemos el ID de la categoría del select
    categoria_id = request.POST.get('categoria_id') 
    
    # El HTML manda 'unidad_medida' en el name, pero la BD usa 'unidad'
    unidad_val = request.POST.get('unidad_medida', 'UNIDAD').upper()
    
    cantidad = int(request.POST.get('cantidad', 0))
    # Intentamos obtener la sede de destino del select, o fallback al redirect
    sede_id = request.POST.get('target_sede_id') or request.POST.get('sede_id_redirect')

    if codigo and nombre and sede_id and categoria_id:
        
        # 1. Buscar la instancia de Categoría
        categoria_obj = get_object_or_404(Categoria, id=categoria_id)
        
        # 2. Crear Producto Global (usando nombres correctos de BD)
        producto, created = Producto.objects.get_or_create(
            codigo_interno=codigo, # CORREGIDO: Antes 'codigo'
            defaults={
                'nombre': nombre,
                'categoria': categoria_obj,
                'unidad': unidad_val, # CORREGIDO: Antes 'unidad_medida'
                'activo': True
            }
        )
        
        # 3. Crear Stock en la Sede
        sede = get_object_or_404(Sede, id=sede_id)
        Stock.objects.get_or_create(
            sede=sede,
            producto=producto,
            defaults={'cantidad': cantidad}
        )

    # Siempre volvemos al escáner
    return redirect("scan")