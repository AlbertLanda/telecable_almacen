import csv
from datetime import date, timedelta
from io import BytesIO, StringIO
from urllib.parse import quote

import xlsxwriter
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.core.exceptions import PermissionDenied
from django.shortcuts import render
from django.utils import timezone

from django.http import Http404, HttpResponse

from .models import DetalleMaterialSicv
from .services.sicv import resumen_recibido
from inventario.models import (
    Categoria,
    Sede,
    MovimientoInventario,
    Producto,
    UserProfile,
)
from .services.registro_materiales import (
    AGRUPACIONES,
    AGRUPACION_DEFECTO,
    CLAVES,
    CLAVES_EQUIPOS,
    CLAVES_LIQUIDACIONES,
    CLAVES_MATERIALES,
    CLAVES_OBRAS,
    CLAVES_NUMERICAS,
    CLAVES_NUMERICAS_LIQUIDACIONES,
    CLAVES_NUMERICAS_MATERIALES,
    CLAVES_NUMERICAS_OBRAS,
    CLAVES_NUMERICAS_TECNICO,
    CLAVES_TECNICO,
    COLUMNAS,
    TITULOS,
    TITULOS_EQUIPOS,
    TITULOS_LIQUIDACIONES,
    TITULOS_MATERIALES,
    TITULOS_OBRAS,
    TITULOS_TECNICO,
    construir_filas,
    construir_filas_equipos,
    construir_filas_liquidaciones,
    construir_filas_materiales,
    construir_filas_obras,
    construir_filas_por_tecnico,
)

# Opciones del selector "Filas". Se valida contra esta lista: un ?filas=99999
# en la URL no debe poder pedir el periodo entero en una sola página.
FILAS_OPCIONES = (10, 20, 50)
FILAS_DEFECTO = 10

# Tope de filas de la pantalla. El catálogo son cientos de materiales, no
# cientos de miles, así que se muestran todos juntos; el tope está por si
# algún día crece más de lo previsto y para que la página no se caiga sin
# avisar. El Excel no tiene tope: ahí va el detalle completo.
TOPE_FILAS = 1000

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


def _parse_fecha(valor, por_defecto=None):
    """
    Fecha de la URL, o el valor por defecto.

    Una fecha ilegible cae en el defecto en vez de reventar: un ?desde=ayer
    escrito a mano no debe dejar la pantalla en error.
    """
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


# Roles que ven más de una sede. La decisión es por ROL y no por tener o no
# `sede_principal`: en esta base casi todos los usuarios tienen las tres
# sedes cargadas en `sedes_permitidas`, incluidos dos técnicos, así que
# usar ese campo como criterio les abriría el almacén de las otras sedes.
ROLES_MULTISEDE = (UserProfile.Rol.ADMIN, UserProfile.Rol.JEFA)

# Atajos de periodo. No hay opción "Todo" a propósito: SICV acumula una
# fila por material y por orden, así que sin recorte la pantalla traería
# meses enteros de una. El mes en curso es el defecto porque es el corte
# con el que se trabaja.
PERIODOS = (
    ("hoy", "Hoy"),
    ("ayer", "Ayer"),
    ("7dias", "Últimos 7 días"),
    ("mes", "Este mes"),
    ("mes_pasado", "Mes pasado"),
)

PERIODO_DEFECTO = "mes"

# "personalizado" no tiene atajo: se activa solo cuando alguien toca las
# fechas, y por eso vive aparte de la lista que se dibuja.
PERIODOS_VALIDOS = {clave for clave, _ in PERIODOS} | {"personalizado"}


def _rango_periodo(clave, request):
    """
    Traduce el atajo de periodo a un par de fechas.

    Los rangos son inclusivos en los dos extremos: "Últimos 7 días" cuenta
    hoy, porque quien lo elige espera ver lo de recién.

    Un periodo personalizado sin fechas legibles cae en el mes en curso en
    vez de quedar sin recorte: es lo mismo que pedir el defecto, y evita
    que un ?periodo=personalizado suelto traiga el histórico entero.
    """
    hoy = timezone.localdate()

    if clave == "hoy":
        return hoy, hoy
    if clave == "ayer":
        ayer = hoy - timedelta(days=1)
        return ayer, ayer
    if clave == "7dias":
        return hoy - timedelta(days=6), hoy
    if clave == "mes_pasado":
        # El último día del mes pasado es el día anterior al primero de este.
        ultimo = hoy.replace(day=1) - timedelta(days=1)
        return ultimo.replace(day=1), ultimo
    if clave == "personalizado":
        desde = _parse_fecha(request.GET.get("desde"), None)
        hasta = _parse_fecha(request.GET.get("hasta"), None)
        if desde and hasta and desde > hasta:
            desde, hasta = hasta, desde
        if desde or hasta:
            return desde, hasta

    return hoy.replace(day=1), hoy


def _sedes_visibles(profile):
    """
    Sedes que este usuario puede leer.

    Es el mismo criterio que aplica inventory_list (dashboard.py:280): un
    almacenero ve su sede y nada más; admin y jefa eligen entre las que
    tengan permitidas. Si acá fuera distinto, el reporte sería una forma de
    ver lo que el resto del sistema no deja -o al revés, de esconder lo que
    sí deja-.
    """
    if profile.rol in ROLES_MULTISEDE:
        permitidas = profile.sedes_permitidas.all().order_by("nombre")
        if permitidas.exists():
            return permitidas

    sede = profile.get_sede_operativa()
    return Sede.objects.filter(pk=sede.pk) if sede else Sede.objects.none()


def _sede_pedida(request, profile, disponibles):
    """La sede del selector, validada contra las que el usuario puede ver."""
    pedida = request.GET.get("sede_id")
    if pedida:
        elegida = disponibles.filter(pk=pedida).first()
        if elegida:
            return elegida

    return profile.get_sede_operativa() or disponibles.first()


@login_required
def reportes_sicv(request):
    """
    Material declarado en campo, tal como llegó de SICV.

    La pantalla muestra lo recibido sin interpretarlo: los nombres de
    material y de técnico son los de SICV, no los nuestros. Cruzarlos
    contra el catálogo es un paso aparte que todavía no está hecho, y
    mostrarlos ya traducidos daría a entender que sí.

    Los tres vacíos del contrato (§8.3) se ven distinto a propósito:
    integración apagada, sin datos todavía, y filtro sin resultados no
    significan lo mismo.
    """
    profile = _require_roles(request.user, *ROLES_REPORTES)

    # Un almacenero tiene una sola sede y el selector le muestra esa; una
    # jefa con varias puede elegir entre las suyas. Una sede pedida por URL
    # que no esté entre las permitidas se ignora: el filtro no puede ser una
    # puerta para leer otro almacén.
    sedes_disponibles = _sedes_visibles(profile)
    sede = _sede_pedida(request, profile, sedes_disponibles)

    detalles = (
        DetalleMaterialSicv.objects.filter(sede=sede)
        if sede else DetalleMaterialSicv.objects.none()
    )

    # --- Filtros -------------------------------------------------
    periodo_sel = request.GET.get("periodo") or PERIODO_DEFECTO
    if periodo_sel not in PERIODOS_VALIDOS:
        periodo_sel = PERIODO_DEFECTO

    desde, hasta = _rango_periodo(periodo_sel, request)
    if desde:
        detalles = detalles.filter(atencion__date__gte=desde)
    if hasta:
        detalles = detalles.filter(atencion__date__lte=hasta)

    busqueda = (request.GET.get("q") or "").strip()
    if busqueda:
        detalles = detalles.filter(
            Q(orden__icontains=busqueda)
            | Q(abonado__icontains=busqueda)
            | Q(codigo_abonado__icontains=busqueda)
            | Q(material__icontains=busqueda)
            | Q(tecnico__icontains=busqueda)
            | Q(mac__icontains=busqueda)
            | Q(direccion__icontains=busqueda)
        )

    tecnico_sel = (request.GET.get("tecnico") or "").strip()
    if tecnico_sel:
        detalles = detalles.filter(tecnico__iexact=tecnico_sel)

    material_sel = (request.GET.get("material") or "").strip()
    if material_sel:
        detalles = detalles.filter(material__iexact=material_sel)

    try:
        filas = int(request.GET.get("filas", FILAS_DEFECTO))
    except (TypeError, ValueError):
        filas = FILAS_DEFECTO
    if filas not in FILAS_OPCIONES:
        filas = FILAS_DEFECTO

    # Ordenar por técnico primero es lo que permite agrupar en la plantilla:
    # {% regroup %} solo junta elementos consecutivos, así que si el orden
    # fuera cronológico el mismo técnico aparecería en varios bloques.
    detalles = detalles.order_by("tecnico", "-atencion", "orden")

    paginador = Paginator(detalles, filas)
    pagina = paginador.get_page(request.GET.get("page"))

    # --- Estado general (sin filtrar) ----------------------------
    # Las cifras de cabecera resumen TODO lo recibido, no lo filtrado: son
    # el estado de la recepción, no del filtro que está puesto.
    del_sede = (
        DetalleMaterialSicv.objects.filter(sede=sede)
        if sede else DetalleMaterialSicv.objects.none()
    )
    # resumen_recibido() sigue haciendo falta: de ahí salen las opciones de
    # los filtros de material y de técnico.
    resumen = resumen_recibido(sede) if sede else {"materiales": [], "tecnicos": []}

    filtros = f"filas={filas}"
    if sede:
        filtros += f"&sede_id={sede.pk}"
    if busqueda:
        filtros += f"&q={quote(busqueda)}"
    if tecnico_sel:
        filtros += f"&tecnico={quote(tecnico_sel)}"
    if material_sel:
        filtros += f"&material={quote(material_sel)}"
    # El periodo siempre viaja: ahora que hay un recorte por defecto, no
    # llevarlo haría que pasar de página devuelva al mes en curso.
    filtros += f"&periodo={periodo_sel}"
    if desde:
        filtros += f"&desde={desde.isoformat()}"
    if hasta:
        filtros += f"&hasta={hasta.isoformat()}"

    return render(request, "reportes/sicv.html", {
        "profile": profile,
        "sede": sede,
        "sicv_habilitado": settings.SICV_SYNC_ENABLED,
        "sicv_configurado": bool(settings.SICV_URL and settings.SICV_TOKEN),
        "sicv_url": settings.SICV_URL,

        "detalles": pagina.object_list,
        "pagina_actual": pagina,
        "total_filtrado": paginador.count,
        "filas": filas,
        "filas_opciones": FILAS_OPCIONES,
        "filtros": filtros,

        "sedes_disponibles": sedes_disponibles,
        "periodos": PERIODOS,
        "periodo_sel": periodo_sel,
        "desde": desde,
        "hasta": hasta,
        "busqueda": busqueda,
        "tecnico_sel": tecnico_sel,
        "material_sel": material_sel,
        "tecnicos": [r["tecnico"] for r in resumen["tecnicos"]],
        "materiales": [r["material"] for r in resumen["materiales"]],
        "hay_filtros": bool(
            busqueda or tecnico_sel or material_sel or periodo_sel != PERIODO_DEFECTO
        ),

        "total_filas": del_sede.count(),
    })


def _filtros_movimientos(request, profile):
    """
    Lee los filtros de la URL y arma la consulta base de movimientos.

    Vive aparte porque el Kardex y sus exportaciones tienen que ver
    exactamente lo mismo: si el usuario filtró setiembre y un material, el
    archivo que se descarga no puede traer otra cosa. Devuelve la consulta
    sin ordenar ni paginar; de eso se encarga cada vista según lo que
    necesite mostrar.
    """
    # La sede no se elige: es la del usuario. Un almacenero de Jauja lee el
    # kardex de Jauja y de ningún otro, y tampoco puede pedir otro por URL.
    sede_sel = profile.get_sede_operativa()
    productos = Producto.objects.order_by("nombre")

    desde_def, hasta_def = _rango_mes_actual()
    desde = _parse_fecha(request.GET.get("desde"), desde_def)
    hasta = _parse_fecha(request.GET.get("hasta"), hasta_def)

    if desde > hasta:
        desde, hasta = hasta, desde

    producto_id = request.GET.get("producto") or ""
    producto_sel = productos.filter(pk=producto_id).first() if producto_id else None

    tipo_sel = request.GET.get("tipo") or ""

    busqueda = (request.GET.get("q") or "").strip()

    categoria_id = request.GET.get("categoria") or ""
    categoria_sel = (
        Categoria.objects.filter(pk=categoria_id).first() if categoria_id else None
    )

    agrupacion = request.GET.get("agrupacion") or AGRUPACION_DEFECTO
    if agrupacion not in AGRUPACIONES:
        agrupacion = AGRUPACION_DEFECTO

    qs = MovimientoInventario.objects.none()
    if sede_sel:
        qs = MovimientoInventario.objects.filter(
            sede=sede_sel, creado_en__date__gte=desde, creado_en__date__lte=hasta
        )
        if producto_sel:
            qs = qs.filter(producto=producto_sel)
        if tipo_sel in (MovimientoInventario.TIPO_IN, MovimientoInventario.TIPO_OUT, MovimientoInventario.TIPO_ADJ):
            qs = qs.filter(tipo=tipo_sel)

    return {
        "sede_sel": sede_sel,
        "productos": productos,
        "producto_sel": producto_sel,
        "tipo_sel": tipo_sel,
        "busqueda": busqueda,
        "categoria_sel": categoria_sel,
        "agrupacion": agrupacion,
        "desde": desde,
        "hasta": hasta,
        "desde_def": desde_def,
        "qs": qs,
    }


@login_required
def reportes_kardex(request):
    """
    Reporte general: todos los materiales del almacén, de una.

    Sigue la misma lógica que la pantalla de ONUs / Equipos Serializados
    -filtrar, ver todo junto, exportar- con una diferencia de fondo: aquella
    lista una fila por equipo físico, y eso solo existe para lo serializado.
    Un cable no tiene unidades individuales, existe como cantidad. Así que
    acá la fila es el material, y el detalle de equipos aparece en su
    columna cuando el material los tiene.

    Parte del catálogo y no de los movimientos: un material que nadie tocó
    igual aparece, que es justo lo que hay que ver para reponer.
    """
    profile = _require_roles(request.user, *ROLES_REPORTES)

    sedes_disponibles = _sedes_visibles(profile)
    sede_sel = _sede_pedida(request, profile, sedes_disponibles)

    busqueda = (request.GET.get("q") or "").strip()

    categoria_id = request.GET.get("categoria") or ""
    categoria_sel = (
        Categoria.objects.filter(pk=categoria_id).first() if categoria_id else None
    )

    materiales = construir_filas_materiales(
        sede_sel,
        busqueda=busqueda,
        categoria=categoria_sel,
    )

    filtros = ""
    if sede_sel:
        filtros += f"sede_id={sede_sel.pk}"
    if busqueda:
        filtros += f"&q={quote(busqueda)}"
    if categoria_sel:
        filtros += f"&categoria={categoria_sel.pk}"

    return render(request, "reportes/kardex.html", {
        "profile": profile,
        "sede": sede_sel,
        "sedes_disponibles": sedes_disponibles,
        "categorias": Categoria.objects.order_by("nombre"),

        "busqueda": busqueda,
        "categoria_sel": categoria_sel,
        "hay_filtros": bool(busqueda or categoria_sel),

        # Se muestran todos de una, sin paginar: el catálogo son cientos de
        # filas, no cientos de miles, y partirlo obligaría a pasar páginas
        # para algo que se lee de un vistazo. El tope existe igual por si el
        # catálogo crece más de lo previsto.
        "materiales": materiales[:TOPE_FILAS],
        "total_materiales": len(materiales),
        "tope_filas": TOPE_FILAS,
        "recortado": len(materiales) > TOPE_FILAS,

        "filtros": filtros,
    })


# ==================================================================
# Registro de materiales (exportable)
# ==================================================================
FORMATOS = ("xlsx", "csv", "html")

# Qué tabla lleva el CSV. El Excel las trae todas en hojas separadas; el
# CSV, que es un archivo de una sola tabla, obliga a elegir.
HOJAS_CSV = ("materiales", "movimientos", "equipos", "tecnicos", "liquidaciones", "obras")
HOJA_CSV_DEFECTO = "materiales"

# Columnas que no son texto en ninguna de las tres tablas. Se declaran una
# vez porque los tres formatos tienen que coincidir: si el Excel escribe
# una fecha como fecha y el CSV la escribe como texto, las dos copias del
# mismo reporte dejan de cuadrar.
FECHAS = {"fecha", "registrado", "ultimo_movimiento"}
IMPORTES = {"costo_unitario", "costo_total", "valor_stock"}


def _nombre_archivo(sede, desde, hasta, extension, prefijo="reporte-general"):
    sede_txt = (sede.nombre if sede else "sin-sede").lower().replace(" ", "-")
    return f"{prefijo}_{sede_txt}_{desde:%Y%m%d}-{hasta:%Y%m%d}.{extension}"


@login_required
def reportes_registro_export(request, formato):
    """
    Exporta el reporte general del periodo filtrado.

    Las tres tablas -materiales, movimientos y equipos- salen de las mismas
    funciones que alimentan la pantalla, así que el archivo no puede
    discrepar de lo que el usuario acaba de ver. Lo que cambia es el envase:
    xlsx y csv son tabla plana para llevar a una dinámica; html es la
    versión imprimible, y es la única que lleva totales, porque un total
    dentro de la hoja de datos rompe el rango de la tabla dinámica.
    """
    profile = _require_roles(request.user, *ROLES_REPORTES)

    if formato not in FORMATOS:
        raise Http404("Formato de exportación no soportado.")

    filtros_sel = _filtros_movimientos(request, profile)
    sede = filtros_sel["sede_sel"]
    producto_sel = filtros_sel["producto_sel"]
    desde = filtros_sel["desde"]
    hasta = filtros_sel["hasta"]

    materiales = construir_filas_materiales(
        sede,
        busqueda=filtros_sel["busqueda"],
        categoria=filtros_sel["categoria_sel"],
    )

    # Los equipos no se filtran por fecha: la tabla dice dónde está cada uno
    # HOY, no qué pasó en el periodo. Acotarla al rango daría a entender que
    # los equipos fuera de él dejaron de existir.
    equipos = construir_filas_equipos(sede, producto_sel)

    movimientos = construir_filas(filtros_sel["qs"])
    por_tecnico = construir_filas_por_tecnico(movimientos, filtros_sel["agrupacion"])
    liquidaciones = construir_filas_liquidaciones(
        sede, desde, hasta, filtros_sel["agrupacion"]
    )
    obras = construir_filas_obras(sede, producto=producto_sel)

    if formato == "csv":
        hoja = request.GET.get("hoja") or HOJA_CSV_DEFECTO
        if hoja not in HOJAS_CSV:
            hoja = HOJA_CSV_DEFECTO

        tablas = {
            "materiales": (materiales, TITULOS_MATERIALES, CLAVES_MATERIALES),
            "movimientos": (movimientos, TITULOS, CLAVES),
            "equipos": (equipos, TITULOS_EQUIPOS, CLAVES_EQUIPOS),
            "tecnicos": (por_tecnico, TITULOS_TECNICO, CLAVES_TECNICO),
            "liquidaciones": (liquidaciones, TITULOS_LIQUIDACIONES, CLAVES_LIQUIDACIONES),
            "obras": (obras, TITULOS_OBRAS, CLAVES_OBRAS),
        }
        filas_csv, titulos_csv, claves_csv = tablas[hoja]

        return _exportar_csv(
            filas_csv, titulos_csv, claves_csv,
            _nombre_archivo(sede, desde, hasta, "csv", hoja),
        )

    if formato == "xlsx":
        return _exportar_xlsx(
            materiales, movimientos, por_tecnico, liquidaciones, obras,
            equipos, sede, desde, hasta,
        )

    return render(request, "reportes/registro_print.html", {
        "cabeceras_materiales": TITULOS_MATERIALES,
        "materiales": [
            [_para_imprimir(clave, fila[clave]) for clave in CLAVES_MATERIALES]
            for fila in materiales
        ],
        "cabeceras": [
            {"titulo": titulo, "num": clave in CLAVES_NUMERICAS}
            for clave, titulo in COLUMNAS
        ],
        # Las filas van ya ordenadas por columna y con el valor listo para
        # imprimir: la plantilla de Django no puede indexar un diccionario
        # con una clave variable, y formatear acá evita inventar un filtro.
        "filas": [
            [
                {"valor": _para_imprimir(clave, fila[clave]), "num": clave in CLAVES_NUMERICAS}
                for clave in CLAVES
            ]
            for fila in movimientos
        ],
        "cabeceras_equipos": TITULOS_EQUIPOS,
        "equipos": [
            [_para_imprimir(clave, fila[clave]) for clave in CLAVES_EQUIPOS]
            for fila in equipos
        ],
        "sede": sede,
        "desde": desde,
        "hasta": hasta,
        "producto_sel": producto_sel,
        "generado_en": timezone.localtime(),
        "total_materiales": len(materiales),
        "total_filas": len(movimientos),
        "total_equipos": len(equipos),
    })


def _para_imprimir(clave, valor):
    """Valor tal como debe verse en el papel."""
    if valor is None or valor == "":
        return ""
    if clave in FECHAS:
        return valor.strftime("%d/%m/%Y")
    if clave in IMPORTES:
        return f"{valor:,.2f}"
    return valor


def _escribir_hoja(libro, nombre, titulos, claves, filas, *, numericas, formatos, anchos):
    """
    Vuelca una tabla en una hoja nueva.

    Las tres hojas se escriben igual salvo por sus columnas, así que el
    formato -cabecera congelada, autofiltro, números como números- vive una
    sola vez. Que los importes y las cantidades NO salgan como texto es la
    diferencia entre una hoja que la dinámica suma y una que solo cuenta.
    """
    hoja = libro.add_worksheet(nombre)

    for col, titulo in enumerate(titulos):
        hoja.write(0, col, titulo, formatos["cabecera"])

    for nro, fila in enumerate(filas, start=1):
        for col, clave in enumerate(claves):
            valor = fila[clave]

            if valor is None or valor == "":
                continue

            if clave in FECHAS:
                hoja.write_datetime(nro, col, valor, formatos["fecha"])
            elif clave in IMPORTES:
                hoja.write_number(nro, col, valor, formatos["moneda"])
            elif clave in numericas:
                hoja.write_number(nro, col, valor)
            else:
                hoja.write_string(nro, col, str(valor))

    # Cabecera congelada y autofiltro: es lo primero que hace a mano
    # cualquiera que abra una tabla de este tamaño.
    hoja.freeze_panes(1, 0)
    if filas:
        hoja.autofilter(0, 0, len(filas), len(claves) - 1)

    for col, clave in enumerate(claves):
        hoja.set_column(col, col, anchos.get(clave, 14))

    return hoja


def _exportar_xlsx(
    materiales, movimientos, por_tecnico, liquidaciones, obras,
    equipos, sede, desde, hasta,
):
    salida = BytesIO()
    # constant_memory descarga cada fila apenas se escribe, en vez de
    # sostener la hoja entera en RAM. Exige escribir en orden, que es lo
    # que hacemos.
    libro = xlsxwriter.Workbook(salida, {"in_memory": True, "constant_memory": True})

    formatos = {
        "cabecera": libro.add_format({"bold": True, "bg_color": "#D9EAF7", "border": 1}),
        "fecha": libro.add_format({"num_format": "dd/mm/yyyy"}),
        "moneda": libro.add_format({"num_format": "#,##0.00"}),
    }

    # El orden de las hojas es el de lectura: primero qué hay, después qué
    # pasó con eso, y al final qué equipo está dónde.
    _escribir_hoja(
        libro, "Materiales", TITULOS_MATERIALES, CLAVES_MATERIALES, materiales,
        numericas=CLAVES_NUMERICAS_MATERIALES, formatos=formatos,
        anchos={"material": 34, "categoria": 20, "codigo": 16, "estado": 16,
                "stock": 12, "stock_minimo": 12},
    )

    _escribir_hoja(
        libro, "Movimientos", TITULOS, CLAVES, movimientos,
        numericas=CLAVES_NUMERICAS, formatos=formatos,
        anchos={"material": 34, "obra": 30, "nota": 34, "documento": 18,
                "referencia": 18, "tecnico": 22, "proveedor": 26,
                "categoria": 18, "sede": 16, "ubicacion": 16,
                "registrado_por": 20, "codigo": 15},
    )

    _escribir_hoja(
        libro, "Por técnico", TITULOS_TECNICO, CLAVES_TECNICO, por_tecnico,
        numericas=CLAVES_NUMERICAS_TECNICO, formatos=formatos,
        anchos={"material": 34, "tecnico": 24, "codigo": 15, "periodo": 13,
                "entregado": 13, "devuelto": 13, "neto": 13, "movimientos": 16},
    )

    _escribir_hoja(
        libro, "Liquidaciones", TITULOS_LIQUIDACIONES, CLAVES_LIQUIDACIONES, liquidaciones,
        numericas=CLAVES_NUMERICAS_LIQUIDACIONES, formatos=formatos,
        anchos={"material": 34, "tecnico": 24, "codigo": 15, "periodo": 13,
                "total": 16, "documentos": 18, "pct_merma": 12},
    )

    _escribir_hoja(
        libro, "Obras", TITULOS_OBRAS, CLAVES_OBRAS, obras,
        numericas=CLAVES_NUMERICAS_OBRAS, formatos=formatos,
        anchos={"obra": 18, "nombre_obra": 34, "material": 34, "responsable": 22,
                "codigo": 15, "estado": 18, "tipo": 12,
                "por_liquidar": 14, "desvio": 15, "costo_real": 14},
    )

    # Va aparte porque el sistema no registra qué serial salió en qué
    # movimiento (ver el servicio); mezclarlas insinuaría una relación que
    # no existe.
    _escribir_hoja(
        libro, "Equipos", TITULOS_EQUIPOS, CLAVES_EQUIPOS, equipos,
        numericas=set(), formatos=formatos,
        anchos={"material": 30, "serial": 22, "mac": 18, "serial_secundario": 22,
                "asignado_a": 22, "obra": 30, "sede": 16, "ubicacion": 16},
    )

    libro.close()
    salida.seek(0)

    respuesta = HttpResponse(
        salida.read(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    respuesta["Content-Disposition"] = (
        f'attachment; filename="{_nombre_archivo(sede, desde, hasta, "xlsx")}"'
    )
    return respuesta


def _exportar_csv(filas, titulos, claves, nombre):
    """
    CSV para Excel en español: separador ';' y coma decimal.

    Con separador ',' y punto decimal, un Excel con configuración regional
    de Perú mete toda la fila en una sola columna. El BOM al inicio es lo
    que hace que Excel reconozca UTF-8 y no rompa las tildes.
    """
    buffer = StringIO()
    buffer.write("\ufeff")

    escritor = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    escritor.writerow(titulos)

    for fila in filas:
        registro = []
        for clave in claves:
            valor = fila[clave]
            if valor is None or valor == "":
                registro.append("")
            elif clave in FECHAS:
                registro.append(valor.strftime("%d/%m/%Y"))
            elif clave in IMPORTES:
                registro.append(f"{valor:.2f}".replace(".", ","))
            else:
                registro.append(valor)
        escritor.writerow(registro)

    respuesta = HttpResponse(buffer.getvalue(), content_type="text/csv; charset=utf-8")
    respuesta["Content-Disposition"] = f'attachment; filename="{nombre}"'
    return respuesta
