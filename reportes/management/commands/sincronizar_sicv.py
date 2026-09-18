# -*- coding: utf-8 -*-
"""
Trae de SICV el material declarado en un periodo.

    manage.py sincronizar_sicv --sede SEDE-JAUJA --desde 2026-09-01 --hasta 2026-09-30

Es el mismo camino que usa `importar_sicv` con un archivo: lo único que
cambia es de dónde salen las filas. Por eso lo que ya se probó cargando un
CSV vale igual acá.

Pensado para correr por cron una vez que SICV_SYNC_ENABLED esté prendido.
"""
from datetime import date

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from inventario.models import Sede
from reportes.models import SincronizacionSicv
from reportes.services.sicv import importar_filas, resumen_recibido
from reportes.services.sicv_cliente import ErrorSicv, consultar


class Command(BaseCommand):
    help = "Sincroniza con la API de SICV el material declarado en campo."

    def add_arguments(self, parser):
        parser.add_argument("--sede", required=True, help="Sede a sincronizar.")
        parser.add_argument("--desde", required=True, help="Inicio del periodo (AAAA-MM-DD).")
        parser.add_argument("--hasta", required=True, help="Fin del periodo (AAAA-MM-DD).")
        parser.add_argument(
            "--forzar",
            action="store_true",
            help="Corre aunque SICV_SYNC_ENABLED esté apagado (para probar).",
        )

    def handle(self, *args, **opciones):
        if not settings.SICV_SYNC_ENABLED and not opciones["forzar"]:
            self.stdout.write(self.style.WARNING(
                "SICV_SYNC_ENABLED está apagado. La sincronización no corre.\n"
                "Para probar sin prenderlo: agregá --forzar."
            ))
            return

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

        self.stdout.write(f"Consultando SICV: {sede.nombre} · {desde} a {hasta}…")

        try:
            filas = consultar(sede, desde, hasta)
        except ErrorSicv as e:
            # La corrida fallida se guarda igual. §8.3: "SICV no respondió"
            # tiene que poder distinguirse de "no hubo movimientos", y eso
            # solo se logra si queda registro de que se intentó.
            SincronizacionSicv.objects.create(
                sede=sede,
                desde=desde,
                hasta=hasta,
                origen=SincronizacionSicv.Origen.API,
                estado=SincronizacionSicv.Estado.FALLIDA,
                detalle_error=str(e),
                terminada_en=timezone.now(),
            )
            raise CommandError(str(e))

        if not filas:
            # Una corrida sin filas NO es un error: puede que esa semana no
            # se haya declarado material. Queda registrada para que la
            # pantalla pueda decir "se consultó y no había nada".
            SincronizacionSicv.objects.create(
                sede=sede,
                desde=desde,
                hasta=hasta,
                origen=SincronizacionSicv.Origen.API,
                estado=SincronizacionSicv.Estado.COMPLETADA,
                terminada_en=timezone.now(),
            )
            self.stdout.write(self.style.WARNING("SICV no devolvió filas para ese periodo."))
            return

        self.stdout.write(f"Recibidas {len(filas)} filas.")
        self.stdout.write(f"Columnas: {', '.join(sorted(filas[0].keys()))}")

        sincronizacion = importar_filas(
            filas,
            sede=sede,
            desde=desde,
            hasta=hasta,
            origen=SincronizacionSicv.Origen.API,
        )

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"Guardadas {sincronizacion.filas_recibidas} filas "
            f"({sincronizacion.filas_nuevas} nuevas, "
            f"{sincronizacion.filas_repetidas} ya estaban)."
        ))

        resumen = resumen_recibido(sede)
        if resumen["materiales"]:
            self.stdout.write("")
            self.stdout.write("Materiales más frecuentes:")
            for fila in resumen["materiales"][:8]:
                self.stdout.write(
                    f"   {fila['filas']:>5} filas  {fila['total']:>10}  {fila['material']}"
                )
