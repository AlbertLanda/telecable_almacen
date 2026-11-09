import base64
import math
import random
from io import BytesIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from PIL import Image, ImageDraw

from inventario.models import (
    DocumentoInventario,
    DocumentoItem,
    EstadoDocumento,
    FirmaDocumento,
    Producto,
    Sede,
    StockTecnico,
    TipoDocumento,
    TipoRequerimiento,
)
from inventario.services.firma_service import (
    acta_hash,
    hash_documento,
    registrar_firma,
    registrar_firma_acta,
)
from proyectos.models import EstadoProyecto, Proyecto, ProyectoMaterial

RECEPTOR = FirmaDocumento.Rol.RECEPTOR
ALMACEN = FirmaDocumento.Rol.ALMACEN


def trazo_de_prueba(semilla):
    """Una firma dibujada de mentira (curva distinta para cada usuario), como la manda el navegador."""
    azar = random.Random(semilla)
    imagen = Image.new("RGBA", (600, 200), (0, 0, 0, 0))
    lapiz = ImageDraw.Draw(imagen)
    amplitud, frecuencia, fase = azar.uniform(25, 45), azar.uniform(0.04, 0.07), azar.uniform(0, 3)
    puntos = [
        (x, 100 + amplitud * math.sin(frecuencia * x + fase) * math.cos(0.013 * x))
        for x in range(40, 560, 4)
    ]
    lapiz.line(puntos, fill="black", width=4, joint="curve")
    lapiz.line([(puntos[-1][0] - 120, 150), (puntos[-1][0], 145)], fill="black", width=3)
    salida = BytesIO()
    imagen.save(salida, "PNG")
    return "data:image/png;base64," + base64.b64encode(salida.getvalue()).decode()


class Command(BaseCommand):
    help = (
        "Base de demostración de las firmas: carga Sede Jauja y Sede Oroya (seed_sedes_prueba) "
        "y crea documentos de cada tipo en distintos estados de firma (sin firmas, 1 de 2, "
        "firmados y uno que cambió después de firmarse). Correr en una base nueva."
    )

    # ---------------- ayudas ----------------
    def firmar(self, doc, usuario, rol):
        registrar_firma(doc_id=doc.id, usuario=usuario, rol=rol, trazo=trazo_de_prueba(usuario.username),
                        sha256_visto=hash_documento(doc), acepto=True)

    def firmar_acta(self, obra, usuario, rol):
        registrar_firma_acta(proyecto_id=obra.id, usuario=usuario, rol=rol, trazo=trazo_de_prueba(usuario.username),
                             sha256_visto=acta_hash(obra), acepto=True)

    def items(self, doc, cantidades):
        for codigo, cantidad in cantidades.items():
            DocumentoItem.objects.create(documento=doc, producto=self.p[codigo], cantidad=cantidad)

    def confirmado(self, cantidades, mochila=True, **campos):
        """Crea el documento en borrador y lo confirma con el flujo real (mueve stock y mochila)."""
        doc = DocumentoInventario.objects.create(estado=EstadoDocumento.BORRADOR, **campos)
        self.items(doc, cantidades)
        doc.confirmar(actualizar_mochila_tecnico=mochila)
        doc.refresh_from_db()
        return doc

    def bajar_mochila(self, tecnico, sede, cantidades):
        for codigo, cantidad in cantidades.items():
            st = StockTecnico.objects.get(tecnico=tecnico, producto=self.p[codigo], sede=sede)
            st.cantidad -= cantidad
            st.save(update_fields=["cantidad"])

    def nota_salida(self, sede, almacen, resp, retira, cantidades):
        """Pedido local de un técnico (REQ) atendido por almacén con una nota de salida (SAL)."""
        req = DocumentoInventario.objects.create(
            tipo=TipoDocumento.REQ, estado=EstadoDocumento.REQ_PENDIENTE, sede=sede,
            tipo_requerimiento=TipoRequerimiento.LOCAL, responsable=resp, solicitante=resp, retirado_por=retira,
        )
        req.asignar_numero_si_falta()
        self.items(req, cantidades)
        return self.confirmado(cantidades, tipo=TipoDocumento.SAL, sede=sede, responsable=almacen,
                               solicitante=resp, retirado_por=retira, origen=req,
                               referencia=f"Atención de {req.numero}")

    # ---------------- comando ----------------
    @transaction.atomic
    def handle(self, *args, **options):
        if FirmaDocumento.objects.exists():
            raise CommandError("Esta base ya tiene firmas. La demostración se carga en una base nueva.")

        call_command("seed_sedes_prueba", stdout=self.stdout)
        u = {x.username: x for x in User.objects.filter(username__in=[
            "almacen_jauja_1", "almacen_jauja_2", "almacen_oroya_1", "almacen_oroya_2",
            "tecnico_resp_jauja", "tecnico_crew_jauja", "tecnico_resp_oroya", "tecnico_crew_oroya",
            "disenador_prueba",
        ])}
        jauja, oroya = Sede.objects.get(nombre="Sede Jauja"), Sede.objects.get(nombre="Sede Oroya")
        self.p = {p.codigo_interno: p for p in Producto.objects.filter(codigo_interno__startswith="PRB-")}
        aj1, aj2, ao1 = u["almacen_jauja_1"], u["almacen_jauja_2"], u["almacen_oroya_1"]
        rj, cj, ro, co = u["tecnico_resp_jauja"], u["tecnico_crew_jauja"], u["tecnico_resp_oroya"], u["tecnico_crew_oroya"]
        resumen = []

        # 1. Notas de salida a técnicos (cargan la mochila del Resp)
        n1 = self.nota_salida(jauja, aj1, rj, cj, {"PRB-001": 150, "PRB-002": 20})
        n2 = self.nota_salida(jauja, aj1, rj, cj, {"PRB-003": 8, "PRB-004": 30})
        self.firmar(n2, cj, RECEPTOR)
        n3 = self.nota_salida(jauja, aj2, rj, rj, {"PRB-005": 4})
        self.firmar(n3, rj, RECEPTOR)
        self.firmar(n3, aj2, ALMACEN)
        n4 = self.nota_salida(jauja, aj1, rj, rj, {"PRB-002": 6})
        self.firmar(n4, rj, RECEPTOR)
        self.firmar(n4, aj1, ALMACEN)
        # Simula una corrección hecha después (por ejemplo, desde el admin): la firma queda marcada.
        n4.items.update(cantidad=8)
        n5 = self.nota_salida(oroya, ao1, ro, co, {"PRB-001": 80, "PRB-004": 15})
        resumen += [
            f"Nota de salida {n1.numero} (Jauja): sin firmas — retiró tecnico_crew_jauja",
            f"Nota de salida {n2.numero} (Jauja): 1/2 — firmó el Crew, falta almacen_jauja_1",
            f"Nota de salida {n3.numero} (Jauja): firmada 2/2",
            f"Nota de salida {n4.numero} (Jauja): CAMBIÓ después de firmarse (cantidad 6 → 8)",
            f"Nota de salida {n5.numero} (Oroya): sin firmas — retiró tecnico_crew_oroya",
        ]

        # 2. Devoluciones del técnico
        d1 = self.confirmado({"PRB-001": 10}, tipo=TipoDocumento.ING, sede=jauja, responsable=aj2,
                             entregado_por=rj, referencia="DEVOLUCION-RAPIDA")
        self.bajar_mochila(rj, jauja, {"PRB-001": 10})
        d2 = self.confirmado({"PRB-004": 5}, tipo=TipoDocumento.ING, sede=jauja, responsable=aj1,
                             entregado_por=rj, referencia="DEVOLUCION-RAPIDA")
        self.bajar_mochila(rj, jauja, {"PRB-004": 5})
        self.firmar(d2, rj, RECEPTOR)
        self.firmar(d2, aj1, ALMACEN)
        resumen += [
            f"Devolución {d1.numero} (Jauja): sin firmas — recibió almacen_jauja_2",
            f"Devolución {d2.numero} (Jauja): firmada 2/2",
        ]

        # 3. Liquidación semanal de la mochila (devuelto / merma / consumo)
        liq = DocumentoInventario.objects.create(
            tipo=TipoDocumento.ING, estado=EstadoDocumento.BORRADOR, sede=jauja, sede_origen=jauja,
            responsable=aj1, solicitante=rj, referencia="LIQ-SEMANAL", observaciones="Liquidación Semana. Demo",
        )
        DocumentoItem.objects.create(documento=liq, producto=self.p["PRB-001"], cantidad=20,
                                     cantidad_merma=2, cantidad_usada=30)
        DocumentoItem.objects.create(documento=liq, producto=self.p["PRB-002"], cantidad=5,
                                     cantidad_merma=0, cantidad_usada=10)
        liq.confirmar()
        liq.refresh_from_db()
        self.bajar_mochila(rj, jauja, {"PRB-001": 52, "PRB-002": 15})
        self.firmar(liq, rj, RECEPTOR)
        resumen.append(f"Liquidación {liq.numero} (Jauja): 1/2 — firmó el técnico, falta almacen_jauja_1")

        # 4. Despachos a obra: OBRA-JAUJA-001 recibe de Jauja y de Oroya, y aún le falta material
        obra = Proyecto.objects.get(codigo="OBRA-JAUJA-001")
        despachos = ((jauja, aj1, {"PRB-001": 300, "PRB-002": 20}), (oroya, ao1, {"PRB-001": 100, "PRB-004": 40}))
        salidas_obra = []
        for sede, almacen, cantidades in despachos:
            sal = self.confirmado(cantidades, mochila=False, tipo=TipoDocumento.SAL, sede=sede, responsable=almacen,
                                  solicitante=obra.responsable, retirado_por=cj, referencia=obra.codigo,
                                  observaciones=f"Salida para obra {obra.codigo} | Demo")
            for codigo, cantidad in cantidades.items():
                m = obra.materiales.get(producto=self.p[codigo])
                m.cantidad_entregada += cantidad
                m.save(update_fields=["cantidad_entregada"])
            salidas_obra.append(sal)
        obra.estado = EstadoProyecto.EN_PROCESO
        obra.save(update_fields=["estado"])
        self.firmar(salidas_obra[0], rj, RECEPTOR)
        self.firmar(salidas_obra[0], aj1, ALMACEN)
        resumen += [
            f"Despacho a obra {salidas_obra[0].numero} (Jauja → OBRA-JAUJA-001): firmado 2/2",
            f"Despacho a obra {salidas_obra[1].numero} (Oroya → OBRA-JAUJA-001): sin firmas; firma el Resp aunque retiró el Crew",
            "OBRA-JAUJA-001 queda en ejecución con material por despachar; OBRA-OROYA-001 sigue intacta",
        ]

        # 5. Acta de cierre: una obra ya liquidada
        cerrada = Proyecto.objects.create(
            codigo="OBRA-JAUJA-002", nombre="Reubicación de caja NAP - Plaza de Armas Jauja", sede=jauja,
            creado_por=u["disenador_prueba"], responsable=rj, estado=EstadoProyecto.EN_PROCESO,
        )
        plan = {"PRB-001": 120, "PRB-003": 6, "PRB-006": 1}
        for codigo, cantidad in plan.items():
            ProyectoMaterial.objects.create(proyecto=cerrada, producto=self.p[codigo],
                                            cantidad_planificada=cantidad, cantidad_entregada=cantidad)
        self.confirmado(plan, mochila=False, tipo=TipoDocumento.SAL, sede=jauja, responsable=aj1,
                        solicitante=rj, retirado_por=rj, referencia=cerrada.codigo)
        cierre = {"PRB-001": (15, 3), "PRB-003": (1, 0), "PRB-006": (0, 0)}  # (devuelto, merma)
        for codigo, (devuelto, merma) in cierre.items():
            m = cerrada.materiales.get(producto=self.p[codigo])
            m.cantidad_devuelta, m.cantidad_merma = devuelto, merma
            m.cantidad_usada = m.cantidad_entregada - devuelto - merma
            m.save()
        self.confirmado({c: d for c, (d, _) in cierre.items() if d}, tipo=TipoDocumento.ING, sede=jauja,
                        responsable=aj1, solicitante=rj, referencia=f"RETORNO {cerrada.codigo}")
        cerrada.estado, cerrada.fin, cerrada.liquidado_por = EstadoProyecto.FINALIZADO, timezone.now(), aj1
        cerrada.save()
        self.firmar_acta(cerrada, rj, RECEPTOR)
        resumen.append("Acta de cierre OBRA-JAUJA-002: 1/2 — firmó el responsable, falta almacen_jauja_1 (V° B°)")

        # 6. Pedido entre sedes: Oroya pide a Jauja, Jauja envía y Oroya recepciona
        ped = DocumentoInventario.objects.create(
            tipo=TipoDocumento.REQ, estado=EstadoDocumento.REQ_PENDIENTE, sede=oroya, sede_destino=jauja,
            tipo_requerimiento=TipoRequerimiento.ENTRE_SEDES, responsable=ao1,
        )
        ped.asignar_numero_si_falta()
        envio = {"PRB-002": 40, "PRB-003": 10}
        self.items(ped, envio)
        tras = self.confirmado(envio, mochila=False, tipo=TipoDocumento.SAL, sede=jauja, sede_destino=oroya,
                               responsable=aj2, solicitante=ao1, origen=ped, referencia=f"Atención de {ped.numero}")
        self.confirmado(envio, tipo=TipoDocumento.ING, sede=oroya, sede_origen=jauja, responsable=ao1,
                        origen=tras, referencia=f"Recepción de {tras.numero}")
        tras.refresh_from_db()
        ped.refresh_from_db()
        self.firmar(tras, aj2, ALMACEN)
        self.firmar(ped, ao1, RECEPTOR)
        resumen += [
            f"Traslado {tras.numero} (Jauja → Oroya, ya recepcionado): 1/2 — envió y firmó almacen_jauja_2, falta almacen_oroya_1",
            f"Pedido entre sedes {ped.numero}: 1/2 — firmó almacen_oroya_1, falta almacen_jauja_2 (atendió)",
        ]

        # 7. Orden de compra a proveedor, ya recepcionada
        oc = DocumentoInventario.objects.create(
            tipo=TipoDocumento.REQ, estado=EstadoDocumento.REQ_PENDIENTE, sede=jauja, responsable=aj1,
            tipo_requerimiento=TipoRequerimiento.PROVEEDOR, proveedor_manual="Distribuidora Fibra Andina SAC",
        )
        oc.asignar_numero_si_falta()
        compra = {"PRB-001": 1000, "PRB-006": 4}
        self.items(oc, compra)
        self.confirmado(compra, tipo=TipoDocumento.ING, sede=jauja, responsable=aj2, origen=oc,
                        proveedor_manual=oc.proveedor_manual, referencia=f"Llegada Compra {oc.numero}")
        oc.estado = EstadoDocumento.REQ_ATENDIDO
        oc.save(update_fields=["estado"])
        resumen.append(f"Orden de compra {oc.numero} (Jauja): sin firmas — pidió almacen_jauja_1, recepcionó almacen_jauja_2")

        self.stdout.write(self.style.SUCCESS("\nDemostración de firmas lista:"))
        for linea in resumen:
            self.stdout.write(f"  - {linea}")
