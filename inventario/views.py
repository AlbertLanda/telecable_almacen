from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import F, Sum  # <--- Importante para sumar totales
from django.utils import timezone

# Modelos
from inventario.models import (
    DocumentoInventario,
    EstadoDocumento,
    MovimientoInventario,
    Stock,
    TipoDocumento,
    UserProfile,
)

# Servicios
from inventario.services.scan_service import buscar_producto_y_stock

# --- Helpers de Permisos ---

def _require_roles(user, *roles):
    profile = getattr(user, "profile", None)
    if not profile:
        raise PermissionDenied("Usuario sin perfil (UserProfile).")
    if profile.rol not in roles:
        raise PermissionDenied("No tienes permisos para esta acción.")
    return profile

def _require_sede(profile: UserProfile):
    sede = profile.get_sede_operativa()
    if not sede:
        raise PermissionDenied("No tienes sede operativa asignada.")
    return sede

# --- Vistas ---

@login_required
def dashboard_redirect(request):
    """Redirige al usuario a su dashboard correspondiente según su rol."""
    profile = _require_roles(
        request.user,
        UserProfile.Rol.SOLICITANTE,
        UserProfile.Rol.ALMACEN,
        UserProfile.Rol.JEFA,
        UserProfile.Rol.ADMIN,
    )

    if profile.rol == UserProfile.Rol.SOLICITANTE:
        return redirect("tecnico_dashboard")

    if profile.rol == UserProfile.Rol.ALMACEN:
        return redirect("dash_almacen")

    # JEFA / ADMIN van al dashboard admin
    return redirect("dash_admin")


@login_required
def dash_admin(request):
    """Dashboard principal para Administradores y Jefes."""
    profile = _require_roles(request.user, UserProfile.Rol.ADMIN, UserProfile.Rol.JEFA)
    sede = profile.get_sede_operativa()
    
    # 1. KPI: Total Equipos (Suma de cantidad en stock)
    total_equipos = Stock.objects.filter(sede=sede).aggregate(total=Sum('cantidad'))['total'] or 0

    # 2. KPI: Cables (Opcional: filtra por nombre si tienes productos 'Cable')
    total_cables = Stock.objects.filter(
        sede=sede, 
        producto__nombre__icontains="cable"
    ).aggregate(total=Sum('cantidad'))['total'] or 0

    # 3. KPI: Stock Bajo
    low_stock = Stock.objects.filter(
        sede=sede,
        producto__activo=True,
        cantidad__lt=F("producto__stock_minimo"),
    ).count()

    # 4. Tabla: Últimos Movimientos
    ult_movs = (
        MovimientoInventario.objects.filter(sede=sede)
        .select_related("producto", "usuario")
        .order_by("-creado_en")[:10]
    )

    return render(
        request,
        "inventario/dash_admin.html",
        {
            "profile": profile,
            "sede": sede,
            "total_equipos": total_equipos,
            "total_cables": total_cables,
            "low_stock": low_stock,
            "ult_movs": ult_movs,
        },
    )


@login_required
def dash_almacen(request):
    """Dashboard para encargados de almacén."""
    profile = _require_roles(request.user, UserProfile.Rol.ALMACEN, UserProfile.Rol.JEFA)
    sede = _require_sede(profile)
    hoy = timezone.localdate()

    req_pendientes = DocumentoInventario.objects.filter(
        tipo=TipoDocumento.REQ,
        estado=EstadoDocumento.REQ_PENDIENTE,
        sede=sede,
    ).count()

    sal_hoy = DocumentoInventario.objects.filter(
        tipo=TipoDocumento.SAL,
        estado=EstadoDocumento.CONFIRMADO,
        sede=sede,
        fecha__date=hoy,
    ).count()

    ing_pendientes = DocumentoInventario.objects.filter(
        tipo=TipoDocumento.ING,
        estado=EstadoDocumento.BORRADOR,
        sede=sede,
    ).count()

    stock_bajo = Stock.objects.filter(
        sede=sede,
        producto__activo=True,
        producto__stock_minimo__gt=0,
        cantidad__lt=F("producto__stock_minimo"),
    ).count()

    ult_movs = (
        MovimientoInventario.objects.filter(sede=sede)
        .select_related("producto")
        .order_by("-creado_en")[:12]
    )

    ult_reqs = (
        DocumentoInventario.objects.filter(
            tipo=TipoDocumento.REQ,
            estado=EstadoDocumento.REQ_PENDIENTE,
            sede=sede,
        )
        .select_related("responsable")
        .order_by("-fecha")[:10]
    )

    return render(
        request,
        "inventario/dash_almacen.html",
        {
            "profile": profile,
            "sede": sede,
            "req_pendientes": req_pendientes,
            "sal_hoy": sal_hoy,
            "ing_pendientes": ing_pendientes,
            "stock_bajo": stock_bajo,
            "ult_movs": ult_movs,
            "ult_reqs": ult_reqs,
        },
    )


@login_required
def dash_solicitante(request):
    """Dashboard (legacy) para solicitantes, aunque deberían ir a tecnico_dashboard."""
    profile = _require_roles(request.user, UserProfile.Rol.SOLICITANTE, UserProfile.Rol.JEFA)
    sede = profile.get_sede_operativa()

    mis_reqs = (
        DocumentoInventario.objects.filter(
            tipo=TipoDocumento.REQ,
            responsable=request.user,
        )
        .order_by("-fecha")[:12]
    )

    return render(
        request,
        "inventario/dash_solicitante.html",
        {
            "profile": profile,
            "sede": sede,
            "mis_reqs": mis_reqs,
        },
    )


@login_required
def scan_view(request):
    """Vista para escanear productos."""
    code = (request.GET.get("q") or "").strip()
    # Asumimos que buscar_producto_y_stock maneja la lógica de búsqueda
    producto, stocks, error = buscar_producto_y_stock(code)

    return render(request, "inventario/scan.html", {
        "q": code,
        "producto": producto,
        "stocks": stocks,
        "error": error,
    })
