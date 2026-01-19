from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import F, Sum, Q
from django.shortcuts import redirect, render, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.http import JsonResponse
from django.contrib import messages # <--- IMPORTANTE PARA LAS NOTIFICACIONES

from inventario.models import (
    DocumentoInventario, EstadoDocumento, MovimientoInventario,
    Stock, TipoDocumento, UserProfile, Sede, Producto, Categoria
)

# --- HELPERS ---
def _require_roles(user, *roles):
    profile = getattr(user, "profile", None)
    if not profile: raise PermissionDenied("Usuario sin perfil.")
    if profile.rol not in roles: raise PermissionDenied("No tienes permisos.")
    return profile

def _require_sede(profile: UserProfile):
    sede = profile.get_sede_operativa()
    if not sede: raise PermissionDenied("No tienes sede operativa asignada.")
    return sede

# --- DASHBOARD ADMIN ---
@login_required
def dash_admin(request):
    """Dashboard con Filtro de Sedes Permitidas"""
    profile = _require_roles(request.user, UserProfile.Rol.ADMIN, UserProfile.Rol.JEFA)
    
    # 1. OBTENER SOLO LAS SEDES PERMITIDAS DEL PERFIL
    # Esto asegura que AAALANDA solo vea Huancayo, y admin_almacen vea las 3.
    sedes_disponibles = profile.sedes_permitidas.all().order_by('id')
    
    # Si por alguna razón la lista está vacía, usamos su sede principal como fallback
    if not sedes_disponibles.exists():
        sedes_disponibles = Sede.objects.filter(id=profile.sede_principal.id)

    # 2. DETERMINAR LA SEDE ACTIVA
    sede_id_param = request.GET.get('sede_id')
    sede = profile.get_sede_operativa() # Por defecto, la sede actual del usuario

    if sede_id_param:
        try:
            sede_solicitada = Sede.objects.get(id=sede_id_param)
            
            # VERIFICACIÓN DE SEGURIDAD: ¿El usuario tiene permiso para esta sede?
            if sede_solicitada in sedes_disponibles:
                sede = sede_solicitada
            else:
                # Si intenta entrar a una sede no permitida, lanzamos error y nos quedamos en la actual
                messages.error(request, f"⛔ Acceso Denegado: No tienes permisos para ver la sede {sede_solicitada.nombre}.")
        except Sede.DoesNotExist:
            pass
    
    # 3. CÁLCULOS (Usando la 'sede' validada)
    total_equipos = Stock.objects.filter(sede=sede).aggregate(total=Sum("cantidad"))["total"] or 0
    total_cables = Stock.objects.filter(sede=sede, producto__nombre__icontains="cable").aggregate(total=Sum("cantidad"))["total"] or 0
    
    low_stock = Stock.objects.filter(sede=sede, producto__activo=True).filter(
        Q(producto__stock_minimo__gt=0, cantidad__lte=F("producto__stock_minimo")) |
        Q(producto__stock_minimo=0, cantidad__lte=5)
    ).count()

    ult_movs = MovimientoInventario.objects.filter(sede=sede).select_related("producto", "sede").order_by("-creado_en")[:10]
    
    return render(request, "inventario/dash_admin.html", {
        "profile": profile, "sede": sede, 
        "sedes": sedes_disponibles, # Enviamos solo las permitidas al HTML
        "total_equipos": total_equipos, "total_cables": total_cables, 
        "low_stock": low_stock, "ult_movs": ult_movs, "user": request.user,
    })

# --- INVENTARIO LIST (Misma lógica de seguridad) ---
@login_required
def inventory_list(request):
    profile = _require_roles(request.user, UserProfile.Rol.ADMIN, UserProfile.Rol.JEFA, UserProfile.Rol.ALMACEN)
    
    # 1. Filtrar sedes permitidas
    if profile.rol in [UserProfile.Rol.ADMIN, UserProfile.Rol.JEFA]:
        sedes_disponibles = profile.sedes_permitidas.all().order_by('id')
        if not sedes_disponibles.exists():
             sedes_disponibles = Sede.objects.filter(id=profile.sede_principal.id)
    else:
        sedes_disponibles = [profile.get_sede_operativa()]

    # 2. Validar cambio de sede
    sede_id_param = request.GET.get('sede_id')
    sede_actual = profile.get_sede_operativa()
    
    if sede_id_param and profile.rol in [UserProfile.Rol.ADMIN, UserProfile.Rol.JEFA]:
        try:
            sede_solicitada = Sede.objects.get(id=sede_id_param)
            # Verificación de permiso
            if sede_solicitada in sedes_disponibles:
                sede_actual = sede_solicitada
            else:
                messages.error(request, f"⛔ No tienes acceso a {sede_solicitada.nombre}")
        except Sede.DoesNotExist:
            pass 

    stocks = Stock.objects.filter(sede=sede_actual).select_related("producto", "producto__categoria").order_by("producto__nombre")
    query = (request.GET.get("q") or "").strip()
    if query: stocks = stocks.filter(producto__nombre__icontains=query)
    
    return render(request, "inventario/inventory_list.html", {
        "profile": profile, "sede_actual": sede_actual, "sedes": sedes_disponibles, "stocks": stocks, "query": query,
    })

# ... (El resto de funciones: dashboard_redirect, dash_almacen, dash_solicitante, 
# update_stock_simple, get_product_by_code, add_stock_simple, create_product_simple 
# SE MANTIENEN IGUAL QUE EN TU VERSIÓN ANTERIOR) ...
@login_required
def dashboard_redirect(request):
    profile = _require_roles(request.user, UserProfile.Rol.SOLICITANTE, UserProfile.Rol.ALMACEN, UserProfile.Rol.JEFA, UserProfile.Rol.ADMIN)
    if profile.rol == UserProfile.Rol.SOLICITANTE: return redirect("tecnico_dashboard")
    if profile.rol == UserProfile.Rol.ALMACEN: return redirect("dash_almacen")
    return redirect("dash_admin")

@login_required
def dash_almacen(request):
    profile = _require_roles(request.user, UserProfile.Rol.ALMACEN, UserProfile.Rol.JEFA)
    sede = _require_sede(profile)
    hoy = timezone.localdate()
    req_pendientes = DocumentoInventario.objects.filter(tipo=TipoDocumento.REQ, estado=EstadoDocumento.REQ_PENDIENTE, sede=sede).count()
    sal_hoy = DocumentoInventario.objects.filter(tipo=TipoDocumento.SAL, estado=EstadoDocumento.CONFIRMADO, sede=sede, fecha__date=hoy).count()
    ing_pendientes = DocumentoInventario.objects.filter(tipo=TipoDocumento.ING, estado=EstadoDocumento.BORRADOR, sede=sede).count()
    stock_bajo = Stock.objects.filter(sede=sede, producto__activo=True).filter(Q(producto__stock_minimo__gt=0, cantidad__lte=F("producto__stock_minimo")) | Q(producto__stock_minimo=0, cantidad__lte=5)).count()
    ult_movs = MovimientoInventario.objects.filter(sede=sede).select_related("producto", "sede").order_by("-creado_en")[:12]
    ult_reqs = DocumentoInventario.objects.filter(tipo=TipoDocumento.REQ, estado=EstadoDocumento.REQ_PENDIENTE, sede=sede).select_related("responsable").order_by("-fecha")[:10]
    return render(request, "inventario/dash_almacen.html", {"profile": profile, "sede": sede, "req_pendientes": req_pendientes, "sal_hoy": sal_hoy, "ing_pendientes": ing_pendientes, "stock_bajo": stock_bajo, "ult_movs": ult_movs, "ult_reqs": ult_reqs})

@login_required
def dash_solicitante(request):
    profile = _require_roles(request.user, UserProfile.Rol.SOLICITANTE, UserProfile.Rol.JEFA)
    sede = _require_sede(profile)
    mis_reqs = DocumentoInventario.objects.filter(tipo=TipoDocumento.REQ, responsable=request.user).order_by("-fecha")[:12]
    return render(request, "inventario/dash_solicitante.html", {"profile": profile, "sede": sede, "mis_reqs": mis_reqs})

@require_POST
@login_required
def update_stock_simple(request):
    stock = get_object_or_404(Stock, id=request.POST.get('stock_id'))
    if request.POST.get('cantidad'):
        stock.cantidad = int(request.POST.get('cantidad'))
        stock.save()
    return redirect(f"/dashboard/inventario/?sede_id={request.POST.get('sede_id_redirect')}")

@login_required
def get_product_by_code(request):
    codigo = request.GET.get('codigo', '').strip().upper()
    sede_id = request.GET.get('sede_id')
    if not codigo or not sede_id: return JsonResponse({'found': False, 'error': 'Faltan datos'})
    try:
        stock = Stock.objects.select_related('producto').get(sede_id=sede_id, producto__codigo_interno=codigo)
        return JsonResponse({'found': True, 'type': 'existente', 'stock_id': stock.id, 'nombre': stock.producto.nombre, 'cantidad_actual': stock.cantidad, 'medida': stock.producto.unidad})
    except Stock.DoesNotExist:
        try:
            prod = Producto.objects.get(codigo_interno=codigo)
            return JsonResponse({'found': True, 'type': 'nuevo_en_sede', 'producto_id': prod.id, 'nombre': prod.nombre, 'cantidad_actual': 0, 'medida': prod.unidad})
        except Producto.DoesNotExist:
            return JsonResponse({'found': False, 'code_searched': codigo})

@require_POST
@login_required
def add_stock_simple(request):
    prod = get_object_or_404(Producto, id=request.POST.get('producto_id'))
    sede = get_object_or_404(Sede, id=request.POST.get('target_sede_id'))
    stock, _ = Stock.objects.get_or_create(sede=sede, producto=prod, defaults={'cantidad': 0})
    stock.cantidad += int(request.POST.get('cantidad', 0))
    stock.save()
    if request.POST.get('origen') == 'scan': return redirect("scan")
    return redirect(f"/dashboard/inventario/?sede_id={sede.id}")

@require_POST
@login_required
def create_product_simple(request):
    cat = get_object_or_404(Categoria, id=request.POST.get('categoria_id'))
    prod, _ = Producto.objects.get_or_create(
        codigo_interno=request.POST.get('codigo', '').strip().upper(),
        defaults={'nombre': request.POST.get('nombre', '').strip().upper(), 'categoria': cat, 'unidad': request.POST.get('unidad_medida', 'UNIDAD').upper(), 'activo': True, 'stock_minimo': 5}
    )
    sede = get_object_or_404(Sede, id=request.POST.get('target_sede_id') or request.POST.get('sede_id_redirect'))
    Stock.objects.get_or_create(sede=sede, producto=prod, defaults={'cantidad': int(request.POST.get('cantidad', 0))})
    return redirect("scan")