"""
Firma manuscrita en pantalla para documentos del almacén.

No es firma digital con certificado: es una constancia electrónica
(trazo + quién firmó según su sesión + fecha + hash del contenido).

`validar_trazo` está adaptada de Telecable RRHH
(`apps/empleados/firma_manuscrita.py::validar_trazo`).
"""
import base64
import binascii
import hashlib
import json
from datetime import date, datetime, time
from io import BytesIO

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.migrations.recorder import MigrationRecorder
from django.db.models import Q
from django.utils import timezone
from PIL import Image, ImageChops, UnidentifiedImageError

from inventario.models import (
    DocumentoInventario,
    EstadoDocumento,
    FirmaDocumento,
    TipoDocumento,
    TipoRequerimiento,
)

PREFIJO_PNG = "data:image/png;base64,"
MAX_FIRMA = 250_000  # bytes del PNG (≈ 250 KB)

# Las dos partes que firman cada documento:
# RECEPTOR = el técnico (retira en la nota de salida, entrega en la devolución,
#            da conformidad en la liquidación, recibe como responsable de la obra).
# ALMACEN  = el almacenero (despacha, recibe la devolución o liquida).
RECEPTOR = FirmaDocumento.Rol.RECEPTOR
ALMACEN = FirmaDocumento.Rol.ALMACEN

# Documentos que se firman
NOTA_SALIDA = "SAL"   # nota de salida a técnico (REQ local)
DEVOLUCION = "DEV"    # constancia de devolución del técnico (ING)
LIQUIDACION = "LIQ"   # acta de liquidación semanal del técnico (ING LIQ-SEMANAL)
SALIDA_OBRA = "OBRA"  # despacho de materiales a una obra (SAL con referencia = código de obra)
REF_LIQUIDACION = "LIQ-SEMANAL"

# Texto que cada parte acepta al firmar (queda guardado con la firma).
ACEPTACION = {
    (NOTA_SALIDA, RECEPTOR): (
        "Declaro haber revisado la nota de salida y recibido conforme el material "
        "detallado, y lo confirmo con mi firma manuscrita electrónica."
    ),
    (NOTA_SALIDA, ALMACEN): (
        "Declaro haber entregado el material detallado en esta nota de salida, "
        "y lo confirmo con mi firma manuscrita electrónica."
    ),
    (DEVOLUCION, RECEPTOR): (
        "Declaro haber devuelto a almacén el material detallado en esta constancia, "
        "y lo confirmo con mi firma manuscrita electrónica."
    ),
    (DEVOLUCION, ALMACEN): (
        "Declaro haber recibido conforme el material detallado en esta constancia "
        "de devolución, y lo confirmo con mi firma manuscrita electrónica."
    ),
    (LIQUIDACION, RECEPTOR): (
        "Declaro mi conformidad con lo devuelto, la merma y el consumo registrados "
        "en esta liquidación, y lo confirmo con mi firma manuscrita electrónica."
    ),
    (LIQUIDACION, ALMACEN): (
        "Declaro haber liquidado y recibido el material devuelto que figura en esta "
        "liquidación, y lo confirmo con mi firma manuscrita electrónica."
    ),
    (SALIDA_OBRA, RECEPTOR): (
        "Como responsable de la obra, declaro haber recibido conforme el material "
        "de este despacho, y lo confirmo con mi firma manuscrita electrónica."
    ),
    (SALIDA_OBRA, ALMACEN): (
        "Declaro haber entregado el material de este despacho para la obra, "
        "y lo confirmo con mi firma manuscrita electrónica."
    ),
}


def validar_trazo(valor):
    """
    Recibe la firma tal como la manda el navegador
    ("data:image/png;base64,....") y devuelve los bytes de un PNG limpio.

    Lanza ValidationError si no es un PNG, si es muy grande o si está vacía.
    """
    # 1. Forma y tamaño del texto antes de decodificar (base64 ocupa ~4/3).
    if (
        not isinstance(valor, str)
        or not valor.startswith(PREFIJO_PNG)
        or len(valor) > MAX_FIRMA * 4 // 3 + 100
    ):
        raise ValidationError("Dibuja tu firma en el recuadro antes de confirmar.")

    try:
        raw = base64.b64decode(valor[len(PREFIJO_PNG):], validate=True)
        if len(raw) > MAX_FIRMA:
            raise ValidationError("La firma supera el tamaño permitido.")

        with Image.open(BytesIO(raw)) as imagen:
            # 2. Que de verdad sea PNG y con medidas razonables.
            ancho, alto = imagen.size
            if (
                imagen.format != "PNG"
                or not (20 <= ancho <= 1600 and 10 <= alto <= 800)
                or ancho * alto > 1_000_000
            ):
                raise ValidationError("Las dimensiones de la firma no son válidas.")
            imagen.load()

            # 3. Pasar a escala de grises sobre fondo blanco y buscar "tinta"
            #    (píxeles oscuros). getbbox() da el rectángulo que la contiene.
            rgba = imagen.convert("RGBA")
            fondo = Image.new("RGBA", rgba.size, "white")
            fondo.alpha_composite(rgba)
            gris = fondo.convert("L")
            tinta = gris.point(lambda v: 255 if v < 210 else 0)
            caja = tinta.getbbox()
            if (
                not caja
                or sum(tinta.histogram()[1:]) < 40
                or caja[2] - caja[0] < 15
                or caja[3] - caja[1] < 5
            ):
                raise ValidationError(
                    "La firma está vacía o es demasiado pequeña. Vuelve a dibujarla."
                )

            # 4. Dibujar de nuevo solo la tinta (color fijo, fondo transparente),
            #    recortada con un margen. Así se descartan metadatos del archivo.
            limpio = Image.new("RGBA", rgba.size, (20, 30, 45, 0))
            limpio.putalpha(ImageChops.invert(gris))
            caja = (
                max(0, caja[0] - 8),
                max(0, caja[1] - 8),
                min(ancho, caja[2] + 8),
                min(alto, caja[3] + 8),
            )
            salida = BytesIO()
            limpio.crop(caja).save(salida, format="PNG")
            return salida.getvalue()
    except (binascii.Error, UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValidationError("La firma enviada no es un PNG válido.") from exc


# ============================================================
# Nota de salida (SAL) a técnico: firman quien retira y almacén
# ============================================================

def _nombre(usuario):
    if not usuario:
        return ""
    return usuario.get_full_name() or usuario.username


_MIGRACION_FIRMA = ("inventario", "0022_firma_documento")
_inicio_cache = None


def inicio_firmas():
    """
    Desde cuándo se pide firma: el momento en que se aplicó la migración
    que creó la tabla de firmas (es decir, el despliegue de esta función).
    settings.FIRMA_NOTAS_DESDE ("AAAA-MM-DD") permite fijar otra fecha.
    """
    global _inicio_cache
    fijo = getattr(settings, "FIRMA_NOTAS_DESDE", None)
    if fijo:
        return timezone.make_aware(datetime.combine(date.fromisoformat(fijo), time.min))
    if _inicio_cache is None:
        app, nombre = _MIGRACION_FIRMA
        _inicio_cache = (
            MigrationRecorder.Migration.objects.filter(app=app, name=nombre)
            .values_list("applied", flat=True)
            .first()
        )
    return _inicio_cache


def clase_documento(doc):
    """
    Qué documento firmable es: NOTA_SALIDA (despacha un REQ local a un técnico),
    DEVOLUCION (ingreso por devolución de un técnico), LIQUIDACION (liquidación
    semanal de la mochila de un técnico), SALIDA_OBRA (despacho a una obra)
    o None si no se firma.
    """
    if (
        doc.tipo == TipoDocumento.SAL
        and doc.origen_id is not None
        and doc.origen.tipo == TipoDocumento.REQ
        and doc.origen.tipo_requerimiento == TipoRequerimiento.LOCAL
    ):
        return NOTA_SALIDA
    if (
        doc.tipo == TipoDocumento.ING
        and "DEVOLUCION" in (doc.referencia or "").upper()
        and doc.entregado_por_id is not None
    ):
        return DEVOLUCION
    if (
        doc.tipo == TipoDocumento.ING
        and doc.referencia == REF_LIQUIDACION
        and doc.solicitante_id is not None
    ):
        return LIQUIDACION
    if (
        doc.tipo == TipoDocumento.SAL
        and doc.origen_id is None
        and doc.sede_destino_id is None
        and doc.solicitante_id is not None
        and doc.referencia
        and _es_codigo_de_obra(doc.referencia)
    ):
        return SALIDA_OBRA
    return None


def _es_codigo_de_obra(referencia):
    # Import local: proyectos.models importa inventario.models.
    from proyectos.models import Proyecto
    return Proyecto.objects.filter(codigo=referencia).exists()


def obra_del_documento(doc):
    from proyectos.models import Proyecto
    return Proyecto.objects.filter(codigo=doc.referencia).select_related("sede").first()


def requiere_firma(doc):
    """Nota de salida, devolución, liquidación o despacho a obra confirmado, desde el despliegue de las firmas."""
    desde = inicio_firmas()
    return (
        (desde is None or doc.fecha >= desde)
        and doc.estado == EstadoDocumento.CONFIRMADO
        and clase_documento(doc) is not None
    )


def texto_aceptacion(doc, rol):
    return ACEPTACION[(clase_documento(doc), rol)]


def responsable_cuadrilla(doc):
    """Técnico a cargo (Tecnico_Resp). Mismo orden que usa sal_print.html."""
    if doc.solicitante_id:
        return doc.solicitante
    return doc.origen.responsable if doc.origen_id else None


def firmante_receptor(doc):
    """Quien retiró físicamente el material (Resp o Crew). Mismo orden que sal_print.html."""
    if doc.retirado_por_id:
        return doc.retirado_por
    if doc.origen_id and doc.origen.retirado_por_id:
        return doc.origen.retirado_por
    return responsable_cuadrilla(doc)


def firmante_esperado(doc, rol):
    """
    Quién debe firmar cada recuadro.
    Nota de salida: quien retiró y el almacenero que despachó.
    Devolución: el técnico que devolvió y el almacenero que recibió.
    Liquidación: el técnico liquidado y el almacenero que liquidó.
    Despacho a obra: el técnico responsable de la obra (el material se le carga a él,
    aunque lo retire otro) y el almacenero que despachó.
    """
    if rol == RECEPTOR:
        clase = clase_documento(doc)
        if clase == DEVOLUCION:
            return doc.entregado_por
        if clase in (LIQUIDACION, SALIDA_OBRA):
            return doc.solicitante
        return firmante_receptor(doc)
    if rol == ALMACEN:
        return doc.responsable
    return None


def contenido_documento(doc):
    """
    Los datos registrados que muestra la nota de salida, ordenados siempre igual.
    No incluye cantidades devueltas/usadas: cambian después, al liquidar,
    y no forman parte de lo que se entregó.
    """
    if clase_documento(doc) == DEVOLUCION:
        return {
            "tipo": doc.tipo,
            "numero": doc.numero,
            "fecha": doc.fecha.isoformat(),
            "estado": doc.estado,
            "sede_id": doc.sede_id,
            "almacen_id": doc.responsable_id,
            "tecnico_id": doc.entregado_por_id,
            "items": _items_documento(doc),
        }
    if clase_documento(doc) == SALIDA_OBRA:
        return {
            "tipo": doc.tipo,
            "numero": doc.numero,
            "fecha": doc.fecha.isoformat(),
            "estado": doc.estado,
            "obra": doc.referencia,
            "sede_id": doc.sede_id,
            "almacen_id": doc.responsable_id,
            "responsable_obra_id": doc.solicitante_id,
            "retirado_por_id": doc.retirado_por_id,
            "items": _items_documento(doc),
        }
    if clase_documento(doc) == LIQUIDACION:
        # Aquí sí cuentan merma y consumo: son justamente lo que se liquida.
        return {
            "tipo": doc.tipo,
            "numero": doc.numero,
            "fecha": doc.fecha.isoformat(),
            "estado": doc.estado,
            "sede_id": doc.sede_id,
            "sede_origen_id": doc.sede_origen_id,
            "almacen_id": doc.responsable_id,
            "tecnico_id": doc.solicitante_id,
            "items": [
                [it.producto.codigo_interno, int(it.cantidad or 0),
                 int(it.cantidad_merma or 0), int(it.cantidad_usada or 0)]
                for it in doc.items.select_related("producto").order_by("producto__codigo_interno", "id")
            ],
        }
    receptor = firmante_receptor(doc)
    resp = responsable_cuadrilla(doc)
    return {
        "tipo": doc.tipo,
        "numero": doc.numero,
        "fecha": doc.fecha.isoformat(),
        "estado": doc.estado,
        "sede_id": doc.sede_id,
        "almacen_id": doc.responsable_id,
        "responsable_cuadrilla_id": resp.id if resp else None,
        "retirado_por_id": receptor.id if receptor else None,
        "items": _items_documento(doc),
    }


def _items_documento(doc):
    return [
        [it.producto.codigo_interno, int(it.cantidad)]
        for it in doc.items.select_related("producto").order_by("producto__codigo_interno")
    ]


def hash_documento(doc):
    """Huella SHA-256 del contenido: si cambia un solo dato, cambia toda la huella."""
    texto = json.dumps(contenido_documento(doc), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def _firma_de(doc, rol):
    # Usa doc.firmas.all() para aprovechar prefetch_related cuando se listan muchas notas.
    return next((f for f in doc.firmas.all() if f.rol == rol), None)


def estado_firma(doc, rol=RECEPTOR):
    """
    Estado de UN recuadro: "NO_APLICA", "PENDIENTE", "FIRMADO" o "CAMBIADO"
    (firmado, pero el documento ya no coincide con lo que se firmó).
    """
    if not requiere_firma(doc):
        return "NO_APLICA"
    firma = _firma_de(doc, rol)
    if not firma:
        return "PENDIENTE"
    return "FIRMADO" if firma.sha256_contenido == hash_documento(doc) else "CAMBIADO"


def estado_documento(doc):
    """
    Resumen de las dos firmas: "NO_APLICA", "PENDIENTE" (ninguna),
    "PARCIAL" (1 de 2), "FIRMADO" (las dos) o "CAMBIADO" (alguna ya no coincide).
    """
    if not requiere_firma(doc):
        return "NO_APLICA"
    firmas = [_firma_de(doc, rol) for rol in (RECEPTOR, ALMACEN)]
    hechas = [f for f in firmas if f]
    if not hechas:
        return "PENDIENTE"
    sha = hash_documento(doc)
    if any(f.sha256_contenido != sha for f in hechas):
        return "CAMBIADO"
    return "FIRMADO" if len(hechas) == 2 else "PARCIAL"


def registrar_firma(*, doc_id, usuario, rol, trazo, sha256_visto, acepto):
    """
    Guarda la firma de una de las partes (RECEPTOR o ALMACEN).

    - El firmante es el usuario de la sesión (no un dato del formulario).
    - sha256_visto es la huella del documento que tenía en pantalla:
      si no coincide con la actual, el documento cambió y debe revisarlo de nuevo.
    - acepto: marcó la declaración de ACEPTACION para ese documento y rol.
    """
    if rol not in (RECEPTOR, ALMACEN):
        raise ValidationError("Tipo de firma no válido.")
    if not acepto:
        raise ValidationError("Marca la casilla de conformidad antes de firmar.")
    png = validar_trazo(trazo)

    with transaction.atomic():
        # select_for_update bloquea la fila mientras firmamos (evita doble clic).
        # of=("self",): bloquea solo el documento; PostgreSQL no permite
        # bloquear las tablas unidas que pueden venir vacías (retirado_por, etc.).
        doc = (
            DocumentoInventario.objects.select_for_update(of=("self",))
            .select_related("origen", "solicitante", "retirado_por", "responsable", "sede", "entregado_por")
            .get(id=doc_id)
        )
        if not requiere_firma(doc):
            raise ValidationError("Este documento no requiere firma.")
        clase = clase_documento(doc)

        firmante = firmante_esperado(doc, rol)
        if not firmante or firmante.id != usuario.id:
            if clase == DEVOLUCION:
                if rol == RECEPTOR:
                    raise PermissionDenied("Solo el técnico que devolvió el material puede firmar esta constancia.")
                raise PermissionDenied("Solo el almacenero que recibió la devolución puede firmar esta constancia.")
            if clase == SALIDA_OBRA:
                if rol == RECEPTOR:
                    raise PermissionDenied("Solo el técnico responsable de la obra puede firmar este despacho.")
                raise PermissionDenied("Solo el almacenero que despachó puede firmar este despacho.")
            if clase == LIQUIDACION:
                if rol == RECEPTOR:
                    raise PermissionDenied("Solo el técnico liquidado puede firmar esta liquidación.")
                raise PermissionDenied("Solo el almacenero que liquidó puede firmar esta liquidación.")
            if rol == RECEPTOR:
                raise PermissionDenied("Solo quien retiró el material puede firmar esta nota.")
            raise PermissionDenied("Solo el almacenero que despachó puede firmar esta nota.")

        if doc.firmas.filter(rol=rol).exists():
            raise ValidationError("Ya firmaste este documento.")

        sha = hash_documento(doc)
        if sha256_visto != sha:
            raise ValidationError("El documento cambió. Vuelve a abrirlo, revísalo y firma de nuevo.")

        resp = responsable_cuadrilla(doc) if clase == NOTA_SALIDA else None
        perfil = getattr(usuario, "profile", None)
        sede = perfil.get_sede_operativa() if perfil else None
        if rol == ALMACEN:
            condicion = "Almacén"
        elif clase in (DEVOLUCION, LIQUIDACION):
            condicion = "Técnico"
        elif clase == SALIDA_OBRA:
            condicion = "Responsable de obra"
        else:
            condicion = "Responsable" if resp and resp.id == usuario.id else "Crew"
        datos = {
            "nombre": _nombre(usuario),
            "usuario": usuario.username,
            "sede": sede.nombre if sede else "",
            "condicion": condicion,
            "responsable_cuadrilla": _nombre(resp),
            "documento": doc.numero,
            "aceptacion": ACEPTACION[(clase, rol)],
        }
        try:
            return FirmaDocumento.objects.create(
                documento=doc,
                rol=rol,
                firmante=usuario,
                trazo_png=png,
                datos_firmante=datos,
                sha256_contenido=sha,
                firmado_en=timezone.now(),
            )
        except IntegrityError:
            # La regla única de la base (documento + rol) atrapó una segunda firma.
            raise ValidationError("Ya firmaste este documento.")


def _notas_desde_despliegue():
    """Notas de salida, devoluciones, liquidaciones y despachos a obra que pueden requerir firma."""
    from proyectos.models import Proyecto
    qs = (
        DocumentoInventario.objects.filter(estado=EstadoDocumento.CONFIRMADO)
        .filter(
            Q(
                tipo=TipoDocumento.SAL,
                origen__tipo=TipoDocumento.REQ,
                origen__tipo_requerimiento=TipoRequerimiento.LOCAL,
            )
            | Q(
                tipo=TipoDocumento.ING,
                referencia__icontains="DEVOLUCION",
                entregado_por__isnull=False,
            )
            | Q(
                tipo=TipoDocumento.ING,
                referencia=REF_LIQUIDACION,
                solicitante__isnull=False,
            )
            | Q(
                tipo=TipoDocumento.SAL,
                origen__isnull=True,
                sede_destino__isnull=True,
                solicitante__isnull=False,
                referencia__in=Proyecto.objects.values("codigo"),
            )
        )
        .select_related("origen", "sede", "solicitante", "retirado_por", "responsable", "entregado_por")
        .prefetch_related("firmas")
        .order_by("-fecha")
    )
    desde = inicio_firmas()
    return qs.filter(fecha__gte=desde) if desde else qs


def notas_para_firmar(usuario, rol=RECEPTOR):
    """
    Notas de salida, devoluciones y liquidaciones donde a `usuario` le toca firmar en el recuadro `rol`.
    Devuelve (pendientes, firmadas). Solo notas desde inicio_firmas(),
    para no pedir firma de notas antiguas (anteriores a esta función).
    """
    candidatas = _notas_desde_despliegue()
    if rol == RECEPTOR:
        candidatas = candidatas.filter(
            Q(retirado_por=usuario)
            | Q(retirado_por__isnull=True, origen__retirado_por=usuario)
            | Q(retirado_por__isnull=True, origen__retirado_por__isnull=True, solicitante=usuario)
            | Q(tipo=TipoDocumento.ING, entregado_por=usuario)
            | Q(tipo=TipoDocumento.ING, referencia=REF_LIQUIDACION, solicitante=usuario)
            | Q(tipo=TipoDocumento.SAL, origen__isnull=True, solicitante=usuario)
        )
    else:
        candidatas = candidatas.filter(responsable=usuario)

    pendientes, firmadas = [], []
    for doc in candidatas:
        # Doble chequeo con la misma regla que se usa al guardar la firma.
        firmante = firmante_esperado(doc, rol)
        if not firmante or firmante.id != usuario.id:
            continue
        firma = _firma_de(doc, rol)
        if firma is None:
            pendientes.append(doc)
        else:
            doc.mi_firma = firma
            firmadas.append(doc)
    return pendientes, firmadas


def notas_de_mi_equipo(usuario):
    """
    Para el Tecnico_Resp: notas de salida a su cargo que retiró otra persona
    de su cuadrilla (el Crew). Solo consulta: el Resp no firma por ellos.
    Cada nota trae `estado_crew` ("PENDIENTE", "FIRMADO" o "CAMBIADO").
    """
    # Solo notas de salida de un pedido (REQ): en los despachos a obra firma el propio Resp.
    candidatas = _notas_desde_despliegue().filter(tipo=TipoDocumento.SAL, origen__isnull=False).filter(
        Q(solicitante=usuario) | Q(solicitante__isnull=True, origen__responsable=usuario)
    )
    notas = []
    for doc in candidatas:
        resp = responsable_cuadrilla(doc)
        receptor = firmante_receptor(doc)
        if not resp or resp.id != usuario.id or not receptor or receptor.id == usuario.id:
            continue
        doc.receptor = receptor
        doc.estado_crew = estado_firma(doc, RECEPTOR)
        notas.append(doc)
    return notas


def firmas_para_mostrar(doc):
    """
    Datos para pintar cada recuadro de la nota impresa:
    {"requiere": bool, "RECEPTOR": {...} o None, "ALMACEN": {...} o None}.
    La imagen va incrustada (data URI): no necesita una URL pública aparte.
    """
    resultado = {"requiere": requiere_firma(doc), RECEPTOR: None, ALMACEN: None}
    if not resultado["requiere"]:
        return resultado
    sha = hash_documento(doc)
    for rol in (RECEPTOR, ALMACEN):
        firma = _firma_de(doc, rol)
        if firma:
            datos = firma.datos_firmante or {}
            resultado[rol] = {
                "imagen": PREFIJO_PNG + base64.b64encode(bytes(firma.trazo_png)).decode("ascii"),
                "nombre": datos.get("nombre") or firma.firmante.username,
                "condicion": datos.get("condicion", ""),
                "fecha": firma.firmado_en,
                "vigente": firma.sha256_contenido == sha,
            }
    return resultado
