"""
Una obra recibe despachos de más de una sede: Jauja despacha una parte,
Oroya despacha lo que falta, y así hasta completar lo planificado.
Datos propios: Sede Jauja y Sede Oroya, dos almaceneros por sede y una obra aprobada.
"""
from datetime import datetime

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from inventario.models import (
    DocumentoInventario, MovimientoInventario, Producto, Sede, Stock, StockTecnico,
    TipoDocumento, Ubicacion, UserProfile,
)
from proyectos.models import EstadoProyecto, Proyecto, ProyectoMaterial


class DespachoObraMultisedeTests(TestCase):
    def setUp(self):
        jauja = Sede.objects.create(nombre="Sede Jauja", tipo=Sede.CENTRAL)
        oroya = Sede.objects.create(nombre="Sede Oroya", tipo=Sede.SECUNDARIO)
        for sede in (jauja, oroya):
            Ubicacion.objects.create(sede=sede, nombre="Almacén principal")
        for nombre, sede in (("almacen_jauja_1", jauja), ("almacen_jauja_2", jauja),
                             ("almacen_oroya_1", oroya), ("almacen_oroya_2", oroya)):
            self.usuario(nombre, UserProfile.Rol.ALMACEN, sede)
        resp = self.usuario("tecnico_resp_jauja", UserProfile.Rol.SOLICITANTE, jauja)
        disenador = self.usuario("disenador_prueba", UserProfile.Rol.DISENADOR, jauja)

        cable = Producto.objects.create(codigo_interno="PRB-001", nombre="Cable drop FTTH 1 hilo", unidad="MTS")
        conector = Producto.objects.create(codigo_interno="PRB-002", nombre="Conector rápido SC/APC")
        for producto, en_jauja, en_oroya in ((cable, 2000, 1200), (conector, 300, 150)):
            for sede, cantidad in ((jauja, en_jauja), (oroya, en_oroya)):
                MovimientoInventario.objects.create(
                    producto=producto, sede=sede, tipo=MovimientoInventario.TIPO_IN,
                    qty=cantidad, referencia="CARGA-INICIAL",
                ).aplicar()

        self.obra = Proyecto.objects.create(
            codigo="OBRA-JAUJA-001", nombre="Ampliación red FTTH Jr. Junín - Jauja", sede=jauja,
            creado_por=disenador, responsable=resp, estado=EstadoProyecto.APROBADO,
        )
        self.cable = ProyectoMaterial.objects.create(proyecto=self.obra, producto=cable, cantidad_planificada=600)
        self.conector = ProyectoMaterial.objects.create(proyecto=self.obra, producto=conector, cantidad_planificada=40)

    def usuario(self, nombre, rol, sede):
        user = User.objects.create_user(username=nombre, password="test1234")
        user.profile.rol = rol
        user.profile.sede_principal = sede
        user.profile.save()
        return user

    def stock(self, sede, material):
        return Stock.objects.get(sede__nombre=sede, producto=material.producto).cantidad

    def despachar(self, usuario, cantidades):
        self.client.force_login(User.objects.get(username=usuario))
        datos = {f"input_{m.id}": q for m, q in cantidades.items()}
        return self.client.post(reverse("almacen_generar_salida", args=[self.obra.id]), datos, follow=True)

    def recargar(self):
        for m in (self.cable, self.conector):
            m.refresh_from_db()
        self.obra.refresh_from_db()

    def test_jauja_y_oroya_completan_la_obra_por_partes(self):
        jauja_cable, oroya_cable = self.stock("Sede Jauja", self.cable), self.stock("Sede Oroya", self.cable)

        # 1) Jauja despacha una parte
        self.despachar("almacen_jauja_1", {self.cable: 250, self.conector: 10})
        self.recargar()
        self.assertEqual(self.cable.cantidad_entregada, 250)
        self.assertEqual(self.obra.estado, EstadoProyecto.EN_PROCESO)
        self.assertEqual(self.stock("Sede Jauja", self.cable), jauja_cable - 250)

        # 2) Oroya ve solo lo que falta y despacha otra parte desde SU stock
        self.client.force_login(User.objects.get(username="almacen_oroya_1"))
        pantalla = self.client.get(reverse("almacen_generar_salida", args=[self.obra.id]))
        pendientes = {m.producto.codigo_interno: m.pendiente_temp for m in pantalla.context["items"]}
        self.assertEqual(pendientes["PRB-001"], 350)
        self.assertEqual(pendientes["PRB-002"], 30)
        self.despachar("almacen_oroya_1", {self.cable: 200, self.conector: 30})
        self.recargar()
        self.assertEqual(self.stock("Sede Oroya", self.cable), oroya_cable - 200)
        self.assertEqual(self.stock("Sede Jauja", self.cable), jauja_cable - 250)  # Jauja no cambia

        # 3) Otro almacenero de Jauja completa el resto
        self.despachar("almacen_jauja_2", {self.cable: 150})
        self.recargar()
        self.assertEqual(self.cable.cantidad_entregada, 600)
        self.assertEqual(self.conector.cantidad_entregada, 40)

        # 4) Nadie puede pasarse de lo planificado
        respuesta = self.despachar("almacen_oroya_2", {self.cable: 1})
        self.recargar()
        self.assertEqual(self.cable.cantidad_entregada, 600)

        salidas = DocumentoInventario.objects.filter(tipo=TipoDocumento.SAL, referencia=self.obra.codigo)
        self.assertEqual(
            sorted((s.sede.nombre, s.responsable.username) for s in salidas),
            [("Sede Jauja", "almacen_jauja_1"), ("Sede Jauja", "almacen_jauja_2"), ("Sede Oroya", "almacen_oroya_1")],
        )

        # 5) El vale de salida muestra los despachos de las dos sedes
        vale = self.client.get(reverse("proyecto_pdf_salida", args=[self.obra.id]))
        self.assertContains(vale, "Sede Jauja")
        self.assertContains(vale, "Sede Oroya")

    def test_no_se_puede_despachar_mas_de_lo_pendiente_ni_del_stock_propio(self):
        self.despachar("almacen_jauja_1", {self.cable: 500})
        respuesta = self.despachar("almacen_oroya_1", {self.cable: 200})   # pendiente 100
        self.recargar()
        self.assertEqual(self.cable.cantidad_entregada, 500)
        self.assertContains(respuesta, "No puedes despachar más de lo pendiente")

    def liquidar(self, usuario, buenos, malos=0):
        self.client.force_login(User.objects.get(username=usuario))
        return self.client.post(reverse("almacen_liquidar_proyecto", args=[self.obra.id]),
                                {f"input_good_{self.cable.id}": buenos, f"input_bad_{self.cable.id}": malos})

    def test_lo_devuelto_entra_a_la_sede_que_liquida(self):
        self.despachar("almacen_jauja_1", {self.cable: 300})
        self.despachar("almacen_oroya_1", {self.cable: 300})
        jauja, oroya = self.stock("Sede Jauja", self.cable), self.stock("Sede Oroya", self.cable)

        self.liquidar("almacen_oroya_1", buenos=50)
        self.recargar()

        self.assertEqual(self.obra.estado, EstadoProyecto.FINALIZADO)
        self.assertEqual(self.stock("Sede Oroya", self.cable), oroya + 50)
        self.assertEqual(self.stock("Sede Jauja", self.cable), jauja)
        retorno = DocumentoInventario.objects.get(tipo=TipoDocumento.ING, referencia=f"RETORNO {self.obra.codigo}")
        self.assertEqual(retorno.sede.nombre, "Sede Oroya")
        self.assertIn("Sede de la obra: Sede Jauja", retorno.observaciones)

    def test_cerrar_la_obra_no_borra_la_mochila_personal(self):
        self.despachar("almacen_jauja_1", {self.cable: 600})
        resp = User.objects.get(username="tecnico_resp_jauja")
        StockTecnico.objects.create(tecnico=resp, producto=self.cable.producto, sede=self.obra.sede, cantidad=100)

        self.liquidar("almacen_jauja_1", buenos=50)

        self.assertEqual(StockTecnico.objects.get(tecnico=resp, producto=self.cable.producto).cantidad, 100)

    def test_obra_antigua_si_descuenta_lo_que_sumo_a_la_mochila(self):
        """Antes del 15/09/2026 el despacho de obra sumaba a la mochila: al cerrar se quita solo eso."""
        self.despachar("almacen_jauja_1", {self.cable: 200})
        DocumentoInventario.objects.filter(tipo=TipoDocumento.SAL, referencia=self.obra.codigo).update(
            fecha=timezone.make_aware(datetime(2026, 8, 1, 10, 0)))
        resp = User.objects.get(username="tecnico_resp_jauja")
        # 200 de la obra antigua + 100 de su carga semanal
        StockTecnico.objects.create(tecnico=resp, producto=self.cable.producto, sede=self.obra.sede, cantidad=300)

        self.liquidar("almacen_jauja_1", buenos=0)

        self.assertEqual(StockTecnico.objects.get(tecnico=resp, producto=self.cable.producto).cantidad, 100)

    def test_obra_antigua_reabierta_no_descuenta_la_mochila_dos_veces(self):
        """Caso OBRA-2026-0008: cerrada antes de tiempo, reabierta desde el admin y cerrada de nuevo."""
        self.despachar("almacen_jauja_1", {self.cable: 200})
        DocumentoInventario.objects.filter(tipo=TipoDocumento.SAL, referencia=self.obra.codigo).update(
            fecha=timezone.make_aware(datetime(2026, 8, 1, 10, 0)))
        resp = User.objects.get(username="tecnico_resp_jauja")
        StockTecnico.objects.create(tecnico=resp, producto=self.cable.producto, sede=self.obra.sede, cantidad=300)

        self.liquidar("almacen_jauja_1", buenos=0)   # primer cierre: quita los 200 antiguos
        self.assertEqual(StockTecnico.objects.get(tecnico=resp, producto=self.cable.producto).cantidad, 100)

        # Se reabre desde el admin (solo el estado), se despacha lo que faltaba y se cierra otra vez.
        self.obra.refresh_from_db()
        self.obra.estado = EstadoProyecto.EN_PROCESO
        self.obra.save(update_fields=["estado"])
        self.despachar("almacen_oroya_1", {self.cable: 400, self.conector: 40})
        self.liquidar("almacen_oroya_1", buenos=0)

        self.recargar()
        self.assertEqual(self.obra.estado, EstadoProyecto.FINALIZADO)
        self.assertEqual(self.cable.cantidad_entregada, 600)
        self.assertEqual(StockTecnico.objects.get(tecnico=resp, producto=self.cable.producto).cantidad, 100)
