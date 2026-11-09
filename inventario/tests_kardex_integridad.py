"""Regresiones del Kardex: fechas, concurrencia y consulta sin escrituras."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection, connections, transaction
from django.test import RequestFactory, TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from inventario.models import (
    ItemSerializado, MovimientoInventario, Producto, Sede, Stock, StockTecnico,
    UserProfile,
)
from inventario.views import dashboard


class KardexIntegridadMixin:
    def setUp(self):
        super().setUp()
        self.sede = Sede.objects.create(nombre="KARDEX-INTEGRIDAD", tipo=Sede.CENTRAL)
        self.otra_sede = Sede.objects.create(nombre="KARDEX-OTRA", tipo=Sede.SECUNDARIO)
        self.producto = Producto.objects.create(nombre="Equipo de integridad", codigo_interno="KI-001")
        self.almacen = self.usuario("ki_almacen", self.sede, UserProfile.Rol.ALMACEN)
        self.client.force_login(self.almacen)
        self.url = reverse("kardex_producto")
        self.params = {"desde": "2026-11", "hasta": "2026-11"}

    def usuario(self, nombre, sede, rol):
        usuario = get_user_model().objects.create_user(username=nombre)
        usuario.profile.rol = rol
        usuario.profile.sede_principal = sede
        usuario.profile.save()
        return usuario

    def movimiento(self, tipo, cantidad, fecha, sede=None, usuario=None):
        # La operación completa se confirma junta, como un despacho normal.
        with transaction.atomic():
            mov = MovimientoInventario.objects.create(
                producto=self.producto, sede=sede or self.sede,
                tipo=tipo, qty=cantidad, usuario=usuario,
            )
            mov.aplicar()
            MovimientoInventario.objects.filter(pk=mov.pk).update(
                creado_en=timezone.make_aware(fecha),
            )
        return mov

    def consultar(self, hoy=date(2026, 11, 30), **params):
        with patch("inventario.views.dashboard.timezone.localdate", return_value=hoy):
            response = self.client.get(self.url, {**self.params, **params})
        self.assertEqual(response.status_code, 200)
        return response

    def fila(self, response):
        return next(f for f in response.context["resumen"] if f["producto"].id == self.producto.id)


class KardexIntegridadTests(KardexIntegridadMixin, TestCase):
    def test_reporte_no_cambia_al_cerrar_el_mes_sin_cambios_en_los_datos(self):
        self.movimiento("IN", 20, datetime(2026, 11, 1, 9))
        Stock.objects.filter(producto=self.producto, sede=self.sede).update(cantidad=25)

        noviembre = self.fila(self.consultar(date(2026, 11, 30)))
        diciembre = self.fila(self.consultar(date(2026, 12, 1)))

        campos = ("saldo_inicial", "entradas", "salidas", "ajustes", "saldo_final_periodo")
        self.assertEqual(tuple(noviembre[c] for c in campos), (0, 20, 0, 0, 20))
        self.assertEqual(tuple(noviembre[c] for c in campos), tuple(diciembre[c] for c in campos))
        self.assertEqual((noviembre["diferencia_actual"], diciembre["diferencia_actual"]), (5, 5))

    def test_salida_tras_la_lectura_no_crea_ajustes_ni_mezcla_saldos(self):
        self.movimiento("IN", 100, datetime(2026, 11, 1, 9))
        original = dashboard._kardex_por_mes

        def calcular_despues_de_salida(*args, **kwargs):
            self.movimiento("OUT", 10, datetime(2026, 11, 6, 10))
            return original(*args, **kwargs)

        with patch("inventario.views.dashboard._kardex_por_mes", side_effect=calcular_despues_de_salida):
            fila = self.fila(self.consultar())

        # El reporte conserva la foto anterior completa, no stock 100 + salida 10.
        self.assertEqual((fila["entradas"], fila["salidas"], fila["ajustes"]), (100, 0, 0))
        self.assertEqual((fila["saldo_final_periodo"], fila["stock_actual"], fila["diferencia_actual"]), (100, 100, 0))
        siguiente = self.fila(self.consultar())
        self.assertEqual((siguiente["saldo_final_periodo"], siguiente["stock_actual"], siguiente["diferencia_actual"]), (90, 90, 0))
        self.assertFalse(MovimientoInventario.objects.filter(tipo="ADJ").exists())

    def test_movimientos_posteriores_al_periodo_no_parecen_un_descuadre(self):
        self.movimiento("IN", 100, datetime(2026, 10, 1, 9))
        self.movimiento("OUT", 10, datetime(2026, 11, 1, 9))

        fila = self.fila(self.consultar(desde="2026-10", hasta="2026-10"))

        self.assertEqual((fila["saldo_final_periodo"], fila["stock_actual"]), (100, 90))
        self.assertEqual((fila["ajustes"], fila["diferencia_actual"]), (0, 0))

    def test_stock_sin_movimientos_no_inventa_una_carga_inicial(self):
        Stock.objects.create(producto=self.producto, sede=self.sede, cantidad=17)

        fila = self.fila(self.consultar())

        self.assertEqual((fila["saldo_inicial"], fila["saldo_final_periodo"], fila["ajustes"]), (0, 0, 0))
        self.assertEqual((fila["stock_actual"], fila["diferencia_actual"]), (17, 17))
        self.assertEqual(fila["num_movimientos"], 0)

    def test_saldo_inicial_documentado_no_arrastra_ediciones_sin_movimiento(self):
        self.movimiento("IN", 20, datetime(2026, 10, 1, 9))
        Stock.objects.filter(producto=self.producto, sede=self.sede).update(cantidad=30)
        self.movimiento("OUT", 5, datetime(2026, 10, 2, 9))
        self.movimiento("OUT", 3, datetime(2026, 11, 1, 9))

        fila = self.fila(self.consultar())

        self.assertEqual((fila["saldo_inicial"], fila["saldo_final_periodo"], fila["ajustes"]), (15, 12, 0))
        self.assertEqual((fila["stock_actual"], fila["diferencia_actual"]), (22, 10))

    def test_ajustes_registrados_conservan_signo_usuario_y_saldo_documentado(self):
        self.movimiento("IN", 20, datetime(2026, 11, 1, 9))
        self.movimiento("ADJ", -4, datetime(2026, 11, 2, 9), usuario=self.almacen)
        self.movimiento("ADJ", 3, datetime(2026, 11, 3, 9), usuario=self.almacen)

        response = self.consultar(producto_id=self.producto.id, vista="mensual")
        detalle = response.context["detalle"]

        self.assertEqual((detalle["ajustes"], detalle["saldo_final"], detalle["diferencia_actual"]), (-1, 19, 0))
        movimientos = detalle["meses"][0]["movimientos"]
        self.assertEqual([m["saldo_documentado"] for m in movimientos], [20, 16, 19])
        self.assertEqual([m["tipo"] for m in movimientos], ["IN", "ADJ", "ADJ"])
        self.assertContains(response, self.almacen.username)

    def test_cada_almacen_ve_solo_su_stock_y_sus_diferencias(self):
        self.movimiento("IN", 10, datetime(2026, 11, 1, 9))
        self.movimiento("IN", 80, datetime(2026, 11, 1, 9), sede=self.otra_sede)
        Stock.objects.filter(producto=self.producto, sede=self.otra_sede).update(cantidad=85)

        primera = self.fila(self.consultar(sede=self.otra_sede.id))
        self.assertEqual((primera["entradas"], primera["stock_actual"], primera["diferencia_actual"]), (10, 10, 0))

        otro_almacen = self.usuario("ki_almacen_otro", self.otra_sede, UserProfile.Rol.ALMACEN)
        self.client.force_login(otro_almacen)
        segunda = self.fila(self.consultar(sede=self.sede.id))
        self.assertEqual((segunda["entradas"], segunda["stock_actual"], segunda["diferencia_actual"]), (80, 85, 5))

    def test_consultar_no_escribe_ni_altera_inventarios_de_usuarios(self):
        self.movimiento("IN", 20, datetime(2026, 11, 1, 9))
        Stock.objects.filter(producto=self.producto, sede=self.sede).update(cantidad=25)
        for i, sede in enumerate((self.sede, self.otra_sede), start=1):
            tecnico = self.usuario(f"ki_tecnico_{i}", sede, UserProfile.Rol.SOLICITANTE)
            StockTecnico.objects.create(tecnico=tecnico, producto=self.producto, sede=sede, cantidad=i)
            ItemSerializado.objects.create(
                producto=self.producto, serial=f"KI-SERIAL-{i}",
                estado=ItemSerializado.Estado.ASIGNADO, asignado_a=tecnico,
                sede_asignacion=sede,
            )

        modelos = (Stock, StockTecnico, ItemSerializado, MovimientoInventario, UserProfile)
        antes = {m: list(m.objects.order_by("pk").values()) for m in modelos}
        with CaptureQueriesContext(connection) as queries:
            self.consultar()
            self.consultar(producto_id=self.producto.id, vista="mensual")
        despues = {m: list(m.objects.order_by("pk").values()) for m in modelos}

        self.assertEqual(antes, despues)
        escrituras = [q["sql"] for q in queries if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]
        self.assertEqual(escrituras, [])

    def test_lectura_de_stock_y_movimientos_es_una_sola_consulta(self):
        self.movimiento("IN", 20, datetime(2026, 11, 1, 9))
        request = RequestFactory().get(self.url, self.params)
        with patch("inventario.views.dashboard.timezone.localdate", return_value=date(2026, 11, 30)):
            periodo = dashboard._periodo_desde_request(request)

        with self.assertNumQueries(1):
            snapshot = dashboard._leer_snapshot_kardex(self.sede, periodo)
        # El cálculo no puede leer un estado nuevo tras materializar el informe.
        with self.assertNumQueries(0):
            resultado = dashboard._kardex_por_mes(snapshot, periodo, con_detalle=True)
        self.assertEqual(resultado[self.producto.id]["saldo_final"], 20)


@skipUnless(connection.vendor == "postgresql", "Requiere dos conexiones PostgreSQL reales")
class KardexConcurrenciaPostgresTests(KardexIntegridadMixin, TransactionTestCase):
    def test_salida_confirmada_en_otra_conexion_no_modifica_el_reporte_en_curso(self):
        self.movimiento("IN", 100, datetime(2026, 11, 1, 9))
        original = dashboard._kardex_por_mes

        def salida_independiente():
            try:
                self.movimiento("OUT", 10, datetime(2026, 11, 6, 10))
            finally:
                connections["default"].close()

        def calcular_despues_del_commit(*args, **kwargs):
            with ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(salida_independiente).result(timeout=10)
            return original(*args, **kwargs)

        with patch("inventario.views.dashboard._kardex_por_mes", side_effect=calcular_despues_del_commit):
            fila = self.fila(self.consultar())

        self.assertEqual((fila["stock_actual"], fila["saldo_final_periodo"], fila["salidas"], fila["ajustes"], fila["diferencia_actual"]), (100, 100, 0, 0, 0))
        self.assertEqual(Stock.objects.get(producto=self.producto, sede=self.sede).cantidad, 90)
        siguiente = self.fila(self.consultar())
        self.assertEqual((siguiente["stock_actual"], siguiente["saldo_final_periodo"], siguiente["salidas"], siguiente["ajustes"]), (90, 90, 10, 0))
