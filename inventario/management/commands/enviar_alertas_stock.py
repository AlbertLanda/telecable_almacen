from django.core.management.base import BaseCommand
from django.conf import settings

from inventario.models import Sede
from inventario.services.alertas_stock_service import enviar_alerta_stock_por_correo


class Command(BaseCommand):
    help = "Envía alertas de stock crítico por correo."

    def add_arguments(self, parser):
        parser.add_argument(
            "--sede-id",
            type=int,
            default=None,
            help="ID de la sede a revisar. Si no se envía, revisa todas las sedes activas."
        )

    def handle(self, *args, **options):
        destinatarios = getattr(settings, "ALERTAS_STOCK_EMAILS", [])

        if not destinatarios:
            self.stdout.write(self.style.WARNING("No hay destinatarios configurados en ALERTAS_STOCK_EMAILS."))
            return

        sedes = Sede.objects.filter(activo=True)

        if options["sede_id"]:
            sedes = sedes.filter(id=options["sede_id"])

        enviados = 0

        for sede in sedes:
            enviado = enviar_alerta_stock_por_correo(sede, destinatarios)

            if enviado:
                enviados += 1
                self.stdout.write(self.style.SUCCESS(f"Alerta enviada para {sede.nombre}"))
            else:
                self.stdout.write(f"Sin stock crítico en {sede.nombre}")

        self.stdout.write(self.style.SUCCESS(f"Proceso terminado. Correos enviados: {enviados}"))