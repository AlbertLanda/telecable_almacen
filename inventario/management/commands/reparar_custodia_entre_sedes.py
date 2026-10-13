"""Reparación de dos custodias cruzadas mediante un plan privado. Por defecto solo presenta el plan."""
import json
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from inventario.models import (
    CorreccionSeriales, DocumentoInventario, DocumentoItem, DocumentoItemSerializado,
    EstadoDocumento, ItemSerializado, MovimientoInventario, Producto, Sede, Stock,
    StockTecnico, TipoDocumento,
)



class Command(BaseCommand):
    help = "Plan privado de reparación de dos equipos; simulación por defecto."

    def add_arguments(self, parser):
        parser.add_argument("--plan", required=True, help="Archivo JSON privado con el diagnóstico revisado.")
        parser.add_argument("--aplicar", action="store_true")
        parser.add_argument("--usuario", help="Usuario administrador que ejecuta la conciliación.")
        parser.add_argument("--confirmar-retorno", help="Número de ingreso, solo tras confirmar el retorno erróneo del equipo.")
        parser.add_argument("--stock-destino-esperado", type=int)

    @transaction.atomic
    def handle(self, *args, **options):
        try:
            cfg = json.loads(Path(options["plan"]).read_text(encoding="utf-8"))
            required = {"clave", "tecnico", "producto", "sede_origen", "sede_destino", "salida", "ingreso", "mochila_destino", "retorno", "consumo", "merma", "instalado", "en_almacen"}
            if set(cfg) != required or cfg["sede_origen"] == cfg["sede_destino"]:
                raise ValueError("Claves del plan inválidas o sedes iguales")
            if not isinstance(cfg["clave"], str) or not 1 <= len(cfg["clave"]) <= 100:
                raise ValueError("Clave de auditoría inválida")
            for key in ("mochila_destino", "retorno", "consumo", "merma"):
                if type(cfg[key]) is not int or cfg[key] < 0:
                    raise ValueError("Cantidades del plan inválidas")
            if cfg["retorno"] < 1:
                raise ValueError("El ingreso debe incluir al menos un retorno")
            for key in ("instalado", "en_almacen"):
                if set(cfg[key]) != {"codigo", "serial"} or not all(isinstance(v, str) and v for v in cfg[key].values()):
                    raise ValueError("Identificación de equipos inválida")
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            raise CommandError(f"Plan privado inválido: {exc}")
        clave = cfg["clave"]
        # Este bloqueo también serializa dos ejecuciones del mismo comando.
        tecnico = get_user_model().objects.select_for_update().get(username=cfg["tecnico"])
        if CorreccionSeriales.objects.filter(clave=clave).exists():
            self.stdout.write("La reparación ya fue aplicada. No se repite ningún movimiento.")
            return
        producto = Producto.objects.get(codigo_interno=cfg["producto"])
        sede_origen = Sede.objects.get(nombre=cfg["sede_origen"])
        sede_destino = Sede.objects.get(nombre=cfg["sede_destino"])
        sal = DocumentoInventario.objects.select_for_update().get(numero=cfg["salida"])
        ing = DocumentoInventario.objects.select_for_update().get(numero=cfg["ingreso"])
        if (sal.tipo, sal.estado, sal.sede_id, sal.solicitante_id) != (
            TipoDocumento.SAL, EstadoDocumento.CONFIRMADO, sede_origen.id, tecnico.id,
        ) or (ing.tipo, ing.estado, ing.sede_id, ing.solicitante_id) != (
            TipoDocumento.ING, EstadoDocumento.CONFIRMADO, sede_destino.id, tecnico.id,
        ) or ing.referencia != "LIQ-SEMANAL":
            raise CommandError("Los documentos no corresponden al diagnóstico. Se requiere nueva revisión.")
        linea_sal = DocumentoItem.objects.select_for_update().get(documento=sal, producto=producto)
        linea_ing = DocumentoItem.objects.select_for_update().get(documento=ing, producto=producto)
        mochila = list(StockTecnico.objects.select_for_update().filter(tecnico=tecnico, producto=producto).order_by("id"))
        saldos = {s.sede_id: s.cantidad for s in mochila}
        if saldos != {sede_origen.id: 2, sede_destino.id: cfg["mochila_destino"]} or linea_sal.cantidad != 2:
            raise CommandError("Cambió la mochila o la salida. No se puede reutilizar este plan.")
        if (linea_ing.cantidad, linea_ing.cantidad_usada, linea_ing.cantidad_merma) != (cfg["retorno"], cfg["consumo"], cfg["merma"]):
            raise CommandError("La liquidación ya cambió; no se aplicará una segunda corrección.")

        equipos = list(ItemSerializado.objects.select_for_update().filter(
            producto=producto, codigo_trazabilidad__in=[cfg["instalado"]["codigo"], cfg["en_almacen"]["codigo"]],
        ).order_by("id"))
        por_codigo = {e.codigo_trazabilidad: e for e in equipos}
        if len(equipos) != 2 or set(por_codigo) != {cfg["instalado"]["codigo"], cfg["en_almacen"]["codigo"]}:
            raise CommandError("Faltan equipos o los códigos son ambiguos.")
        equipo_instalado, equipo_retorno = por_codigo[cfg["instalado"]["codigo"]], por_codigo[cfg["en_almacen"]["codigo"]]
        if equipo_instalado.serial != cfg["instalado"]["serial"] or equipo_retorno.serial != cfg["en_almacen"]["serial"]:
            raise CommandError("Los números de serie no coinciden con los equipos revisados.")
        if any(e.asignado_a_id or e.proyecto_id or e.sede_asignacion_id for e in equipos):
            raise CommandError("Los equipos tienen otra asignación o proyecto. Requieren nueva revisión.")
        if equipo_instalado.estado != ItemSerializado.Estado.INSTALADO or equipo_instalado.ubicacion_id:
            raise CommandError("Cambió el estado del equipo instalado.")
        if equipo_retorno.estado != ItemSerializado.Estado.EN_ALMACEN or not equipo_retorno.ubicacion_id or equipo_retorno.ubicacion.sede_id != sede_destino.id:
            raise CommandError("Cambió el estado o ubicación del equipo en almacén.")
        if ItemSerializado.objects.filter(
            producto=producto, asignado_a=tecnico, sede_asignacion=sede_origen,
            estado=ItemSerializado.Estado.ASIGNADO, proyecto__isnull=True,
        ).exists():
            raise CommandError("La sede de origen ya tiene otros seriales vinculados a su saldo; conciliar primero.")
        existentes = set(linea_sal.seriales_seleccionados.values_list("item_serializado_id", flat=True))
        if existentes and existentes != {equipo_instalado.id, equipo_retorno.id}:
            raise CommandError("La salida ya tiene seriales diferentes documentados.")

        stock_h = Stock.objects.select_for_update().get(producto=producto, sede=sede_destino)
        ingresos = list(MovimientoInventario.objects.filter(producto=producto, referencia=ing.numero))
        if len(ingresos) != 1 or (ingresos[0].tipo, ingresos[0].qty, ingresos[0].sede_id) != ("IN", cfg["retorno"], sede_destino.id):
            raise CommandError("El ingreso tiene movimientos adicionales o distintos; revisar el kardex.")
        if stock_h.cantidad < 1:
            raise CommandError("La sede de retorno no tiene saldo suficiente para revertir el ingreso erróneo del equipo en almacén.")
        antes = {
            "equipos": list(ItemSerializado.objects.filter(pk__in=[e.id for e in equipos]).values()),
            "mochila": list(StockTecnico.objects.filter(pk__in=[s.id for s in mochila]).values()),
            "stock_sede_destino": {"id": stock_h.id, "cantidad": stock_h.cantidad},
            "linea_liquidacion": {"id": linea_ing.id, "cantidad": linea_ing.cantidad, "cantidad_usada": linea_ing.cantidad_usada, "cantidad_merma": linea_ing.cantidad_merma},
            "observaciones_liquidacion": ing.observaciones,
        }
        plan = {
            "custodia": {"tecnico": tecnico.username, "sede": sede_origen.nombre, "codigos": list(por_codigo), "salida": sal.numero},
            "mochilas": "Sin cambios; no se crea otra salida.",
            "otros_equipos": "Sin cambios; su procedencia requiere evidencia independiente.",
            "stock_origen": "Sin cambios",
            "stock_destino": {"sede": sede_destino.nombre, "antes": stock_h.cantidad, "despues": stock_h.cantidad - 1},
            "documento": ing.numero,
            "historial": "Se conserva el documento original y se anota el ADJ -1 vinculado. No se infiere consumo.",
            "condicion": "Confirmar con el responsable que este ingreso incluyó el retorno ficticio del equipo. El historial sin seriales NO lo demuestra por sí solo.",
        }
        self.stdout.write(json.dumps(plan, ensure_ascii=False, indent=2))
        if not options["aplicar"]:
            self.stdout.write("SIMULACIÓN: no se ha modificado ningún registro.")
            return
        if options["confirmar_retorno"] != ing.numero:
            raise CommandError("Falta confirmar el retorno erróneo del equipo en el ingreso indicado. No se aplica el plan.")
        if options["stock_destino_esperado"] != stock_h.cantidad:
            raise CommandError("El saldo de La sede de retorno no coincide con el revisado. Ejecuta nuevamente la simulación.")
        if not options["usuario"]:
            raise CommandError("Indica --usuario para registrar quién ejecuta la conciliación.")
        actor = get_user_model().objects.get(username=options["usuario"], is_active=True)
        if not actor.is_superuser and getattr(getattr(actor, "profile", None), "rol", None) not in ("ADMIN", "JEFA"):
            raise CommandError("La corrección entre sedes requiere un administrador o jefa.")

        for equipo in equipos:
            equipo.estado = ItemSerializado.Estado.ASIGNADO
            equipo.asignado_a = tecnico
            equipo.sede_asignacion = sede_origen
            equipo.ubicacion = None
            equipo.save(update_fields=["estado", "asignado_a", "sede_asignacion", "ubicacion", "actualizado_en"])
            DocumentoItemSerializado.objects.update_or_create(
                documento_item=linea_sal, item_serializado=equipo,
                defaults={"estado_resultante": ItemSerializado.Estado.ASIGNADO},
            )
        ing.observaciones += f" | Conciliación {clave}: retorno erróneo de {equipo_retorno.codigo_trazabilidad} revertido mediante ADJ -1; custodia {tecnico.username}/{sede_origen.nombre}, salida {sal.numero}. Cantidades históricas conservadas."
        ing.save(update_fields=["observaciones"])
        movimiento = MovimientoInventario.objects.create(
            producto=producto, sede=sede_destino, tipo=MovimientoInventario.TIPO_ADJ, qty=-1,
            referencia=clave, usuario=actor,
            nota=f"Reversión del retorno erróneo de {equipo_retorno.codigo_trazabilidad} en {ing.numero}. Custodia de origen conservada.",
        )
        movimiento.aplicar()
        # Snapshot y marca de idempotencia comparten la transacción con la reparación.
        CorreccionSeriales.objects.create(
            clave=clave, usuario=actor,
            detalle=json.loads(json.dumps({"antes": antes, "plan": plan, "movimiento_id": movimiento.id}, default=str)),
        )
        self.stdout.write(self.style.SUCCESS("Reparación aplicada una sola vez. Mochilas sin cambios; respaldo en CorreccionSeriales."))
