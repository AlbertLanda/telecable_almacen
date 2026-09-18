# -*- coding: utf-8 -*-
"""
Carga un archivo de materiales de SICV.

Es la puerta de entrada que sirve HOY, mientras la API de SICV siga
apagada: alguien exporta el reporte "Registro de materiales" a CSV y lo
carga acá. Cuando la API esté disponible, el cliente HTTP va a llamar a la
misma función `importar_filas()`, así que lo que se valide ahora vale para
después.

    manage.py importar_sicv registro.csv --sede Jauja --desde 2026-09-01 --hasta 2026-09-30
"""
import csv
import json
from datetime import date
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from inventario.models import Sede
from reportes.models import SincronizacionSicv
from reportes.services.sicv import importar_filas, resumen_recibido


def _leer_csv(ruta):
    """
    Lee CSV o TSV, detectando el separador.

    Se prueba utf-8-sig primero porque un CSV exportado desde Excel casi
    siempre trae BOM, y leerlo como utf-8 puro deja la primera cabecera con
    basura adelante y esa columna no mapea nunca.
    """
    for codificacion in ("utf-8-sig", "latin-1"):
        try:
            with open(ruta, encoding=codificacion, newline="") as archivo:
                muestra = archivo.read(8192)
                archivo.seek(0)
                try:
                    dialecto = csv.Sniffer().sniff(muestra, delimiters=";,\t|")
                except csv.Error:
                    dialecto = csv.excel
                    dialecto.delimiter = ";" if muestra.count(";") > muestra.count(",") else ","
                return list(csv.DictReader(archivo, dialect=dialecto))
        except UnicodeDecodeError:
            continue
    raise CommandError("No se pudo leer el archivo ni como UTF-8 ni como Latin-1.")


def _leer_json(ruta):
    with open(ruta, encoding="utf-8") as archivo:
        datos = json.load(archivo)

    # Una API suele envolver las filas en algo: se aceptan las envolturas
    # más comunes para no tener que editar el archivo a mano.
    if isinstance(datos, dict):
        for clave in ("data", "results", "rows", "items", "detalles"):
            if isinstance(datos.get(clave), list):
                return datos[clave]
        raise CommandError(
            "El JSON es un objeto y no se encontró la lista de filas "
            "(se buscó en data, results, rows, items, detalles)."
        )
    if not isinstance(datos, list):
        raise CommandError("El JSON debe ser una lista de filas o un objeto que la contenga.")
    return datos


class Command(BaseCommand):
    help = "Importa un archivo de materiales declarados en SICV (CSV, TSV o JSON)."

    def add_arguments(self, parser):
        parser.add_argument("archivo", help="Ruta del CSV, TSV o JSON exportado de SICV.")
        parser.add_argument("--sede", required=True, help="Sede a la que corresponde el reporte.")
        parser.add_argument("--desde", required=True, help="Inicio del periodo (AAAA-MM-DD).")
        parser.add_argument("--hasta", required=True, help="Fin del periodo (AAAA-MM-DD).")
        parser.add_argument(
            "--simular",
            action="store_true",
            help="Lee el archivo y muestra qué haría, sin guardar nada.",
        )

    def handle(self, *args, **opciones):
        ruta = Path(opciones["archivo"])
        if not ruta.exists():
            raise CommandError(f"No existe el archivo {ruta}.")

        sede = Sede.objects.filter(nombre__iexact=opciones["sede"]).first()
        if not sede:
            disponibles = ", ".join(Sede.objects.values_list("nombre", flat=True))
            raise CommandError(f"No existe la sede {opciones['sede']!r}. Hay: {disponibles}")

        try:
            desde = date.fromisoformat(opciones["desde"])
            hasta = date.fromisoformat(opciones["hasta"])
        except ValueError:
            raise CommandError("Las fechas van en formato AAAA-MM-DD.")

        if desde > hasta:
            raise CommandError("La fecha inicial es posterior a la final.")

        filas = _leer_json(ruta) if ruta.suffix.lower() == ".json" else _leer_csv(ruta)

        if not filas:
            self.stdout.write(self.style.WARNING("El archivo no tiene filas."))
            return

        self.stdout.write(f"Leídas {len(filas)} filas de {ruta.name}.")
        self.stdout.write(f"Columnas detectadas: {', '.join(filas[0].keys())}")

        if opciones["simular"]:
            self._simular(filas)
            return

        sincronizacion = importar_filas(
            filas,
            sede=sede,
            desde=desde,
            hasta=hasta,
            origen=SincronizacionSicv.Origen.ARCHIVO,
        )

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"Importadas {sincronizacion.filas_recibidas} filas "
            f"({sincronizacion.filas_nuevas} nuevas, {sincronizacion.filas_repetidas} ya estaban)."
        ))

        self._informar_contenido(sede)

    # ------------------------------------------------------------------

    def _simular(self, filas):
        from reportes.services.sicv import normalizar_fila

        self.stdout.write("")
        self.stdout.write(self.style.WARNING("SIMULACIÓN: no se guardó nada."))
        self.stdout.write("Así quedarían las primeras filas:")
        for cruda in filas[:5]:
            datos = normalizar_fila(cruda)
            self.stdout.write(
                f"  orden={datos['orden']!r} material={datos['material']!r} "
                f"cant={datos['cantidad']} accion={datos['accion']!r} "
                f"tecnico={datos['tecnico']!r} atencion={datos['atencion']}"
            )

        sin_fecha = sum(1 for f in filas if normalizar_fila(f)["atencion"] is None)
        if sin_fecha:
            self.stdout.write(self.style.WARNING(
                f"  {sin_fecha} fila(s) sin fecha de atención legible: revisá el formato."
            ))

    def _informar_contenido(self, sede):
        """
        Qué nombres trajo el archivo, ordenados por volumen.

        Sirve para revisar de un vistazo que lo importado se parece a lo que
        se esperaba: un material escrito de dos formas distintas, o un
        técnico que no debería estar, se ven acá antes que en la pantalla.
        """
        resumen = resumen_recibido(sede)

        if resumen["materiales"]:
            self.stdout.write("")
            self.stdout.write("Materiales recibidos:")
            for fila in resumen["materiales"][:12]:
                self.stdout.write(
                    f"   {fila['filas']:>5} filas  {fila['total']:>10}  {fila['material']}"
                )

        if resumen["tecnicos"]:
            self.stdout.write("")
            self.stdout.write("Técnicos con material declarado:")
            for fila in resumen["tecnicos"][:12]:
                self.stdout.write(f"   {fila['filas']:>5} filas  {fila['tecnico']}")
