from datetime import date, timedelta

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q, Sum
from django.db.models.functions import TruncMonth
from django.core.exceptions import PermissionDenied
from django.shortcuts import render
from django.utils import timezone

from inventario.models import (
    MovimientoInventario,
    Producto,
    UserProfile,
)

# Opciones del selector "Filas". Se valida contra esta lista: un ?filas=99999
# en la URL no debe poder pedir el periodo entero en una sola página.
FILAS_OPCIONES = (10, 20, 50)
FILAS_DEFECTO = 10

ROLES_REPORTES = (
    UserProfile.Rol.ALMACEN,
    UserProfile.Rol.JEFA,
    UserProfile.Rol.ADMIN,
)


def _require_roles(user, *roles):
    profile = getattr(user, "profile", None)
    if not profile:
        raise PermissionDenied("Usuario sin perfil (UserProfile).")
    if profile.rol not in roles:
        raise PermissionDenied("No tienes permisos para esta acción.")
    return profile


def _rango_mes_actual():
    hoy = timezone.localdate()
    desde = hoy.replace(day=1)
    return desde, hoy


def _mes_anterior(primero_de_mes):
    """Primer día del mes anterior a uno dado."""
    if primero_de_mes.month == 1:
        return primero_de_mes.replace(year=primero_de_mes.year - 1, month=12)
    return primero_de_mes.replace(month=primero_de_mes.month - 1)


def _mes_siguiente(primero_de_mes):
    if primero_de_mes.month == 12:
        return primero_de_mes.replace(year=primero_de_mes.year + 1, month=1)
    return primero_de_mes.replace(month=primero_de_mes.month + 1)


def _totales_del_mes(qs, primero_de_mes):
    """Conteo y sumas por tipo de un mes completo."""
    del_mes = qs.filter(
        creado_en__date__gte=primero_de_mes,
        creado_en__date__lt=_mes_siguiente(primero_de_mes),
    )
    sumas = del_mes.aggregate(
        entra=Sum("qty", filter=Q(tipo=MovimientoInventario.TIPO_IN)),
        sale=Sum("qty", filter=Q(tipo=MovimientoInventario.TIPO_OUT)),
        ajusta=Sum("qty", filter=Q(tipo=MovimientoInventario.TIPO_ADJ)),
    )
    return {
        "movs": del_mes.count(),
        "in": sumas["entra"] or 0,
        "out": sumas["sale"] or 0,
        "adj": sumas["ajusta"] or 0,
    }


def _variacion(actual, previo, *, bueno_si_sube=True):
    """
    Variación porcentual contra el mes anterior.

    Devuelve None cuando no hay con qué comparar: un "+100%" sobre un mes
    sin datos no informa nada y se leería como crecimiento real.
    """
    if not previo:
        return None
    pct = (actual - previo) * 100.0 / previo
    sube = pct > 0
    return {
        "pct": abs(round(pct, 1)),
        "sube": sube,
        "plano": abs(pct) < 0.05,
        "bueno": (sube == bueno_si_sube) if abs(pct) >= 0.05 else None,
    }


def _parse_fecha(valor, por_defecto):
    if not valor:
        return por_defecto
    try:
        return date.fromisoformat(valor)
    except ValueError:
        return por_defecto


@login_required
def reportes_home(request):
    """
    Portada de Reportes: las dos fuentes disponibles.
    """
    profile = _require_roles(request.user, *ROLES_REPORTES)

    return render(request, "reportes/home.html", {
        "profile": profile,
        "sede": profile.get_sede_operativa(),
        "sicv_habilitado": settings.SICV_SYNC_ENABLED,
        "sicv_configurado": bool(settings.SICV_URL and settings.SICV_TOKEN),
    })


@login_required
def reportes_sicv(request):
    """
    Órdenes liquidadas por los técnicos en campo, traídas de SICV.

    Mientras SICV_SYNC_ENABLED esté en False esta pantalla solo explica el
    estado de la conexión. Los tres vacíos del contrato (§8.3) se muestran
    distintos a propósito: integración apagada, sin movimientos, y SICV sin
    responder no significan lo mismo.
    """
    profile = _require_roles(request.user, *ROLES_REPORTES)
    desde, hasta = _rango_mes_actual()

    return render(request, "reportes/sicv.html", {
        "profile": profile,
        "sede": profile.get_sede_operativa(),
        "sicv_habilitado": settings.SICV_SYNC_ENABLED,
        "sicv_configurado": bool(settings.SICV_URL and settings.SICV_TOKEN),
        "sicv_url": settings.SICV_URL,
        "desde": desde,
        "hasta": hasta,
    })


@login_required
def reportes_kardex(request):
    """
    Kardex de materiales: cada movimiento con el saldo que dejó.

    saldo_resultante es el stock de producto+sede (inventario/models.py:301),
    así que el reporte se ve de a una sede: mezclarlas haría saltar la columna
    de saldo entre almacenes distintos y el número dejaría de poder auditarse.
    """
    profile = _require_roles(request.user, *ROLES_REPORTES)

    # La sede no se elige: es la del usuario. Un almacenero de Jauja lee el
    # kardex de Jauja y de ningún otro, y tampoco puede pedir otro por URL.
    sede_sel = profile.get_sede_operativa()
    productos = Producto.objects.order_by("nombre")

    # --- Filtros -------------------------------------------------
    desde_def, hasta_def = _rango_mes_actual()
    desde = _parse_fecha(request.GET.get("desde"), desde_def)
    hasta = _parse_fecha(request.GET.get("hasta"), hasta_def)

    if desde > hasta:
        desde, hasta = hasta, desde

    producto_id = request.GET.get("producto") or ""
    producto_sel = productos.filter(pk=producto_id).first() if producto_id else None

    tipo_sel = request.GET.get("tipo") or ""

    try:
        filas = int(request.GET.get("filas", FILAS_DEFECTO))
    except (TypeError, ValueError):
        filas = FILAS_DEFECTO
    if filas not in FILAS_OPCIONES:
        filas = FILAS_DEFECTO

    # --- Consulta ------------------------------------------------
    qs = MovimientoInventario.objects.none()
    if sede_sel:
        qs = (
            MovimientoInventario.objects
            .filter(sede=sede_sel, creado_en__date__gte=desde, creado_en__date__lte=hasta)
            .select_related("producto", "usuario", "ubicacion")
            .annotate(mes=TruncMonth("creado_en"))
            # Mes más reciente primero, pero dentro de cada mes los movimientos
            # de un producto van en orden cronológico: la columna de saldo solo
            # se puede leer hacia adelante, si se invirtiera dejaría de encadenar.
            .order_by("-mes", "producto__nombre", "creado_en", "id")
        )
        if producto_sel:
            qs = qs.filter(producto=producto_sel)
        if tipo_sel in (MovimientoInventario.TIPO_IN, MovimientoInventario.TIPO_OUT, MovimientoInventario.TIPO_ADJ):
            qs = qs.filter(tipo=tipo_sel)

    # --- Tarjetas del mes más reciente ---------------------------
    # No resumen todo el rango filtrado sino el último mes con movimientos:
    # en una auditoría mensual lo que se mira primero es cómo cerró el mes,
    # no un acumulado de medio año.
    #
    # Se ignora el filtro de Tipo a propósito: las tarjetas SON el desglose
    # por tipo, y con tipo=Salida seleccionado mostrarían "Ingresos 0", que
    # se leería como que no entró nada ese mes.
    kpi_base = MovimientoInventario.objects.none()
    if sede_sel:
        kpi_base = MovimientoInventario.objects.filter(sede=sede_sel)
        if producto_sel:
            kpi_base = kpi_base.filter(producto=producto_sel)

    mes_ref = (
        qs.order_by("-mes").values_list("mes", flat=True).first()
        if sede_sel else None
    )

    kpis = None
    mes_comparado = None
    if mes_ref:
        inicio = timezone.localtime(mes_ref).date().replace(day=1)
        anterior = _mes_anterior(inicio)
        mes_comparado = anterior

        actuales = _totales_del_mes(kpi_base, inicio)
        previos = _totales_del_mes(kpi_base, anterior)

        kpis = [
            {
                "clave": "movs", "titulo": "Movimientos", "icono": "bx-transfer",
                "color": "azul", "valor": actuales["movs"], "signo": "",
                "delta": _variacion(actuales["movs"], previos["movs"]),
            },
            {
                "clave": "in", "titulo": "Ingresos", "icono": "bx-down-arrow-alt",
                "color": "verde", "valor": actuales["in"], "signo": "+",
                "delta": _variacion(actuales["in"], previos["in"]),
            },
            {
                "clave": "out", "titulo": "Salidas", "icono": "bx-up-arrow-alt",
                "color": "rojo", "valor": actuales["out"], "signo": "-",
                # En salidas, subir es gastar más: la flecha sube pero no es
                # una buena noticia, así que no se pinta de verde.
                "delta": _variacion(actuales["out"], previos["out"], bueno_si_sube=False),
            },
            {
                "clave": "adj", "titulo": "Ajustes", "icono": "bx-slider-alt",
                "color": "ambar", "valor": actuales["adj"], "signo": "",
                "delta": _variacion(abs(actuales["adj"]), abs(previos["adj"]), bueno_si_sube=False),
            },
        ]

    # Totales del rango completo, para el pie de la tabla. Se agregan en la
    # base, no recorriendo en Python.
    def _suma(tipo):
        return qs.filter(tipo=tipo).aggregate(t=Sum("qty"))["t"] or 0

    total_in = _suma(MovimientoInventario.TIPO_IN)
    total_out = _suma(MovimientoInventario.TIPO_OUT)
    total_adj = _suma(MovimientoInventario.TIPO_ADJ)

    paginator = Paginator(qs, filas)
    pagina = paginator.get_page(request.GET.get("page"))

    # Filtros vigentes, para que los enlaces de paginación no los pierdan.
    filtros = f"desde={desde.isoformat()}&hasta={hasta.isoformat()}&filas={filas}"
    if producto_sel:
        filtros += f"&producto={producto_sel.pk}"
    if tipo_sel:
        filtros += f"&tipo={tipo_sel}"

    return render(request, "reportes/kardex.html", {
        "profile": profile,
        "productos": productos,
        "sede_sel": sede_sel,
        "producto_sel": producto_sel,
        "tipo_sel": tipo_sel,
        "desde": desde,
        "hasta": hasta,
        "pagina": pagina,
        "movimientos": pagina.object_list,
        "total_movimientos": paginator.count,
        "total_in": total_in,
        "total_out": total_out,
        "total_adj": total_adj,
        "kpis": kpis,
        "mes_ref": mes_ref,
        "mes_comparado": mes_comparado,
        "filas": filas,
        "filas_opciones": FILAS_OPCIONES,
        "filtros": filtros,
        "tipos": MovimientoInventario.TIPOS,
        "mes_anterior": (desde_def - timedelta(days=1)).replace(day=1),
    })
