# -*- coding: utf-8 -*-
"""
Sonda para descubrir cómo responde SICV.

No guarda nada: pega en el endpoint y muestra en crudo lo que conteste. Es
la herramienta para completar los cuatro FALTA_SICV de sicv_cliente.py el
día que haya host y token, sin tener que adivinar ni escribir código a
ciegas.

    manage.py probar_sicv --sede SEDE-JAUJA --desde 2026-09-01 --hasta 2026-09-30

Si la ruta por defecto no es la correcta, se prueba otra sin tocar código:

    manage.py probar_sicv ... --endpoint /v1/ordenes/materiales
"""
import json
from datetime import date, timedelta

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from inventario.models import Sede
from reportes.services.sicv import ALIAS_COLUMNAS, normalizar_fila
from reportes.services.sicv_cliente import _encabezados, _parametros, extraer_filas


class Command(BaseCommand):
    help = "Consulta SICV y muestra la respuesta cruda. No guarda nada."

    def add_arguments(self, parser):
        parser.add_argument("--sede", help="Sede a consultar. Por defecto, la CENTRAL.")
        parser.add_argument("--desde", help="Inicio (AAAA-MM-DD). Por defecto, hace 7 días.")
        parser.add_argument("--hasta", help="Fin (AAAA-MM-DD). Por defecto, hoy.")
        parser.add_argument("--endpoint", help="Ruta a probar, si no es la configurada.")

    def handle(self, *args, **opciones):
        if not settings.SICV_URL:
            raise CommandError("Falta SICV_URL en el .env.")
        if not settings.SICV_TOKEN:
            self.stdout.write(self.style.WARNING(
                "SICV_TOKEN vacío: si la API pide autenticación, va a responder 401."
            ))

        sede = self._sede(opciones["sede"])
        hoy = timezone.localdate()
        desde = date.fromisoformat(opciones["desde"]) if opciones["desde"] else hoy - timedelta(days=7)
        hasta = date.fromisoformat(opciones["hasta"]) if opciones["hasta"] else hoy

        endpoint = opciones["endpoint"] or getattr(settings, "SICV_ENDPOINT", "/api/materiales")
        url = f"{settings.SICV_URL}{endpoint}"
        parametros = _parametros(sede, desde, hasta)

        self.stdout.write(self.style.MIGRATE_HEADING("CONSULTA"))
        self.stdout.write(f"  URL        {url}")
        self.stdout.write(f"  Parámetros {parametros}")
        self.stdout.write(f"  Auth       {'Bearer ' + settings.SICV_TOKEN[:6] + '…' if settings.SICV_TOKEN else '(sin token)'}")

        try:
            respuesta = requests.get(
                url, headers=_encabezados(), params=parametros, timeout=settings.SICV_TIMEOUT
            )
        except requests.RequestException as e:
            raise CommandError(f"No se pudo conectar: {e}")

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("RESPUESTA"))
        self.stdout.write(f"  HTTP        {respuesta.status_code}")
        self.stdout.write(f"  Tipo        {respuesta.headers.get('Content-Type', '(sin Content-Type)')}")
        self.stdout.write(f"  Tamaño      {len(respuesta.content)} bytes")

        if not respuesta.ok:
            self.stdout.write("")
            self.stdout.write(self.style.ERROR("  Cuerpo (primeros 600 caracteres):"))
            self.stdout.write(f"  {(respuesta.text or '').strip()[:600]}")
            self._pistas(respuesta.status_code)
            return

        try:
            cuerpo = respuesta.json()
        except ValueError:
            self.stdout.write("")
            self.stdout.write(self.style.ERROR("  No es JSON. Empieza con:"))
            self.stdout.write(f"  {(respuesta.text or '').strip()[:400]!r}")
            return

        self._describir(cuerpo)

    # ------------------------------------------------------------------

    def _sede(self, nombre):
        if nombre:
            sede = Sede.objects.filter(nombre__iexact=nombre).first()
            if not sede:
                raise CommandError(f"No existe la sede {nombre!r}.")
            return sede

        sede = Sede.objects.filter(tipo=Sede.CENTRAL).first() or Sede.objects.first()
        if not sede:
            raise CommandError("No hay ninguna sede cargada.")
        return sede

    def _describir(self, cuerpo):
        """Dice qué forma tiene la respuesta y si el importador la entiende."""
        self.stdout.write("")

        if isinstance(cuerpo, dict):
            self.stdout.write(f"  Es un objeto con las claves: {', '.join(sorted(cuerpo))}")

        try:
            filas = extraer_filas(cuerpo)
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"  {e}"))
            self.stdout.write("  Ajustar extraer_filas() en sicv_cliente.py (FALTA_SICV 4).")
            return

        self.stdout.write(self.style.SUCCESS(f"  Se encontraron {len(filas)} filas."))

        if not filas:
            self.stdout.write("  El periodo vino vacío: probá con otro rango.")
            return

        primera = filas[0]
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("PRIMERA FILA, TAL COMO LLEGA"))
        self.stdout.write(json.dumps(primera, indent=2, ensure_ascii=False, default=str))

        self._verificar_columnas(primera)

    def _verificar_columnas(self, fila):
        """
        Qué campos reconoce el importador y cuáles no.

        Es lo que decide si hace falta tocar ALIAS_COLUMNAS o si la
        respuesta ya entra tal cual.
        """
        recibidas = {str(k).strip().lower() for k in fila}

        reconocidas, faltantes = [], []
        for campo, alias in ALIAS_COLUMNAS.items():
            coincide = next((a for a in alias if a in recibidas), None)
            (reconocidas if coincide else faltantes).append(
                f"{campo} <- {coincide}" if coincide else campo
            )

        usadas = {a for alias in ALIAS_COLUMNAS.values() for a in alias} & recibidas
        sobrantes = sorted(recibidas - usadas)

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("MAPEO AL MODELO"))
        for linea in reconocidas:
            self.stdout.write(self.style.SUCCESS(f"  OK    {linea}"))
        for campo in faltantes:
            self.stdout.write(self.style.WARNING(f"  falta {campo}"))

        if sobrantes:
            self.stdout.write("")
            self.stdout.write(f"  Columnas que SICV manda y el importador ignora: {', '.join(sobrantes)}")

        if faltantes:
            self.stdout.write("")
            self.stdout.write(
                "  Para los que faltan, agregar su nombre a ALIAS_COLUMNAS en "
                "services/sicv.py. No hace falta tocar el modelo."
            )

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("CÓMO QUEDARÍA GUARDADA"))
        datos = normalizar_fila(fila)
        for campo, valor in datos.items():
            marca = "  " if valor not in ("", None) else " ·"
            self.stdout.write(f"{marca} {campo:<16} {valor!r}")

    def _pistas(self, codigo):
        pistas = {
            401: "Token inválido o esquema de auth distinto: revisar _encabezados() (FALTA_SICV 2).",
            403: "El token no tiene permiso sobre ese recurso.",
            404: "La ruta no existe: probar otra con --endpoint (FALTA_SICV 1).",
            422: "Los parámetros no son los que espera: revisar _parametros() (FALTA_SICV 3).",
        }
        if codigo in pistas:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING(f"  {pistas[codigo]}"))
