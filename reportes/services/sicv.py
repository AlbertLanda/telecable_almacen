# -*- coding: utf-8 -*-
"""
Ingesta de los materiales declarados en campo (SICV).

El diseño tiene una costura deliberada: `importar_filas()` recibe una lista
de diccionarios, **no** una respuesta HTTP. Así el mismo camino de entrada
sirve para el cliente de la API cuando SICV esté disponible y para un
archivo exportado a mano hoy, que es lo único que se puede probar mientras
SICV_SYNC_ENABLED esté apagado.

Acá no se cruza nada contra nuestro catálogo: se recibe y se conserva. La
regla es §8.1 del contrato: ninguna fila se descarta, ni siquiera si no se
entiende ninguno de sus nombres. Si se descartaran, el material declarado
en campo nunca cuadraría contra el almacén y nadie podría explicar la
diferencia.
"""
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Count, Sum
from django.utils import timezone

from ..models import DetalleMaterialSicv, SincronizacionSicv

# Nombres de columna que se aceptan para cada campo. SICV podría entregar
# el reporte con encabezados en español (como el que se ve hoy) o con
# claves técnicas; se admiten ambos para no tener que tocar código cuando
# se defina el formato definitivo de la API.
ALIAS_COLUMNAS = {
    "id_origen": ("id", "id_origen", "row_id"),
    "orden": ("orden", "nro_orden", "numero_orden", "order"),
    "tipo_orden": ("tipo", "tipo_orden", "tipo_servicio"),
    "emision": ("emision", "emisión", "fecha_emision"),
    "codigo_abonado": ("codigo", "código", "codigo_abonado", "cod_abonado"),
    "abonado": ("abonado", "cliente", "suscriptor"),
    "direccion": ("direccion", "dirección", "domicilio"),
    "material": ("material", "descripcion_material", "producto"),
    "cantidad": ("cantidad", "cant", "qty"),
    "accion": ("accion", "acción", "action"),
    "mac": ("mac", "mac_address", "serie"),
    "atencion": ("atencion", "atención", "fecha_atencion"),
    "situacion": ("situacion", "situación", "estado"),
    "tecnico": ("tecnico", "técnico", "tecnico_nombre", "responsable"),
}

# Formatos de fecha que se intentan, en orden. SICV muestra dd/mm/aaaa
# hh:mm en el reporte; los otros cubren una API que devuelva ISO.
FORMATOS_FECHA = (
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
)

# Largos máximos de cada campo de texto, para recortar antes de guardar.
# Un nombre más largo de lo previsto no debe tumbar la importación entera.
LARGOS = {
    "id_origen": 64,
    "orden": 32,
    "tipo_orden": 80,
    "codigo_abonado": 40,
    "abonado": 200,
    "direccion": 255,
    "material": 200,
    "accion": 40,
    "mac": 40,
    "situacion": 40,
    "tecnico": 200,
}

CAMPOS_FECHA = ("emision", "atencion")


def _valor(fila, campo):
    """Busca el campo por cualquiera de sus alias, sin distinguir mayúsculas."""
    normalizada = {str(k).strip().lower(): v for k, v in fila.items()}
    for alias in ALIAS_COLUMNAS[campo]:
        if alias in normalizada:
            valor = normalizada[alias]
            return "" if valor is None else str(valor).strip()
    return ""


def _a_fecha(texto):
    """
    Convierte a datetime con zona, o devuelve None.

    Una fecha ilegible no invalida la fila: el resto de los datos sirve
    igual, y perder la orden entera por un formato raro sería peor que
    tenerla sin fecha.
    """
    if not texto:
        return None

    limpio = texto.replace("T", " ").strip()
    if limpio.endswith("Z"):
        limpio = limpio[:-1].strip()

    for formato in FORMATOS_FECHA:
        try:
            crudo = datetime.strptime(limpio, formato)
        except ValueError:
            continue
        if timezone.is_naive(crudo):
            return timezone.make_aware(crudo)
        return crudo
    return None


def _a_decimal(texto):
    if not texto:
        return Decimal("0")
    try:
        # SICV manda "19.00"; un export regionalizado podría mandar "19,00".
        return Decimal(texto.replace(",", "."))
    except (InvalidOperation, AttributeError):
        return Decimal("0")


def normalizar_fila(fila):
    """Pasa una fila cruda a las claves del modelo, recortada a su largo."""
    datos = {campo: _valor(fila, campo)[:largo] for campo, largo in LARGOS.items()}
    datos.update({campo: _a_fecha(_valor(fila, campo)) for campo in CAMPOS_FECHA})
    datos["cantidad"] = _a_decimal(_valor(fila, "cantidad"))
    return datos


@transaction.atomic
def importar_filas(filas, *, sede, desde, hasta, origen=SincronizacionSicv.Origen.ARCHIVO,
                   usuario=None):
    """
    Guarda un lote de filas de SICV y devuelve la sincronización.

    Reimportar el mismo periodo es seguro: cada fila se identifica por su
    huella, así que la segunda pasada actualiza en vez de duplicar. Eso
    permite volver a traer un rango cuando SICV corrige algo.
    """
    sincronizacion = SincronizacionSicv.objects.create(
        sede=sede,
        desde=desde,
        hasta=hasta,
        origen=origen,
        ejecutada_por=usuario,
    )

    nuevas = repetidas = 0

    for cruda in filas:
        datos = normalizar_fila(cruda)

        # Sin material ni orden no hay fila que guardar: es ruido del
        # archivo (una línea en blanco, un pie de página).
        if not datos["material"] and not datos["orden"]:
            continue

        detalle = DetalleMaterialSicv(sincronizacion=sincronizacion, sede=sede, **datos)
        huella = detalle.calcular_huella()

        existente = DetalleMaterialSicv.objects.filter(huella=huella).first()
        if existente:
            for campo, valor in datos.items():
                setattr(existente, campo, valor)
            existente.sincronizacion = sincronizacion
            existente.save()
            repetidas += 1
        else:
            detalle.save()
            nuevas += 1

    sincronizacion.filas_recibidas = nuevas + repetidas
    sincronizacion.filas_nuevas = nuevas
    sincronizacion.filas_repetidas = repetidas
    sincronizacion.estado = SincronizacionSicv.Estado.COMPLETADA
    sincronizacion.terminada_en = timezone.now()
    sincronizacion.save()

    return sincronizacion


def resumen_recibido(sede=None):
    """
    Qué nombres llegaron y cuánto pesa cada uno.

    Sirve para dos cosas: ver de un vistazo qué materiales y técnicos usa
    SICV realmente, y tener la lista ordenada por volumen para cuando se
    definan las equivalencias contra nuestro catálogo.
    """
    detalles = DetalleMaterialSicv.objects.all()
    if sede:
        detalles = detalles.filter(sede=sede)

    return {
        "materiales": list(
            detalles.exclude(material="")
            .values("material")
            .annotate(filas=Count("id"), total=Sum("cantidad"))
            .order_by("-filas")
        ),
        "tecnicos": list(
            detalles.exclude(tecnico="")
            .values("tecnico")
            .annotate(filas=Count("id"))
            .order_by("-filas")
        ),
    }
