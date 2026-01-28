from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import F, Sum, Q
from django.shortcuts import redirect, render
from django.utils import timezone

from inventario.models import (
    UserProfile,
    Stock,
    DocumentoInventario,
    TipoDocumento,
    EstadoDocumento,
    MovimientoInventario,
    Sede,
    Proveedor
)

# --------------------
# HELPERS
# --------------------
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

def _sedes_disponibles_para_admin(profile: UserProfile):
    """
    Para ADMIN/JEFA: devolver sedes permitidas (si existen).
    Fallback seguro: sede operativa o todas.
    """
    qs = profile.sedes_permitidas.all().order_by("id")
    if qs.exists():
        return qs

    sede_op = profile.get_sede_operativa()
    if sede_op:
        return Sede.objects.filter(id=sede_op.id).order_by("id")

    return Sede.objects.all().order_by("id")

def _resolve_sede_activa(request, profile: UserProfile, sedes_disponibles):
    """
    Decide la sede activa:
    - por defecto: profile.get_sede_operativa()
    - si viene ?sede_id= y está dentro de sedes_disponibles => usarla
    """
    sede = profile.get_sede_operativa()
    sede_id_param = request.GET.get("sede_id")

    if sede_id_param:
        try:
            sede_solicitada = Sede.objects.get(id=sede_id_param)
            if sede_solicitada in sedes_disponibles:
                sede = sede_solicitada
            else:
                messages.error(
                    request,
                    f"⛔ Acceso Denegado: No tienes permisos para ver la sede {sede_solicitada.nombre}.",
                )
        except Sede.DoesNotExist:
            pass

    if not sede and hasattr(sedes_disponibles, "first"):
        sede = sedes_disponibles.first()

    return sede


# --------------------
# REDIRECT POR ROL
# --------------------
@login_required
def dashboard_redirect(request):
    """
    Redirige al dashboard correspondiente según el rol del usuario.
    """
    try:
        profile = _require_roles(
            request.user,
            UserProfile.Rol.SOLICITANTE,
            UserProfile.Rol.ALMACEN,
            UserProfile.Rol.JEFA,
            UserProfile.Rol.ADMIN,
        )
    except PermissionDenied:
        # Si no tiene rol válido, lo mandamos al login o a una página de error
        return redirect("login")

    # El SOLICITANTE (Técnico) ahora va a la app 'operaciones'
    if profile.rol == UserProfile.Rol.SOLICITANTE:
        return redirect("tecnico_dashboard")
    
    if profile.rol == UserProfile.Rol.ALMACEN:
        return redirect("dash_almacen")
        
    return redirect("dash_admin")


# --------------------
# DASH ADMIN (ADMIN/JEFA)
# --------------------
@login_required
def dash_admin(request):
    profile = _require_roles(request.user, UserProfile.Rol.ADMIN, UserProfile.Rol.JEFA)

    sedes_disponibles = _sedes_disponibles_para_admin(profile)
    sede = _resolve_sede_activa(request, profile, sedes_disponibles)

    # 1) Total equipos (sum de stock)
    total_equipos = Stock.objects.filter(sede=sede).aggregate(total=Sum("cantidad"))["total"] or 0

    # 2) Cables (por nombre contiene "cable")
    total_cables = (
        Stock.objects.filter(sede=sede, producto__nombre__icontains="cable")
        .aggregate(total=Sum("cantidad"))["total"]
        or 0
    )

    # 3) Stock bajo
    low_stock = (
        Stock.objects.filter(sede=sede, producto__activo=True)
        .filter(
            Q(producto__stock_minimo__gt=0, cantidad__lte=F("producto__stock_minimo"))
            | Q(producto__stock_minimo=0, cantidad__lte=5)
        )
        .count()
    )

    # 4) Últimos movimientos
    ult_movs = (
        MovimientoInventario.objects.filter(sede=sede)
        .select_related("producto", "sede")
        .order_by("-creado_en")[:10]
    )

    return render(
        request,
        "inventario/dash_admin.html",
        {
            "profile": profile,
            "sede": sede,
            "sedes": sedes_disponibles,
            "total_equipos": total_equipos,
            "total_cables": total_cables,
            "low_stock": low_stock,
            "ult_movs": ult_movs,
            "user": request.user,
        },
    )


# --------------------
# INVENTORY LIST (ADMIN/JEFA/ALMACEN)
# --------------------
@login_required
def inventory_list(request):
    profile = _require_roles(
        request.user,
        UserProfile.Rol.ADMIN,
        UserProfile.Rol.JEFA,
        UserProfile.Rol.ALMACEN,
    )

    if profile.rol in (UserProfile.Rol.ADMIN, UserProfile.Rol.JEFA):
        sedes_disponibles = _sedes_disponibles_para_admin(profile)
        sede_actual = _resolve_sede_activa(request, profile, sedes_disponibles)
    else:
        sede_actual = _require_sede(profile)
        sedes_disponibles = [sede_actual]

    stocks = (
        Stock.objects.filter(sede=sede_actual)
        .select_related("producto", "producto__categoria")
        .order_by("producto__nombre")
    )

    query = (request.GET.get("q") or "").strip()
    if query:
        stocks = stocks.filter(producto__nombre__icontains=query)

    return render(
        request,
        "inventario/inventory_list.html",
        {
            "profile": profile,
            "sede_actual": sede_actual,
            "sedes": sedes_disponibles,
            "stocks": stocks,
            "query": query,
        },
    )


# --------------------
# DASH ALMACEN (ALMACEN/JEFA)
# --------------------
@login_required
def dash_almacen(request):
    # 1. Seguridad y Contexto (Tu lógica original)
    profile = _require_roles(request.user, UserProfile.Rol.ALMACEN, UserProfile.Rol.JEFA)
    sede = _require_sede(profile)
    hoy = timezone.localdate()

    # 2. Listas auxiliares (para modales o futuros usos)
    sedes_central = Sede.objects.filter(tipo=Sede.CENTRAL, activo=True).order_by("nombre")
    proveedores = Proveedor.objects.filter(activo=True).order_by("razon_social")

    # 3. BANDEJA DE ENTRADA (Tabla Principal)
    # Obtenemos la lista completa de pendientes para mostrar en la tabla grande
    reqs_pendientes_list = DocumentoInventario.objects.filter(
        tipo=TipoDocumento.REQ,
        estado=EstadoDocumento.REQ_PENDIENTE,
        sede=sede,
    ).select_related("responsable").order_by("fecha")

    # 4. STOCK BAJO (Lógica Doble)
    # Creamos la consulta base
    query_stock_bajo = Stock.objects.filter(sede=sede, producto__activo=True).filter(
        Q(producto__stock_minimo__gt=0, cantidad__lte=F("producto__stock_minimo"))
        | Q(producto__stock_minimo=0, cantidad__lte=5)
    ).select_related('producto')

    # A) Para el KPI (Número)
    count_stock_bajo = query_stock_bajo.count()
    # B) Para el Panel Lateral (Lista de objetos, solo los primeros 5)
    items_stock_bajo = query_stock_bajo[:5] 

    # 5. MOVIMIENTOS HOY (KPI)
    # Sumamos Salidas confirmadas + Ingresos confirmados hoy
    movimientos_hoy = DocumentoInventario.objects.filter(
        sede=sede,
        estado=EstadoDocumento.CONFIRMADO,
        fecha__date=hoy
    ).count()

    return render(
        request,
        "inventario/dash_almacen.html",
        {
            "profile": profile,
            "sede": sede,
            
            # Datos para KPIs (Tarjetas de arriba)
            "kpi_pendientes": reqs_pendientes_list.count(),
            "kpi_movimientos": movimientos_hoy,
            "kpi_stock": count_stock_bajo,

            # Datos para Tablas y Listas
            "reqs_pendientes": reqs_pendientes_list, # Tabla izquierda
            "items_bajos": items_stock_bajo,         # Alerta derecha
            
            # Extras
            "sedes_central": sedes_central,
            "proveedores": proveedores,
        },
    )

# --------------------
# DASH SOLICITANTE (SOLICITANTE/JEFA)
# --------------------
@login_required
def dash_solicitante(request):
    """
    Vista de respaldo si el técnico entra a /dashboard/solicitante/
    aunque lo ideal es que use /operaciones/tecnico/
    """
    profile = _require_roles(request.user, UserProfile.Rol.SOLICITANTE, UserProfile.Rol.JEFA)
    sede = _require_sede(profile)

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
        {"profile": profile, "sede": sede, "mis_reqs": mis_reqs},
    )
