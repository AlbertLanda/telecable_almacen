"""Las impresiones conservan su acceso, pero los trazos requieren participación."""
import base64

from django.contrib.auth.models import AnonymousUser
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from inventario.models import (
    DocumentoInventario, DocumentoItem, EstadoDocumento, FirmaDocumento,
    TipoDocumento, UserProfile,
)
from inventario.services.firma_service import (
    acta_firmas_para_mostrar, acta_hash, firmas_para_mostrar,
    puede_ver_firmas_obra, registrar_firma_acta,
)
from inventario.tests_firma import EntreSedesMixin, png_base64
from proyectos.models import (
    AsignacionCuadrilla, EstadoProyecto, Proyecto, ProyectoAsignacion, ProyectoMaterial,
)


@override_settings(FIRMA_NOTAS_DESDE="2000-01-01")
class PrivacidadFirmasTests(EntreSedesMixin, TestCase):
    def trazo(self, firma):
        return "data:image/png;base64," + base64.b64encode(bytes(firma.trazo_png)).decode("ascii")

    def ver(self, usuario, ruta, objeto):
        self.client.force_login(usuario)
        return self.client.get(reverse(ruta, args=[objeto.id]))

    def assert_restringida(self, respuesta, firma):
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Firmas restringidas")
        self.assertNotContains(respuesta, self.trazo(firma))
        self.assertNotContains(respuesta, "Pendiente de firma")
        self.assertNotContains(respuesta, "Firmado electrónicamente por")

    def obra_firmada(self):
        obra = Proyecto.objects.create(
            codigo="OBRA-PRIV-001", nombre="Obra privacidad", sede=self.sede,
            creado_por=self.almacen, responsable=self.resp,
            estado=EstadoProyecto.FINALIZADO, fin=timezone.now(), liquidado_por=self.almacen,
        )
        ProyectoMaterial.objects.create(
            proyecto=obra, producto=self.producto, cantidad_planificada=7,
            cantidad_entregada=7, cantidad_usada=7,
        )
        doc = DocumentoInventario.objects.create(
            tipo=TipoDocumento.SAL, estado=EstadoDocumento.CONFIRMADO,
            numero="SAL-PRIV-OBRA", sede=self.sede, responsable=self.almacen,
            solicitante=self.resp, retirado_por=self.crew, referencia=obra.codigo,
        )
        DocumentoItem.objects.create(documento=doc, producto=self.producto, cantidad=7)
        firma_salida = self.firmar(doc, self.resp)
        firma_acta = registrar_firma_acta(
            proyecto_id=obra.id, usuario=self.resp, rol=FirmaDocumento.Rol.RECEPTOR,
            trazo=png_base64(), sha256_visto=acta_hash(obra), acepto=True,
        )
        return obra, doc, firma_salida, firma_acta

    def test_sal_almacen_ajeno_y_disenador_no_reciben_trazos(self):
        sal = self.nota_salida(self.crew)
        firma = self.firmar(sal, self.crew)
        disenador = self.usuario("disenador_priv", UserProfile.Rol.DISENADOR)
        for usuario in (self.alm_oroya, disenador):
            with self.subTest(usuario=usuario.username):
                self.assert_restringida(self.ver(usuario, "sal_print", sal), firma)
        self.assertEqual(FirmaDocumento.objects.count(), 1)

    def test_sal_participantes_jefatura_y_superusuario_conservan_trazo(self):
        sal = self.nota_salida(self.crew)
        firma = self.firmar(sal, self.crew)
        admin = self.usuario("admin_priv", UserProfile.Rol.ADMIN)
        jefa = self.usuario("jefa_priv", UserProfile.Rol.JEFA)
        superuser = self.usuario("super_priv", UserProfile.Rol.SOLICITANTE)
        superuser.is_superuser = True
        superuser.save(update_fields=["is_superuser"])
        for usuario in (self.almacen, self.crew, self.resp, admin, jefa, superuser):
            with self.subTest(usuario=usuario.username):
                respuesta = self.ver(usuario, "sal_print", sal)
                self.assertContains(respuesta, self.trazo(firma))
                self.assertNotContains(respuesta, "Firmas restringidas")

    def test_traslado_y_pedido_ambas_sedes_conservan_trazos(self):
        req, sal = self.pedido_atendido(recibido=True)
        firma_req = self.firmar(req, self.alm_oroya)
        firma_sal = self.firmar(sal, self.almacen, rol=FirmaDocumento.Rol.ALMACEN)
        for usuario in (self.almacen, self.alm_oroya):
            with self.subTest(usuario=usuario.username):
                self.assertContains(self.ver(usuario, "sal_print", sal), self.trazo(firma_sal))
                self.assertContains(self.ver(usuario, "req_print", req), self.trazo(firma_req))

    def test_pedido_tecnico_ajeno_conserva_impresion_sin_firmas(self):
        req, _ = self.pedido_atendido()
        firma = self.firmar(req, self.alm_oroya)
        self.assert_restringida(self.ver(self.crew, "req_print", req), firma)

    def test_devolucion_otra_sede_no_recibe_firma(self):
        doc = DocumentoInventario.objects.create(
            tipo=TipoDocumento.ING, estado=EstadoDocumento.CONFIRMADO,
            numero="ING-PRIV-DEV", sede=self.sede, responsable=self.almacen,
            entregado_por=self.crew, referencia="DEVOLUCION-RAPIDA",
        )
        DocumentoItem.objects.create(documento=doc, producto=self.producto, cantidad=3)
        firma = self.firmar(doc, self.crew)
        self.assert_restringida(self.ver(self.alm_oroya, "devolucion_print", doc), firma)
        self.assertContains(self.ver(self.crew, "devolucion_print", doc), self.trazo(firma))

    def test_obra_tecnico_ajeno_no_recibe_firma_de_salida_ni_acta(self):
        obra, _, firma_salida, firma_acta = self.obra_firmada()
        ajeno = self.usuario("ajeno_priv", UserProfile.Rol.SOLICITANTE)
        self.assert_restringida(self.ver(ajeno, "proyecto_pdf_salida", obra), firma_salida)
        self.assert_restringida(self.ver(ajeno, "proyecto_pdf_liquidacion", obra), firma_acta)

    def test_obra_gestion_multisede_y_tecnicos_vinculados_conservan_firmas(self):
        obra, _, firma_salida, firma_acta = self.obra_firmada()
        colaborador = self.usuario("colaborador_priv", UserProfile.Rol.SOLICITANTE)
        ProyectoAsignacion.objects.create(proyecto=obra, tecnico=colaborador, activo=True)
        asignado = self.usuario("asignado_priv", UserProfile.Rol.SOLICITANTE)
        AsignacionCuadrilla.objects.create(
            proyecto=obra, entregado_por=self.resp, recibido_por=asignado,
            producto=self.producto, cantidad=1,
        )
        disenador = self.usuario("disenador_priv", UserProfile.Rol.DISENADOR)
        for usuario in (self.alm_oroya, self.almacen, self.resp, self.crew, colaborador, asignado, disenador):
            with self.subTest(usuario=usuario.username):
                self.assertContains(self.ver(usuario, "proyecto_pdf_salida", obra), self.trazo(firma_salida))
                self.assertContains(self.ver(usuario, "proyecto_pdf_liquidacion", obra), self.trazo(firma_acta))

    def test_vinculos_cancelados_no_autorizan_firmas(self):
        obra, _, _, _ = self.obra_firmada()
        ajeno = self.usuario("ajeno_priv", UserProfile.Rol.SOLICITANTE)
        ProyectoAsignacion.objects.create(proyecto=obra, tecnico=ajeno, activo=False)
        AsignacionCuadrilla.objects.create(
            proyecto=obra, entregado_por=self.resp, recibido_por=ajeno,
            producto=self.producto, cantidad=1, estado="ANULADO",
        )
        self.assertFalse(puede_ver_firmas_obra(ajeno, obra))

    def test_helpers_sin_usuario_y_anonimo_no_devuelven_datos_de_firmante(self):
        obra, doc, _, _ = self.obra_firmada()
        for usuario in (None, AnonymousUser()):
            with self.subTest(usuario=usuario):
                for datos in (
                    firmas_para_mostrar(doc, usuario=usuario),
                    acta_firmas_para_mostrar(obra, usuario=usuario),
                ):
                    self.assertTrue(datos["restringidas"])
                    self.assertEqual(datos["RECEPTOR"], {"restringida": True})
                    self.assertEqual(datos["ALMACEN"], {"restringida": True})

    def test_contexto_de_otra_obra_no_habilita_firmas_del_documento(self):
        obra, doc, _, _ = self.obra_firmada()
        otra = Proyecto.objects.create(
            codigo="OBRA-PRIV-002", nombre="Otra obra", sede=self.sede,
            creado_por=self.almacen, responsable=self.crew,
        )
        datos = firmas_para_mostrar(doc, usuario=self.crew, proyecto=otra)
        self.assertTrue(datos["restringidas"])
