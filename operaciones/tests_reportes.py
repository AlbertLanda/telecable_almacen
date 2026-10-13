from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from inventario.models import (DocumentoInventario, DocumentoItem, DocumentoItemSerializado,
                               ItemSerializado, Producto, Sede, UserProfile)
from operaciones.reportes import items_con_trazabilidad


class ReporteSerialesTests(TestCase):
    def setUp(self):
        self.sede = Sede.objects.create(nombre='Sede reporte A')
        self.otra = Sede.objects.create(nombre='Sede reporte B')
        self.tecnico = get_user_model().objects.create_user(username='tecnico_reporte')
        self.tecnico.profile.rol = UserProfile.Rol.SOLICITANTE
        self.tecnico.profile.save()
        self.p = Producto.objects.create(nombre='ONU de prueba', codigo_interno='ONU-REPORTE', es_serializado=True)
        self.doc = DocumentoInventario.objects.create(tipo='ING', numero='ING-REPORTE',
            estado='CONFIRMADO', referencia='LIQ-SEMANAL', sede=self.sede,
            solicitante=self.tecnico, responsable=self.tecnico)
        self.item = DocumentoItem.objects.create(documento=self.doc, producto=self.p,
            cantidad=1, cantidad_usada=1, cantidad_merma=1)
        self.url = reverse('liquidacion_tecnico_print', args=[self.doc.id])
        self.client.force_login(self.tecnico)

    def vinculo(self, codigo, resultado, estado_actual='ASIGNADO', producto=None):
        equipo = ItemSerializado.objects.create(producto=producto or self.p,
            codigo_trazabilidad=codigo, serial='SN-'+codigo, estado=estado_actual)
        DocumentoItemSerializado.objects.create(documento_item=self.item,
            item_serializado=equipo, estado_resultante=resultado)
        return equipo

    def test_resultado_historico_no_cambia_con_estado_actual(self):
        self.vinculo('801', 'EN_ALMACEN', 'INSTALADO')
        self.vinculo('802', 'INSTALADO', 'EN_ALMACEN')
        self.vinculo('803', 'MERMA', 'ASIGNADO')
        item = items_con_trazabilidad(self.doc)[0]
        self.assertEqual([[e.codigo_trazabilidad for e in g['equipos']] for g in item.detalle_equipos],
                         [['801'], ['802'], ['803']])
        self.assertFalse(item.trazabilidad_incompleta)
        response = self.client.get(self.url)
        self.assertContains(response, '#801')
        self.assertContains(response, 'SN-802')
        self.assertContains(response, 'Devueltas dañadas (merma)')

    def test_documento_antiguo_no_infiere_equipos_actuales(self):
        ItemSerializado.objects.create(producto=self.p, serial='SN-ACTUAL',
            codigo_trazabilidad='999', estado='ASIGNADO', asignado_a=self.tecnico)
        response = self.client.get(self.url)
        self.assertContains(response, 'Sin trazabilidad histórica')
        self.assertNotContains(response, 'SN-ACTUAL')

    def test_vinculo_antiguo_sin_resultado_no_infiere_consumo(self):
        self.vinculo('804', '', 'INSTALADO')
        item = items_con_trazabilidad(self.doc)[0]
        self.assertEqual([len(g['equipos']) for g in item.detalle_equipos], [0,0,0])
        self.assertEqual([e.codigo_trazabilidad for e in item.equipos_sin_resultado], ['804'])
        self.assertTrue(item.trazabilidad_incompleta)

    def test_avisa_si_las_cantidades_no_coinciden(self):
        self.vinculo('805', 'EN_ALMACEN')
        self.assertContains(self.client.get(self.url), 'Trazabilidad incompleta o cantidades diferentes')

    def test_no_muestra_resultado_de_otro_producto_como_valido(self):
        otro = Producto.objects.create(nombre='Otro', codigo_interno='OTRO-REPORTE', es_serializado=True)
        self.vinculo('806', 'EN_ALMACEN', producto=otro)
        item = items_con_trazabilidad(self.doc)[0]
        self.assertEqual(item.detalle_equipos[0]['equipos'], [])
        self.assertEqual(len(item.equipos_sin_resultado), 1)

    def test_prefetch_y_escape_de_identificadores(self):
        self.vinculo('<script>mal</script>', 'EN_ALMACEN')
        with self.assertNumQueries(2):
            items_con_trazabilidad(self.doc)
        response = self.client.get(self.url)
        self.assertContains(response, '&lt;script&gt;mal&lt;/script&gt;')
        self.assertNotContains(response, '<script>mal</script>')

    def test_almacen_no_ve_el_reporte_de_otra_sede(self):
        user = get_user_model().objects.create_user(username='almacen_reporte')
        user.profile.rol = UserProfile.Rol.ALMACEN
        user.profile.sede_principal = self.otra
        user.profile.save()
        user.profile.sedes_permitidas.add(self.sede)
        self.client.force_login(user)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        user.profile.sede_principal = self.sede
        user.profile.save()
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_otro_tecnico_no_ve_el_reporte_y_admin_si(self):
        user = get_user_model().objects.create_user(username='otro_reporte')
        user.profile.rol = UserProfile.Rol.SOLICITANTE
        user.profile.save()
        self.client.force_login(user)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        user.profile.rol = UserProfile.Rol.ADMIN
        user.profile.save()
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_otra_clase_de_documento_no_se_presenta_como_liquidacion(self):
        self.doc.referencia = 'Ingreso de compra'
        self.doc.save(update_fields=['referencia'])
        self.assertEqual(self.client.get(self.url).status_code, 404)
