"""Vincula custodia antigua con una salida revisada; nunca mueve cantidades."""
import json
from hashlib import sha256

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from inventario.models import (
    CorreccionSeriales, DocumentoInventario, DocumentoItemSerializado,
    ItemSerializado, StockTecnico,
)


class Command(BaseCommand):
    help = "Concilia la sede de seriales antiguos contra una SAL revisada. Simulación por defecto."

    def add_arguments(self, parser):
        parser.add_argument("--salida", required=True)
        parser.add_argument("--producto", required=True)
        parser.add_argument("--codigos", required=True, help="Códigos pintados separados por coma.")
        parser.add_argument("--usuario")
        parser.add_argument("--motivo", help="Evidencia de entrega consultada, necesaria al aplicar.")
        parser.add_argument("--aplicar", action="store_true")

    @transaction.atomic
    def handle(self, *args, **options):
        sal = DocumentoInventario.objects.select_for_update().get(numero=options["salida"])
        if sal.tipo != "SAL" or sal.estado != "CONFIRMADO" or not sal.sede_id or not sal.solicitante_id:
            raise CommandError("Se requiere una salida confirmada, con sede y técnico.")
        # Las transferencias entre sedes no son mochilas personales.
        if sal.sede_destino_id and sal.sede_destino_id != sal.sede_id:
            raise CommandError("Esta salida es una transferencia entre sedes, no una mochila.")
        linea = sal.items.select_related("producto").get(producto__codigo_interno=options["producto"])
        codigos = [c.strip().upper() for c in options["codigos"].split(",") if c.strip()]
        if not codigos or len(set(codigos)) != len(codigos) or len(codigos) != linea.cantidad:
            raise CommandError("Identifica todos los seriales de la salida, sin duplicados.")
        stock = StockTecnico.objects.select_for_update().get(tecnico=sal.solicitante, sede=sal.sede, producto=linea.producto)
        equipos = list(ItemSerializado.objects.select_for_update().filter(producto=linea.producto, codigo_trazabilidad__in=codigos).order_by("id"))
        if len(equipos) != len(codigos) or {e.codigo_trazabilidad for e in equipos} != set(codigos):
            raise CommandError("Códigos ausentes o ambiguos. No se asignará una sede por aproximación.")
        ids = [e.id for e in equipos]
        clave = "sede-" + sha256(f"{sal.pk}:{sorted(ids)}".encode()).hexdigest()[:40]
        if CorreccionSeriales.objects.filter(clave=clave).exists():
            self.stdout.write("Esta conciliación ya fue registrada; no se repite.")
            return
        if any(e.estado != "ASIGNADO" or e.asignado_a_id != sal.solicitante_id or e.proyecto_id or e.sede_asignacion_id not in (None, sal.sede_id) for e in equipos):
            raise CommandError("Hay equipos con otro estado, responsable, sede o proyecto. Requieren revisión específica.")
        otros = ItemSerializado.objects.filter(producto=linea.producto, asignado_a=sal.solicitante, sede_asignacion=sal.sede, estado="ASIGNADO", proyecto__isnull=True).exclude(pk__in=ids).count()
        if otros + len(ids) > stock.cantidad:
            raise CommandError("Los equipos exceden el saldo de esta sede. No se modifica la mochila para hacerlos encajar.")
        documentados = set(linea.seriales_seleccionados.values_list("item_serializado_id", flat=True))
        if documentados and documentados != set(ids):
            raise CommandError("La salida ya documenta otros seriales.")
        plan = {"salida": sal.numero, "tecnico": sal.solicitante.username, "sede": sal.sede.nombre, "codigos": codigos, "saldo_sin_cambios": stock.cantidad}
        self.stdout.write(json.dumps(plan, ensure_ascii=False, indent=2))
        if not options["aplicar"]:
            self.stdout.write("SIMULACIÓN. Verifica la entrega física antes de aplicar; las cantidades por sí solas no prueban procedencia.")
            return
        if not options["usuario"] or not options["motivo"]:
            raise CommandError("Indica --usuario y --motivo con la evidencia revisada.")
        actor = get_user_model().objects.get(username=options["usuario"], is_active=True)
        if not actor.is_superuser and getattr(getattr(actor, "profile", None), "rol", None) not in ("ADMIN", "JEFA"):
            raise CommandError("La conciliación requiere un administrador o jefa.")
        antes = list(ItemSerializado.objects.filter(pk__in=ids).values())
        for equipo in equipos:
            equipo.sede_asignacion = sal.sede
            equipo.save(update_fields=["sede_asignacion", "actualizado_en"])
            DocumentoItemSerializado.objects.get_or_create(documento_item=linea, item_serializado=equipo)
        CorreccionSeriales.objects.create(clave=clave, usuario=actor, detalle=json.loads(json.dumps({"antes": antes, "plan": plan, "motivo": options["motivo"]}, default=str)))
        self.stdout.write(self.style.SUCCESS("Sede conciliada. Ningún stock, estado o responsable fue modificado."))
