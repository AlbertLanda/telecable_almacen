from __future__ import annotations

from datetime import datetime, timedelta
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
)
from django.db.models.functions import TruncDate
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
    # 6. KPI: PROYECTOS ACTIVOS
    # ==========================================
    count_proyectos_activos = Proyecto.objects.filter(
        sede=sede,
        estado=EstadoProyecto.APROBADO
    ).count()

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

    paginator = Paginator(historial, 10)
    page_obj = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "inventario/almacen_historial_global.html",
        {
            "historial": page_obj,
            "sede": sede,
        },
    )


def _mes_desde_request(request):
    """Resuelve (anio, mes, inicio_mes, fin_mes, 'YYYY-MM') a partir de ?mes=."""
    hoy = timezone.localdate()
    anio, mes = hoy.year, hoy.month

    mes_param = request.GET.get("mes")  # formato YYYY-MM
    if mes_param:
        try:
            anio_param, mes_param_num = (int(x) for x in mes_param.split("-"))
            if 1 <= mes_param_num <= 12:
                anio, mes = anio_param, mes_param_num
        except (ValueError, TypeError):
            pass

    inicio_mes = timezone.make_aware(datetime(anio, mes, 1))
    if mes == 12:
        fin_mes = timezone.make_aware(datetime(anio + 1, 1, 1))
    else:
        fin_mes = timezone.make_aware(datetime(anio, mes + 1, 1))

    return anio, mes, inicio_mes, fin_mes, f"{anio:04d}-{mes:02d}"


# --------------------
# KARDEX DE PRODUCTO (ALMACEN/ADMIN/JEFA)
# --------------------
@login_required
def kardex_producto(request):
    """
    Informe mensual de movimientos de inventario. Por defecto muestra una
    tabla con TODOS los productos de la sede en el mes elegido (saldo
    inicial, entradas, salidas, saldo actual), para que almacén vea de un
    vistazo qué se movió sin tener que ir producto por producto. Si se
    pasa ?producto_id=, muestra el detalle movimiento por movimiento de
    ese producto ("tenías 30 clavos, le diste 5 a Juan, hoy tienes 25").
    """
    profile = _require_roles(
        request.user,
        UserProfile.Rol.ADMIN,
        UserProfile.Rol.JEFA,
        UserProfile.Rol.ALMACEN,
    )
    sede = _require_sede(profile)

    anio, mes, inicio_mes, fin_mes, mes_actual = _mes_desde_request(request)

    producto = None
    producto_id = request.GET.get("producto_id")
    if producto_id:
        producto = Producto.objects.filter(id=producto_id).first()

    context = {
        "profile": profile,
        "sede": sede,
        "producto": producto,
        "productos": Producto.objects.order_by("nombre"),
        "mes_actual": mes_actual,
    }

    if producto:
        # ---- Vista detalle: un producto, movimiento por movimiento ----
        movimiento_previo = (
            MovimientoInventario.objects
            .filter(producto=producto, sede=sede, creado_en__lt=inicio_mes)
            .order_by("-creado_en")
            .first()
        )
        saldo_inicial = movimiento_previo.saldo_resultante if movimiento_previo else 0

        movimientos = list(
            MovimientoInventario.objects
            .filter(
                producto=producto,
                sede=sede,
                creado_en__gte=inicio_mes,
                creado_en__lt=fin_mes,
            )
            .select_related("usuario")
            .order_by("creado_en")
        )

        saldo_final_periodo = movimientos[-1].saldo_resultante if movimientos else saldo_inicial

        stock_actual_obj = Stock.objects.filter(producto=producto, sede=sede).first()
        stock_actual = stock_actual_obj.cantidad if stock_actual_obj else 0

        total_entradas = sum(m.qty for m in movimientos if m.tipo == MovimientoInventario.TIPO_IN)
        total_salidas = sum(m.qty for m in movimientos if m.tipo == MovimientoInventario.TIPO_OUT)

        context.update({
            "saldo_inicial": saldo_inicial,
            "movimientos": movimientos,
            "saldo_final_periodo": saldo_final_periodo,
            "stock_actual": stock_actual,
            "total_entradas": total_entradas,
            "total_salidas": total_salidas,
        })
    else:
        # ---- Vista resumen: todos los productos de la sede en el mes ----
        stocks_sede = (
            Stock.objects.filter(sede=sede)
            .select_related("producto")
            .order_by("producto__nombre")
        )
        producto_ids = [s.producto_id for s in stocks_sede]

        movs_mes = (
            MovimientoInventario.objects
            .filter(sede=sede, producto_id__in=producto_ids, creado_en__gte=inicio_mes, creado_en__lt=fin_mes)
            .values("producto_id")
            .annotate(
                entradas=Sum(Case(
                    When(tipo=MovimientoInventario.TIPO_IN, then=F("qty")),
                    default=0, output_field=IntegerField(),
                )),
                salidas=Sum(Case(
                    When(tipo=MovimientoInventario.TIPO_OUT, then=F("qty")),
                    default=0, output_field=IntegerField(),
                )),
                ajustes=Sum(Case(
                    When(tipo=MovimientoInventario.TIPO_ADJ, then=F("qty")),
                    default=0, output_field=IntegerField(),
                )),
                num_movimientos=Count("id"),
            )
        )
        movs_por_producto = {m["producto_id"]: m for m in movs_mes}

        # Saldo inicial = saldo_resultante del último movimiento ANTES del mes.
        saldos_iniciales = {}
        vistos = set()
        previos = (
            MovimientoInventario.objects
            .filter(sede=sede, producto_id__in=producto_ids, creado_en__lt=inicio_mes)
            .order_by("producto_id", "-creado_en")
            .values("producto_id", "saldo_resultante")
        )
        for m in previos:
            pid = m["producto_id"]
            if pid not in vistos:
                saldos_iniciales[pid] = m["saldo_resultante"]
                vistos.add(pid)

        resumen = []
        for s in stocks_sede:
            pid = s.producto_id
            agg = movs_por_producto.get(pid, {})
            entradas = agg.get("entradas") or 0
            salidas = agg.get("salidas") or 0
            ajustes = agg.get("ajustes") or 0
            num_movimientos = agg.get("num_movimientos") or 0
            saldo_inicial = saldos_iniciales.get(pid, 0)
            saldo_final_periodo = saldo_inicial + entradas - salidas + ajustes

            resumen.append({
                "producto": s.producto,
                "saldo_inicial": saldo_inicial,
                "entradas": entradas,
                "salidas": salidas,
                "ajustes": ajustes,
                "saldo_final_periodo": saldo_final_periodo,
                "stock_actual": s.cantidad,
                "num_movimientos": num_movimientos,
            })

        context["resumen"] = resumen

    return render(request, "inventario/kardex_producto.html", context)