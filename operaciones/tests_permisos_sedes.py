from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.test import TestCase, RequestFactory
from django.urls import reverse

from inventario.models import Sede, Producto, UserProfile
from operaciones.models import LiquidacionSemanal
from operaciones.views import LiquidacionListView


class PermisosLiquidacionSedeTests(TestCase):
    def setUp(self):
        self.a = Sede.objects.create(nombre='SEDE-PRUEBA-A', tipo=Sede.CENTRAL)
        self.b = Sede.objects.create(nombre='SEDE-PRUEBA-B', tipo=Sede.SECUNDARIO)
        self.user = get_user_model().objects.create_user(username='almacen_prueba')
        self.user.profile.rol = UserProfile.Rol.ALMACEN
        self.user.profile.sede_principal = self.a
        self.user.profile.sede_activa = self.b
        self.user.profile.save()
        self.user.profile.sedes_permitidas.set([self.a, self.b])
        self.client.force_login(self.user)

    @patch('operaciones.views.LiquidacionService.liquidar_sede')
    @patch('operaciones.views.puede_liquidar_hoy', return_value=(True, '', 0))
    def test_almacen_central_no_liquida_otra_sede_aunque_este_permitida(self, dia, servicio):
        for method in (self.client.get, self.client.post):
            response = method(reverse('liquidar_sede', args=[self.b.id]))
            self.assertEqual(response.status_code, 403)
        servicio.assert_not_called()

    @patch('operaciones.views.LiquidacionService.liquidar_central')
    @patch('operaciones.views.puede_liquidar_hoy', return_value=(True, '', 0))
    def test_almacen_central_no_ejecuta_consolidado(self, dia, servicio):
        self.assertEqual(self.client.post(reverse('liquidar_central')).status_code, 403)
        servicio.assert_not_called()

    @patch('operaciones.views.LiquidacionService.liquidar_sede', return_value=[])
    @patch('operaciones.views.puede_liquidar_hoy', return_value=(True, '', 0))
    def test_almacen_conserva_liquidacion_de_su_propia_sede(self, dia, servicio):
        self.assertEqual(self.client.post(reverse('liquidar_sede', args=[self.a.id])).status_code, 302)
        servicio.assert_called_once()
        self.assertEqual(servicio.call_args.args[0], self.a.id)
        self.assertEqual(servicio.call_args.args[3], self.user.id)

    @patch('operaciones.views.LiquidacionService.liquidar_sede')
    def test_almacen_sin_principal_no_toma_sede_activa_ni_permitidas(self, servicio):
        self.user.profile.sede_principal = None
        self.user.profile.save()
        self.assertEqual(self.client.post(reverse('liquidar_sede', args=[self.b.id])).status_code, 403)
        servicio.assert_not_called()

    @patch('operaciones.views.LiquidacionService.liquidar_sede', return_value=[])
    @patch('operaciones.views.LiquidacionService.liquidar_central', return_value=[])
    @patch('operaciones.views.puede_liquidar_hoy', return_value=(True, '', 0))
    def test_admin_y_jefa_conservan_operacion_global(self, dia, central, sede):
        for rol in (UserProfile.Rol.ADMIN, UserProfile.Rol.JEFA):
            self.user.profile.rol = rol
            self.user.profile.save()
            self.assertEqual(self.client.post(reverse('liquidar_sede', args=[self.b.id])).status_code, 302)
            self.assertEqual(self.client.post(reverse('liquidar_central')).status_code, 302)
        self.assertEqual(central.call_count, 2)
        self.assertEqual(sede.call_count, 2)

    def test_dashboard_no_ofrece_otras_sedes_a_almacen_central(self):
        # El repositorio no incluye la plantilla del módulo antiguo. Se comprueba
        # el contexto autorizado sin depender de esa plantilla ausente.
        with patch('operaciones.views.render', return_value=HttpResponse('ok')) as render:
            response = self.client.get(reverse('liquidacion_dashboard'), {'sede_id': self.b.id})
        self.assertEqual(response.status_code, 200)
        context = render.call_args.args[2]
        self.assertEqual(list(context['sedes_disponibles']), [self.a])
        self.assertFalse(context['puede_liquidar_central'])

    def test_lista_y_detalle_no_exponen_liquidacion_de_otra_sede(self):
        producto = Producto.objects.create(nombre='Producto de prueba', codigo_interno='PROD-PERMISOS')
        registros = [LiquidacionSemanal.objects.create(sede=sede, producto=producto,
            fecha_liquidacion=date(2026, 1, 5), semana=2, anio=2026) for sede in (self.a, self.b)]
        request = RequestFactory().get(reverse('liquidacion_lista'))
        request.user = self.user
        view = LiquidacionListView()
        view.setup(request)
        self.assertEqual(list(view.get_queryset()), [registros[0]])
        self.assertEqual(self.client.get(reverse('liquidacion_detalle', args=[registros[1].id])).status_code, 404)

    @patch('operaciones.views.LiquidacionService.liquidar_sede')
    def test_tecnico_no_liquida_ni_consulta_lista_de_almacen(self, servicio):
        self.user.profile.rol = UserProfile.Rol.SOLICITANTE
        self.user.profile.save()
        self.assertEqual(self.client.post(reverse('liquidar_sede', args=[self.a.id])).status_code, 403)
        self.assertEqual(self.client.get(reverse('liquidacion_lista')).status_code, 403)
        servicio.assert_not_called()
