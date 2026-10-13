from django.test import TestCase
from django.urls import reverse

from inventario.tests_sedes import SedesMixin
from inventario.models import (
    CorreccionSeriales, DocumentoInventario, DocumentoItem, DocumentoItemSerializado,
    MovimientoInventario, StockTecnico, UserProfile,
)


class LiquidacionCruzadaTests(SedesMixin, TestCase):
    def payload(self, filas=None, **campos):
        return {'mochila_version': self.client.get(self.url).context['mochila_version'],
                'filas': filas or [self.mj.id], **campos}

    def test_recepcion_en_otra_sede_cierra_origen_y_conserva_la_otra_carga(self):
        receptor = self.usuario('receptor', self.h, UserProfile.Rol.ALMACEN)
        self.client.force_login(receptor)
        viejo = self.payload(**{f'check_devuelto_{self.mj.id}': [self.j1.id]})
        self.assertEqual(self.client.post(self.url, viejo).status_code, 302)
        self.mj.refresh_from_db(); self.mh.refresh_from_db()
        self.sj.refresh_from_db(); self.sh.refresh_from_db()
        self.assertEqual((self.mj.cantidad, self.mh.cantidad, self.sj.cantidad, self.sh.cantidad), (0, 3, 20, 11))
        self.j1.refresh_from_db(); self.j2.refresh_from_db()
        self.assertEqual((self.j1.estado, self.j1.ubicacion_id), ('EN_ALMACEN', self.uh.id))
        self.assertEqual(self.j2.estado, 'INSTALADO')
        doc = DocumentoInventario.objects.get(tipo='ING')
        self.assertEqual((doc.sede_id, doc.sede_origen_id), (self.h.id, self.j.id))
        self.assertEqual(DocumentoItemSerializado.objects.filter(estado_resultante='INSTALADO').count(), 1)
        self.client.post(self.url, viejo)
        self.assertEqual(MovimientoInventario.objects.count(), 1)
        self.client.force_login(self.almacen)
        self.assertNotIn(self.mj.id, [i.id for i in self.client.get(self.url).context['mochila']])
        reporte = self.client.get(reverse('liquidacion_tecnico_print', args=[doc.pk]))
        self.assertContains(reporte, self.j.nombre)
        self.assertContains(reporte, self.h.nombre)
        self.assertContains(reporte, '#701')
        self.assertContains(reporte, '#702')
        tercero = self.usuario('tercero', None, UserProfile.Rol.ALMACEN)
        self.client.force_login(tercero)
        self.assertEqual(self.client.get(reverse('liquidacion_tecnico_print', args=[doc.pk])).status_code, 403)

    def test_mismo_producto_dos_origenes_documentados_por_separado(self):
        self.client.post(self.url, self.payload([self.mj.id, self.mh.id], **{
            f'check_devuelto_{self.mj.id}': [self.j1.id],
            f'check_merma_{self.mh.id}': [self.h1.id],
        }))
        docs = list(DocumentoInventario.objects.filter(tipo='ING').order_by('sede_origen_id'))
        self.assertEqual(len(docs), 2)
        self.assertEqual([d.sede_origen_id for d in docs], [self.j.id, self.h.id])
        self.assertEqual([d.items.get().cantidad_usada for d in docs], [1, 2])
        self.assertFalse(StockTecnico.objects.filter(tecnico=self.tecnico, cantidad__gt=0).exists())

    def test_formulario_viejo_no_consume_nuevas_cargas_ni_retorna_dos_veces(self):
        viejo = self.payload()
        self.client.post(self.url, viejo)
        self.mj.refresh_from_db(); self.mj.cantidad = 2; self.mj.save()
        self.client.post(self.url, viejo)
        self.mj.refresh_from_db()
        self.assertEqual(self.mj.cantidad, 2)
        self.assertEqual(DocumentoInventario.objects.count(), 1)

    def test_snapshot_obligatorio_y_filas_explicitas(self):
        token = self.payload()['mochila_version']
        for data in ({}, {'filas': [self.mj.pk]}, {'mochila_version': token},
                     {'mochila_version': token+'x', 'filas': [self.mj.pk]},
                     {'mochila_version': token, 'filas': [self.mj.pk, self.mj.pk]},
                     {'mochila_version': token, 'filas': [99999]}):
            with self.subTest(data=data):
                self.client.post(self.url, data)
                self.assertFalse(DocumentoInventario.objects.exists())

    def test_snapshot_no_reutilizable_por_otro_usuario(self):
        data = self.payload()
        self.client.force_login(self.admin)
        self.client.post(self.url, data)
        self.assertFalse(DocumentoInventario.objects.exists())

    def test_fila_inconsistente_no_bloquea_otras_y_error_revierte_todo(self):
        self.j1.sede_asignacion = None; self.j1.save()
        self.client.post(self.url, self.payload([self.mj.pk, self.mh.pk]))
        self.assertFalse(DocumentoInventario.objects.exists())
        self.client.post(self.url, self.payload([self.mh.pk]))
        self.mj.refresh_from_db(); self.mh.refresh_from_db()
        self.assertEqual((self.mj.cantidad, self.mh.cantidad), (2, 0))

    def test_lista_incluye_tecnico_con_saldo_solo_en_otra_sede(self):
        self.mj.cantidad = 0; self.mj.save()
        response = self.client.get(reverse('liquidacion_tecnico_lista'))
        self.assertContains(response, self.tecnico.username)
        self.assertContains(response, self.h.nombre)


class ConciliarTransferenciaTests(SedesMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.receptor = self.usuario('receptor', self.h, UserProfile.Rol.ALMACEN)
        self.sal = DocumentoInventario.objects.create(tipo='SAL', estado='CONFIRMADO', numero='SAL-PRUEBA',
            sede=self.j, sede_destino=self.h, solicitante=self.receptor, responsable=self.almacen)
        self.linea = DocumentoItem.objects.create(documento=self.sal, producto=self.p, cantidad=2)
        for e in (self.j1, self.j2):
            e.asignado_a = self.receptor; e.sede_asignacion = None; e.save()
        self.client.force_login(self.receptor)
        self.url = reverse('conciliar_traspaso', args=[self.sal.pk])

    def data(self, ids=None):
        return {'confirmacion_fisica': 'si', f'equipos_{self.linea.pk}': ids or [self.j1.pk, self.j2.pk]}

    def test_lectura_no_infiere_y_recepcion_redirige(self):
        response = self.client.get(self.url)
        self.assertContains(response, '#701')
        self.assertContains(response, '#702')
        self.assertFalse(DocumentoItemSerializado.objects.exists())
        response = self.client.post(reverse('almacen_recepcionar_traspaso', args=[self.sal.pk]))
        self.assertRedirects(response, self.url)
        self.assertFalse(MovimientoInventario.objects.exists())

    def test_identificar_muestra_numeros_sin_stock_y_recepciona_una_vez(self):
        self.client.post(self.url, self.data())
        self.client.post(self.url, self.data())
        self.assertEqual(CorreccionSeriales.objects.count(), 1)
        self.assertEqual(DocumentoItemSerializado.objects.count(), 2)
        self.assertFalse(MovimientoInventario.objects.exists())
        self.sal.refresh_from_db(); self.assertFalse(self.sal.recibido)
        response = self.client.get(reverse('sal_detail', args=[self.sal.pk]))
        self.assertContains(response, '701</span>', html=False)
        self.sh.refresh_from_db(); self.assertEqual(self.sh.cantidad, 10)
        recepcion = reverse('almacen_recepcionar_traspaso', args=[self.sal.pk])
        self.client.post(recepcion); self.client.post(recepcion)
        self.assertEqual(MovimientoInventario.objects.count(), 1)
        self.sh.refresh_from_db(); self.assertEqual(self.sh.cantidad, 12)
        self.j1.refresh_from_db(); self.assertEqual(self.j1.ubicacion_id, self.uh.pk)

    def test_cantidad_identidad_y_confirmacion_obligatorias(self):
        for data in ({}, self.data([self.j1.pk]), self.data([self.j1.pk, self.j1.pk]), self.data([self.j1.pk, self.h1.pk])):
            with self.subTest(data=data):
                self.client.post(self.url, data)
                self.assertFalse(DocumentoItemSerializado.objects.exists())
                self.assertFalse(CorreccionSeriales.objects.exists())

    def test_equipo_identificado_en_otra_salida_no_se_adjudica(self):
        otra = DocumentoInventario.objects.create(tipo='SAL', estado='CONFIRMADO', sede=self.j, sede_destino=self.h,
            solicitante=self.receptor, responsable=self.almacen)
        item = DocumentoItem.objects.create(documento=otra, producto=self.p, cantidad=1)
        DocumentoItemSerializado.objects.create(documento_item=item, item_serializado=self.j1)
        self.client.post(self.url, self.data())
        self.assertFalse(self.linea.seriales_seleccionados.exists())
        self.assertFalse(CorreccionSeriales.objects.exists())

    def test_origen_no_puede_confirmar_en_nombre_del_receptor(self):
        self.client.force_login(self.almacen)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.post(self.url, self.data()).status_code, 403)
        self.assertFalse(DocumentoItemSerializado.objects.exists())
