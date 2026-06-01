from __future__ import annotations
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import F, Sum, Q
from django.shortcuts import redirect, render
from django.utils import timezone
from datetime import timedelta
from django.db.models import Count
from django.db.models.functions import TruncDate
from ..models import Stock, MovimientoInventario
from django.db.models import Case, When, IntegerField
from django.db.models.functions import TruncDate
from django.db.models import Count
from itertools import chain
from operator import attrgetter

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

    stocks_criticos = (
        Stock.objects.filter(sede=sede, producto__activo=True)
        .filter(
            Q(producto__stock_minimo__gt=0, cantidad__lte=F("producto__stock_minimo"))
            | Q(producto__stock_minimo=0, cantidad__lte=5)
        )
        .select_related("producto", "sede")
        .order_by("cantidad", "producto__nombre")[:10]
    )

    alertas_stock = [
        {
            "producto": s.producto.nombre,
            "codigo": s.producto.codigo_interno or "",
            "cantidad": int(s.cantidad or 0),
            "stock_minimo": int(s.producto.stock_minimo or 0),
            "sede": s.sede.nombre,
            "unidad": getattr(s.producto, "unidad", "") or "UND",
        }
        for s in stocks_criticos
    ]

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
    # ==========================================
    # 1. SEGURIDAD Y CONTEXTO
    # ==========================================
    try:
        profile = request.user.profile
    except UserProfile.DoesNotExist:
        messages.error(request, "Tu usuario no tiene un perfil configurado.")
        return redirect('home') # O a donde prefieras

    # ✅ CORRECCIÓN CLAVE: Usamos el método del modelo
    sede = profile.get_sede_operativa()

    if not sede:
        # Si el usuario es nuevo y no tiene sede, evitamos el error 500
        messages.error(request, "Tu usuario no tiene ninguna sede asignada.")
        return redirect('home') 

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
    reqs_pendientes_list = DocumentoInventario.objects.filter(
        tipo=TipoDocumento.REQ,
        estado=EstadoDocumento.REQ_PENDIENTE
    ).filter(
        Q(sede=sede, tipo_requerimiento=TipoRequerimiento.LOCAL) |  # A) Mis técnicos
        Q(sede_destino=sede, tipo_requerimiento=TipoRequerimiento.ENTRE_SEDES) | # B) Me piden a mí
        Q(sede=sede, tipo_requerimiento=TipoRequerimiento.PROVEEDOR) # C) Compras mías
    ).select_related("responsable", "sede").order_by("fecha")

    # ==========================================
    # 4. KPI: STOCK BAJO
    # ==========================================
    query_stock_bajo = Stock.objects.filter(sede=sede, producto__activo=True).filter(
        Q(producto__stock_minimo__gt=0, cantidad__lte=F("producto__stock_minimo"))
        | Q(producto__stock_minimo=0, cantidad__lte=5)
    ).select_related('producto')

    count_stock_bajo = query_stock_bajo.count()
    items_stock_bajo = query_stock_bajo[:5] 

    # ==========================================
    # 5. KPI: MOVIMIENTOS HOY
    # ==========================================
    movimientos_hoy = DocumentoInventario.objects.filter(
        sede=sede,
        estado=EstadoDocumento.CONFIRMADO,
        fecha__date=hoy
    ).count()

    # ==========================================
    # 6. KPI: PROYECTOS ACTIVOS
    # ==========================================
    count_proyectos_activos = Proyecto.objects.filter(
        sede=sede
    ).exclude(
        estado__in=[EstadoProyecto.FINALIZADO, EstadoProyecto.ANULADO]
    ).count()

    # ==========================================
    # 7. KPI TOTAL
    # ==========================================
    total_pendientes_kpi = reqs_pendientes_list.count() + count_proyectos_activos

    # ==========================================
    # 8. TRANSFERENCIAS ENTRANTES
    # ==========================================
    transferencias_entrantes = DocumentoInventario.objects.filter(
        tipo=TipoDocumento.SAL,         
        sede_destino=sede,              
        estado=EstadoDocumento.CONFIRMADO, 
        recibido=False                  
    ).exclude(
        sede=sede
    ).select_related('sede', 'responsable')

    # ==========================================
    # 📊 9. LÓGICA PARA GRÁFICOS (CHART.JS)
    # ==========================================

    # --- A) GRÁFICO DE DONA (Salud del Stock) ---
    CRITICO = 5
    BAJO = 15

    agg_stock = Stock.objects.filter(sede=sede_actual).aggregate(
        stock_critico=Sum(
            Case(When(cantidad__lte=CRITICO, then=1), default=0, output_field=IntegerField())
        ),
        stock_bajo=Sum(
            Case(When(cantidad__gt=CRITICO, cantidad__lte=BAJO, then=1), default=0, output_field=IntegerField())
        ),
        stock_saludable=Sum(
            Case(When(cantidad__gt=BAJO, then=1), default=0, output_field=IntegerField())
        ),
    )

    stock_critico = agg_stock["stock_critico"] or 0
    stock_bajo = agg_stock["stock_bajo"] or 0
    stock_saludable = agg_stock["stock_saludable"] or 0

    # --- B) GRÁFICO DE BARRAS (Actividad últimos 7 días) ---
    labels_dias = []
    data_movimientos = []

    start_day = hoy - timedelta(days=6)

    qs = (
        MovimientoInventario.objects
        .filter(sede=sede_actual, creado_en__date__gte=start_day, creado_en__date__lte=hoy)
        .annotate(dia=TruncDate("creado_en"))
        .values("dia")
        .annotate(cnt=Count("id"))
    )

    # Convertimos a dict para rellenar días sin movimientos con 0
    map_cnt = {row["dia"]: row["cnt"] for row in qs}

    # 🔥 FIX: Arreglo manual de nombres de días en español
    nombres_dias = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]

    for i in range(6, -1, -1):
        dia = hoy - timedelta(days=i)
        labels_dias.append(nombres_dias[dia.weekday()]) # Aquí mapeamos al español
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
        
        # DATOS PARA LOS GRÁFICOS 📊
        'chart_stock_data': [stock_saludable, stock_bajo, stock_critico],
        'chart_mov_labels': labels_dias,
        'chart_mov_data': data_movimientos,
    }

    return render(request, "inventario/dash_almacen.html", context)


# --------------------
# DASH SOLICITANTE (SOLICITANTE/JEFA)
# --------------------
@login_required
def almacen_historial_global(request):
    try:
        profile = request.user.profile
        sede = profile.get_sede_operativa()
    except:
        return redirect('home')

    # 1. Liquidaciones de Técnicos (ING con referencia LIQ-SEMANAL)
    liquidaciones = DocumentoInventario.objects.filter(
        sede=sede,
        tipo=TipoDocumento.ING,
        referencia__icontains="LIQ-SEMANAL"
    ).select_related('responsable', 'solicitante')

    for l in liquidaciones:
        l.tipo_movimiento = 'LIQ_TECNICO'
        if l.solicitante:
            l.tecnico_nombre = l.solicitante.get_full_name() or l.solicitante.username
        else:
            l.tecnico_nombre = "Desconocido"

    # 2. Compras a Proveedores (ING que NO son Liquidaciones NI Traslados)
    compras = DocumentoInventario.objects.filter(
        sede=sede,
        tipo=TipoDocumento.ING
    ).exclude(
        referencia__icontains="LIQ-SEMANAL" # Excluir liquidaciones (esas van aparte)
    ).exclude(
        sede_origen__isnull=False # Excluir traslados desde otra sede
    ).select_related('responsable', 'proveedor', 'entregado_por') # <--- AGREGAMOS entregado_por

    for c in compras:
        # 🆕 LÓGICA DE CLASIFICACIÓN
        if "DEVOLUCION" in (c.referencia or "").upper():
            c.tipo_movimiento = 'DEVOLUCION'
            if c.entregado_por:
                c.tecnico_nombre = c.entregado_por.get_full_name() or c.entregado_por.username
            else:
                c.tecnico_nombre = "Técnico (Sin datos)"
        else:
            c.tipo_movimiento = 'COMPRA'
            # Mostrar nombre del proveedor en la columna "Técnico/Responsable"
            if c.proveedor:
                c.tecnico_nombre = c.proveedor.razon_social
            elif c.proveedor_manual:
                c.tecnico_nombre = c.proveedor_manual
            else:
                c.tecnico_nombre = "Proveedor Externo"

    # 3. Proyectos Cerrados
    proyectos = Proyecto.objects.filter(
        sede=sede,
        estado=EstadoProyecto.FINALIZADO
    ).select_related('responsable')

    for p in proyectos:
        p.tipo_movimiento = 'CIERRE_OBRA'
        p.fecha = p.fin or p.actualizado_en
        if p.responsable:
            p.tecnico_nombre = p.responsable.get_full_name() or p.responsable.username
        else:
            p.tecnico_nombre = "Sin Asignar"

    # 4. Transferencias (Salidas a otras sedes)
    transferencias = DocumentoInventario.objects.filter(
        sede=sede,
        tipo=TipoDocumento.SAL,
        sede_destino__isnull=False,
        estado=EstadoDocumento.CONFIRMADO
    ).select_related('responsable', 'sede_destino')

    for t in transferencias:
        t.tipo_movimiento = 'TRANSFERENCIA'
        t.tecnico_nombre = f"Destino: {t.sede_destino.nombre}"

    # 5. Unificar todo
    historial = sorted(
        chain(liquidaciones, compras, proyectos, transferencias),
        key=attrgetter('fecha'),
        reverse=True
    )

    return render(request, 'inventario/almacen_historial_global.html', {
        'historial': historial,
        'sede': sede
    })