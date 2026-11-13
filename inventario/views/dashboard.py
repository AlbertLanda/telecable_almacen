from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import urlencode
from itertools import chain
from operator import attrgetter

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import (
    F,
    Q,
    Sum,
    Count,
    Case,
    When,
    IntegerField,
    CharField,
    DateTimeField,
    Exists,
    Value,
    Min,
    OuterRef,
    Subquery,
)
from django.db.models.functions import Coalesce, TruncDate
from django.http import JsonResponse
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
    Proveedor,
    TipoRequerimiento,
    Producto,
)

from proyectos.models import Proyecto, EstadoProyecto
from proyectos.queries import proyectos_por_atender
from inventario.services.firma_service import acta_estado, estado_documento, inicio_firmas
from inventario.views.firma import documentos_del_almacenero


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


def _stocks_criticos_por_sede(sede, limite=10):
    """
    Devuelve productos con stock crítico real de una sede.

    Regla:
    - Solo alerta si el producto tiene stock_minimo configurado.
    - Si stock_minimo = 0, NO se alerta porque se interpreta como
      "sin mínimo configurado".
    """
    if not sede:
        return Stock.objects.none()

    return (
        Stock.objects.filter(
            sede=sede,
            producto__activo=True,
            producto__stock_minimo__gt=0,
            cantidad__lte=F("producto__stock_minimo"),
        )
        .select_related("producto", "sede")
        .order_by("cantidad", "producto__nombre")[:limite]
    )


def _serializar_alertas_stock(stocks):
    """
    Convierte los stocks críticos en datos seguros para JSON.
    El campo id sirve para que el navegador no repita la misma alerta de voz.
    """
    alertas = []

    for stock in stocks:
        producto = stock.producto

        alertas.append(
            {
                "id": f"{stock.sede_id}-{producto.id}-{stock.cantidad}",
                "sede": stock.sede.nombre,
                "producto": producto.nombre,
                "codigo": producto.codigo_interno or "",
                "cantidad": int(stock.cantidad or 0),
                "stock_minimo": int(producto.stock_minimo or 0),
                "unidad": getattr(producto, "unidad", "") or "UND",
            }
        )

    return alertas


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
        return redirect("login")

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

    # 1) Total equipos
    total_equipos = (
        Stock.objects.filter(sede=sede).aggregate(total=Sum("cantidad"))["total"]
        or 0
    )

    # 2) Cables
    total_cables = (
        Stock.objects.filter(sede=sede, producto__nombre__icontains="cable")
        .aggregate(total=Sum("cantidad"))["total"]
        or 0
    )

    # 3) Stock crítico / bajo
    stocks_criticos_qs = _stocks_criticos_por_sede(sede, limite=10)
    alertas_stock = _serializar_alertas_stock(stocks_criticos_qs)
    low_stock = len(alertas_stock)

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
            "alertas_stock": alertas_stock,
            "ult_movs": ult_movs,
            "user": request.user,
        },
    )


# --------------------
# API ALERTAS DE VOZ STOCK
# --------------------
@login_required
def api_alertas_stock_voz(request):
    """
    API para el dashboard de ADMIN/JEFA.
    Devuelve los productos con stock crítico para que el navegador los lea con voz.
    """
    profile = _require_roles(request.user, UserProfile.Rol.ADMIN, UserProfile.Rol.JEFA)

    sedes_disponibles = _sedes_disponibles_para_admin(profile)
    sede = _resolve_sede_activa(request, profile, sedes_disponibles)

    if not sede:
        return JsonResponse(
            {
                "ok": False,
                "error": "No se encontró sede activa.",
                "alertas": [],
            }
        )

    stocks_criticos = _stocks_criticos_por_sede(sede, limite=10)
    alertas = _serializar_alertas_stock(stocks_criticos)

    return JsonResponse(
        {
            "ok": True,
            "sede": sede.nombre,
            "total": len(alertas),
            "alertas": alertas,
        }
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

    filtro = request.GET.get("filtro")
    if filtro == "critico":
        stocks = stocks.filter(
            producto__activo=True,
            producto__stock_minimo__gt=0,
            cantidad__lte=F("producto__stock_minimo")
        )

    return render(
        request,
        "inventario/inventory_list.html",
        {
            "profile": profile,
            "sede_actual": sede_actual,
            "sedes": sedes_disponibles,
            "stocks": stocks,
            "query": query,
            "filtro": filtro,
        },
    )


# --------------------
# DASH ALMACEN (ALMACEN/JEFA)
# --------------------
@login_required
def dash_almacen(request):
    # ==========================================
    # 1. SEGURIDAD Y CONTEXTO
    # ==========================================
    try:
        profile = request.user.profile
    except UserProfile.DoesNotExist:
        messages.error(request, "Tu usuario no tiene un perfil configurado.")
        return redirect("home")

    sede = profile.get_sede_operativa()

    if not sede:
        messages.error(request, "Tu usuario no tiene ninguna sede asignada.")
        return redirect("home")

    hoy = timezone.localdate()
    sede_actual = sede

    # ==========================================
    # 2. LISTAS AUXILIARES
    # ==========================================
    sedes_central = Sede.objects.filter(tipo=Sede.CENTRAL, activo=True).order_by("nombre")
    proveedores = Proveedor.objects.filter(activo=True).order_by("razon_social")

    # ==========================================
    # 3. BANDEJA DE ENTRADA (REQs PENDIENTES)
    # ==========================================
    reqs_pendientes_list = (
        DocumentoInventario.objects.filter(
            tipo=TipoDocumento.REQ,
            estado=EstadoDocumento.REQ_PENDIENTE,
        )
        .filter(
            Q(sede=sede, tipo_requerimiento=TipoRequerimiento.LOCAL)
            | Q(sede_destino=sede, tipo_requerimiento=TipoRequerimiento.ENTRE_SEDES)
            | Q(sede=sede, tipo_requerimiento=TipoRequerimiento.PROVEEDOR)
        )
        .select_related("responsable", "sede")
        .order_by("fecha")
    )

    # ==========================================
    # 4. KPI: STOCK BAJO
    # ==========================================
    query_stock_bajo = (
        Stock.objects.filter(
            sede=sede,
            producto__activo=True,
            producto__stock_minimo__gt=0,
            cantidad__lte=F("producto__stock_minimo"),
        )
        .select_related("producto")
    )

    count_stock_bajo = query_stock_bajo.count()
    items_stock_bajo = query_stock_bajo[:5]

    # ==========================================
    # 5. KPI: MOVIMIENTOS HOY
    # ==========================================
    movimientos_hoy = DocumentoInventario.objects.filter(
        sede=sede,
        estado=EstadoDocumento.CONFIRMADO,
        fecha__date=hoy,
    ).count()

    # ==========================================
    # 6. KPI: PROYECTOS POR ATENDER (mismo alcance multisede que la bandeja)
    # ==========================================
    proyectos_pendientes = proyectos_por_atender()
    if profile.rol != UserProfile.Rol.ALMACEN:
        # Solo Almacén tiene acceso a la bandeja multisede. Otros accesos
        # existentes al dashboard conservan el alcance de su propia sede.
        proyectos_pendientes = proyectos_pendientes.filter(sede=sede)
    count_proyectos_activos = proyectos_pendientes.count()

    # ==========================================
    # 7. KPI TOTAL
    # ==========================================
    total_pendientes_kpi = reqs_pendientes_list.count() + count_proyectos_activos

    # ==========================================
    # 8. TRANSFERENCIAS ENTRANTES
    # ==========================================
    transferencias_entrantes = (
        DocumentoInventario.objects.filter(
            tipo=TipoDocumento.SAL,
            sede_destino=sede,
            estado=EstadoDocumento.CONFIRMADO,
            recibido=False,
        )
        .exclude(sede=sede)
        .select_related("sede", "responsable")
    )

    # ==========================================
    # 9. LÓGICA PARA GRÁFICOS
    # ==========================================
    agg_stock = Stock.objects.filter(sede=sede_actual).aggregate(
        stock_critico=Sum(
            Case(
                When(
                    producto__activo=True,
                    producto__stock_minimo__gt=0,
                    cantidad__lte=F("producto__stock_minimo"),
                    then=1
                ),
                default=0,
                output_field=IntegerField(),
            )
        ),
        stock_bajo=Sum(
            Case(
                When(
                    producto__activo=True,
                    producto__stock_minimo__gt=0,
                    cantidad__gt=F("producto__stock_minimo"),
                    cantidad__lte=F("producto__stock_minimo") * 1.5, # Ejemplo: Un 50% por encima del mínimo es "Bajo"
                    then=1
                ),
                default=0,
                output_field=IntegerField(),
            )
        ),
        stock_saludable=Sum(
            Case(
                # Es saludable si supera el umbral de bajo, o si el producto no tiene mínimo configurado
                When(
                    Q(cantidad__gt=F("producto__stock_minimo") * 1.5) | 
                    Q(producto__stock_minimo=0) | 
                    Q(producto__activo=False),
                    then=1
                ),
                default=0,
                output_field=IntegerField(),
            )
        ),
    )

    stock_critico = agg_stock["stock_critico"] or 0
    stock_bajo = agg_stock["stock_bajo"] or 0
    stock_saludable = agg_stock["stock_saludable"] or 0

    labels_dias = []
    data_movimientos = []

    start_day = hoy - timedelta(days=6)

    qs = (
        MovimientoInventario.objects.filter(
            sede=sede_actual,
            creado_en__date__gte=start_day,
            creado_en__date__lte=hoy,
        )
        .annotate(dia=TruncDate("creado_en"))
        .values("dia")
        .annotate(cnt=Count("id"))
    )

    map_cnt = {row["dia"]: row["cnt"] for row in qs}

    nombres_dias = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]

    for i in range(6, -1, -1):
        dia = hoy - timedelta(days=i)
        labels_dias.append(nombres_dias[dia.weekday()])
        data_movimientos.append(int(map_cnt.get(dia, 0)))

    # ==========================================
    # 10. RENDERIZADO FINAL
    # ==========================================
    context = {
        "profile": profile,
        "sede": sede,

        # KPIs
        "kpi_pendientes": total_pendientes_kpi,
        "kpi_movimientos": movimientos_hoy,
        "kpi_stock": count_stock_bajo,

        # Listas de datos
        "reqs_pendientes": reqs_pendientes_list,
        "items_bajos": items_stock_bajo,
        "sedes_central": sedes_central,
        "proveedores": proveedores,
        "transferencias_entrantes": transferencias_entrantes,

        # Notificaciones
        "notificacion_proyectos": count_proyectos_activos,
        "firmas_pendientes": len(documentos_del_almacenero(request.user)[0]),

        # Datos para gráficos
        "chart_stock_data": [stock_saludable, stock_bajo, stock_critico],
        "chart_mov_labels": labels_dias,
        "chart_mov_data": data_movimientos,
    }

    return render(request, "inventario/dash_almacen.html", context)


# --------------------
# HISTORIAL GLOBAL ALMACÉN
# --------------------
@login_required
def almacen_historial_global(request):
    try:
        profile = request.user.profile
        sede = profile.get_sede_operativa()
    except Exception:
        return redirect("home")

    # 1. Liquidaciones de Técnicos
    liquidaciones = DocumentoInventario.objects.filter(
        sede=sede,
        tipo=TipoDocumento.ING,
        referencia__icontains="LIQ-SEMANAL",
    ).select_related("responsable", "solicitante")

    for l in liquidaciones:
        l.tipo_movimiento = "LIQ_TECNICO"
        if l.solicitante:
            l.tecnico_nombre = l.solicitante.get_full_name() or l.solicitante.username
        else:
            l.tecnico_nombre = "Desconocido"

    # 2. Compras a Proveedores
    compras = (
        DocumentoInventario.objects.filter(
            sede=sede,
            tipo=TipoDocumento.ING,
        )
        .exclude(referencia__icontains="LIQ-SEMANAL")
        .exclude(sede_origen__isnull=False)
        .select_related("responsable", "proveedor", "entregado_por", "solicitante")
    )

    for c in compras:
        referencia = (c.referencia or "").upper()

        if "DEVOLUCION" in referencia:
            c.tipo_movimiento = "DEVOLUCION"

            if c.entregado_por:
                c.tecnico_nombre = c.entregado_por.get_full_name() or c.entregado_por.username
            elif c.solicitante:
                c.tecnico_nombre = c.solicitante.get_full_name() or c.solicitante.username
            else:
                c.tecnico_nombre = "Técnico (Sin datos)"

        elif referencia.startswith("RETORNO OBRA-") or referencia.startswith("RETORNO DE OBRA-"):
            c.tipo_movimiento = "RETORNO_OBRA"

            if c.entregado_por:
                c.tecnico_nombre = c.entregado_por.get_full_name() or c.entregado_por.username
            elif c.solicitante:
                c.tecnico_nombre = c.solicitante.get_full_name() or c.solicitante.username
            elif c.responsable:
                c.tecnico_nombre = c.responsable.get_full_name() or c.responsable.username
            else:
                c.tecnico_nombre = "Responsable PEX"

        else:
            c.tipo_movimiento = "COMPRA"

            if c.proveedor:
                c.tecnico_nombre = c.proveedor.razon_social
            elif c.proveedor_manual:
                c.tecnico_nombre = c.proveedor_manual
            else:
                c.tecnico_nombre = "Proveedor Externo"

    # 3. Proyectos Cerrados
    proyectos = Proyecto.objects.filter(
        sede=sede,
        estado=EstadoProyecto.FINALIZADO,
    ).select_related("responsable")

    for p in proyectos:
        p.tipo_movimiento = "CIERRE_OBRA"
        p.fecha = p.fin or p.actualizado_en

        if p.responsable:
            p.tecnico_nombre = p.responsable.get_full_name() or p.responsable.username
        else:
            p.tecnico_nombre = "Sin Asignar"

    # 4. Salidas por obras PEX
    salidas_obras = DocumentoInventario.objects.filter(
        sede=sede,
        tipo=TipoDocumento.SAL,
        estado=EstadoDocumento.CONFIRMADO,
        referencia__startswith="OBRA-",
    ).select_related("responsable", "solicitante")

    for s in salidas_obras:
        s.tipo_movimiento = "SALIDA_OBRA"

        if s.solicitante:
            s.tecnico_nombre = s.solicitante.get_full_name() or s.solicitante.username
        else:
            s.tecnico_nombre = "Responsable PEX"

    # 4.5. Salidas Directas a Técnicos
    salidas_tecnicos = (
        DocumentoInventario.objects.filter(
            sede=sede,
            tipo=TipoDocumento.SAL,
            estado=EstadoDocumento.CONFIRMADO,
            solicitante__isnull=False,
        )
        .exclude(referencia__startswith="OBRA-")
        .select_related("responsable", "solicitante")
    )

    for s in salidas_tecnicos:
        s.tipo_movimiento = "SALIDA_TECNICO"
        s.tecnico_nombre = s.solicitante.get_full_name() or s.solicitante.username

    # 5. Transferencias Reales Entre Sedes
    transferencias = (
        DocumentoInventario.objects.filter(
            sede=sede,
            tipo=TipoDocumento.SAL,
            sede_destino__isnull=False,
            solicitante__isnull=True,
            estado=EstadoDocumento.CONFIRMADO,
        )
        .exclude(referencia__startswith="OBRA-")
        .select_related("responsable", "sede_destino")
    )

    for t in transferencias:
        t.tipo_movimiento = "TRANSFERENCIA"
        t.tecnico_nombre = f"Destino: {t.sede_destino.nombre}"

    # 6. Solicitudes anuladas
    reqs_anulados = (
        DocumentoInventario.objects.filter(
            tipo=TipoDocumento.REQ,
            estado=EstadoDocumento.ANULADO,
        )
        .filter(
            Q(sede=sede) | Q(sede_destino=sede)
        )
        .select_related("responsable", "solicitante", "sede", "sede_destino")
    )

    for r in reqs_anulados:
        r.tipo_movimiento = "REQ_ANULADO"

        if r.responsable:
            r.tecnico_nombre = r.responsable.get_full_name() or r.responsable.username
        elif r.solicitante:
            r.tecnico_nombre = r.solicitante.get_full_name() or r.solicitante.username
        else:
            r.tecnico_nombre = "Sin responsable"

    # 7. Unificar todo
    historial = sorted(
        chain(
            liquidaciones,
            compras,
            proyectos,
            salidas_obras,
            salidas_tecnicos,
            transferencias,
            reqs_anulados,
        ),
        key=attrgetter("fecha"),
        reverse=True,
    )

    # Filtro por firma: ?firma=pendientes (falta alguna firma o cambió tras firmarse) o ?firma=firmados.
    firma = request.GET.get("firma", "todos")
    if firma not in FILTROS_FIRMA:
        firma = "todos"
    if firma != "todos":
        # Solo se revisan los documentos desde el despliegue de las firmas: los anteriores no se firman.
        desde = inicio_firmas()
        filtrado = []
        for item in historial:
            if item.tipo_movimiento not in TIPOS_CON_FIRMA or (desde and item.fecha < desde):
                continue
            item.estado_firma = _estado_firma_historial(item)
            if item.estado_firma in FILTROS_FIRMA[firma]:
                filtrado.append(item)
        historial = filtrado

    paginator = Paginator(historial, 10)
    page_obj = paginator.get_page(request.GET.get("page"))

    # Sin filtro, el estado de firmas se calcula solo para esta página, no para todo el historial.
    for item in page_obj:
        if not hasattr(item, "estado_firma"):
            item.estado_firma = _estado_firma_historial(item)

    return render(
        request,
        "inventario/almacen_historial_global.html",
        {
            "historial": page_obj,
            "page_range": paginator.get_elided_page_range(
                page_obj.number, on_each_side=2, on_ends=1
            ),
            "sede": sede,
            "firma": firma,
            # Para no perder el filtro al cambiar de página.
            "qs_firma": f"&firma={firma}" if firma != "todos" else "",
        },
    )


# Filtro de firma del historial: qué estados entran en cada opción.
FILTROS_FIRMA = {
    "todos": None,
    "pendientes": ("PENDIENTE", "PARCIAL", "CAMBIADO"),
    "firmados": ("FIRMADO",),
}
TIPOS_CON_FIRMA = (
    "SALIDA_TECNICO", "SALIDA_OBRA", "TRANSFERENCIA", "DEVOLUCION", "LIQ_TECNICO", "CIERRE_OBRA", "COMPRA",
)


def _estado_firma_historial(item):
    """Estado de firmas de una fila del historial ("NO_APLICA" si ese movimiento no se firma)."""
    tipo = item.tipo_movimiento
    if tipo == "CIERRE_OBRA":
        return acta_estado(item)
    if tipo == "COMPRA":
        # La que se firma es la orden de compra (el REQ de origen), no el ingreso.
        return estado_documento(item.origen) if item.origen_id else "NO_APLICA"
    if tipo in TIPOS_CON_FIRMA:
        return estado_documento(item)
    return "NO_APLICA"


# --------------------
# INFORME DE MOVIMIENTOS (KARDEX) POR RANGO DE MESES
# --------------------
KARDEX_MAX_MESES = 24
NOMBRES_MES = [
    "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
]


def _parse_mes(valor):
    """'YYYY-MM' -> (anio, mes) o None si no es válido."""
    try:
        anio, mes = (int(x) for x in (valor or "").split("-"))
    except (ValueError, TypeError):
        return None
    if 1 <= mes <= 12 and 2000 <= anio <= 2100:
        return anio, mes
    return None


def _mes_siguiente(anio, mes):
    return (anio + 1, 1) if mes == 12 else (anio, mes + 1)


def _inicio_de_mes(anio, mes):
    return timezone.make_aware(datetime(anio, mes, 1))


def _etiqueta_mes(anio, mes, corta=False):
    nombre = NOMBRES_MES[mes - 1]
    return f"{nombre[:3]} {anio}" if corta else f"{nombre} {anio}"


def _periodo_desde_request(request):
    """
    Resuelve el periodo del informe a partir de ?desde=YYYY-MM&hasta=YYYY-MM.
    Sigue aceptando el formato anterior ?mes=YYYY-MM (desde = hasta = mes).
    Sin parámetros: mes actual.
    """
    hoy = timezone.localdate()
    actual = (hoy.year, hoy.month)
    avisos = []

    mes_anterior = _parse_mes(request.GET.get("mes"))
    desde = _parse_mes(request.GET.get("desde")) or mes_anterior or actual
    hasta = _parse_mes(request.GET.get("hasta")) or mes_anterior or desde

    if hasta < desde:
        desde, hasta = hasta, desde
    if hasta > actual:
        hasta = actual
    if desde > actual:
        desde = actual

    meses = [desde]
    while meses[-1] < hasta:
        meses.append(_mes_siguiente(*meses[-1]))

    if len(meses) > KARDEX_MAX_MESES:
        meses = meses[-KARDEX_MAX_MESES:]
        desde = meses[0]
        avisos.append(
            f"El rango máximo es de {KARDEX_MAX_MESES} meses. "
            f"Se muestra desde {_etiqueta_mes(*desde)}."
        )

    if desde == hasta:
        etiqueta = _etiqueta_mes(*desde)
    else:
        etiqueta = f"{_etiqueta_mes(*desde)} – {_etiqueta_mes(*hasta)}"

    return {
        "desde": desde,
        "hasta": hasta,
        "meses": meses,
        "inicio": _inicio_de_mes(*desde),
        "fin": _inicio_de_mes(*_mes_siguiente(*hasta)),
        "desde_str": f"{desde[0]:04d}-{desde[1]:02d}",
        "hasta_str": f"{hasta[0]:04d}-{hasta[1]:02d}",
        "actual_str": f"{actual[0]:04d}-{actual[1]:02d}",
        "incluye_mes_actual": hasta == actual,
        "es_un_mes": desde == hasta,
        "etiqueta": etiqueta,
        "avisos": avisos,
    }


def _delta_movimiento(tipo, qty):
    """Efecto del movimiento sobre el stock (los ADJ ya traen su signo)."""
    if tipo == MovimientoInventario.TIPO_OUT:
        return -qty
    return qty


def _leer_snapshot_kardex(sede, periodo, producto_id=None):
    """Lee cantidades y movimientos en UNA sentencia, sin bloqueos ni escrituras.

    UNION ALL mantiene una sola instantánea de lectura en SQLite y PostgreSQL,
    incluso con READ COMMITTED. Las sumas históricas se calculan una vez por
    producto; sólo se cargan los movimientos del periodo solicitado.
    """
    stock = Stock.objects.filter(sede=sede, producto_id=OuterRef("pk"))
    historial = MovimientoInventario.objects.filter(
        sede=sede, producto_id=OuterRef("pk")
    )

    def suma_documental(qs):
        return (
            qs.order_by().values("producto_id")
            .annotate(total=Sum(Case(
                When(tipo=MovimientoInventario.TIPO_OUT, then=-F("qty")),
                default=F("qty"), output_field=IntegerField(),
            )))
            .values("total")[:1]
        )

    productos = Producto.objects.all()
    movimientos = MovimientoInventario.objects.filter(
        sede=sede, creado_en__gte=periodo["inicio"], creado_en__lt=periodo["fin"]
    )
    if producto_id is not None:
        productos = productos.filter(pk=producto_id)
        movimientos = movimientos.filter(producto_id=producto_id)
    else:
        productos = productos.annotate(
            kx_tiene_stock=Exists(stock),
            kx_tiene_historia=Exists(historial.filter(creado_en__lt=periodo["fin"])),
        ).filter(Q(kx_tiene_stock=True) | Q(kx_tiene_historia=True))

    # Todas las columnas son anotaciones en el mismo orden y con tipos
    # compatibles: Django 4.2 reordena campos directos frente a anotaciones.
    campos = {
        "kx_clase": Value("producto", output_field=CharField()),
        "kx_producto": F("pk"),
        "kx_nombre": F("nombre"),
        "kx_codigo": F("codigo_interno"),
        "kx_stock": Coalesce(Subquery(stock.values("cantidad")[:1]), 0),
        "kx_inicial": Coalesce(Subquery(suma_documental(
            historial.filter(creado_en__lt=periodo["inicio"])
        )), 0),
        "kx_documentado": Coalesce(Subquery(suma_documental(historial)), 0),
        "kx_movimiento": Value(0, output_field=IntegerField()),
        "kx_fecha": Value(None, output_field=DateTimeField()),
        "kx_tipo": Value("", output_field=CharField()),
        "kx_qty": Value(0, output_field=IntegerField()),
        "kx_guardado": Value(0, output_field=IntegerField()),
        "kx_referencia": Value("", output_field=CharField()),
        "kx_nota": Value("", output_field=CharField()),
        "kx_usuario": Value("", output_field=CharField()),
        "kx_nombres": Value("", output_field=CharField()),
        "kx_apellidos": Value("", output_field=CharField()),
    }
    campos_mov = {
        **campos,
        "kx_clase": Value("movimiento", output_field=CharField()),
        "kx_producto": F("producto_id"),
        "kx_nombre": Value("", output_field=CharField()),
        "kx_codigo": Value("", output_field=CharField()),
        "kx_stock": Value(0, output_field=IntegerField()),
        "kx_inicial": Value(0, output_field=IntegerField()),
        "kx_documentado": Value(0, output_field=IntegerField()),
        "kx_movimiento": F("pk"),
        "kx_fecha": F("creado_en"),
        "kx_tipo": F("tipo"),
        "kx_qty": F("qty"),
        "kx_guardado": F("saldo_resultante"),
        "kx_referencia": F("referencia"),
        "kx_nota": F("nota"),
        "kx_usuario": Coalesce(F("usuario__username"), Value("")),
        "kx_nombres": Coalesce(F("usuario__first_name"), Value("")),
        "kx_apellidos": Coalesce(F("usuario__last_name"), Value("")),
    }
    cabeceras = productos.order_by().annotate(**campos).values_list(*campos)
    filas_mov = movimientos.order_by().annotate(**campos_mov).values_list(*campos)
    filas = list(cabeceras.union(filas_mov, all=True))

    snapshot = {}
    pendientes = []
    for fila in filas:
        (clase, pid, nombre, codigo, cantidad, inicial, documentado,
         mid, fecha, tipo, qty, guardado, referencia, nota,
         usuario, nombres, apellidos) = fila
        if clase == "producto":
            snapshot[pid] = {
                "producto": Producto(id=pid, nombre=nombre, codigo_interno=codigo),
                "stock_actual": cantidad,
                "saldo_inicial": inicial,
                "saldo_documentado_actual": documentado,
                "movimientos": [],
            }
        else:
            pendientes.append((pid, {
                "id": mid, "creado_en": fecha, "tipo": tipo, "qty": qty,
                "saldo_resultante": guardado, "referencia": referencia, "nota": nota,
                "usuario_nombre": f"{nombres} {apellidos}".strip() or usuario,
            }))
    # UNION no garantiza el orden de las ramas ni el de sus filas.
    for pid, movimiento in pendientes:
        snapshot[pid]["movimientos"].append(movimiento)
    for datos in snapshot.values():
        datos["movimientos"].sort(key=lambda m: (m["creado_en"], m["id"]))
    return snapshot


def _kardex_por_mes(snapshot, periodo, con_detalle=False):
    """Calcula saldos documentales; nunca inventa ajustes ni fechas.

    La diferencia actual se compara con TODO el historial, no con el cierre
    de un periodo antiguo. No se suma a los movimientos de ningún mes.
    """
    resultado = {}
    for pid, fuente in snapshot.items():
        saldo_corriente = fuente["saldo_inicial"]
        datos = {
            "saldo_inicial": saldo_corriente,
            "diferencia_actual": fuente["stock_actual"] - fuente["saldo_documentado_actual"],
            "tiene_diferencias_historicas": False,
            "meses": {
                mes: {
                    "anio": mes[0],
                    "mes": mes[1],
                    "clave": f"{mes[0]:04d}-{mes[1]:02d}",
                    "etiqueta": _etiqueta_mes(*mes),
                    "etiqueta_corta": _etiqueta_mes(*mes, corta=True),
                    "entradas": 0,
                    "salidas": 0,
                    "ajustes": 0,
                    "num_movimientos": 0,
                    "movimientos": [],
                }
                for mes in periodo["meses"]
            },
        }

        for mov in fuente["movimientos"]:
            local = timezone.localtime(mov["creado_en"])
            fila = datos["meses"][(local.year, local.month)]
            if mov["tipo"] == MovimientoInventario.TIPO_IN:
                fila["entradas"] += mov["qty"]
            elif mov["tipo"] == MovimientoInventario.TIPO_OUT:
                fila["salidas"] += mov["qty"]
            elif mov["tipo"] == MovimientoInventario.TIPO_ADJ:
                fila["ajustes"] += mov["qty"]
            fila["num_movimientos"] += 1
            saldo_corriente += _delta_movimiento(mov["tipo"], mov["qty"])
            difiere = mov["saldo_resultante"] != saldo_corriente
            datos["tiene_diferencias_historicas"] |= difiere
            if con_detalle:
                fila["movimientos"].append({
                    **mov, "saldo_documentado": saldo_corriente, "saldo_difiere": difiere,
                })

        saldo = datos["saldo_inicial"]
        meses = []
        for fila in datos["meses"].values():
            fila["saldo_inicial"] = saldo
            saldo = saldo + fila["entradas"] - fila["salidas"] + fila["ajustes"]
            fila["saldo_final"] = saldo
            meses.append(fila)

        datos["meses"] = meses
        datos["entradas"] = sum(f["entradas"] for f in meses)
        datos["salidas"] = sum(f["salidas"] for f in meses)
        datos["ajustes"] = sum(f["ajustes"] for f in meses)
        datos["num_movimientos"] = sum(f["num_movimientos"] for f in meses)
        datos["saldo_final"] = saldo
        resultado[pid] = datos

    return resultado


@login_required
def kardex_producto(request):
    """
    Informe de movimientos de inventario por rango de meses
    (?desde=YYYY-MM&hasta=YYYY-MM). Por defecto muestra una tabla con todos
    los productos de la sede (saldo inicial, entradas, salidas, ajustes,
    saldo final y stock de hoy); con ?vista=mensual cada producto se puede
    desplegar mes a mes. Con ?producto_id= muestra el detalle movimiento por
    movimiento de ese producto, agrupado por mes.
    """
    profile = _require_roles(
        request.user,
        UserProfile.Rol.ADMIN,
        UserProfile.Rol.JEFA,
        UserProfile.Rol.ALMACEN,
    )
    sede = _require_sede(profile)

    periodo = _periodo_desde_request(request)
    vista = "mensual" if request.GET.get("vista") == "mensual" else "total"

    primer_movimiento = (
        MovimientoInventario.objects.filter(sede=sede).aggregate(primero=Min("creado_en"))["primero"]
    )
    mes_minimo = timezone.localtime(primer_movimiento).strftime("%Y-%m") if primer_movimiento else ""

    producto = None
    producto_id = request.GET.get("producto_id")
    if producto_id:
        producto = Producto.objects.filter(id=producto_id).first()

    context = {
        "profile": profile,
        "sede": sede,
        "producto": producto,
        "productos": Producto.objects.order_by("nombre"),
        "periodo": periodo,
        "vista": vista,
        "mes_minimo": mes_minimo,
        "query_periodo": urlencode({
            "desde": periodo["desde_str"],
            "hasta": periodo["hasta_str"],
            "vista": vista,
        }),
        # Mismo periodo, pero forzando la vista "Por mes" (botón del detalle totalizado).
        "query_periodo_mensual": urlencode({
            "desde": periodo["desde_str"],
            "hasta": periodo["hasta_str"],
            "vista": "mensual",
        }),
    }

    snapshot = _leer_snapshot_kardex(sede, periodo, producto.id if producto else None)
    kardex = _kardex_por_mes(snapshot, periodo, con_detalle=bool(producto))

    if producto:
        # ---- Vista detalle: un producto, movimiento por movimiento ----
        context.update({
            "detalle": kardex[producto.id],
            "stock_actual": snapshot[producto.id]["stock_actual"],
        })
    else:
        # ---- Vista resumen: todos los productos de la sede en el periodo ----
        resumen = []
        for fuente in sorted(snapshot.values(), key=lambda f: (f["producto"].nombre, f["producto"].id)):
            p = fuente["producto"]
            datos = kardex[p.id]
            resumen.append({
                "producto": p,
                "saldo_inicial": datos["saldo_inicial"],
                "entradas": datos["entradas"],
                "salidas": datos["salidas"],
                "ajustes": datos["ajustes"],
                "saldo_final_periodo": datos["saldo_final"],
                "stock_actual": fuente["stock_actual"],
                "diferencia_actual": datos["diferencia_actual"],
                "tiene_diferencias_historicas": datos["tiene_diferencias_historicas"],
                "num_movimientos": datos["num_movimientos"],
                "meses": datos["meses"],
            })

        totales_por_mes = []
        for i, mes in enumerate(periodo["meses"]):
            totales_por_mes.append({
                "etiqueta": _etiqueta_mes(*mes),
                "etiqueta_corta": _etiqueta_mes(*mes, corta=True),
                "entradas": sum(f["meses"][i]["entradas"] for f in resumen),
                "salidas": sum(f["meses"][i]["salidas"] for f in resumen),
                "ajustes": sum(f["meses"][i]["ajustes"] for f in resumen),
                # Productos con al menos un movimiento ese mes.
                "productos": sum(1 for f in resumen if f["meses"][i]["num_movimientos"]),
            })

        context.update({
            "resumen": resumen,
            "totales_por_mes": totales_por_mes,
            "total_productos": len(resumen),
            "productos_con_movimiento": sum(1 for f in resumen if f["num_movimientos"]),
            "total_entradas_periodo": sum(f["entradas"] for f in resumen),
            "total_salidas_periodo": sum(f["salidas"] for f in resumen),
            "total_ajustes_periodo": sum(f["ajustes"] for f in resumen),
            "productos_con_diferencias": sum(
                1 for f in resumen if f["diferencia_actual"] or f["tiene_diferencias_historicas"]
            ),
        })

    return render(request, "inventario/kardex_producto.html", context)
