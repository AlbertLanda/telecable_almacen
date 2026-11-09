"""Registra una conciliación ya realizada; nunca cambia cantidades ni custodias."""
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from proyectos.models import Proyecto


class Command(BaseCommand):
    help = "Revisa el bloqueo de una obra antigua; solo lectura salvo --confirmar. No modifica stock."

    def add_arguments(self, parser):
        parser.add_argument("--codigo", required=True, help="Código exacto de la obra.")
        parser.add_argument("--confirmar", action="store_true")
        parser.add_argument("--usuario", help="Superusuario activo que confirma la revisión.")
        parser.add_argument("--motivo", help="Evidencia o referencia de la conciliación realizada por separado.")

    def handle(self, *args, **options):
        with transaction.atomic():
            qs = Proyecto.objects
            if options["confirmar"]:
                qs = qs.select_for_update()
            try:
                proyecto = qs.get(codigo=options["codigo"])
            except Proyecto.DoesNotExist as exc:
                raise CommandError("No existe una obra con ese código.") from exc

            if not options["confirmar"]:
                self.stdout.write(
                    f"{proyecto.codigo}: cierre registrado={proyecto.cierre_registrado}; "
                    f"mochila revisada={proyecto.mochila_antigua_conciliada}; "
                    f"requiere conciliación={proyecto.requiere_conciliacion_mochila}. "
                    "Solo lectura. La confirmación exige una conciliación previa independiente; no ajusta stock."
                )
                return

            motivo = (options["motivo"] or "").strip()
            if not motivo or not options["usuario"]:
                raise CommandError("Indica --usuario y --motivo de la conciliación ya realizada.")
            try:
                usuario = get_user_model().objects.get(
                    username=options["usuario"], is_active=True, is_superuser=True,
                )
            except get_user_model().DoesNotExist as exc:
                raise CommandError("La confirmación requiere un superusuario activo.") from exc
            if proyecto.mochila_antigua_conciliada:
                self.stdout.write("La revisión ya está registrada. No se repite.")
                return
            proyecto.mochila_antigua_conciliada = True
            proyecto.save(update_fields=["mochila_antigua_conciliada", "actualizado_en"])
            LogEntry.objects.log_action(
                user_id=usuario.pk,
                content_type_id=ContentType.objects.get_for_model(Proyecto).pk,
                object_id=proyecto.pk,
                object_repr=str(proyecto),
                action_flag=CHANGE,
                change_message=f"Conciliación de mochila de obra confirmada por separado. Sin cambios de stock. Motivo: {motivo}",
            )
            self.stdout.write(self.style.SUCCESS("Revisión registrada. Cantidades, asignaciones y estado de la obra conservados."))
