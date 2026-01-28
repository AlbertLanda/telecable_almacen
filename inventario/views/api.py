from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import F
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from django.shortcuts import get_object_or_404

# Importamos los modelos del Core
from inventario.models import (
    UserProfile,
    Stock,
    DocumentoInventario,
    TipoDocumento,
    EstadoDocumento,
    MovimientoInventario,
    Sede,
    Proveedor,
    TipoRequerimiento
)

def _require_almacen(user):
    """Helper para validar permisos de almacén"""
    profile = getattr(user, "profile", None)
    if not profile:
        raise PermissionDenied("Usuario sin perfil (UserProfile).")

    if profile.rol not in (UserProfile.Rol.ALMACEN, UserProfile.Rol.JEFA, UserProfile.Rol.ADMIN):
        raise PermissionDenied("No autorizado para dashboard de almacén.")

    sede = profile.get_sede_operativa()
    if not sede:
        raise PermissionDenied("No tienes sede operativa asignada.")

    return profile, sede

@login_required
def api_dashboard_almacen(request):
    """
    Devuelve JSON con KPIs + series para charts + tablas.
    """
    _, sede = _require_almacen(request.user)

    # 1. KPIs
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

    # Valor inventario
    valor_inv = Decimal("0.00")
    # Nota: Esto puede ser lento si hay muchos datos, optimizar en futuro
    stocks = Stock.objects.filter(sede=sede).select_related("producto")
    for s in stocks:
        valor_inv += (Decimal(s.cantidad) * (s.producto.costo_unitario or Decimal("0.00")))

    # 2. Movimientos últimos 60 min
    now = timezone.now()
    start = (now - timedelta(minutes=60)).replace(second=0, microsecond=0)

    movs = (
        MovimientoInventario.objects.filter(sede=sede, creado_en__gte=start)
        .only("creado_en", "tipo", "qty")
        .order_by("creado_en")
    )

    buckets = {}
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

    # 3. Top stock
    top = list(
        Stock.objects.filter(sede=sede, producto__activo=True)
        .select_related("producto")
        .order_by("-cantidad")[:10]
    )
    top_names = [x.producto.nombre for x in top]
    top_qty = [int(x.cantidad) for x in top]

    # 4. Tablas (Stock bajo / Reqs recientes)
    stock_rows = []
    # Top 40 para la tabla
    for x in Stock.objects.filter(sede=sede, producto__activo=True).select_related("producto").order_by("-cantidad")[:40]:
        low = (x.producto.stock_minimo > 0 and x.cantidad < x.producto.stock_minimo)
        stock_rows.append({
            "codigo": x.producto.codigo_interno,
            "nombre": x.producto.nombre,
            "cantidad": int(x.cantidad),
            "low": bool(low),
        })

    reqs = (
        DocumentoInventario.objects.filter(
            tipo=TipoDocumento.REQ,
            estado=EstadoDocumento.REQ_PENDIENTE,
            sede=sede,
        )
        .select_related("responsable")
        .order_by("-fecha")[:25]
    )

    req_rows = []
    reqs_pendientes = []
    
    for r in reqs:
        fecha_str = r.fecha.strftime("%Y-%m-%d %H:%M")
        # Para la tabla simple
        req_rows.append({
            "numero": r.numero or "(sin número)",
            "solicitante": r.responsable.username,
            "fecha": fecha_str,
            "estado": r.estado,
        })
        # Para la tabla avanzada con botones
        reqs_pendientes.append({
            "id": r.id,
            "numero": r.numero or "(sin número)",
            "tipo_requerimiento": getattr(r, "tipo_requerimiento", "") or "LOCAL",
            "solicitante": r.responsable.username,
            "fecha": fecha_str,
            "estado": r.get_estado_display() if hasattr(r, "get_estado_display") else r.estado,
        })

    return JsonResponse({
        "kpis": {
            "valor_inventario": float(valor_inv.quantize(Decimal("0.01"))),
            "req_pendientes": req_pendientes, # Cantidad o lista? Usamos lista en el JS
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
        "reqs_pendientes": reqs_pendientes, # Data cruda para el render JS
    })


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

    if tipo not in (TipoRequerimiento.LOCAL, TipoRequerimiento.PROVEEDOR, TipoRequerimiento.ENTRE_SEDES):
        return JsonResponse({"ok": False, "error": "Tipo de requerimiento inválido."}, status=400)

    # Validaciones rápidas
    if tipo == TipoRequerimiento.PROVEEDOR:
        if sede.tipo != Sede.CENTRAL:
            return JsonResponse({"ok": False, "error": "PROVEEDOR solo aplica en sede CENTRAL."}, status=400)
        if not proveedor_id:
            return JsonResponse({"ok": False, "error": "Selecciona un proveedor."}, status=400)

    if tipo == TipoRequerimiento.ENTRE_SEDES:
        if sede.tipo == Sede.CENTRAL:
            return JsonResponse({"ok": False, "error": "CENTRAL no debe generar REQ 'ENTRE SEDES'."}, status=400)
        if not sede_destino_id:
            return JsonResponse({"ok": False, "error": "Selecciona sede destino CENTRAL."}, status=400)

    # Crear borrador
    req = DocumentoInventario(
        tipo=TipoDocumento.REQ,
        estado=EstadoDocumento.REQ_BORRADOR,
        sede=sede,
        ubicacion=None,
        responsable=request.user,
        fecha=timezone.now(),
        tipo_requerimiento=tipo,
    )

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

    try:
        req.full_clean()
        req.save()
    except ValidationError as e:
        msg = str(e)
        if hasattr(e, "message_dict"):
            msg = " ".join([" ".join(v) for v in e.message_dict.values()])
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