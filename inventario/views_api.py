from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import F
from django.http import JsonResponse
from django.utils import timezone

from inventario.models import (
    UserProfile,
    Stock,
    DocumentoInventario,
    TipoDocumento,
    EstadoDocumento,
    MovimientoInventario,
)

def _require_almacen(user):
    profile = getattr(user, "profile", None)
    if not profile:
        raise PermissionDenied("Usuario sin perfil (UserProfile).")

    if profile.rol not in (UserProfile.Rol.ALMACEN, UserProfile.Rol.JEFA):
        raise PermissionDenied("No autorizado para dashboard de almacén.")

    sede = profile.get_sede_operativa()
    if not sede:
        raise PermissionDenied("No tienes sede operativa asignada.")

    return profile, sede

@login_required
def api_dashboard_almacen(request):
    """
    Devuelve JSON con KPIs + series para charts + tablas.
    Todo filtrado por SEDE (ubicacion es opcional).
    """
    _, sede = _require_almacen(request.user)

    # =========================
    # KPIs
    # =========================
    req_pendientes = DocumentoInventario.objects.filter(
        tipo=TipoDocumento.REQ,
        estado=EstadoDocumento.REQ_PENDIENTE,
        sede=sede,
    ).count()

    stock_bajo = Stock.objects.filter(
        sede=sede,
        producto__activo=True,
        producto__stock_minimo__gt=0,
        cantidad__lt=F("producto__stock_minimo"),
    ).count()

    # Valor inventario (sumatoria simple)
    valor_inv = Decimal("0.00")
    for s in Stock.objects.filter(sede=sede).select_related("producto"):
        valor_inv += (Decimal(s.cantidad) * (s.producto.costo_unitario or Decimal("0.00")))

    # =========================
    # Movimientos últimos 60 min (por minuto)
    # =========================
    now = timezone.now()
    start = (now - timedelta(minutes=60)).replace(second=0, microsecond=0)

    movs = (
        MovimientoInventario.objects.filter(sede=sede, creado_en__gte=start)
        .only("creado_en", "tipo", "qty")
        .order_by("creado_en")
    )

    buckets = {}  # {datetime_minuto: {"IN": x, "OUT": y}}
    for m in movs:
        key = m.creado_en.replace(second=0, microsecond=0)
        if key not in buckets:
            buckets[key] = {"IN": 0, "OUT": 0}
        if m.tipo == MovimientoInventario.TIPO_IN:
            buckets[key]["IN"] += int(m.qty)
        elif m.tipo == MovimientoInventario.TIPO_OUT:
            buckets[key]["OUT"] += int(m.qty)

    labels = []
    series_in = []
    series_out = []

    t = start
    for _ in range(61):
        labels.append(t.strftime("%H:%M"))
        b = buckets.get(t, {"IN": 0, "OUT": 0})
        series_in.append(b["IN"])
        series_out.append(b["OUT"])
        t += timedelta(minutes=1)

    # =========================
    # Top stock (por cantidad)
    # =========================
    top = list(
        Stock.objects.filter(sede=sede, producto__activo=True)
        .select_related("producto")
        .order_by("-cantidad")[:10]
    )
    top_names = [x.producto.nombre for x in top]
    top_qty = [int(x.cantidad) for x in top]

    # =========================
    # Tablas
    # =========================
    stock_rows = []
    for x in (
        Stock.objects.filter(sede=sede, producto__activo=True)
        .select_related("producto")
        .order_by("-cantidad")[:40]
    ):
        low = (x.producto.stock_minimo > 0 and x.cantidad < x.producto.stock_minimo)
        stock_rows.append(
            {
                "codigo": x.producto.codigo_interno,
                "nombre": x.producto.nombre,
                "cantidad": int(x.cantidad),
                "low": bool(low),
            }
        )

    reqs = (
        DocumentoInventario.objects.filter(
            tipo=TipoDocumento.REQ,
            estado=EstadoDocumento.REQ_PENDIENTE,
            sede=sede,
        )
        .select_related("responsable")
        .order_by("-fecha")[:25]
    )
    req_rows = [
        {
            "numero": r.numero or "(sin número)",
            "solicitante": r.responsable.username,
            "fecha": r.fecha.strftime("%Y-%m-%d %H:%M"),
            "estado": r.estado,
        }
        for r in reqs
    ]

    return JsonResponse(
        {
            "kpis": {
                "valor_inventario": float(valor_inv.quantize(Decimal("0.01"))),
                "req_pendientes": req_pendientes,
                "stock_bajo": stock_bajo,
            },
            "charts": {
                "mov_labels": labels,
                "mov_in": series_in,
                "mov_out": series_out,
                "top_names": top_names,
                "top_qty": top_qty,
            },
            "tables": {
                "stock": stock_rows,
                "reqs": req_rows,
            },
        }
    )