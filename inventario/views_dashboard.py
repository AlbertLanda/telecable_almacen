from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import F, Sum, Q
from django.http import JsonResponse
from django.shortcuts import redirect, render, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_POST
from inventario.models import Sede
from inventario.models import Proveedor


from inventario.models import (
    DocumentoInventario,
    EstadoDocumento,
    MovimientoInventario,
    Stock,
    TipoDocumento,
    UserProfile,
    Sede,
    Producto,
    Categoria,
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
    Fallback seguro:
      - sede operativa si existe
      - si no, todas (especialmente útil para JEFA / casos raros)
    """
    qs = profile.sedes_permitidas.all().order_by("id")

    if qs.exists():
        return qs

    sede_op = profile.get_sede_operativa()
    if sede_op:
        return Sede.objects.filter(id=sede_op.id).order_by("id")

    # último fallback (no debería pasar si profile bien configurado)
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

    # si por algún motivo sede quedó None, usamos la primera disponible
    if not sede and hasattr(sedes_disponibles, "first"):
        sede = sedes_disponibles.first()

    return sede


# --------------------
# REDIRECT POR ROL
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

    # 3) Stock bajo (regla como la de Diego: stock_minimo>0 vs default <=5)
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
            "sedes": sedes_disponibles,  # 👈 para el combo
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

    # sedes disponibles según rol
    if profile.rol in (UserProfile.Rol.ADMIN, UserProfile.Rol.JEFA):
        sedes_disponibles = _sedes_disponibles_para_admin(profile)
        sede_actual = _resolve_sede_activa(request, profile, sedes_disponibles)
    else:
        sede_actual = _require_sede(profile)
        sedes_disponibles = [sede_actual]  # para que el template no reviente si lo usa

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
    profile = _require_roles(request.user, UserProfile.Rol.ALMACEN, UserProfile.Rol.JEFA)
    sede = _require_sede(profile)
    hoy = timezone.localdate()

    sedes_central = Sede.objects.filter(tipo=Sede.CENTRAL, activo=True).order_by("nombre")
    proveedores = Proveedor.objects.filter(activo=True).order_by("razon_social")  # ✅ NUEVO

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

    stock_bajo = (
        Stock.objects.filter(sede=sede, producto__activo=True)
        .filter(
            Q(producto__stock_minimo__gt=0, cantidad__lte=F("producto__stock_minimo"))
            | Q(producto__stock_minimo=0, cantidad__lte=5)
        )
        .count()
    )

    ult_movs = (
        MovimientoInventario.objects.filter(sede=sede)
        .select_related("producto", "sede")
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
            "sedes_central": sedes_central,
            "proveedores": proveedores,  # ✅ NUEVO
        },
    )



# --------------------
# DASH SOLICITANTE (SOLICITANTE/JEFA)
# --------------------
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
        {"profile": profile, "sede": sede, "mis_reqs": mis_reqs},
    )


# --------------------
# ENDPOINTS SIMPLES (SI LOS USAS EN TU UI)
# --------------------
@require_POST
@login_required
def update_stock_simple(request):
    stock = get_object_or_404(Stock, id=request.POST.get("stock_id"))

    cantidad_raw = request.POST.get("cantidad", "").strip()
    if cantidad_raw:
        stock.cantidad = int(cantidad_raw)
        stock.save(update_fields=["cantidad"])

    sede_id_redirect = request.POST.get("sede_id_redirect")
    if sede_id_redirect:
        return redirect(f"/dashboard/inventario/?sede_id={sede_id_redirect}")

    return redirect("inventory_list")


@login_required
def get_product_by_code(request):
    codigo = request.GET.get("codigo", "").strip().upper()
    sede_id = request.GET.get("sede_id")

    if not codigo or not sede_id:
        return JsonResponse({"found": False, "error": "Faltan datos"})

    try:
        stock = Stock.objects.select_related("producto").get(
            sede_id=sede_id,
            producto__codigo_interno=codigo,
        )
        return JsonResponse(
            {
                "found": True,
                "type": "existente",
                "stock_id": stock.id,
                "nombre": stock.producto.nombre,
                "cantidad_actual": stock.cantidad,
                "medida": stock.producto.unidad,
            }
        )
    except Stock.DoesNotExist:
        try:
            prod = Producto.objects.get(codigo_interno=codigo)
            return JsonResponse(
                {
                    "found": True,
                    "type": "nuevo_en_sede",
                    "producto_id": prod.id,
                    "nombre": prod.nombre,
                    "cantidad_actual": 0,
                    "medida": prod.unidad,
                }
            )
        except Producto.DoesNotExist:
            return JsonResponse({"found": False, "code_searched": codigo})


@require_POST
@login_required
def add_stock_simple(request):
    prod = get_object_or_404(Producto, id=request.POST.get("producto_id"))
    sede = get_object_or_404(Sede, id=request.POST.get("target_sede_id"))

    stock, _ = Stock.objects.get_or_create(sede=sede, producto=prod, defaults={"cantidad": 0})
    stock.cantidad += int(request.POST.get("cantidad", 0) or 0)
    stock.save(update_fields=["cantidad"])

    if request.POST.get("origen") == "scan":
        return redirect("scan")

    return redirect(f"/dashboard/inventario/?sede_id={sede.id}")


@require_POST
@login_required
def create_product_simple(request):
    cat = get_object_or_404(Categoria, id=request.POST.get("categoria_id"))

    codigo = (request.POST.get("codigo") or "").strip().upper()
    nombre = (request.POST.get("nombre") or "").strip().upper()
    unidad = (request.POST.get("unidad_medida") or "UND").strip().upper()

    prod, _ = Producto.objects.get_or_create(
        codigo_interno=codigo,
        defaults={
            "nombre": nombre,
            "categoria": cat,
            "unidad": unidad,
            "activo": True,
            "stock_minimo": 5,
        },
    )

    sede = get_object_or_404(Sede, id=(request.POST.get("target_sede_id") or request.POST.get("sede_id_redirect")))
    Stock.objects.get_or_create(
        sede=sede,
        producto=prod,
        defaults={"cantidad": int(request.POST.get("cantidad", 0) or 0)},
    )

    return redirect("scan")
