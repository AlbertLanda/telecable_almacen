"""Retorno entre almacenes: borrador, envío y recepción sin cargar mochilas."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.db import connection, connections
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse

from inventario.models import (
    DocumentoInventario, DocumentoItemSerializado, EstadoDocumento,
    ItemSerializado, MovimientoInventario, Producto, Sede, Stock,
    StockTecnico, TipoDocumento, TipoRequerimiento, Ubicacion, UserProfile,
)


class RetornoCentralTests(TestCase):
    def setUp(self):
        self.origen = Sede.objects.create(nombre="RETORNO HUANCAYO", tipo=Sede.SECUNDARIO)
        self.central = Sede.objects.create(nombre="RETORNO CENTRAL", tipo=Sede.CENTRAL)
        self.otra = Sede.objects.create(nombre="RETORNO OTRA", tipo=Sede.SECUNDARIO)
        self.ubicacion_origen = Ubicacion.objects.create(nombre="Rack retorno", sede=self.origen)
        self.ubicacion_central = Ubicacion.objects.create(nombre="Rack central", sede=self.central)
        self.ubicacion_otra = Ubicacion.objects.create(nombre="Rack otra", sede=self.otra)
        self.almacen = self.usuario("retorno_huancayo", self.origen, UserProfile.Rol.ALMACEN)
        self.receptor = self.usuario("retorno_central", self.central, UserProfile.Rol.ALMACEN)
        self.ajeno = self.usuario("retorno_otro", self.otra, UserProfile.Rol.ALMACEN)
        self.tecnico = self.usuario("retorno_tecnico", self.origen, UserProfile.Rol.SOLICITANTE)
        self.cable = Producto.objects.create(nombre="Cable por metro", codigo_interno="RT-MTS", unidad="MTS")
        self.router = Producto.objects.create(nombre="Router con serial", codigo_interno="RT-SN", es_serializado=True)
        self.stock_cable = Stock.objects.create(producto=self.cable, sede=self.origen, cantidad=1000)
        self.stock_central = Stock.objects.create(producto=self.cable, sede=self.central, cantidad=40)
        Stock.objects.create(producto=self.cable, sede=self.otra, cantidad=55)
        Stock.objects.create(producto=self.router, sede=self.origen, cantidad=3)
        Stock.objects.create(producto=self.router, sede=self.central, cantidad=2)
        StockTecnico.objects.create(tecnico=self.tecnico, producto=self.cable, sede=self.origen, cantidad=33)
        StockTecnico.objects.create(tecnico=self.tecnico, producto=self.cable, sede=self.central, cantidad=21)
        self.mochilas_antes = list(StockTecnico.objects.order_by("pk").values())
        self.crear_url = reverse("retorno_central_crear")
        self.client.force_login(self.almacen)

    def usuario(self, nombre, sede, rol):
        usuario = get_user_model().objects.create_user(username=nombre)
        usuario.profile.rol = rol
        usuario.profile.sede_principal = sede
        usuario.profile.save()
        return usuario

    def datos(self, cantidad="125", producto=None, documento_id=None, **extra):
        producto = producto or self.cable
        if documento_id is None:
            formulario = self.client.get(self.crear_url)
            self.assertEqual(formulario.status_code, 200)
            documento_id = formulario.context["documento"].pk
        return {
            "documento_id": str(documento_id),
            "motivo": "Material disponible que retorna al almacen central",
            "producto": str(producto.pk),
            "cantidad": str(cantidad),
            **extra,
        }

    def crear(self, **datos):
        response = self.client.post(self.crear_url, self.datos(**datos))
        self.assertEqual(response.status_code, 302)
        sal = DocumentoInventario.objects.get(tipo=TipoDocumento.SAL)
        self.assertRedirects(response, reverse("retorno_central_detalle", args=[sal.pk]))
        return sal

    def confirmar(self, sal, **datos):
        detalle = self.client.get(reverse("retorno_central_detalle", args=[sal.pk]))
        revision = detalle.context["revision"] if detalle.status_code == 200 else ""
        return self.client.post(reverse("retorno_central_confirmar", args=[sal.pk]),
                                {"revision": revision, "acepto": "1", **datos})

    def recibir(self, sal):
        return self.client.post(reverse("almacen_recepcionar_traspaso", args=[sal.pk]))

    def saldo(self, producto, sede):
        return Stock.objects.get(producto=producto, sede=sede).cantidad

    def mochilas_intactas(self):
        self.assertEqual(list(StockTecnico.objects.order_by("pk").values()), self.mochilas_antes)

    def equipo(self, serial, ubicacion=None, **extra):
        return ItemSerializado.objects.create(
            producto=self.router, serial=serial,
            ubicacion=ubicacion or self.ubicacion_origen,
            estado=ItemSerializado.Estado.EN_ALMACEN, **extra,
        )

    def test_dashboard_y_formulario_permiten_preparar_retorno_sin_mover_stock(self):
        dashboard = self.client.get(reverse("dash_almacen"))
        self.assertContains(dashboard, self.crear_url)
        formulario = self.client.get(self.crear_url)
        self.assertEqual(formulario.status_code, 200)
        self.assertContains(formulario, self.central.nombre)
        self.assertContains(formulario, "MTS")
        disponibles = [p["disponible"] for p in formulario.context["productos_retorno"] if p["id"] == self.cable.pk]
        self.assertEqual(disponibles, [1000])
        self.assertFalse(MovimientoInventario.objects.exists())
        self.assertEqual(self.saldo(self.cable, self.origen), 1000)
        self.assertEqual(self.saldo(self.cable, self.central), 40)
        self.mochilas_intactas()

    def test_borrador_conserva_stock_y_distingue_retorno_de_requerimiento(self):
        sal = self.crear()
        self.assertEqual((sal.estado, sal.sede_id, sal.sede_destino_id),
                         (EstadoDocumento.BORRADOR, self.origen.pk, self.central.pk))
        self.assertEqual(sal.tipo_requerimiento, TipoRequerimiento.RETORNO_CENTRAL)
        self.assertEqual(sal.responsable_id, self.almacen.pk)
        self.assertEqual(sal.solicitante_id, self.almacen.pk)
        self.assertIsNone(sal.origen_id)
        self.assertEqual(sal.items.get().cantidad, 125)
        self.assertFalse(MovimientoInventario.objects.exists())
        self.assertFalse(DocumentoInventario.objects.filter(tipo=TipoDocumento.REQ).exists())
        self.assertEqual(self.saldo(self.cable, self.origen), 1000)
        self.assertEqual(self.saldo(self.cable, self.central), 40)
        self.mochilas_intactas()

    def test_metros_salen_una_vez_y_entran_solo_al_recibir_sin_conversion(self):
        sal = self.crear()
        self.assertEqual(self.confirmar(sal).status_code, 302)
        sal.refresh_from_db()
        self.assertEqual(sal.estado, EstadoDocumento.CONFIRMADO)
        self.assertFalse(sal.recibido)
        self.assertEqual((self.saldo(self.cable, self.origen), self.saldo(self.cable, self.central)), (875, 40))
        self.assertEqual(list(MovimientoInventario.objects.values_list("tipo", "qty", "sede_id")),
                         [("OUT", 125, self.origen.pk)])
        self.assertFalse(DocumentoInventario.objects.filter(tipo=TipoDocumento.ING).exists())
        self.confirmar(sal)
        self.assertEqual(MovimientoInventario.objects.count(), 1)
        self.client.force_login(self.receptor)
        dashboard = self.client.get(reverse("dash_almacen"))
        self.assertContains(dashboard, sal.numero)
        self.assertEqual(self.recibir(sal).status_code, 302)
        ingreso = DocumentoInventario.objects.get(tipo=TipoDocumento.ING, origen=sal)
        self.assertEqual((ingreso.estado, ingreso.sede_id, ingreso.sede_origen_id),
                         (EstadoDocumento.CONFIRMADO, self.central.pk, self.origen.pk))
        self.assertEqual(ingreso.items.get().cantidad, 125)
        self.assertEqual((self.saldo(self.cable, self.origen), self.saldo(self.cable, self.central)), (875, 165))
        sal.refresh_from_db()
        self.assertTrue(sal.recibido)
        self.assertEqual(sal.recibido_por_id, self.receptor.pk)
        self.recibir(sal)
        self.assertEqual(DocumentoInventario.objects.filter(tipo=TipoDocumento.ING, origen=sal).count(), 1)
        self.assertEqual(MovimientoInventario.objects.count(), 2)
        self.assertEqual(self.saldo(self.cable, self.central), 165)
        self.assertEqual(self.saldo(self.cable, self.otra), 55)
        self.mochilas_intactas()

    def test_guardar_borrador_repetido_no_duplica_la_salida(self):
        sal = self.crear()
        for _ in range(2):
            response = self.client.post(self.crear_url, self.datos(documento_id=sal.pk))
            self.assertEqual(response.status_code, 302)
        self.assertEqual(DocumentoInventario.objects.filter(tipo=TipoDocumento.SAL).count(), 1)
        self.assertEqual(sal.items.count(), 1)
        self.assertFalse(MovimientoInventario.objects.exists())

    def test_post_sin_borrador_no_crea_otra_salida(self):
        datos = self.datos(documento_id="")
        response = self.client.post(self.crear_url, datos)
        self.assertIn(response.status_code, (200, 400, 403, 404))
        self.assertFalse(DocumentoInventario.objects.exists())
        self.assertFalse(MovimientoInventario.objects.exists())

    def test_reintento_de_guardado_tras_envio_no_crea_otro_retorno(self):
        sal = self.crear()
        self.confirmar(sal)
        response = self.client.post(self.crear_url, self.datos(cantidad=50, documento_id=sal.pk))
        self.assertIn(response.status_code, (200, 400))
        self.assertEqual(DocumentoInventario.objects.filter(tipo=TipoDocumento.SAL).count(), 1)
        sal.refresh_from_db()
        self.assertEqual((sal.estado, sal.items.get().cantidad), (EstadoDocumento.CONFIRMADO, 125))
        self.assertEqual(self.saldo(self.cable, self.origen), 875)
        self.assertEqual(MovimientoInventario.objects.count(), 1)

    def test_revision_anterior_y_falta_de_aceptacion_no_confirman(self):
        sal = self.crear()
        detalle = self.client.get(reverse("retorno_central_detalle", args=[sal.pk]))
        revision_anterior = detalle.context["revision"]
        self.client.post(self.crear_url, self.datos(cantidad=100, documento_id=sal.pk))
        for extra in ({"revision": revision_anterior}, {"acepto": ""}):
            with self.subTest(extra=extra):
                self.confirmar(sal, **extra)
                sal.refresh_from_db()
                self.assertEqual(sal.estado, EstadoDocumento.BORRADOR)
                self.assertFalse(MovimientoInventario.objects.exists())
                self.assertEqual(self.saldo(self.cable, self.origen), 1000)
                self.assertEqual(self.saldo(self.cable, self.central), 40)

    def test_confirmar_y_recibir_exigen_post_y_csrf(self):
        sal = self.crear()
        self.assertEqual(self.client.get(reverse("retorno_central_confirmar", args=[sal.pk])).status_code, 405)
        self.client.force_login(self.receptor)
        self.assertEqual(self.client.get(reverse("almacen_recepcionar_traspaso", args=[sal.pk])).status_code, 405)
        estricto = Client(enforce_csrf_checks=True)
        estricto.force_login(self.almacen)
        self.assertEqual(estricto.post(reverse("retorno_central_confirmar", args=[sal.pk])).status_code, 403)
        self.assertFalse(MovimientoInventario.objects.exists())

    def test_tecnico_y_almacen_central_no_pueden_originar_retorno(self):
        for usuario in (self.tecnico, self.receptor):
            with self.subTest(usuario=usuario.username):
                self.client.force_login(usuario)
                self.assertEqual(self.client.get(self.crear_url).status_code, 403)
                self.assertEqual(self.client.post(self.crear_url, self.datos(documento_id=1)).status_code, 403)
        self.assertFalse(DocumentoInventario.objects.exists())
        self.assertFalse(MovimientoInventario.objects.exists())

    def test_sede_ajena_no_puede_ver_modificar_o_confirmar_el_borrador(self):
        sal = self.crear()
        self.client.force_login(self.ajeno)
        for response in (
            self.client.get(reverse("retorno_central_detalle", args=[sal.pk])),
            self.client.post(self.crear_url, self.datos(cantidad=1, documento_id=sal.pk)),
            self.confirmar(sal),
        ):
            self.assertIn(response.status_code, (403, 404))
        sal.refresh_from_db()
        self.assertEqual(sal.estado, EstadoDocumento.BORRADOR)
        self.assertEqual(sal.items.get().cantidad, 125)
        self.assertFalse(MovimientoInventario.objects.exists())

    def test_parametros_de_sede_o_tecnico_no_cambian_origen_ni_crean_mochila(self):
        sal = self.crear(sede=str(self.central.pk), sede_origen=str(self.otra.pk),
                         sede_destino=str(self.otra.pk), solicitante=str(self.tecnico.pk))
        self.assertEqual((sal.sede_id, sal.sede_destino_id, sal.solicitante_id),
                         (self.origen.pk, self.central.pk, self.almacen.pk))
        self.confirmar(sal)
        self.assertEqual(self.saldo(self.cable, self.otra), 55)
        self.mochilas_intactas()

    def test_recepcion_de_otra_sede_no_mueve_stock(self):
        sal = self.crear()
        self.confirmar(sal)
        for usuario in (self.almacen, self.ajeno, self.tecnico):
            with self.subTest(usuario=usuario.username):
                self.client.force_login(usuario)
                self.recibir(sal)
                sal.refresh_from_db()
                self.assertFalse(sal.recibido)
                self.assertFalse(DocumentoInventario.objects.filter(tipo=TipoDocumento.ING).exists())
                self.assertEqual(self.saldo(self.cable, self.central), 40)
                self.assertEqual(MovimientoInventario.objects.count(), 1)
        self.mochilas_intactas()

    def test_cantidades_invalidas_o_mayores_al_stock_no_generan_movimientos(self):
        for cantidad in ("0", "-1", "1.5", "abc", "1001"):
            with self.subTest(cantidad=cantidad):
                response = self.client.post(self.crear_url, self.datos(cantidad=cantidad))
                self.assertIn(response.status_code, (200, 400))
                self.assertFalse(MovimientoInventario.objects.exists())
                self.assertFalse(DocumentoInventario.objects.filter(estado=EstadoDocumento.CONFIRMADO).exists())
                self.assertEqual(self.saldo(self.cable, self.origen), 1000)
                self.assertEqual(self.saldo(self.cable, self.central), 40)

    def test_stock_que_disminuyo_tras_revision_bloquea_envio_completo(self):
        sal = self.crear(cantidad=125)
        Stock.objects.filter(pk=self.stock_cable.pk).update(cantidad=100)
        response = self.confirmar(sal)
        self.assertIn(response.status_code, (200, 302, 400))
        sal.refresh_from_db()
        self.assertEqual(sal.estado, EstadoDocumento.BORRADOR)
        self.assertFalse(MovimientoInventario.objects.exists())
        self.assertFalse(DocumentoInventario.objects.filter(tipo=TipoDocumento.ING).exists())
        self.assertEqual(self.saldo(self.cable, self.origen), 100)
        self.assertEqual(self.saldo(self.cable, self.central), 40)
        self.mochilas_intactas()

    def test_motivo_obligatorio_impide_preparar_un_envio_sin_trazabilidad(self):
        response = self.client.post(self.crear_url, self.datos(motivo=" "))
        self.assertIn(response.status_code, (200, 400))
        self.assertFalse(MovimientoInventario.objects.exists())
        self.assertFalse(DocumentoInventario.objects.filter(estado=EstadoDocumento.CONFIRMADO).exists())

    def test_seriales_viajan_documentados_y_solo_se_ubican_en_central_al_recibir(self):
        enviados = [self.equipo("RT-SERIAL-1"), self.equipo("RT-SERIAL-2")]
        intacto = self.equipo("RT-SERIAL-3")
        sal = self.crear(cantidad=2, producto=self.router,
                         seriales=[str(e.pk) for e in enviados])
        self.assertEqual(self.confirmar(sal).status_code, 302)
        for equipo in enviados:
            equipo.refresh_from_db()
            self.assertIsNone(equipo.ubicacion_id)
            self.assertEqual(equipo.asignado_a_id, self.almacen.pk)
        self.assertEqual(self.saldo(self.router, self.origen), 1)
        self.assertEqual(self.saldo(self.router, self.central), 2)
        self.assertCountEqual(DocumentoItemSerializado.objects.filter(documento_item__documento=sal)
                              .values_list("item_serializado_id", flat=True), [e.pk for e in enviados])
        self.client.force_login(self.receptor)
        self.recibir(sal)
        ingreso = DocumentoInventario.objects.get(tipo=TipoDocumento.ING, origen=sal)
        for equipo in enviados:
            equipo.refresh_from_db()
            self.assertEqual(equipo.estado, ItemSerializado.Estado.EN_ALMACEN)
            self.assertEqual(equipo.ubicacion.sede_id, self.central.pk)
            self.assertIsNone(equipo.asignado_a_id)
            self.assertIsNone(equipo.sede_asignacion_id)
        self.assertCountEqual(DocumentoItemSerializado.objects.filter(documento_item__documento=ingreso)
                              .values_list("item_serializado_id", flat=True), [e.pk for e in enviados])
        intacto.refresh_from_db()
        self.assertEqual((intacto.estado, intacto.ubicacion_id),
                         (ItemSerializado.Estado.EN_ALMACEN, self.ubicacion_origen.pk))
        self.assertEqual(self.saldo(self.router, self.central), 4)
        self.mochilas_intactas()

    def test_seriales_ajenos_repetidos_o_de_tecnico_no_se_pueden_devolver(self):
        local = self.equipo("RT-LOCAL")
        ajeno = self.equipo("RT-AJENO", ubicacion=self.ubicacion_otra)
        asignado = self.equipo("RT-TECNICO", asignado_a=self.tecnico, sede_asignacion=self.origen)
        for ids, cantidad in (([ajeno.pk], 1), ([local.pk, local.pk], 2), ([asignado.pk], 1), ([], 1)):
            with self.subTest(ids=ids, cantidad=cantidad):
                response = self.client.post(self.crear_url, self.datos(cantidad=cantidad, producto=self.router,
                    seriales=[str(pk) for pk in ids]))
                self.assertIn(response.status_code, (200, 400))
                self.assertFalse(MovimientoInventario.objects.exists())
                self.assertEqual(self.saldo(self.router, self.origen), 3)
                self.assertEqual(self.saldo(self.router, self.central), 2)
        self.mochilas_intactas()

    def test_cambio_de_identidad_del_serial_invalida_la_revision_anterior(self):
        equipo = self.equipo("RT-IDENTIDAD-ORIGINAL")
        sal = self.crear(cantidad=1, producto=self.router, seriales=[str(equipo.pk)])
        detalle = self.client.get(reverse("retorno_central_detalle", args=[sal.pk]))
        revision = detalle.context["revision"]
        ItemSerializado.objects.filter(pk=equipo.pk).update(serial="RT-IDENTIDAD-CAMBIADA")

        self.confirmar(sal, revision=revision)

        sal.refresh_from_db()
        self.assertEqual(sal.estado, EstadoDocumento.BORRADOR)
        self.assertFalse(MovimientoInventario.objects.exists())
        self.assertEqual(self.saldo(self.router, self.origen), 3)
        self.mochilas_intactas()

    def test_busqueda_de_equipos_limita_producto_sede_y_rol(self):
        local = self.equipo("RT-BUSQUEDA-LOCAL")
        self.equipo("RT-BUSQUEDA-AJENO", ubicacion=self.ubicacion_otra)
        self.equipo("RT-BUSQUEDA-CUSTODIA", asignado_a=self.tecnico, sede_asignacion=self.origen)
        url = reverse("retorno_central_equipos")

        response = self.client.get(url, {"producto": self.router.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([e["id"] for e in response.json()["equipos"]], [local.pk])
        self.assertEqual(self.client.get(url, {"producto": self.cable.pk}).json()["equipos"], [])
        sin_stock = Producto.objects.create(nombre="Equipo sin stock local", codigo_interno="RT-SIN", es_serializado=True)
        Stock.objects.create(producto=sin_stock, sede=self.otra, cantidad=2)
        self.assertEqual(self.client.get(url, {"producto": sin_stock.pk}).status_code, 404)
        self.client.force_login(self.tecnico)
        self.assertEqual(self.client.get(url, {"producto": self.router.pk}).status_code, 403)
        self.assertFalse(MovimientoInventario.objects.exists())


@skipUnless(connection.vendor == "postgresql", "Requiere dos conexiones PostgreSQL reales")
class RetornoCentralConcurrenciaTests(TransactionTestCase):
    def test_dos_envios_simultaneos_del_mismo_borrador_descuentan_una_vez(self):
        from inventario.services.retorno_service import (
            confirmar_retorno, crear_borrador_retorno, guardar_borrador_retorno, huella_retorno,
        )
        origen = Sede.objects.create(nombre="RETORNO-CONC-ORIGEN", tipo=Sede.SECUNDARIO)
        central = Sede.objects.create(nombre="RETORNO-CONC-CENTRAL", tipo=Sede.CENTRAL)
        usuario = get_user_model().objects.create_user(username="retorno_concurrente")
        usuario.profile.rol = UserProfile.Rol.ALMACEN
        usuario.profile.sede_principal = origen
        usuario.profile.save()
        producto = Producto.objects.create(nombre="Cable concurrente", codigo_interno="RT-CONC", unidad="MTS")
        Stock.objects.create(producto=producto, sede=origen, cantidad=100)
        Stock.objects.create(producto=producto, sede=central, cantidad=40)
        sal = crear_borrador_retorno(user=usuario)
        guardar_borrador_retorno(user=usuario, documento_id=sal.pk, observaciones="Retorno concurrente",
                                 items=[{"producto_id": producto.pk, "cantidad": 25, "seriales_ids": []}])
        sal.refresh_from_db()
        revision = huella_retorno(sal)
        inicio = Barrier(2)

        def enviar_desde_otra_conexion():
            try:
                with connections["default"].cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                actor = get_user_model().objects.get(pk=usuario.pk)
                inicio.wait(timeout=10)
                return confirmar_retorno(user=actor, documento_id=sal.pk, revision=revision).pk
            finally:
                connections["default"].close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            tareas = [executor.submit(enviar_desde_otra_conexion) for _ in range(2)]
            resultados = [tarea.result(timeout=20) for tarea in tareas]

        self.assertEqual(resultados, [sal.pk, sal.pk])
        self.assertEqual(Stock.objects.get(producto=producto, sede=origen).cantidad, 75)
        self.assertEqual(Stock.objects.get(producto=producto, sede=central).cantidad, 40)
        self.assertEqual(MovimientoInventario.objects.filter(tipo="OUT", producto=producto, sede=origen).count(), 1)
        self.assertFalse(DocumentoInventario.objects.filter(tipo=TipoDocumento.ING).exists())
        self.assertFalse(StockTecnico.objects.exists())
