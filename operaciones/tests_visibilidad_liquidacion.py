from unittest.mock import patch

from django.core.exceptions import PermissionDenied
from django.test import TestCase
from django.urls import reverse

from inventario.models import DocumentoInventario, MovimientoInventario, StockTecnico, UserProfile
from inventario.tests_sedes import SedesMixin
from operaciones.liquidacion import bloquear_seleccion, preparar_mochila


class VisibilidadLiquidacionTests(SedesMixin, TestCase):
    def lista(self):
        response = self.client.get(reverse('liquidacion_tecnico_lista'))
        self.assertEqual(response.status_code, 200)
        return {t.pk for t in response.context['tecnicos']}

    def test_solo_cargas_de_la_sede_sin_importar_sede_del_perfil_del_tecnico(self):
        # Otro técnico vive en B, pero recibió una carga de A: A sí lo ve.
        StockTecnico.objects.create(tecnico=self.otro, producto=self.p, sede=self.j, cantidad=1)
        # Un técnico de A con carga exclusivamente de B no se muestra en A.
        exclusivo = self.usuario('exclusivo_b', self.j, UserProfile.Rol.SOLICITANTE)
        StockTecnico.objects.create(tecnico=exclusivo, producto=self.p, sede=self.h, cantidad=2)
        self.assertEqual(self.lista(), {self.tecnico.pk, self.otro.pk})
        self.assertEqual(self.client.get(reverse('liquidar_tecnico', args=[exclusivo.pk])).status_code, 403)

    def test_tecnico_compartido_visible_para_ambas_sedes_con_origenes_separados(self):
        receptor = self.usuario('almacen_b', self.h, UserProfile.Rol.ALMACEN)
        for usuario in (self.almacen, receptor):
            self.client.force_login(usuario)
            self.assertIn(self.tecnico.pk, self.lista())
            response = self.client.get(self.url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual({i.sede_id for i in response.context['mochila']}, {self.j.pk, self.h.pk})

    def test_saldo_cero_o_sin_origen_no_habilita_lista_ni_enlaces_ni_post(self):
        token = self.client.get(self.url).context['mochila_version']
        for cantidad, sede in ((0, self.j), (2, None)):
            self.mj.cantidad, self.mj.sede = cantidad, sede
            self.mj.save()
            self.assertNotIn(self.tecnico.pk, self.lista())
            self.assertEqual(self.client.get(self.url).status_code, 403)
            self.assertEqual(self.client.post(self.url, {
                'mochila_version': token, 'filas': [self.mh.pk],
            }).status_code, 403)
        self.assertFalse(DocumentoInventario.objects.exists())
        self.assertFalse(MovimientoInventario.objects.exists())

    def test_sedes_permitidas_y_activa_no_amplian_el_acceso(self):
        self.mj.cantidad = 0; self.mj.save()
        perfil = self.almacen.profile
        perfil.sede_activa = self.h
        perfil.sedes_permitidas.add(self.h)
        perfil.save()
        self.assertNotIn(self.tecnico.pk, self.lista())
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_sin_sede_principal_no_hay_acceso(self):
        perfil = self.almacen.profile
        perfil.sede_principal = None
        perfil.sede_activa = self.h
        perfil.sedes_permitidas.add(self.h)
        perfil.save()
        self.assertEqual(self.client.get(reverse('liquidacion_tecnico_lista')).status_code, 403)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_admin_y_jefa_conservan_supervision_global(self):
        self.mj.cantidad = 0; self.mj.save()
        jefa = self.usuario('jefa', self.j, UserProfile.Rol.JEFA)
        for usuario in (self.admin, jefa):
            self.client.force_login(usuario)
            self.assertIn(self.tecnico.pk, self.lista())
            self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_otra_sede_liquida_la_carga_que_habilitaba_el_acceso_durante_el_post(self):
        token = self.client.get(self.url).context['mochila_version']

        def cerrar_carga_antes_del_bloqueo(*args):
            StockTecnico.objects.filter(pk=self.mj.pk).update(cantidad=0)
            return bloquear_seleccion(*args)

        with patch('operaciones.liquidacion.bloquear_seleccion', side_effect=cerrar_carga_antes_del_bloqueo):
            response = self.client.post(self.url, {'mochila_version': token, 'filas': [self.mh.pk]})
        self.assertEqual(response.status_code, 403)
        self.mh.refresh_from_db()
        self.assertEqual(self.mh.cantidad, 3)
        self.assertFalse(DocumentoInventario.objects.exists())

    def test_preparar_formulario_tambien_verifica_carga_propia(self):
        self.mj.cantidad = 0; self.mj.save()
        with self.assertRaises(PermissionDenied):
            preparar_mochila(self.tecnico, self.almacen, self.j)
