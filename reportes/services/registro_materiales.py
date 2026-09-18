# -*- coding: utf-8 -*-
"""
Tabla plana de movimientos de material, para exportar.

Es deliberadamente distinta del Kardex en pantalla. El Kardex agrupa por
material con un encabezado por bloque, porque la columna de saldo solo se
puede leer hacia abajo dentro de un mismo material. Acá no: el destino de
este archivo son tablas dinámicas, y una dinámica necesita una fila por
hecho, la cabecera en la fila 1 y ninguna celda combinada. Por eso el
material va repetido en cada fila y no hay subtotales.
"""
from django.db.models import Q
from django.utils import timezone

from inventario.models import (
    DocumentoInventario,
    DocumentoItem,
    ItemSerializado,
    MovimientoInventario,
    Producto,
    Stock,
    TipoDocumento,
)

# (clave, título). El orden es el de las columnas del archivo.
COLUMNAS = [
    # Cuándo. Mes y Semana van precalculadas: Excel sabe agrupar fechas,
    # pero es un paso manual en cada dinámica que se arme, y la operación
    # del almacén se liquida por semana.
    ("fecha", "Fecha"),
    ("hora", "Hora"),
    ("mes", "Mes"),
    ("semana", "Semana"),

    # Dónde
    ("sede", "Sede"),
    ("ubicacion", "Ubicación"),

    # Qué material. La unidad es la que evita que alguien sume metros de
    # fibra con unidades de ONU en una misma celda de la dinámica.
    ("codigo", "Código"),
    ("material", "Material"),
    ("categoria", "Categoría"),
    ("unidad", "Unidad"),

    # Cuánto. "Cantidad con signo" existe para que el pivot pueda sumar
    # directo: con solo Cantidad positiva habría que crear un campo
    # calculado en cada tabla que se arme.
    ("movimiento", "Movimiento"),
    ("cantidad", "Cantidad"),
    ("cantidad_signo", "Cantidad con signo"),
    ("saldo", "Saldo resultante"),
    ("costo_unitario", "Costo unitario"),
    ("costo_total", "Costo total"),

    # De dónde viene / a dónde va
    ("documento", "Documento"),
    ("tipo_doc", "Tipo doc"),
    ("referencia", "Referencia"),
    ("obra", "Obra"),
    ("tecnico", "Técnico"),
    ("proveedor", "Proveedor"),
    ("sede_destino", "Sede destino"),

    # Auditoría
    ("registrado_por", "Registrado por"),
    ("nota", "Nota"),
]

TITULOS = [titulo for _, titulo in COLUMNAS]
CLAVES = [clave for clave, _ in COLUMNAS]

# Columnas que deben viajar como número, no como texto: si se escriben como
# texto la dinámica no las puede sumar.
CLAVES_NUMERICAS = {"cantidad", "cantidad_signo", "saldo", "costo_unitario", "costo_total"}


def _nombre(usuario):
    if not usuario:
        return ""
    return usuario.get_full_name() or usuario.username


def _documentos_de(movimientos):
    """
    Mapa {número de documento -> documento} para los movimientos dados.

    `MovimientoInventario.referencia` guarda el número del documento, no una
    FK, así que el enlace hay que armarlo a mano. Es exacto porque el número
    es único, y se resuelve en una sola consulta en vez de una por fila.
    """
    numeros = {(m.referencia or "").strip() for m in movimientos}
    numeros.discard("")
    if not numeros:
        return {}

    documentos = (
        DocumentoInventario.objects
        .filter(numero__in=numeros)
        .select_related("proveedor", "sede_destino", "solicitante", "retirado_por")
    )
    return {d.numero: d for d in documentos}


def _obras_por_codigo(documentos):
    """
    Las salidas a obra guardan el código de obra en `documento.referencia`.
    Se traduce a nombre acá para que la columna Obra sea legible.
    """
    from proyectos.models import Proyecto

    codigos = set()
    for doc in documentos.values():
        ref = (doc.referencia or "").strip().upper()
        if ref.startswith("RETORNO "):
            ref = ref[len("RETORNO "):].strip()
        if ref:
            codigos.add(ref)

    if not codigos:
        return {}

    return {
        p.codigo.strip().upper(): p
        for p in Proyecto.objects.filter(codigo__in=codigos)
    }


def construir_filas(qs):
    """
    Devuelve la lista de filas (dict por clave de COLUMNAS).

    Se materializa el queryset porque los documentos y las obras se resuelven
    en bloque: recorrerlo dos veces con .iterator() volvería a consultar.
    """
    movimientos = list(
        qs.select_related(
            "producto", "producto__categoria", "sede", "ubicacion", "usuario"
        ).order_by("creado_en", "id")
    )

    documentos = _documentos_de(movimientos)
    obras = _obras_por_codigo(documentos)

    filas = []
    for mov in movimientos:
        momento = timezone.localtime(mov.creado_en)
        anio_iso, semana_iso, _ = momento.isocalendar()

        doc = documentos.get((mov.referencia or "").strip())

        obra = ""
        referencia_doc = ""
        tecnico = ""
        proveedor = ""
        sede_destino = ""
        tipo_doc = ""

        if doc:
            tipo_doc = doc.get_tipo_display()
            referencia_doc = doc.referencia or ""
            tecnico = _nombre(doc.solicitante) or _nombre(doc.retirado_por)
            sede_destino = doc.sede_destino.nombre if doc.sede_destino_id else ""

            if doc.proveedor_id:
                proveedor = doc.proveedor.razon_social
            elif doc.proveedor_manual:
                proveedor = doc.proveedor_manual

            clave_obra = referencia_doc.strip().upper()
            if clave_obra.startswith("RETORNO "):
                clave_obra = clave_obra[len("RETORNO "):].strip()
            if clave_obra in obras:
                obra = f"{obras[clave_obra].codigo} - {obras[clave_obra].nombre}"

        # Un ingreso suma y una salida resta. El ajuste ya viene firmado
        # desde el modelo, así que se respeta tal cual.
        cantidad = int(mov.qty or 0)
        if mov.tipo == MovimientoInventario.TIPO_OUT:
            con_signo = -abs(cantidad)
        elif mov.tipo == MovimientoInventario.TIPO_IN:
            con_signo = abs(cantidad)
        else:
            con_signo = cantidad

        filas.append({
            "fecha": momento.date(),
            "hora": momento.strftime("%H:%M"),
            "mes": momento.strftime("%Y-%m"),
            "semana": f"{anio_iso}-W{semana_iso:02d}",

            "sede": mov.sede.nombre if mov.sede_id else "",
            "ubicacion": mov.ubicacion.nombre if mov.ubicacion_id else "",

            "codigo": mov.producto.codigo_interno or "",
            "material": mov.producto.nombre,
            "categoria": mov.producto.categoria.nombre if mov.producto.categoria_id else "",
            "unidad": mov.producto.unidad or "",

            "movimiento": mov.get_tipo_display(),
            "cantidad": abs(cantidad),
            "cantidad_signo": con_signo,
            "saldo": int(mov.saldo_resultante or 0),
            "costo_unitario": float(mov.costo_unitario) if mov.costo_unitario is not None else None,
            "costo_total": float(mov.costo_total) if mov.costo_total is not None else None,

            "documento": mov.referencia or "",
            "tipo_doc": tipo_doc,
            "referencia": referencia_doc,
            "obra": obra,
            "tecnico": tecnico,
            "proveedor": proveedor,
            "sede_destino": sede_destino,

            "registrado_por": _nombre(mov.usuario),
            "nota": mov.nota or "",
        })

    return filas


# ==================================================================
# Equipos serializados
# ==================================================================
# Va como tabla aparte y no como columnas de la tabla de movimientos.
#
# El motivo es una limitación real del sistema, no una decisión estética:
# no existe forma de saber qué serial salió en qué movimiento. La tabla que
# guardaría ese enlace (DocumentoItemSerializado) nunca se llenó -está en
# cero-, y los despachos solo dejan rastro en el propio equipo
# (`asignado_a`, `proyecto`, `ubicacion`), que es su estado ACTUAL, no el
# historial. Pegar seriales a una fila de movimiento sería inventar una
# correspondencia que el sistema no registró.
#
# Lo que sí se puede afirmar es dónde está hoy cada equipo y con quién,
# y eso es exactamente lo que exporta esta tabla.

COLUMNAS_EQUIPOS = [
    ("material", "Material"),
    ("codigo", "Código"),
    ("serial", "Serial / GPON SN"),
    ("mac", "MAC"),
    ("serial_secundario", "Serial secundario"),
    ("trazabilidad", "Código pintado"),
    ("estado", "Estado"),
    ("sede", "Sede"),
    ("ubicacion", "Ubicación"),
    ("asignado_a", "Asignado a"),
    ("obra", "Obra"),
    ("registrado", "Registrado"),
]

TITULOS_EQUIPOS = [titulo for _, titulo in COLUMNAS_EQUIPOS]
CLAVES_EQUIPOS = [clave for clave, _ in COLUMNAS_EQUIPOS]


def construir_filas_equipos(sede=None, producto=None):
    """
    Un equipo serializado por fila, con dónde está y de quién es hoy.
    """
    equipos = ItemSerializado.objects.select_related(
        "producto", "ubicacion", "ubicacion__sede",
        "asignado_a", "asignado_a__profile", "asignado_a__profile__sede_principal",
        "proyecto", "proyecto__sede",
    )

    if sede:
        # Un equipo ASIGNADO se queda sin ubicación (el despacho la pone en
        # None), así que filtrar solo por ubicacion__sede lo dejaría fuera
        # justo cuando importa saber quién lo tiene. Se lo rescata por el
        # técnico que lo cargó o por la obra a la que salió.
        equipos = equipos.filter(
            Q(ubicacion__sede=sede)
            | Q(ubicacion__isnull=True, asignado_a__profile__sede_principal=sede)
            | Q(ubicacion__isnull=True, proyecto__sede=sede)
        )

    if producto:
        equipos = equipos.filter(producto=producto)

    filas = []
    for eq in equipos.order_by("producto__nombre", "serial"):
        if eq.ubicacion_id:
            sede_eq = eq.ubicacion.sede.nombre if eq.ubicacion.sede_id else ""
        elif eq.proyecto_id:
            sede_eq = eq.proyecto.sede.nombre if eq.proyecto.sede_id else ""
        else:
            perfil = getattr(eq.asignado_a, "profile", None) if eq.asignado_a_id else None
            sede_eq = perfil.sede_principal.nombre if perfil and perfil.sede_principal_id else ""

        filas.append({
            "material": eq.producto.nombre,
            "codigo": eq.producto.codigo_interno or "",
            "serial": eq.serial or "",
            "mac": eq.mac_address or "",
            "serial_secundario": eq.serial_secundario or "",
            "trazabilidad": eq.codigo_trazabilidad or "",
            "estado": eq.get_estado_display(),
            "sede": sede_eq,
            "ubicacion": eq.ubicacion.nombre if eq.ubicacion_id else "",
            "asignado_a": _nombre(eq.asignado_a),
            "obra": f"{eq.proyecto.codigo} - {eq.proyecto.nombre}" if eq.proyecto_id else "",
            "registrado": timezone.localtime(eq.creado_en).date(),
        })

    return filas


# ==================================================================
# Materiales
# ==================================================================
# Esta es la tabla principal del reporte general, y se diferencia del
# kardex en algo que no es cosmético: el kardex parte de los MOVIMIENTOS,
# así que un material que nadie tocó en el periodo simplemente no existe
# para él. Acá se parte del CATÁLOGO, de modo que un material sin
# movimiento igual aparece -con sus existencias en cero o intactas-, que
# es justo lo que hay que ver para reponer o para dar de baja.

# Las mismas columnas que la tabla de la pantalla, en el mismo orden. Que
# coincidan importa: quien descarga el Excel está buscando lo que acaba de
# ver, y una hoja con otras columnas obliga a revisar si es el mismo dato.
#
# Es el catálogo tal como está guardado: nada derivado ni clasificado. La
# actividad, el costo y el semáforo de existencias salieron a propósito, y
# se replantean más adelante con su lógica definida.
COLUMNAS_MATERIALES = [
    ("material", "Material"),
    ("codigo", "Código interno"),
    ("categoria", "Categoría"),
    ("unidad", "Unidad"),
    ("stock", "Stock"),
    ("stock_minimo", "Mínimo"),
]

TITULOS_MATERIALES = [titulo for _, titulo in COLUMNAS_MATERIALES]
CLAVES_MATERIALES = [clave for clave, _ in COLUMNAS_MATERIALES]

CLAVES_NUMERICAS_MATERIALES = {"stock", "stock_minimo"}


def construir_filas_materiales(sede, *, busqueda="", categoria=None):
    """
    Un material por fila: qué hay en el catálogo y cuánto queda.

    Parte de Producto y no de los movimientos, que es la diferencia con un
    kardex: un material que nadie tocó igual aparece -con su stock en cero
    o intacto- y es justo el que hay que ver para reponer o dar de baja.
    """
    productos = Producto.objects.filter(activo=True).select_related("categoria")

    if busqueda:
        productos = productos.filter(
            Q(nombre__icontains=busqueda)
            | Q(codigo_interno__icontains=busqueda)
            | Q(barcode__icontains=busqueda)
        )

    if categoria:
        productos = productos.filter(categoria=categoria)

    if not sede:
        return []

    # Tres consultas agregadas en bloque, en vez de una por material: con
    # un catálogo de varios cientos de productos, resolverlo fila por fila
    # sería el problema N+1 de manual.
    existencias = {
        s.producto_id: int(s.cantidad or 0)
        for s in Stock.objects.filter(sede=sede)
    }

    filas = []
    for producto in productos.order_by("nombre"):
        stock = existencias.get(producto.id, 0)

        filas.append({
            "codigo": producto.codigo_interno or "",
            "material": producto.nombre,
            "categoria": producto.categoria.nombre if producto.categoria_id else "",
            "unidad": producto.unidad or "",
            "stock": stock,
            "stock_minimo": int(producto.stock_minimo or 0),
            "producto_id": producto.id,
        })

    return filas


# ==================================================================
# Consumo por técnico
# ==================================================================
# Se arma sobre las filas que ya produjo construir_filas(), y no con una
# consulta propia, por una razón: el técnico no está en el movimiento sino
# en su documento, y esa unión ya vive allá. Recalcularla acá abriría la
# puerta a que las dos tablas del mismo reporte atribuyan distinto.

AGRUPACIONES = ("mes", "semana")
AGRUPACION_DEFECTO = "mes"

COLUMNAS_TECNICO = [
    ("codigo", "Código"),
    ("material", "Material"),
    ("unidad", "Unidad"),
    ("tecnico", "Técnico"),
    ("periodo", "Periodo"),
    ("entregado", "Entregado"),
    ("devuelto", "Devuelto"),
    ("neto", "Neto usado"),
    ("movimientos", "N.º movimientos"),
]

TITULOS_TECNICO = [titulo for _, titulo in COLUMNAS_TECNICO]
CLAVES_TECNICO = [clave for clave, _ in COLUMNAS_TECNICO]

CLAVES_NUMERICAS_TECNICO = {"entregado", "devuelto", "neto", "movimientos"}



def movimientos_sin_tecnico(filas_movimientos):
    """
    Cuántas entradas y salidas quedaron fuera del corte por técnico.

    El técnico se recupera del documento del movimiento, y muchos
    movimientos no tienen documento enlazado. Sin este número, la tabla
    parecería completa cuando en realidad puede estar mostrando una
    fracción del movimiento real.
    """
    return sum(
        1
        for fila in filas_movimientos
        if fila["movimiento"] in ("Ingreso", "Salida") and not fila["tecnico"]
    )


def construir_filas_por_tecnico(filas_movimientos, agrupacion=AGRUPACION_DEFECTO):
    """
    Qué material se llevó cada técnico en cada periodo, y cuánto devolvió.

    "Neto usado" es entregado menos devuelto, y es una aproximación: lo que
    un técnico consumió de verdad recién se sabe cuando liquida. Hasta
    entonces el neto incluye lo que todavía tiene en la mochila. Por eso se
    muestran las tres cifras y no solo el neto.
    """
    if agrupacion not in AGRUPACIONES:
        agrupacion = AGRUPACION_DEFECTO

    clave_periodo = "mes" if agrupacion == "mes" else "semana"

    acumulado = {}
    for fila in filas_movimientos:
        # Un ajuste no tiene técnico a quién imputarle: no es material que
        # alguien se llevó, es una corrección del almacén.
        if fila["movimiento"] not in ("Ingreso", "Salida"):
            continue

        # Sin técnico no hay consumo que atribuir, y meterlo igual falsea
        # las dos columnas: una compra o una carga inicial son ingresos que
        # se contarían como "devuelto", y un traslado entre sedes como
        # "entregado". Lo que queda fuera se cuenta aparte, con
        # movimientos_sin_tecnico(), para no esconder el hueco.
        tecnico = fila["tecnico"]
        if not tecnico:
            continue

        llave = (fila[clave_periodo], tecnico, fila["codigo"], fila["material"], fila["unidad"])
        registro = acumulado.setdefault(llave, {"entregado": 0, "devuelto": 0, "movimientos": 0})

        if fila["movimiento"] == "Salida":
            registro["entregado"] += fila["cantidad"]
        else:
            registro["devuelto"] += fila["cantidad"]

        registro["movimientos"] += 1

    filas = []
    for (periodo, tecnico, codigo, material, unidad), registro in acumulado.items():
        filas.append({
            "periodo": periodo,
            "tecnico": tecnico,
            "codigo": codigo,
            "material": material,
            "unidad": unidad,
            "entregado": registro["entregado"],
            "devuelto": registro["devuelto"],
            "neto": registro["entregado"] - registro["devuelto"],
            "movimientos": registro["movimientos"],
        })

    # El material manda, igual que en las demás pestañas: se lee "del cable
    # drop, Kevin se llevó tanto en setiembre". Dentro de cada material, el
    # periodo más reciente primero.
    # Son dos pasadas porque el orden del periodo es inverso al de los
    # otros, y el sort de Python es estable: la segunda respeta a la primera.
    filas.sort(key=lambda f: f["periodo"], reverse=True)
    filas.sort(key=lambda f: (f["material"], f["tecnico"]))

    return filas


# ==================================================================
# Liquidaciones de técnico
# ==================================================================
# A diferencia del corte "por técnico", que INFIERE el consumo restando lo
# devuelto de lo entregado, acá se lee lo que el técnico DECLARÓ al liquidar.
# Es el dato autoritativo y además separa merma de consumo, cosa que una
# resta no puede hacer.
#
# Ojo con la convención: en una liquidación de técnico lo devuelto viaja en
# DocumentoItem.cantidad, y cantidad_devuelta queda en cero. No es un
# descuido: DocumentoItem.clean() saltea su validación justamente para las
# referencias LIQ/DEVOLUCION/RETORNO. Leer cantidad_devuelta acá daría
# devoluciones en cero para todos.

COLUMNAS_LIQUIDACIONES = [
    ("codigo", "Código"),
    ("material", "Material"),
    ("unidad", "Unidad"),
    ("tecnico", "Técnico"),
    ("periodo", "Periodo"),
    ("devuelto", "Devuelto"),
    ("usado", "Usado"),
    ("merma", "Merma"),
    ("total", "Total liquidado"),
    ("pct_merma", "% merma"),
    ("documentos", "N.º liquidaciones"),
]

TITULOS_LIQUIDACIONES = [titulo for _, titulo in COLUMNAS_LIQUIDACIONES]
CLAVES_LIQUIDACIONES = [clave for clave, _ in COLUMNAS_LIQUIDACIONES]

CLAVES_NUMERICAS_LIQUIDACIONES = {
    "devuelto", "usado", "merma", "total", "pct_merma", "documentos",
}


def construir_filas_liquidaciones(sede, desde, hasta, agrupacion=AGRUPACION_DEFECTO):
    """
    Qué declaró cada técnico al liquidar, por periodo y material.
    """
    if agrupacion not in AGRUPACIONES:
        agrupacion = AGRUPACION_DEFECTO

    if not sede:
        return []

    items = (
        DocumentoItem.objects
        .filter(
            documento__tipo=TipoDocumento.ING,
            documento__referencia__istartswith="LIQ",
            documento__sede=sede,
            documento__fecha__date__gte=desde,
            documento__fecha__date__lte=hasta,
        )
        .select_related("documento", "documento__solicitante", "producto")
    )

    acumulado = {}
    for item in items:
        doc = item.documento
        tecnico = doc.solicitante
        if not tecnico:
            continue

        momento = timezone.localtime(doc.fecha)
        if agrupacion == "mes":
            periodo = momento.strftime("%Y-%m")
        else:
            anio_iso, semana_iso, _ = momento.isocalendar()
            periodo = f"{anio_iso}-W{semana_iso:02d}"

        nombre = tecnico.get_full_name() or tecnico.username
        llave = (
            periodo,
            nombre,
            item.producto.codigo_interno or "",
            item.producto.nombre,
            item.producto.unidad or "",
        )
        registro = acumulado.setdefault(
            llave, {"devuelto": 0, "usado": 0, "merma": 0, "documentos": set()}
        )

        registro["devuelto"] += int(item.cantidad or 0)
        registro["usado"] += int(item.cantidad_usada or 0)
        registro["merma"] += int(item.cantidad_merma or 0)
        registro["documentos"].add(doc.id)

    filas = []
    for (periodo, tecnico, codigo, material, unidad), registro in acumulado.items():
        total = registro["devuelto"] + registro["usado"] + registro["merma"]
        filas.append({
            "periodo": periodo,
            "tecnico": tecnico,
            "codigo": codigo,
            "material": material,
            "unidad": unidad,
            "devuelto": registro["devuelto"],
            "usado": registro["usado"],
            "merma": registro["merma"],
            "total": total,
            # Sobre el total liquidado y no sobre lo usado: la pregunta es
            # qué proporción del material que pasó por sus manos se perdió.
            "pct_merma": round(registro["merma"] * 100.0 / total, 1) if total else 0.0,
            "documentos": len(registro["documentos"]),
        })

    filas.sort(key=lambda f: f["periodo"], reverse=True)
    filas.sort(key=lambda f: (f["material"], f["tecnico"]))

    return filas


# ==================================================================
# Liquidaciones de obra
# ==================================================================
# No se filtra por fecha a propósito: una obra no pertenece a un mes. Se
# planifica en uno, se despacha en otro y se liquida en un tercero.
# Recortarla al periodo mostraría obras a medias sin decir que lo están.

COLUMNAS_OBRAS = [
    ("codigo", "Código"),
    ("material", "Material"),
    ("unidad", "Unidad"),
    ("obra", "Obra"),
    ("nombre_obra", "Nombre"),
    ("tipo", "Tipo"),
    ("estado", "Estado"),
    ("responsable", "Responsable"),
    ("planificado", "Planificado"),
    ("entregado", "Entregado"),
    ("devuelto", "Devuelto"),
    ("usado", "Usado"),
    ("merma", "Merma"),
    ("por_liquidar", "Por liquidar"),
    ("desvio", "Desvío vs. plan"),
    ("costo_unitario", "Costo unitario"),
    ("costo_real", "Costo real"),
]

TITULOS_OBRAS = [titulo for _, titulo in COLUMNAS_OBRAS]
CLAVES_OBRAS = [clave for clave, _ in COLUMNAS_OBRAS]

CLAVES_NUMERICAS_OBRAS = {
    "planificado", "entregado", "devuelto", "usado", "merma",
    "por_liquidar", "desvio", "costo_unitario", "costo_real",
}


def construir_filas_obras(sede, *, producto=None):
    """
    Material de cada obra: lo planificado contra lo que realmente se gastó.
    """
    if not sede:
        return []

    from proyectos.models import ProyectoMaterial

    materiales = (
        ProyectoMaterial.objects
        .filter(proyecto__sede=sede)
        .select_related("proyecto", "proyecto__responsable", "producto")
    )

    if producto:
        materiales = materiales.filter(producto=producto)

    filas = []
    for m in materiales.order_by("producto__nombre", "-proyecto__creado_en"):
        obra = m.proyecto
        planificado = int(m.cantidad_planificada or 0)
        entregado = int(m.cantidad_entregada or 0)
        usado = int(m.cantidad_usada or 0)
        merma = int(m.cantidad_merma or 0)

        responsable = ""
        if obra.responsable_id:
            responsable = obra.responsable.get_full_name() or obra.responsable.username

        filas.append({
            "obra": obra.codigo,
            "nombre_obra": obra.nombre,
            "tipo": obra.get_tipo_display(),
            "estado": obra.get_estado_display(),
            "responsable": responsable,
            "codigo": m.producto.codigo_interno or "",
            "material": m.producto.nombre,
            "unidad": m.producto.unidad or "",
            "planificado": planificado,
            "entregado": entregado,
            "devuelto": int(m.cantidad_devuelta or 0),
            "usado": usado,
            "merma": merma,
            # Lo entregado que todavía no se cerró como devuelto/usado/merma.
            "por_liquidar": m.cantidad_por_liquidar,
            # Positivo = se gastó más de lo planificado. Se mide contra el
            # consumo real (usado + merma) y no contra lo entregado, porque
            # lo entregado todavía puede volver.
            "desvio": (usado + merma) - planificado,
            "costo_unitario": float(m.costo_unitario or 0),
            "costo_real": float(m.costo_total_real),
            "proyecto_id": obra.id,
        })

    return filas
