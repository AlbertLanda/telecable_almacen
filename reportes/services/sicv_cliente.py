# -*- coding: utf-8 -*-
"""
Cliente HTTP de SICV.

Es la única pieza que conoce la API del otro sistema. Todo lo demás -el
modelo, la huella, la pantalla- trabaja sobre listas de diccionarios y no
sabe de dónde salieron, así que cuando SICV defina su endpoint solo hay que
tocar este archivo.

Lo que falta para que funcione está marcado con FALTA_SICV. Son cuatro
cosas, y ninguna se puede adivinar:

  1. La ruta del endpoint (hoy configurable con SICV_ENDPOINT).
  2. Cómo viaja el token: encabezado Bearer, X-API-Key, o parámetro.
  3. Cómo se nombran el periodo y la sede en la consulta.
  4. Dónde vienen las filas en la respuesta y cómo se llaman sus campos.

Mientras tanto el comando `probar_sicv` sirve para descubrirlas: pega en el
endpoint y muestra en crudo lo que conteste.
"""
import requests
from django.conf import settings

# Envolturas que suele usar una API para devolver una lista. Se prueban en
# orden; si ninguna aparece, se informa qué claves trajo la respuesta en vez
# de fallar con un mensaje que no dice nada.
ENVOLTURAS = ("data", "results", "rows", "items", "detalles", "registros")


class ErrorSicv(Exception):
    """
    Falla de la integración, con un mensaje que se pueda guardar.

    El texto termina en `SincronizacionSicv.detalle_error` y se muestra en
    pantalla: §8.3 pide que "SICV no respondió" se distinga de "no hubo
    movimientos", y eso solo se logra si el error queda escrito.
    """


def _configurado():
    return bool(settings.SICV_URL and settings.SICV_TOKEN)


def _encabezados():
    """
    FALTA_SICV (2): el esquema de autenticación.

    Se asume Bearer, que es lo más común. Si SICV usa otra cosa -X-API-Key,
    Basic, un parámetro en la URL- se cambia acá y nada más.
    """
    return {
        "Authorization": f"Bearer {settings.SICV_TOKEN}",
        "Accept": "application/json",
    }


def _parametros(sede, desde, hasta):
    """
    FALTA_SICV (3): los nombres de los parámetros y el formato de fecha.

    Se asume ISO (AAAA-MM-DD) y que la sede va por su nombre. Si SICV
    espera un código propio, hará falta guardarlo en el modelo Sede o
    mapearlo acá.
    """
    return {
        "desde": desde.isoformat(),
        "hasta": hasta.isoformat(),
        "sede": sede.nombre,
    }


def extraer_filas(cuerpo):
    """
    FALTA_SICV (4): de dónde salen las filas en la respuesta.

    Cubre los dos casos previsibles -una lista pelada, o una lista adentro
    de alguna envoltura común-. Si SICV devuelve otra forma, este es el
    único lugar a tocar.

    No se renombra ningún campo: de eso se encarga ALIAS_COLUMNAS en
    sicv.py, que ya acepta los encabezados del reporte y varias claves
    técnicas.
    """
    if isinstance(cuerpo, list):
        return cuerpo

    if isinstance(cuerpo, dict):
        for clave in ENVOLTURAS:
            if isinstance(cuerpo.get(clave), list):
                return cuerpo[clave]

        raise ErrorSicv(
            "La respuesta es un objeto y no se encontró la lista de filas. "
            f"Claves recibidas: {', '.join(sorted(cuerpo)) or '(ninguna)'}. "
            "Ajustar extraer_filas() en sicv_cliente.py."
        )

    raise ErrorSicv(f"Respuesta inesperada de SICV: {type(cuerpo).__name__}.")


def consultar(sede, desde, hasta):
    """
    Pide a SICV el material declarado en un periodo y devuelve las filas.

    Devuelve una lista de diccionarios -lo mismo que leería un archivo-, así
    que el importador no distingue si vino de acá o de un CSV.
    """
    if not _configurado():
        raise ErrorSicv(
            "Falta configurar SICV_URL y SICV_TOKEN en el .env."
        )

    # FALTA_SICV (1): la ruta. Configurable para no tener que redeployar
    # cuando SICV la confirme.
    endpoint = getattr(settings, "SICV_ENDPOINT", "/api/materiales")
    url = f"{settings.SICV_URL}{endpoint}"

    try:
        respuesta = requests.get(
            url,
            headers=_encabezados(),
            params=_parametros(sede, desde, hasta),
            timeout=settings.SICV_TIMEOUT,
        )
    except requests.Timeout:
        raise ErrorSicv(
            f"SICV no respondió en {settings.SICV_TIMEOUT} segundos ({url})."
        )
    except requests.RequestException as e:
        raise ErrorSicv(f"No se pudo conectar con SICV ({url}): {e}")

    if respuesta.status_code == 401 or respuesta.status_code == 403:
        raise ErrorSicv(
            f"SICV rechazó las credenciales (HTTP {respuesta.status_code}). "
            "Revisar SICV_TOKEN y el esquema de autenticación en _encabezados()."
        )

    if not respuesta.ok:
        # Se recorta el cuerpo: un HTML de error entero no aporta y llena la
        # columna de la base.
        detalle = (respuesta.text or "").strip()[:300]
        raise ErrorSicv(f"SICV respondió HTTP {respuesta.status_code}. {detalle}")

    try:
        cuerpo = respuesta.json()
    except ValueError:
        muestra = (respuesta.text or "").strip()[:200]
        raise ErrorSicv(f"SICV no devolvió JSON. Empieza con: {muestra!r}")

    return extraer_filas(cuerpo)
