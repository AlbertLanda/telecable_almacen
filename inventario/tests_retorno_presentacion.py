"""Seguimiento del retorno en ambas sedes, comprobante y receptor de la firma."""
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from inventario.models import (
    DocumentoInventario, DocumentoItem, EstadoDocumento, FirmaDocumento,
    Producto, Sede, TipoDocumento, TipoRequerimiento, UserProfile,
)
from inventario.services.firma_service import clase_documento, firmante_esperado, notas_para_firmar, TRASLADO


@override_settings(FIRMA_NOTAS_DESDE="2000-01-01")
class RetornoPresentacionTests(TestCase):
    def setUp(self):
        self.origen = Sede.objects.create(nombre="Oroya presentación", tipo=Sede.SECUNDARIO)
        self.central = Sede.objects.create(nombre="Jauja presentación", tipo=Sede.CENTRAL)
        self.emisor = self.usuario("emisor_retorno_ui", self.origen)
        self.receptor = self.usuario("receptor_retorno_ui", self.central)
        self.otro_central = self.usuario("otro_central_ui", self.central)
        self.producto = Producto.objects.create(nombre="UTP de prueba", codigo_interno="RET-UI-UTP", unidad="MTS")
        self.salida = DocumentoInventario.objects.create(
            tipo=TipoDocumento.SAL, tipo_requerimiento=TipoRequerimiento.RETORNO_CENTRAL,
            estado=EstadoDocumento.CONFIRMADO, numero="SAL-RET-UI-001",
            sede=self.origen, sede_origen=self.origen, sede_destino=self.central,
            responsable=self.emisor, solicitante=self.emisor, origen=None,
            referencia="Retorno a central", observaciones="Material sobrante",
        )
        DocumentoItem.objects.create(documento=self.salida, producto=self.producto, cantidad=12)

    def usuario(self, nombre, sede):
        usuario = get_user_model().objects.create_user(username=nombre)
        usuario.profile.rol = UserProfile.Rol.ALMACEN
        usuario.profile.sede_principal = sede
        usuario.profile.save()
        return usuario

    def ver(self, usuario, ruta, *args):
        self.client.force_login(usuario)
        return self.client.get(reverse(ruta, args=args))

    def marcar_recibido(self):
        self.salida.recibido = True
        self.salida.recibido_por = self.receptor
        self.salida.recibido_en = timezone.now()
        self.salida.save(update_fields=["recibido", "recibido_por", "recibido_en"])
        return DocumentoInventario.objects.create(
            tipo=TipoDocumento.ING, tipo_requerimiento=TipoRequerimiento.RETORNO_CENTRAL,
            estado=EstadoDocumento.CONFIRMADO, numero="ING-RET-UI-001",
            sede=self.central, sede_origen=self.origen, origen=self.salida,
            responsable=self.receptor, referencia=self.salida.numero,
        )

    def test_historial_ambas_sedes_sin_duplicar_ni_parecer_salida_tecnica(self):
        for usuario in (self.emisor, self.receptor):
            with self.subTest(usuario=usuario.username):
                respuesta = self.ver(usuario, "almacen_historial_global")
                self.assertEqual(respuesta.status_code, 200)
                filas = list(respuesta.context["historial"])
                self.assertEqual([(f.id, f.tipo_movimiento) for f in filas], [(self.salida.id, "RETORNO_CENTRAL")])
                self.assertContains(respuesta, "En tránsito")
                self.assertContains(respuesta, self.salida.numero)
                self.assertContains(respuesta, self.origen.nombre)
                self.assertContains(respuesta, self.central.nombre)
                self.assertNotContains(respuesta, "Salida a Técnico")
        ingreso = self.marcar_recibido()
        for usuario in (self.emisor, self.receptor):
            respuesta = self.ver(usuario, "almacen_historial_global")
            self.assertEqual(len(respuesta.context["historial"]), 1)
            self.assertContains(respuesta, "Recibido")
            self.assertContains(respuesta, ingreso.numero)

    def test_vale_y_detalle_identifican_retorno_unidad_y_recepcion(self):
        vale = self.ver(self.emisor, "sal_print", self.salida.id)
        self.assertContains(vale, "VALE DE RETORNO")
        self.assertContains(vale, "12 MTS")
        self.assertContains(vale, self.central.nombre)
        self.assertNotContains(vale, "RESP. CUADRILLA")
        self.assertNotContains(vale, "ORIGEN (REQ)")
        detalle = self.ver(self.receptor, "sal_detail", self.salida.id)
        self.assertContains(detalle, "12 <small>MTS</small>", html=True)
        self.assertContains(detalle, "cantidad y unidad")
        self.assertContains(detalle, "deja el retorno pendiente")
        ingreso = self.marcar_recibido()
        vale = self.ver(self.receptor, "sal_print", self.salida.id)
        self.assertContains(vale, ingreso.numero)
        self.assertContains(vale, self.receptor.username)
        self.assertContains(vale, "RECIBIDO")

    def test_firma_recepcion_corresponde_al_almacenero_que_recibio(self):
        rol = FirmaDocumento.Rol.RECEPTOR
        self.assertEqual(clase_documento(self.salida), TRASLADO)
        self.assertIsNone(firmante_esperado(self.salida, rol))
        self.assertNotIn(self.salida, notas_para_firmar(self.receptor, rol)[0])
        self.assertIn(self.salida, notas_para_firmar(self.emisor, FirmaDocumento.Rol.ALMACEN)[0])
        self.marcar_recibido()
        self.assertEqual(firmante_esperado(self.salida, rol), self.receptor)
        self.assertIn(self.salida, notas_para_firmar(self.receptor, rol)[0])
        self.assertNotIn(self.salida, notas_para_firmar(self.otro_central, rol)[0])
        enlace = reverse("almacen_firmar_recepcion", args=[self.salida.id])
        self.assertContains(self.ver(self.receptor, "sal_detail", self.salida.id), enlace)
        self.assertNotContains(self.ver(self.otro_central, "sal_detail", self.salida.id), enlace)

    def test_historial_admite_retorno_sin_solicitante(self):
        self.salida.solicitante = None
        self.salida.save(update_fields=["solicitante"])
        for usuario in (self.emisor, self.receptor):
            respuesta = self.ver(usuario, "almacen_historial_global")
            filas = list(respuesta.context["historial"])
            self.assertEqual([(f.id, f.tipo_movimiento) for f in filas], [(self.salida.id, "RETORNO_CENTRAL")])
