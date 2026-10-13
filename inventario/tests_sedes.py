import json
from io import StringIO
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse

from inventario.models import (
    CorreccionSeriales, DocumentoInventario, DocumentoItem, DocumentoItemSerializado,
    EstadoDocumento, ItemSerializado, MovimientoInventario, Producto, Sede, Stock,
    StockTecnico, TipoDocumento, TipoRequerimiento, Ubicacion, UserProfile,
)


class SedesMixin:
    def setUp(self):
        self.j = Sede.objects.create(nombre="SEDE-PRUEBA-A", tipo=Sede.CENTRAL)
        self.h = Sede.objects.create(nombre="SEDE-PRUEBA-B", tipo=Sede.SECUNDARIO)
        self.uj = Ubicacion.objects.create(nombre="Rack", sede=self.j)
        self.uh = Ubicacion.objects.create(nombre="Rack de prueba", sede=self.h)
        self.almacen = self.usuario("almacen", self.j, UserProfile.Rol.ALMACEN)
        self.tecnico = self.usuario("tecnico_prueba", self.j, UserProfile.Rol.SOLICITANTE)
        self.otro = self.usuario("otro", self.h, UserProfile.Rol.SOLICITANTE)
        self.admin = self.usuario("administrador", self.j, UserProfile.Rol.ADMIN)
        self.p = Producto.objects.create(nombre="Equipo de prueba", codigo_interno="PROD-TEST", es_serializado=True, es_activo=False)
        self.sj = Stock.objects.create(producto=self.p, sede=self.j, cantidad=20)
        self.sh = Stock.objects.create(producto=self.p, sede=self.h, cantidad=10)
        self.mj = StockTecnico.objects.create(tecnico=self.tecnico, producto=self.p, sede=self.j, cantidad=2)
        self.mh = StockTecnico.objects.create(tecnico=self.tecnico, producto=self.p, sede=self.h, cantidad=3)
        self.j1 = self.equipo("701", self.j)
        self.j2 = self.equipo("702", self.j)
        self.h1 = self.equipo("801", self.h)
        self.h2 = self.equipo("802", self.h)
        self.h3 = self.equipo("803", self.h)
        self.client.force_login(self.almacen)
        self.url = reverse("liquidar_tecnico", args=[self.tecnico.id])

    def usuario(self, nombre, sede, rol):
        user = get_user_model().objects.create_user(username=nombre)
        user.profile.rol = rol
        user.profile.sede_principal = sede
        user.profile.save()
        return user

    def equipo(self, codigo, sede):
        return ItemSerializado.objects.create(
            producto=self.p, serial="SN-" + codigo, codigo_trazabilidad=codigo,
            estado="ASIGNADO", asignado_a=self.tecnico, sede_asignacion=sede,
        )

    def sede_b_intacto(self):
        self.mh.refresh_from_db(); self.sh.refresh_from_db()
        self.assertEqual(self.mh.cantidad, 3)
        self.assertEqual(self.sh.cantidad, 10)
        for e in (self.h1, self.h2, self.h3):
            e.refresh_from_db()
            self.assertEqual((e.estado, e.asignado_a_id, e.sede_asignacion_id), ("ASIGNADO", self.tecnico.id, self.h.id))


class SedesBase(SedesMixin, TestCase):
    pass


class LiquidacionSedesTests(SedesBase):
    def test_pantalla_muestra_solo_los_dos_de_jauja(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual({e.id for e in response.context['equipos_asignados']}, {self.j1.id, self.j2.id})
        self.assertFalse(response.context['inconsistencias'])

    def test_consumo_no_toca_sede_b(self):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 302)
        self.sede_b_intacto()
        self.mj.refresh_from_db(); self.sj.refresh_from_db()
        self.assertEqual(self.mj.cantidad, 0)
        self.assertEqual(self.sj.cantidad, 20)
        for e in (self.j1, self.j2):
            e.refresh_from_db(); self.assertEqual(e.estado, 'INSTALADO')
        self.assertEqual(DocumentoItemSerializado.objects.filter(estado_resultante="INSTALADO").count(), 2)

    def test_retorno_y_merma_solo_en_sede_actual(self):
        self.client.post(self.url, {f'check_devuelto_{self.mj.id}': [self.j1.id], f'check_merma_{self.mj.id}': [self.j2.id]})
        self.sede_b_intacto()
        self.j1.refresh_from_db(); self.j2.refresh_from_db(); self.sj.refresh_from_db()
        self.assertEqual((self.j1.estado, self.j1.ubicacion_id, self.j1.sede_asignacion_id), ('EN_ALMACEN', self.uj.id, None))
        self.assertEqual(self.j2.estado, 'MERMA')
        self.assertEqual(self.sj.cantidad, 21)

    def test_seleccion_otra_sede_rechazada_y_rollback(self):
        response = self.client.post(self.url, {f'check_devuelto_{self.mj.id}': [self.h1.id]})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(DocumentoInventario.objects.exists())
        self.assertFalse(MovimientoInventario.objects.exists())
        self.mj.refresh_from_db(); self.assertEqual(self.mj.cantidad, 2)
        self.sede_b_intacto()

    def test_ids_repetidos_y_bueno_merma_simultaneo_rechazados(self):
        for data in (
            {f'check_devuelto_{self.mj.id}': [self.j1.id, self.j1.id]},
            {f'check_devuelto_{self.mj.id}': [self.j1.id], f'check_merma_{self.mj.id}': [self.j1.id]},
        ):
            with self.subTest(data=data):
                self.client.post(self.url, data)
                self.assertFalse(DocumentoInventario.objects.exists())

    def test_sin_sede_y_desfase_bloquean_sin_adivinar(self):
        self.j1.sede_asignacion = None; self.j1.save()
        response = self.client.get(self.url)
        self.assertTrue(response.context['inconsistencias'])
        self.client.post(self.url)
        self.assertFalse(DocumentoInventario.objects.exists())
        self.j1.refresh_from_db(); self.assertEqual(self.j1.estado, 'ASIGNADO')

    def test_equipos_de_proyecto_no_se_liquidan(self):
        from proyectos.models import Proyecto
        proyecto = Proyecto.objects.create(codigo="OBRA-TEST", nombre="Obra", sede=self.j, responsable=self.tecnico, creado_por=self.admin)
        self.j1.proyecto = proyecto; self.j1.save()
        response = self.client.get(self.url)
        self.assertNotIn(self.j1, response.context['equipos_asignados'])
        self.client.post(self.url)
        self.assertFalse(DocumentoInventario.objects.exists())

    def test_segundo_envio_no_duplica_ingreso(self):
        data = {f'check_devuelto_{self.mj.id}': [self.j1.id, self.j2.id]}
        self.client.post(self.url, data); self.client.post(self.url, data)
        self.assertEqual(DocumentoInventario.objects.count(), 1)
        self.assertEqual(MovimientoInventario.objects.count(), 1)
        self.sj.refresh_from_db(); self.assertEqual(self.sj.cantidad, 22)

    def test_herramienta_no_devuelta_conserva_custodia(self):
        self.p.es_activo = True; self.p.save()
        self.client.post(self.url, {f'check_devuelto_{self.mj.id}': [self.j1.id]})
        self.mj.refresh_from_db(); self.j2.refresh_from_db()
        self.assertEqual(self.mj.cantidad, 1)
        self.assertEqual(self.j2.estado, 'ASIGNADO')
        self.sede_b_intacto()

    def test_cantidad_negativa_no_crea_stock(self):
        self.p.es_serializado = False; self.p.save()
        self.client.post(self.url, {f'devuelto_{self.mj.id}': -1})
        self.assertFalse(DocumentoInventario.objects.exists())
        self.sj.refresh_from_db(); self.assertEqual(self.sj.cantidad, 20)


class DespachoSedesTests(SedesBase):
    def setUp(self):
        super().setUp()
        self.req = DocumentoInventario.objects.create(
            tipo=TipoDocumento.REQ, estado=EstadoDocumento.REQ_PENDIENTE,
            sede=self.j, responsable=self.tecnico, solicitante=self.tecnico,
            tipo_requerimiento=TipoRequerimiento.LOCAL,
        )
        self.linea = DocumentoItem.objects.create(documento=self.req, producto=self.p, cantidad=2)
        for e in (self.j1, self.j2):
            e.estado = 'EN_ALMACEN'; e.asignado_a = None; e.sede_asignacion = None; e.ubicacion = self.uj; e.save()
        self.mj.cantidad = 0; self.mj.save()
        self.url = reverse('req_atender', args=[self.req.id])

    def despachar(self, ids):
        return self.client.post(self.url, {f'qty_{self.linea.id}': 2, f'detalles_json_{self.linea.id}': json.dumps(ids)})

    def test_despacho_persiste_sede_y_seriales_de_salida(self):
        self.despachar([self.j1.id, self.j2.id])
        self.j1.refresh_from_db(); self.mj.refresh_from_db(); self.sj.refresh_from_db()
        self.assertEqual(self.j1.sede_asignacion_id, self.j.id)
        self.assertIsNone(self.j1.ubicacion_id)
        self.assertEqual(self.mj.cantidad, 2); self.assertEqual(self.sj.cantidad, 18)
        self.assertEqual(DocumentoItemSerializado.objects.filter(documento_item__documento__tipo='SAL', estado_resultante='ASIGNADO').count(), 2)
        self.despachar([self.j1.id, self.j2.id])
        self.assertEqual(DocumentoInventario.objects.filter(tipo='SAL').count(), 1)

    def test_ids_faltantes_duplicados_o_inexistentes_no_descontar(self):
        for ids in ([], [self.j1.id], [self.j1.id, self.j1.id], [self.j1.id, 99999]):
            with self.subTest(ids=ids):
                self.despachar(ids)
                self.assertFalse(MovimientoInventario.objects.exists())
                self.assertFalse(DocumentoInventario.objects.filter(tipo='SAL').exists())

    def test_otra_sede_y_estado_cambiado_rechazados(self):
        for ubicacion, estado in ((self.uh, 'EN_ALMACEN'), (self.uj, 'INSTALADO')):
            self.j2.ubicacion = ubicacion; self.j2.estado = estado; self.j2.save()
            self.despachar([self.j1.id, self.j2.id])
            self.assertFalse(MovimientoInventario.objects.exists())

    def test_rango_usa_codigo_pintado_no_comparacion_lexicografica_sn(self):
        self.j2.codigo_trazabilidad = '702'; self.j2.save()
        self.client.post(self.url, {f'qty_{self.linea.id}': 2, f'rango_inicio_{self.linea.id}': 701, f'rango_fin_{self.linea.id}': 702})
        self.assertEqual(DocumentoInventario.objects.filter(tipo='SAL').count(), 1)

    def test_confirmar_sin_seriales_no_carga_mochila(self):
        from django.core.exceptions import ValidationError
        sal = DocumentoInventario.objects.create(tipo='SAL', estado='BORRADOR', sede=self.j, responsable=self.almacen, solicitante=self.tecnico)
        DocumentoItem.objects.create(documento=sal, producto=self.p, cantidad=2)
        with self.assertRaises(ValidationError):
            sal.confirmar()
        self.assertFalse(MovimientoInventario.objects.exists())


class DevolucionPrestamoTests(SedesBase):
    def test_devolucion_rechaza_serial_ajeno_sin_alterar_cantidades(self):
        self.client.post(reverse('almacen_devolucion_rapida'), {
            'tecnico_id': self.tecnico.id, 'productos[]': [self.p.id], 'cantidades[]': [1],
            'seriales_json[]': [json.dumps([{'sn': self.h1.serial}])],
        })
        self.assertFalse(MovimientoInventario.objects.exists())
        self.assertFalse(DocumentoInventario.objects.exists())
        self.sede_b_intacto()

    def test_devolucion_valida_baja_solo_la_mochila_de_jauja(self):
        self.client.post(reverse('almacen_devolucion_rapida'), {
            'tecnico_id': self.tecnico.id, 'productos[]': [self.p.id], 'cantidades[]': [1],
            'seriales_json[]': [json.dumps([{'sn': self.j1.serial}])],
        })
        self.mj.refresh_from_db(); self.sj.refresh_from_db()
        self.assertEqual(self.mj.cantidad, 1); self.assertEqual(self.sj.cantidad, 21)
        self.sede_b_intacto()

    def test_prestamo_identifica_fila_de_sede_y_conserva_procedencia(self):
        self.client.force_login(self.tecnico)
        self.client.post(reverse('prestar_a_tecnico'), {'receptor_id': self.otro.id, f'qty_{self.mj.id}': 1, f'codigos_{self.mj.id}': '701'})
        self.j1.refresh_from_db(); self.mj.refresh_from_db()
        self.assertEqual((self.j1.asignado_a_id, self.j1.sede_asignacion_id), (self.otro.id, self.j.id))
        self.assertEqual(self.mj.cantidad, 1)
        self.assertEqual(StockTecnico.objects.get(tecnico=self.otro, producto=self.p, sede=self.j).cantidad, 1)
        self.sede_b_intacto()

    def test_prestamo_no_acepta_otro_origen(self):
        self.client.force_login(self.tecnico)
        self.client.post(reverse('prestar_a_tecnico'), {'receptor_id': self.otro.id, f'qty_{self.mj.id}': 1, f'codigos_{self.mj.id}': '801'})
        self.assertFalse(StockTecnico.objects.filter(tecnico=self.otro).exists())
        self.sede_b_intacto()


class ReparacionCustodiaTests(SedesBase):
    def setUp(self):
        super().setUp()
        self.j1.serial = 'SN-TEST-A'; self.j1.estado = 'INSTALADO'; self.j1.asignado_a = None; self.j1.sede_asignacion = None; self.j1.save()
        self.j2.serial = "SN-TEST-B"; self.j2.estado = 'EN_ALMACEN'; self.j2.asignado_a = None; self.j2.sede_asignacion = None; self.j2.ubicacion = self.uh; self.j2.save()
        self.sal = DocumentoInventario.objects.create(numero='SAL-TEST-A', tipo='SAL', estado='CONFIRMADO', sede=self.j, solicitante=self.tecnico, responsable=self.almacen)
        DocumentoItem.objects.create(documento=self.sal, producto=self.p, cantidad=2)
        self.ing = DocumentoInventario.objects.create(numero='ING-TEST-B', tipo='ING', estado='CONFIRMADO', sede=self.h, solicitante=self.tecnico, responsable=self.almacen, referencia='LIQ-SEMANAL')
        self.li = DocumentoItem.objects.create(documento=self.ing, producto=self.p, cantidad=4, cantidad_usada=2)
        MovimientoInventario.objects.create(producto=self.p, sede=self.h, tipo='IN', qty=4, referencia=self.ing.numero)

    def reparar(self, **kwargs):
        cfg = dict(clave="custodia-test", tecnico=self.tecnico.username, producto=self.p.codigo_interno,
            sede_origen=self.j.nombre, sede_destino=self.h.nombre, salida=self.sal.numero, ingreso=self.ing.numero,
            mochila_destino=3, retorno=4, consumo=2, merma=0,
            instalado=dict(codigo="701", serial="SN-TEST-A"), en_almacen=dict(codigo="702", serial="SN-TEST-B"))
        with TemporaryDirectory() as carpeta:
            archivo = Path(carpeta) / 'plan.json'
            archivo.write_text(json.dumps(cfg), encoding='utf-8')
            return call_command('reparar_custodia_entre_sedes', plan=str(archivo), stdout=StringIO(), **kwargs)

    def opciones(self):
        return dict(aplicar=True, usuario=self.admin.username, confirmar_retorno=self.ing.numero, stock_destino_esperado=10)

    def test_simulacion_no_escribe(self):
        self.reparar()
        self.assertFalse(CorreccionSeriales.objects.exists())
        self.j1.refresh_from_db(); self.assertEqual(self.j1.estado, 'INSTALADO')
        self.sh.refresh_from_db(); self.assertEqual(self.sh.cantidad, 10)

    def test_aplicar_sin_confirmacion_no_escribe(self):
        with self.assertRaises(CommandError):
            self.reparar(aplicar=True)
        self.assertFalse(CorreccionSeriales.objects.exists())

    def test_reparacion_una_vez_no_duplica_mochila_ni_salida(self):
        self.reparar(**self.opciones()); self.reparar(**self.opciones())
        for e in (self.j1, self.j2):
            e.refresh_from_db()
            self.assertEqual((e.estado, e.asignado_a_id, e.sede_asignacion_id), ('ASIGNADO', self.tecnico.id, self.j.id))
        self.mj.refresh_from_db(); self.mh.refresh_from_db(); self.sh.refresh_from_db(); self.sj.refresh_from_db(); self.li.refresh_from_db()
        self.assertEqual((self.mj.cantidad, self.mh.cantidad), (2, 3))
        self.assertEqual((self.sj.cantidad, self.sh.cantidad), (20, 9))
        self.assertEqual((self.li.cantidad, self.li.cantidad_usada), (4, 2))
        self.ing.refresh_from_db()
        self.assertIn('ADJ -1', self.ing.observaciones)
        self.assertEqual(DocumentoInventario.objects.filter(tipo='SAL').count(), 1)
        self.assertEqual(MovimientoInventario.objects.filter(tipo='ADJ').count(), 1)
        self.assertEqual(CorreccionSeriales.objects.count(), 1)
        self.assertEqual(DocumentoItemSerializado.objects.count(), 2)
        for e in (self.h1, self.h2, self.h3):
            e.refresh_from_db(); self.assertEqual(e.asignado_a_id, self.tecnico.id)

    def test_cambio_desde_diagnostico_rechaza(self):
        self.mj.cantidad = 3; self.mj.save()
        with self.assertRaises(CommandError): self.reparar(**self.opciones())
        self.assertFalse(CorreccionSeriales.objects.exists())

    def test_stock_distinto_al_revisado_rechaza(self):
        opciones = self.opciones(); opciones['stock_destino_esperado'] = 11
        with self.assertRaises(CommandError): self.reparar(**opciones)
        self.assertFalse(CorreccionSeriales.objects.exists())

    def test_fallo_al_final_revierte_toda_la_reparacion(self):
        with patch.object(CorreccionSeriales.objects, 'create', side_effect=RuntimeError('Fallo simulado')):
            with self.assertRaises(RuntimeError): self.reparar(**self.opciones())
        self.j1.refresh_from_db(); self.sh.refresh_from_db(); self.li.refresh_from_db()
        self.assertEqual(self.j1.estado, 'INSTALADO')
        self.assertEqual(self.sh.cantidad, 10)
        self.assertEqual((self.li.cantidad, self.li.cantidad_usada), (4, 2))
        self.assertFalse(MovimientoInventario.objects.filter(tipo='ADJ').exists())


class ConciliacionHistoricaTests(SedesBase):
    def setUp(self):
        super().setUp()
        self.sal = DocumentoInventario.objects.create(numero='SAL-TEST', tipo='SAL', estado='CONFIRMADO', sede=self.j, solicitante=self.tecnico, responsable=self.almacen)
        DocumentoItem.objects.create(documento=self.sal, producto=self.p, cantidad=2)
        for e in (self.j1, self.j2):
            e.sede_asignacion = None; e.save()
        self.args = dict(salida='SAL-TEST', producto=self.p.codigo_interno, codigos='701,702', stdout=StringIO())

    def test_simula_y_concilia_sin_mover_stock(self):
        call_command('conciliar_sede_seriales', **self.args)
        self.j1.refresh_from_db(); self.assertIsNone(self.j1.sede_asignacion_id)
        call_command('conciliar_sede_seriales', aplicar=True, usuario=self.admin.username, motivo='Vale de entrega revisado', **self.args)
        call_command('conciliar_sede_seriales', aplicar=True, usuario=self.admin.username, motivo='Vale de entrega revisado', **self.args)
        self.j1.refresh_from_db(); self.assertEqual(self.j1.sede_asignacion_id, self.j.id)
        self.mj.refresh_from_db(); self.assertEqual(self.mj.cantidad, 2)
        self.assertEqual(CorreccionSeriales.objects.count(), 1)
        self.assertFalse(MovimientoInventario.objects.exists())

    def test_no_reasigna_equipos_de_otra_sede(self):
        self.j1.sede_asignacion = self.h; self.j1.save()
        with self.assertRaises(CommandError): call_command('conciliar_sede_seriales', **self.args)
        self.assertFalse(CorreccionSeriales.objects.exists())


class TransferenciasSedesTests(SedesBase):
    def setUp(self):
        super().setUp()
        self.sal = DocumentoInventario.objects.create(numero='SAL-TRASPASO', tipo='SAL', estado='CONFIRMADO', sede=self.j, sede_destino=self.h, solicitante=self.tecnico, responsable=self.almacen)
        self.linea = DocumentoItem.objects.create(documento=self.sal, producto=self.p, cantidad=1)
        DocumentoItemSerializado.objects.create(documento_item=self.linea, item_serializado=self.j1)
        self.receptor = self.usuario('almacen_h', self.h, UserProfile.Rol.ALMACEN)
        self.client.force_login(self.receptor)
        self.url = reverse('almacen_recepcionar_traspaso', args=[self.sal.id])

    def test_recepciona_solo_serial_documentado(self):
        self.client.post(self.url); self.client.post(self.url)
        self.j1.refresh_from_db(); self.j2.refresh_from_db()
        self.assertEqual((self.j1.estado, self.j1.ubicacion_id), ('EN_ALMACEN', self.uh.id))
        self.assertEqual(self.j2.estado, 'ASIGNADO')
        self.assertEqual(MovimientoInventario.objects.count(), 1)
        self.sh.refresh_from_db(); self.assertEqual(self.sh.cantidad, 11)

    def test_sin_seriales_documentados_no_recibe_a_todos(self):
        self.linea.seriales_seleccionados.all().delete()
        self.client.post(self.url)
        self.assertFalse(MovimientoInventario.objects.exists())
        self.assertFalse(DocumentoInventario.objects.filter(tipo='ING').exists())
        self.j1.refresh_from_db(); self.assertEqual(self.j1.estado, 'ASIGNADO')

    def test_salida_local_no_puede_recibirse_como_transferencia(self):
        self.sal.sede = self.h; self.sal.save()
        self.client.post(self.url)
        self.assertFalse(MovimientoInventario.objects.exists())
        self.assertFalse(DocumentoInventario.objects.filter(tipo='ING').exists())

    def test_ingreso_preexistente_no_se_duplica(self):
        DocumentoInventario.objects.create(tipo='ING', estado='CONFIRMADO', sede=self.h, responsable=self.receptor, origen=self.sal)
        self.client.post(self.url)
        self.assertFalse(MovimientoInventario.objects.exists())
        self.assertEqual(DocumentoInventario.objects.filter(tipo='ING').count(), 1)


from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
from django.db import connection, connections, close_old_connections
from django.test import Client, TransactionTestCase


@skipUnless(connection.vendor == 'postgresql', 'Bloqueo de filas: requiere PostgreSQL')
class LiquidacionConcurrenteTests(SedesMixin, TransactionTestCase):
    def test_dos_solicitudes_crean_un_solo_retorno(self):
        barrera = Barrier(2)
        def enviar():
            close_old_connections()
            try:
                cliente = Client()
                cliente.force_login(get_user_model().objects.get(pk=self.almacen.pk))
                barrera.wait(timeout=10)
                return cliente.post(self.url, {f'check_devuelto_{self.mj.id}': [self.j1.id, self.j2.id]}).status_code
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as executor:
            resultados = list(executor.map(lambda _: enviar(), range(2)))
        self.assertIn(302, resultados)
        self.assertTrue(all(r in (200, 302) for r in resultados))
        self.assertEqual(DocumentoInventario.objects.filter(tipo='ING').count(), 1)
        self.assertEqual(MovimientoInventario.objects.count(), 1)
        self.sj.refresh_from_db(); self.assertEqual(self.sj.cantidad, 22)
        self.sede_b_intacto()
