from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import F
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


@login_required
def dashboard_redirect(request):
    profile = _require_roles(
        request.user,
        UserProfile.Rol.SOLICITANTE,
        UserProfile.Rol.ALMACEN,
        UserProfile.Rol.JEFA,
        UserProfile.Rol.ADMIN,
    )

    # ✅ Técnico (SOLICITANTE) => dashboard técnico
    if profile.rol == UserProfile.Rol.SOLICITANTE:
        return redirect("tecnico_dashboard")

    # ✅ Almacén
    if profile.rol == UserProfile.Rol.ALMACEN:
        return redirect("dash_almacen")

    # ✅ JEFA / ADMIN
    return redirect("dash_admin")


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
    """
    Si aún quieres mantener esta vista, ok.
    Pero el técnico ya no caerá aquí, porque lo mandamos a tecnico_dashboard.
    (JEFA puede entrar manualmente si quieres.)
    """
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
def dash_admin(request):
    profile = _require_roles(request.user, UserProfile.Rol.ADMIN, UserProfile.Rol.JEFA)
    sede = profile.get_sede_operativa()

    return render(
        request,
        "inventario/dash_admin.html",
        {
            "profile": profile,
            "sede": sede,
        },
    )
