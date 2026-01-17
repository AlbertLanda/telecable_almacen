from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import F, Sum
from django.shortcuts import redirect, render
from django.utils import timezone

from inventario.models import (
    DocumentoInventario,
    EstadoDocumento,
    MovimientoInventario,
    Stock,
    TipoDocumento,
    UserProfile,
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


# --------------------
# VISTAS
# --------------------
@login_required
def dashboard_redirect(request):
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
    return redirect("dash_admin")


@login_required
def dash_admin(request):
    """Dashboard principal para Administradores y Jefes."""
    profile = _require_roles(request.user, UserProfile.Rol.ADMIN, UserProfile.Rol.JEFA)
    sede = _require_sede(profile)

    # 1) Total equipos (sum de stock)
    total_equipos = (
        Stock.objects.filter(sede=sede).aggregate(total=Sum("cantidad"))["total"] or 0
    )

    # 2) Cables (por nombre contiene "cable")
    total_cables = (
        Stock.objects.filter(sede=sede, producto__nombre__icontains="cable")
        .aggregate(total=Sum("cantidad"))["total"]
        or 0
    )

    # 3) Stock bajo
    low_stock = Stock.objects.filter(
        sede=sede,
        producto__activo=True,
        cantidad__lt=F("producto__stock_minimo"),
    ).count()

    # 4) Últimos movimientos (ojo: sin "usuario" si tu modelo no lo tiene como FK)
    ult_movs = (
        MovimientoInventario.objects.filter(sede=sede)
        .select_related("producto")
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
            "user": request.user,
        },
    )


@login_required
def dash_almacen(request):
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
        {
            "profile": profile,
            "sede": sede,
            "mis_reqs": mis_reqs,
        },
    )


@login_required
def inventory_list(request):
    """
    Inventario con el mismo diseño.
    Roles: ADMIN / JEFA / ALMACEN
    """
    profile = _require_roles(
        request.user,
        UserProfile.Rol.ADMIN,
        UserProfile.Rol.JEFA,
        UserProfile.Rol.ALMACEN,
    )
    sede = _require_sede(profile)

    stocks = (
        Stock.objects.filter(sede=sede)
        .select_related("producto")
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
            "sede": sede,
            "stocks": stocks,
            "query": query,
        },
    )
