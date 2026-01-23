from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import F
from django.http import JsonResponse
from django.utils import timezone

from django.views.decorators.http import require_GET, require_POST
from django.utils import timezone
from django.shortcuts import get_object_or_404
from django.core.exceptions import ValidationError

from inventario.models import Proveedor, TipoRequerimiento, Sede

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
    # Tablas (las que ya usas hoy)
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

    # =========================
    # ✅ NUEVO: REQs pendientes (para pestaña "Requerimientos")
    # - esto es lo que usa loadReqs() en tu dash_almacen.html
    # =========================
    reqs_pendientes = []
    for r in reqs:  # reutilizamos el queryset de arriba (mismo filtro)
        reqs_pendientes.append({
            "id": r.id,
            "numero": r.numero or "(sin número)",
            "tipo_requerimiento": getattr(r, "tipo_requerimiento", "") or "LOCAL",
            "solicitante": r.responsable.username,
            "fecha": r.fecha.strftime("%Y-%m-%d %H:%M"),
            "estado": r.get_estado_display() if hasattr(r, "get_estado_display") else r.estado,
        })

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
            # ✅ lo nuevo
            "reqs_pendientes": reqs_pendientes,
        }
    )


@require_GET
@login_required
def api_reqs_almacen_list(request):
    _, sede = _require_almacen(request.user)

    qs = (
        DocumentoInventario.objects
        .filter(tipo=TipoDocumento.REQ, sede=sede)
        .select_related("responsable")
        .order_by("-fecha")[:80]
    )

    results = []
    for r in qs:
        results.append({
            "id": r.id,
            "numero": r.numero or "(sin número)",
            "tipo_requerimiento": r.tipo_requerimiento,
            "solicitante": r.responsable.username if r.responsable else "-",
            "fecha": r.fecha.strftime("%Y-%m-%d %H:%M") if r.fecha else "",
            "estado": r.estado,
        })

    return JsonResponse({"ok": True, "results": results})


@require_POST
@login_required
def api_reqs_almacen_create(request):
    _, sede = _require_almacen(request.user)

    tipo = (request.POST.get("tipo_requerimiento") or TipoRequerimiento.LOCAL).strip().upper()
    sede_destino_id = (request.POST.get("sede_destino_id") or "").strip()
    proveedor_id = (request.POST.get("proveedor_id") or "").strip()

    # Validar tipo
    if tipo not in (TipoRequerimiento.LOCAL, TipoRequerimiento.PROVEEDOR, TipoRequerimiento.ENTRE_SEDES):
        return JsonResponse({"ok": False, "error": "Tipo de requerimiento inválido."}, status=400)

    # Reglas rápidas (para mensajes más claros)
    if tipo == TipoRequerimiento.PROVEEDOR:
        # Solo CENTRAL puede crear PROVEEDOR
        if sede.tipo != Sede.CENTRAL:
            return JsonResponse({"ok": False, "error": "PROVEEDOR solo aplica en sede CENTRAL."}, status=400)
        if not proveedor_id:
            return JsonResponse({"ok": False, "error": "Selecciona un proveedor."}, status=400)

    if tipo == TipoRequerimiento.ENTRE_SEDES:
        # CENTRAL no debe generar ENTRE_SEDES (según tu clean)
        if sede.tipo == Sede.CENTRAL:
            return JsonResponse({"ok": False, "error": "CENTRAL no debe generar REQ 'ENTRE SEDES'."}, status=400)
        if not sede_destino_id:
            return JsonResponse({"ok": False, "error": "Selecciona sede destino CENTRAL."}, status=400)

    # Crear borrador
    req = DocumentoInventario(
        tipo=TipoDocumento.REQ,
        estado=EstadoDocumento.REQ_BORRADOR,
        sede=sede,
        ubicacion=None,  # luego si quieres eliges ubicación
        responsable=request.user,
        fecha=timezone.now(),
        tipo_requerimiento=tipo,
    )

    # Set campos según tipo
    if tipo == TipoRequerimiento.LOCAL:
        req.sede_destino = None
        req.proveedor = None

    elif tipo == TipoRequerimiento.PROVEEDOR:
        req.sede_destino = None
        req.proveedor = get_object_or_404(Proveedor, id=proveedor_id, activo=True)

    elif tipo == TipoRequerimiento.ENTRE_SEDES:
        req.proveedor = None
        sede_destino = get_object_or_404(Sede, id=sede_destino_id, activo=True)
        if sede_destino.tipo != Sede.CENTRAL:
            return JsonResponse({"ok": False, "error": "Destino debe ser CENTRAL."}, status=400)
        req.sede_destino = sede_destino

    # Validación del modelo (tu clean)
    try:
        req.full_clean()
        req.save()
    except ValidationError as e:
        if hasattr(e, "message_dict"):
            msg = " ".join([" ".join(v) for v in e.message_dict.values()])
        else:
            msg = str(e)
        return JsonResponse({"ok": False, "error": msg}, status=400)

    return JsonResponse({
        "ok": True,
        "req": {
            "id": req.id,
            "numero": req.numero or "(sin número)",
            "tipo_requerimiento": req.tipo_requerimiento,
            "solicitante": request.user.username,
            "fecha": req.fecha.strftime("%Y-%m-%d %H:%M"),
            "estado": req.estado,
        }
    })

