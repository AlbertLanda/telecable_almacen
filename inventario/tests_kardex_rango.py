"""
Informe de movimientos (kardex) por rango de meses.

Los casos de "ajustes" reproducen lo que se encontró en producción: stock
editado sin movimiento, movimientos borrados después de aplicarse y stock
cambiado después del último movimiento. En todos, el saldo final de cada
mes debe coincidir con el stock real.
"""
from datetime import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from inventario.models import MovimientoInventario, Producto, Sede, Stock, UserProfile


class KardexRangoTests(TestCase):
    def setUp(self):
        self.sede = Sede.objects.create(nombre="SEDE-KARDEX", tipo=Sede.CENTRAL)
        self.otra_sede = Sede.objects.create(nombre="SEDE-OTRA", tipo=Sede.SECUNDARIO)
        self.almacen = self.usuario("almacen_kardex", UserProfile.Rol.ALMACEN)
        self.p = Producto.objects.create(nombre="Cable de prueba", codigo_interno="KX-001")
        self.client.force_login(self.almacen)
        self.url = reverse("kardex_producto")

    # ---------- helpers ----------
    def usuario(self, nombre, rol):
        user = get_user_model().objects.create_user(username=nombre)
        user.profile.rol = rol
        user.profile.sede_principal = self.sede
        user.profile.save()
        return user

    def mov(self, tipo, qty, fecha, producto=None, sede=None):
        """Crea y aplica un movimiento, y lo fecha en hora de Lima."""
        m = MovimientoInventario.objects.create(
            producto=producto or self.p, sede=sede or self.sede, tipo=tipo, qty=qty,
        )
        m.aplicar()
        MovimientoInventario.objects.filter(id=m.id).update(creado_en=timezone.make_aware(fecha))
        m.refresh_from_db()
        return m

    def editar_stock(self, cantidad, producto=None):
        """Simula una edición directa del stock (sin movimiento)."""
        Stock.objects.filter(producto=producto or self.p, sede=self.sede).update(cantidad=cantidad)

    def resumen(self, **params):
        resp = self.client.get(self.url, params)
        self.assertEqual(resp.status_code, 200)
        return resp.context

    def fila(self, ctx, producto=None):
        producto = producto or self.p
        return next(f for f in ctx["resumen"] if f["producto"].id == producto.id)

    # ---------- rango sin ajustes ----------
    def test_rango_de_meses_encadena_saldos(self):
        self.mov("IN", 100, datetime(2025, 4, 10))
        self.mov("OUT", 30, datetime(2025, 5, 3))
        self.mov("IN", 10, datetime(2025, 7, 20))

        ctx = self.resumen(desde="2025-04", hasta="2025-08")
        f = self.fila(ctx)

        self.assertEqual(ctx["periodo"]["etiqueta"], "Abril 2025 – Agosto 2025")
        self.assertEqual((f["saldo_inicial"], f["entradas"], f["salidas"], f["ajustes"]), (0, 110, 30, 0))
        self.assertEqual(f["saldo_final_periodo"], 80)
        self.assertEqual([m["saldo_final"] for m in f["meses"]], [100, 70, 70, 80, 80])
        self.assertEqual([m["saldo_inicial"] for m in f["meses"]], [0, 100, 70, 70, 80])

    def test_saldo_inicial_viene_de_antes_del_rango(self):
        self.mov("IN", 40, datetime(2025, 2, 1))
        self.mov("OUT", 5, datetime(2025, 4, 2))

        f = self.fila(self.resumen(desde="2025-04", hasta="2025-05"))

        self.assertEqual(f["saldo_inicial"], 40)
        self.assertEqual(f["saldo_final_periodo"], 35)

    def test_vista_mensual_suma_igual_que_totalizado(self):
        self.mov("IN", 100, datetime(2025, 4, 10))
        self.mov("OUT", 30, datetime(2025, 5, 3))
        self.editar_stock(90)
        self.mov("OUT", 10, datetime(2025, 6, 1))

        f = self.fila(self.resumen(desde="2025-04", hasta="2025-06", vista="mensual"))

        for campo in ("entradas", "salidas", "ajustes"):
            self.assertEqual(sum(m[campo] for m in f["meses"]), f[campo])
        self.assertEqual(f["meses"][-1]["saldo_final"], f["saldo_final_periodo"])

    # ---------- ajustes de conciliación ----------
    def test_stock_editado_sin_movimiento_va_a_ajustes(self):
        self.mov("IN", 50, datetime(2025, 4, 10))
        self.editar_stock(150)  # +100 sin movimiento
        ultimo = self.mov("OUT", 20, datetime(2025, 6, 5))

        f = self.fila(self.resumen(desde="2025-04", hasta="2025-06"))

        self.assertEqual((f["entradas"], f["salidas"], f["ajustes"]), (50, 20, 100))
        self.assertEqual(f["saldo_final_periodo"], ultimo.saldo_resultante)  # 130
        self.assertEqual([m["ajustes"] for m in f["meses"]], [0, 0, 100])

    def test_movimiento_borrado_queda_como_ajuste(self):
        self.mov("IN", 10, datetime(2025, 4, 10))
        borrado = self.mov("IN", 5, datetime(2025, 5, 10))
        borrado.delete()  # el stock se queda con las 5 unidades
        ultimo = self.mov("OUT", 3, datetime(2025, 6, 10))

        f = self.fila(self.resumen(desde="2025-04", hasta="2025-06"))

        self.assertEqual((f["entradas"], f["salidas"], f["ajustes"]), (10, 3, 5))
        self.assertEqual(f["saldo_final_periodo"], ultimo.saldo_resultante)  # 12

    def test_ajuste_registrado_suma_con_su_signo(self):
        self.mov("IN", 20, datetime(2025, 4, 10))
        self.mov("ADJ", -4, datetime(2025, 4, 11))

        f = self.fila(self.resumen(desde="2025-04", hasta="2025-04"))

        self.assertEqual((f["entradas"], f["ajustes"], f["saldo_final_periodo"]), (20, -4, 16))

    def test_descuadre_despues_del_ultimo_movimiento_cuadra_con_stock_de_hoy(self):
        hoy = timezone.localdate()
        actual = f"{hoy.year:04d}-{hoy.month:02d}"
        self.mov("IN", 20, datetime(hoy.year, hoy.month, 1, 9, 0))
        self.editar_stock(25)

        f = self.fila(self.resumen(desde=actual, hasta=actual))

        self.assertEqual(f["ajustes"], 5)
        self.assertEqual(f["saldo_final_periodo"], 25)
        self.assertEqual(f["stock_actual"], 25)

    def test_descuadre_actual_no_afecta_meses_pasados(self):
        self.mov("IN", 20, datetime(2025, 4, 10))
        self.editar_stock(25)

        f = self.fila(self.resumen(desde="2025-04", hasta="2025-04"))

        self.assertEqual((f["ajustes"], f["saldo_final_periodo"], f["stock_actual"]), (0, 20, 25))

    # ---------- periodo ----------
    def test_corte_de_mes_en_hora_de_lima(self):
        # 30/04 23:30 en Lima es 01/05 04:30 en UTC: debe contar en abril.
        self.mov("IN", 7, datetime(2025, 4, 30, 23, 30))

        abril = self.fila(self.resumen(desde="2025-04", hasta="2025-04"))
        mayo = self.fila(self.resumen(desde="2025-05", hasta="2025-05"))

        self.assertEqual(abril["entradas"], 7)
        self.assertEqual((mayo["saldo_inicial"], mayo["entradas"]), (7, 0))

    def test_rango_que_cruza_de_anio(self):
        self.mov("IN", 10, datetime(2024, 12, 15))
        self.mov("OUT", 4, datetime(2025, 1, 15))

        ctx = self.resumen(desde="2024-11", hasta="2025-02")
        f = self.fila(ctx)

        self.assertEqual([m["clave"] for m in f["meses"]], ["2024-11", "2024-12", "2025-01", "2025-02"])
        self.assertEqual([m["saldo_final"] for m in f["meses"]], [0, 10, 6, 6])

    def test_parametro_mes_anterior_sigue_funcionando(self):
        self.mov("IN", 9, datetime(2025, 5, 2))

        ctx = self.resumen(mes="2025-05")

        self.assertEqual((ctx["periodo"]["desde_str"], ctx["periodo"]["hasta_str"]), ("2025-05", "2025-05"))
        self.assertEqual(self.fila(ctx)["entradas"], 9)

    def test_hasta_anterior_a_desde_se_invierte(self):
        ctx = self.resumen(desde="2025-08", hasta="2025-04")

        self.assertEqual((ctx["periodo"]["desde_str"], ctx["periodo"]["hasta_str"]), ("2025-04", "2025-08"))

    def test_rango_mayor_al_maximo_se_recorta(self):
        ctx = self.resumen(desde="2020-01", hasta="2025-12")

        self.assertEqual(len(ctx["periodo"]["meses"]), 24)
        self.assertTrue(ctx["periodo"]["avisos"])

    def test_hasta_en_el_futuro_se_limita_al_mes_actual(self):
        hoy = timezone.localdate()
        ctx = self.resumen(desde=f"{hoy.year:04d}-{hoy.month:02d}", hasta=f"{hoy.year + 1:04d}-01")

        self.assertEqual(ctx["periodo"]["hasta"], (hoy.year, hoy.month))

    def test_valor_invalido_usa_mes_actual(self):
        hoy = timezone.localdate()
        ctx = self.resumen(desde="abc", hasta="2025-99")

        self.assertEqual(ctx["periodo"]["meses"], [(hoy.year, hoy.month)])

    # ---------- alcance ----------
    def test_producto_con_movimientos_pero_sin_fila_de_stock_aparece(self):
        self.mov("IN", 3, datetime(2025, 4, 1))
        self.mov("OUT", 3, datetime(2025, 4, 2))
        Stock.objects.filter(producto=self.p, sede=self.sede).delete()

        f = self.fila(self.resumen(desde="2025-04", hasta="2025-04"))

        self.assertEqual((f["entradas"], f["salidas"], f["stock_actual"]), (3, 3, 0))

    def test_no_mezcla_movimientos_de_otra_sede(self):
        self.mov("IN", 10, datetime(2025, 4, 1))
        self.mov("IN", 99, datetime(2025, 4, 1), sede=self.otra_sede)

        f = self.fila(self.resumen(desde="2025-04", hasta="2025-04"))

        self.assertEqual(f["entradas"], 10)

    def test_detalle_de_producto_muestra_fila_de_ajuste(self):
        self.mov("IN", 50, datetime(2025, 4, 10))
        self.editar_stock(150)
        self.mov("OUT", 20, datetime(2025, 6, 5))

        resp = self.client.get(self.url, {"producto_id": self.p.id, "desde": "2025-04", "hasta": "2025-06"})
        detalle = resp.context["detalle"]

        self.assertEqual(resp.status_code, 200)
        self.assertEqual((detalle["entradas"], detalle["salidas"], detalle["ajustes"]), (50, 20, 100))
        self.assertEqual(detalle["saldo_final"], 130)
        junio = detalle["meses"][2]["movimientos"]
        self.assertTrue(isinstance(junio[0], dict) and junio[0]["es_ajuste_conciliacion"])
        self.assertEqual(junio[0]["saldo_resultante"], 150)
        self.assertContains(resp, "Ajuste de inventario")
        self.assertContains(resp, "Abril 2025 – Junio 2025")

    def test_vista_mensual_renderiza_desglose(self):
        self.mov("IN", 5, datetime(2025, 4, 1))

        resp = self.client.get(self.url, {"desde": "2025-04", "hasta": "2025-05", "vista": "mensual"})

        self.assertContains(resp, "kx-meses-detalle")
        self.assertContains(resp, "Mayo 2025")

    def test_solicitante_no_tiene_acceso(self):
        self.client.force_login(self.usuario("tecnico_kardex", UserProfile.Rol.SOLICITANTE))

        resp = self.client.get(self.url)

        self.assertEqual(resp.status_code, 403)
