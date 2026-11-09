import base64
from datetime import datetime
from io import BytesIO

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from PIL import Image, ImageDraw

from inventario.models import (
    DocumentoInventario, DocumentoItem, EstadoDocumento, FirmaDocumento,
    Producto, Sede, TipoDocumento, TipoRequerimiento, UserProfile,
)
from inventario.services.firma_service import (
    estado_documento, estado_firma, hash_documento, notas_para_firmar, registrar_firma, validar_trazo,
)
from inventario.views.firma import documentos_del_almacenero


def png_base64(con_trazo=True, formato="PNG"):
    """Simula lo que manda el navegador: un lienzo con (o sin) una línea dibujada."""
    imagen = Image.new("RGBA", (400, 150), (0, 0, 0, 0))
    if con_trazo:
        ImageDraw.Draw(imagen).line([(20, 100), (200, 30), (380, 120)], fill="black", width=4)
    salida = BytesIO()
    if formato == "PNG":
        imagen.save(salida, "PNG")
    else:  # p. ej. JPEG disfrazado con el prefijo de PNG
        imagen.convert("RGB").save(salida, formato)
    return "data:image/png;base64," + base64.b64encode(salida.getvalue()).decode()


class ValidarTrazoTests(TestCase):
    def test_firma_dibujada_devuelve_png_recortado(self):
        png = validar_trazo(png_base64())
        with Image.open(BytesIO(png)) as imagen:
            self.assertEqual(imagen.format, "PNG")
            self.assertLess(imagen.width, 400)  # se recortó al trazo

    def test_rechaza_lienzo_vacio_texto_y_otro_formato(self):
        for valor in (png_base64(con_trazo=False), "hola", None,
                      "data:image/png;base64,no-es-base64!", png_base64(formato="JPEG")):
            with self.subTest(valor=str(valor)[:30]):
                with self.assertRaises(ValidationError):
                    validar_trazo(valor)


class NotaSalidaMixin:
    """Datos comunes: una sede, un almacenero, un técnico Resp y su Crew."""

    def setUp(self):
        self.sede = Sede.objects.create(nombre="SEDE-FIRMA", tipo=Sede.CENTRAL)
        self.almacen = self.usuario("almacen_f", UserProfile.Rol.ALMACEN)
        self.resp = self.usuario("resp_f", UserProfile.Rol.SOLICITANTE)
        self.crew = self.usuario("crew_f", UserProfile.Rol.SOLICITANTE)
        self.producto = Producto.objects.create(nombre="Cable de prueba", codigo_interno="PROD-FIRMA")

    def usuario(self, nombre, rol):
        user = get_user_model().objects.create_user(username=nombre, first_name=nombre.title())
        user.profile.rol = rol
        user.profile.sede_principal = self.sede
        user.profile.save()
        return user

    def nota_salida(self, retirado_por, tipo_requerimiento=TipoRequerimiento.LOCAL, numero="SAL-F-001", **campos):
        """Igual que req_atender: un REQ despachado genera una SAL confirmada."""
        req = DocumentoInventario.objects.create(
            tipo=TipoDocumento.REQ, estado=EstadoDocumento.REQ_ATENDIDO, sede=self.sede,
            responsable=self.resp, solicitante=self.resp, retirado_por=retirado_por,
            tipo_requerimiento=tipo_requerimiento,
        )
        sal = DocumentoInventario.objects.create(
            tipo=TipoDocumento.SAL, estado=EstadoDocumento.CONFIRMADO, numero=numero,
            sede=self.sede, responsable=self.almacen, solicitante=self.resp,
            retirado_por=retirado_por, origen=req, **campos,
        )
        DocumentoItem.objects.create(documento=sal, producto=self.producto, cantidad=5)
        return sal

    def firmar(self, sal, usuario, sha=None, rol=FirmaDocumento.Rol.RECEPTOR):
        return registrar_firma(
            doc_id=sal.id, usuario=usuario, rol=rol, trazo=png_base64(),
            sha256_visto=sha or hash_documento(sal), acepto=True,
        )


class FirmaNotaSalidaTests(NotaSalidaMixin, TestCase):

    def test_crew_firma_y_queda_su_responsable(self):
        sal = self.nota_salida(retirado_por=self.crew)
        self.assertEqual(estado_firma(sal), "PENDIENTE")

        firma = self.firmar(sal, self.crew)

        self.assertEqual(firma.firmante, self.crew)
        self.assertEqual(firma.rol, FirmaDocumento.Rol.RECEPTOR)
        self.assertEqual(firma.datos_firmante["condicion"], "Crew")
        self.assertEqual(firma.datos_firmante["responsable_cuadrilla"], "Resp_F")
        self.assertEqual(firma.datos_firmante["sede"], "SEDE-FIRMA")
        self.assertEqual(estado_firma(sal), "FIRMADO")

    def test_si_retira_el_responsable_firma_el(self):
        sal = self.nota_salida(retirado_por=self.resp)
        firma = self.firmar(sal, self.resp)
        self.assertEqual(firma.datos_firmante["condicion"], "Responsable")

    def test_solo_quien_retiro_puede_firmar(self):
        sal = self.nota_salida(retirado_por=self.crew)
        for intruso in (self.resp, self.almacen):
            with self.subTest(usuario=intruso.username):
                with self.assertRaises(PermissionDenied):
                    self.firmar(sal, intruso)
        self.assertFalse(FirmaDocumento.objects.exists())

    def test_no_se_firma_dos_veces(self):
        sal = self.nota_salida(retirado_por=self.crew)
        self.firmar(sal, self.crew)
        with self.assertRaises(ValidationError):
            self.firmar(sal, self.crew)
        self.assertEqual(FirmaDocumento.objects.count(), 1)

    def test_documento_cambiado_antes_de_firmar_se_rechaza(self):
        sal = self.nota_salida(retirado_por=self.crew)
        visto = hash_documento(sal)
        sal.items.update(cantidad=7)  # almacén corrigió la nota mientras tanto
        with self.assertRaises(ValidationError):
            self.firmar(sal, self.crew, sha=visto)
        self.assertFalse(FirmaDocumento.objects.exists())

    def test_cambio_despues_de_firmar_se_detecta(self):
        sal = self.nota_salida(retirado_por=self.crew)
        self.firmar(sal, self.crew)
        sal.items.update(cantidad=7)
        self.assertEqual(estado_firma(sal), "CAMBIADO")

    def test_liquidar_no_rompe_la_firma(self):
        sal = self.nota_salida(retirado_por=self.crew)
        self.firmar(sal, self.crew)
        sal.items.update(cantidad_devuelta=2, cantidad_usada=3)
        self.assertEqual(estado_firma(sal), "FIRMADO")

    def test_pedido_entre_sedes_no_lleva_firma_de_tecnico(self):
        sal = self.nota_salida(retirado_por=self.crew, tipo_requerimiento=TipoRequerimiento.ENTRE_SEDES)
        self.assertEqual(estado_firma(sal), "NO_APLICA")
        with self.assertRaises(ValidationError):
            self.firmar(sal, self.crew)


class ApartadoPorFirmarTests(NotaSalidaMixin, TestCase):
    url = reverse_lazy("tecnico_por_firmar")

    def test_crew_ve_su_nota_pendiente_y_el_responsable_no(self):
        self.nota_salida(retirado_por=self.crew, numero="SAL-F-CREW")

        self.client.force_login(self.crew)
        self.assertContains(self.client.get(self.url), "SAL-F-CREW")
        self.assertContains(self.client.get(reverse("tecnico_dashboard")), "1 documento pendiente")

        # El Resp no la tiene para firmar (solo la ve en "Notas de mi equipo").
        self.client.force_login(self.resp)
        self.assertEqual(self.client.get(self.url).context["pendientes"], [])

    def test_firmada_pasa_a_firmados(self):
        sal = self.nota_salida(retirado_por=self.crew)
        self.firmar(sal, self.crew)
        self.client.force_login(self.crew)
        respuesta = self.client.get(self.url)
        self.assertEqual(respuesta.context["pendientes"], [])
        self.assertEqual(respuesta.context["firmadas"], [sal])

    def test_notas_anteriores_al_despliegue_no_se_piden(self):
        # La migración de firmas se aplicó al crear la base de pruebas (hoy):
        # una nota de octubre es anterior al "despliegue".
        vieja = self.nota_salida(retirado_por=self.crew, numero="SAL-VIEJA",
                                 fecha=timezone.make_aware(datetime(2026, 10, 1, 9, 0)))
        self.assertEqual(estado_firma(vieja), "NO_APLICA")
        self.client.force_login(self.crew)
        self.assertNotContains(self.client.get(self.url), "SAL-VIEJA")

    @override_settings(FIRMA_NOTAS_DESDE="2026-09-01")
    def test_fecha_fija_en_settings_tiene_prioridad(self):
        self.nota_salida(retirado_por=self.crew, numero="SAL-SETIEMBRE",
                         fecha=timezone.make_aware(datetime(2026, 10, 1, 9, 0)))
        self.client.force_login(self.crew)
        self.assertContains(self.client.get(self.url), "SAL-SETIEMBRE")

    def test_almacen_no_entra_al_apartado(self):
        self.client.force_login(self.almacen)
        self.assertNotEqual(self.client.get(self.url).status_code, 200)


class PantallaFirmaTests(NotaSalidaMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.sal = self.nota_salida(retirado_por=self.crew)
        self.url = reverse("tecnico_firmar_nota", args=[self.sal.id])

    def datos(self, **cambios):
        datos = {"trazo": png_base64(), "sha256": hash_documento(self.sal), "acepto": "1"}
        datos.update(cambios)
        return datos

    def test_crew_ve_la_nota_y_firma(self):
        self.client.force_login(self.crew)
        pagina = self.client.get(self.url)
        self.assertContains(pagina, "SAL-F-001")
        self.assertContains(pagina, "Responsable de cuadrilla: Resp_F")
        self.assertContains(pagina, 'id="firmaLienzo"')

        respuesta = self.client.post(self.url, self.datos())
        self.assertRedirects(respuesta, reverse("tecnico_por_firmar"))
        self.assertEqual(estado_firma(self.sal), "FIRMADO")
        firma = FirmaDocumento.objects.get()
        self.assertEqual(firma.firmante, self.crew)
        self.assertIn("recibido conforme", firma.datos_firmante["aceptacion"])

    def test_sin_casilla_o_sin_trazo_no_firma(self):
        self.client.force_login(self.crew)
        for datos in (self.datos(acepto=""), self.datos(trazo=png_base64(con_trazo=False))):
            with self.subTest(datos=list(datos)):
                respuesta = self.client.post(self.url, datos)
                self.assertEqual(respuesta.status_code, 200)
        self.assertFalse(FirmaDocumento.objects.exists())

    def test_otro_tecnico_o_almacen_no_pueden_abrirla(self):
        for usuario in (self.resp, self.almacen):
            with self.subTest(usuario=usuario.username):
                self.client.force_login(usuario)
                self.assertEqual(self.client.get(self.url).status_code, 403)
                self.assertEqual(self.client.post(self.url, self.datos()).status_code, 403)
        self.assertFalse(FirmaDocumento.objects.exists())

    def test_ya_firmada_vuelve_a_la_lista(self):
        self.firmar(self.sal, self.crew)
        self.client.force_login(self.crew)
        self.assertRedirects(self.client.get(self.url), reverse("tecnico_por_firmar"))


class FirmaAlmacenTests(NotaSalidaMixin, TestCase):
    ALMACEN = FirmaDocumento.Rol.ALMACEN

    def test_almacenero_que_despacho_firma_su_recuadro(self):
        sal = self.nota_salida(retirado_por=self.crew)
        firma = self.firmar(sal, self.almacen, rol=self.ALMACEN)
        self.assertEqual(firma.datos_firmante["condicion"], "Almacén")
        self.assertIn("entregado", firma.datos_firmante["aceptacion"])
        self.assertEqual(estado_firma(sal, self.ALMACEN), "FIRMADO")
        self.assertEqual(estado_firma(sal), "PENDIENTE")  # falta el Crew

    def test_otro_almacenero_o_el_tecnico_no_firman_por_almacen(self):
        sal = self.nota_salida(retirado_por=self.crew)
        otro = self.usuario("otro_almacen_f", UserProfile.Rol.ALMACEN)
        for usuario in (otro, self.crew):
            with self.subTest(usuario=usuario.username):
                with self.assertRaises(PermissionDenied):
                    self.firmar(sal, usuario, rol=self.ALMACEN)

    def test_estado_del_documento_con_dos_firmas(self):
        sal = self.nota_salida(retirado_por=self.crew)
        self.assertEqual(estado_documento(sal), "PENDIENTE")
        self.firmar(sal, self.crew)
        self.assertEqual(estado_documento(sal), "PARCIAL")
        self.firmar(sal, self.almacen, rol=self.ALMACEN)
        self.assertEqual(estado_documento(sal), "FIRMADO")
        sal.items.update(cantidad=9)
        self.assertEqual(estado_documento(sal), "CAMBIADO")

    def test_lista_del_almacenero(self):
        sal = self.nota_salida(retirado_por=self.crew)
        pendientes, firmadas = notas_para_firmar(self.almacen, self.ALMACEN)
        self.assertEqual((pendientes, firmadas), ([sal], []))
        self.firmar(sal, self.almacen, rol=self.ALMACEN)
        pendientes, firmadas = notas_para_firmar(self.almacen, self.ALMACEN)
        self.assertEqual((pendientes, firmadas), ([], [sal]))


class PantallaAlmacenTests(NotaSalidaMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.sal = self.nota_salida(retirado_por=self.crew)
        self.url = reverse("almacen_firmar_nota", args=[self.sal.id])

    def test_almacenero_ve_su_pendiente_y_firma(self):
        self.client.force_login(self.almacen)
        self.assertContains(self.client.get(reverse("dash_almacen")), "Documentos por firmar (1)")
        self.assertContains(self.client.get(reverse("almacen_por_firmar")), "SAL-F-001")

        pagina = self.client.get(self.url)
        self.assertContains(pagina, "Almacén (entregué el material)")
        self.assertContains(pagina, "Retirado por: Crew_F")

        datos = {"trazo": png_base64(), "sha256": hash_documento(self.sal), "acepto": "1"}
        self.assertRedirects(self.client.post(self.url, datos), reverse("almacen_por_firmar"))
        self.assertEqual(estado_firma(self.sal, FirmaDocumento.Rol.ALMACEN), "FIRMADO")
        self.assertEqual(estado_documento(self.sal), "PARCIAL")

    def test_otro_almacenero_no_puede(self):
        otro = self.usuario("otro_almacen_f", UserProfile.Rol.ALMACEN)
        self.client.force_login(otro)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertNotContains(self.client.get(reverse("almacen_por_firmar")), "SAL-F-001")

    def test_tecnico_no_entra_a_pantallas_de_almacen(self):
        self.client.force_login(self.crew)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.get(reverse("almacen_por_firmar")).status_code, 403)


class EquipoDelResponsableTests(NotaSalidaMixin, TestCase):
    url = reverse_lazy("tecnico_por_firmar")

    def test_resp_ve_estado_de_la_nota_del_crew_sin_poder_firmarla(self):
        sal = self.nota_salida(retirado_por=self.crew, numero="SAL-EQUIPO")
        self.client.force_login(self.resp)
        pagina = self.client.get(self.url)
        self.assertContains(pagina, "Notas de mi equipo")
        self.assertEqual(pagina.context["equipo"], [sal])
        self.assertEqual(pagina.context["pendientes"], [])  # no le toca firmar
        self.assertNotContains(pagina, reverse("tecnico_firmar_nota", args=[sal.id]))
        self.assertContains(pagina, "Pendiente de firma")

        self.firmar(sal, self.crew)
        pagina = self.client.get(self.url)
        self.assertEqual(pagina.context["equipo"][0].estado_crew, "FIRMADO")

    def test_lo_que_retira_el_resp_no_sale_en_equipo(self):
        self.nota_salida(retirado_por=self.resp)
        self.client.force_login(self.resp)
        pagina = self.client.get(self.url)
        self.assertEqual(pagina.context["equipo"], [])
        self.assertEqual(len(pagina.context["pendientes"]), 1)

    def test_crew_no_ve_seccion_de_equipo(self):
        self.nota_salida(retirado_por=self.crew)
        self.client.force_login(self.crew)
        self.assertNotContains(self.client.get(self.url), "Notas de mi equipo")


class VerNotaSalidaTests(NotaSalidaMixin, TestCase):
    def test_crew_resp_pueden_ver_la_nota_y_otro_tecnico_no(self):
        sal = self.nota_salida(retirado_por=self.crew)
        url = reverse("sal_print", args=[sal.id])
        for usuario in (self.crew, self.resp):
            with self.subTest(usuario=usuario.username):
                self.client.force_login(usuario)
                self.assertEqual(self.client.get(url).status_code, 200)
        otro = self.usuario("otro_tecnico_f", UserProfile.Rol.SOLICITANTE)
        self.client.force_login(otro)
        self.assertEqual(self.client.get(url).status_code, 302)


class NotaImpresaConFirmasTests(NotaSalidaMixin, TestCase):
    """La nota impresa (sal_print) pinta el trazo, la constancia o 'Pendiente'."""

    def ver(self, sal):
        self.client.force_login(self.almacen)
        return self.client.get(reverse("sal_print", args=[sal.id]))

    def test_nota_nueva_sin_firmas_muestra_pendiente_en_ambos_recuadros(self):
        sal = self.nota_salida(retirado_por=self.crew)
        self.assertContains(self.ver(sal), "Pendiente de firma", count=2)

    def test_firma_del_crew_se_ve_con_su_constancia(self):
        sal = self.nota_salida(retirado_por=self.crew)
        self.firmar(sal, self.crew)
        respuesta = self.ver(sal)
        self.assertContains(respuesta, 'src="data:image/png;base64,')
        self.assertContains(respuesta, "Firmado electrónicamente por Crew_F (Crew)")
        self.assertContains(respuesta, "Pendiente de firma", count=1)  # falta almacén
        self.assertNotContains(respuesta, "El documento cambió después de firmarse")

    def test_cambio_despues_de_firmar_se_avisa_en_la_nota(self):
        sal = self.nota_salida(retirado_por=self.crew)
        self.firmar(sal, self.crew)
        sal.items.update(cantidad=9)
        self.assertContains(self.ver(sal), "El documento cambió después de firmarse")

    def test_nota_anterior_a_las_firmas_queda_como_antes(self):
        vieja = self.nota_salida(retirado_por=self.crew, numero="SAL-VIEJA",
                                 fecha=timezone.make_aware(datetime(2026, 10, 1, 9, 0)))
        respuesta = self.ver(vieja)
        self.assertNotContains(respuesta, "Pendiente de firma")
        self.assertContains(respuesta, "FIRMA DE QUIEN RETIRA")


class HistorialConFirmasTests(NotaSalidaMixin, TestCase):
    """El historial de almacén muestra en qué va la firma de cada nota de salida."""

    def setUp(self):
        super().setUp()
        self.url = reverse("almacen_historial_global")

    def ver(self):
        self.client.force_login(self.almacen)
        return self.client.get(self.url)

    def test_estados_de_firma_en_el_historial(self):
        sin_firmas = self.nota_salida(retirado_por=self.crew, numero="SAL-H-1")
        parcial = self.nota_salida(retirado_por=self.crew, numero="SAL-H-2")
        completa = self.nota_salida(retirado_por=self.crew, numero="SAL-H-3")
        self.firmar(parcial, self.crew)
        self.firmar(completa, self.crew)
        self.firmar(completa, self.almacen, rol=FirmaDocumento.Rol.ALMACEN)

        respuesta = self.ver()
        estados = {item.numero: item.estado_firma for item in respuesta.context["historial"]
                   if hasattr(item, "estado_firma")}
        self.assertEqual(estados, {"SAL-H-1": "PENDIENTE", "SAL-H-2": "PARCIAL", "SAL-H-3": "FIRMADO"})
        self.assertContains(respuesta, "Sin firmas")
        self.assertContains(respuesta, "Firma 1/2")
        self.assertContains(respuesta, "Firmada (2/2)")

    def test_cambio_tras_firmar_se_marca(self):
        sal = self.nota_salida(retirado_por=self.crew)
        self.firmar(sal, self.crew)
        sal.items.update(cantidad=9)
        self.assertContains(self.ver(), "Cambió tras firmarse")

    def test_notas_viejas_no_muestran_estado(self):
        self.nota_salida(retirado_por=self.crew, numero="SAL-VIEJA",
                         fecha=timezone.make_aware(datetime(2026, 10, 1, 9, 0)))
        respuesta = self.ver()
        self.assertContains(respuesta, "SAL-VIEJA")
        self.assertNotContains(respuesta, "Sin firmas")


class DevolucionFirmaTests(NotaSalidaMixin, TestCase):
    """Constancia de devolución: firma el técnico que devolvió y el almacenero que recibió."""

    def devolucion(self, tecnico, numero="ING-DEV-001", referencia="DEVOLUCION-RAPIDA", **campos):
        """Igual que almacen_devolucion_rapida: un ING confirmado con entregado_por = técnico."""
        doc = DocumentoInventario.objects.create(
            tipo=TipoDocumento.ING, estado=EstadoDocumento.CONFIRMADO, numero=numero,
            sede=self.sede, responsable=self.almacen, entregado_por=tecnico,
            referencia=referencia, **campos,
        )
        DocumentoItem.objects.create(documento=doc, producto=self.producto, cantidad=3)
        return doc

    def test_tecnico_y_almacen_firman_la_devolucion(self):
        dev = self.devolucion(self.crew)
        self.assertEqual(estado_documento(dev), "PENDIENTE")

        firma = self.firmar(dev, self.crew)
        self.assertEqual(firma.datos_firmante["condicion"], "Técnico")
        self.assertIn("devuelto a almacén", firma.datos_firmante["aceptacion"])
        self.assertEqual(estado_documento(dev), "PARCIAL")

        firma = self.firmar(dev, self.almacen, rol=FirmaDocumento.Rol.ALMACEN)
        self.assertIn("recibido conforme", firma.datos_firmante["aceptacion"])
        self.assertEqual(estado_documento(dev), "FIRMADO")

    def test_solo_el_tecnico_que_devolvio_y_el_almacenero_que_recibio(self):
        dev = self.devolucion(self.crew)
        with self.assertRaises(PermissionDenied):
            self.firmar(dev, self.resp)
        otro_almacen = self.usuario("almacen_otro_f", UserProfile.Rol.ALMACEN)
        with self.assertRaises(PermissionDenied):
            self.firmar(dev, otro_almacen, rol=FirmaDocumento.Rol.ALMACEN)

    def test_cambio_despues_de_firmar_se_detecta(self):
        dev = self.devolucion(self.crew)
        self.firmar(dev, self.crew)
        dev.items.update(cantidad=1)
        self.assertEqual(estado_firma(dev), "CAMBIADO")

    def test_otros_ingresos_y_devoluciones_viejas_no_se_firman(self):
        compra = self.devolucion(self.crew, numero="ING-COMPRA", referencia="FACTURA 123")
        vieja = self.devolucion(self.crew, numero="ING-VIEJA",
                                fecha=timezone.make_aware(datetime(2026, 10, 1, 9, 0)))
        self.assertEqual(estado_documento(compra), "NO_APLICA")
        self.assertEqual(estado_documento(vieja), "NO_APLICA")

    def test_aparece_en_por_firmar_del_tecnico_y_del_almacen(self):
        dev = self.devolucion(self.crew)
        self.assertEqual(notas_para_firmar(self.crew)[0], [dev])
        self.assertEqual(notas_para_firmar(self.almacen, FirmaDocumento.Rol.ALMACEN)[0], [dev])
        self.assertEqual(notas_para_firmar(self.resp)[0], [])

        self.client.force_login(self.crew)
        respuesta = self.client.get(reverse("tecnico_por_firmar"))
        self.assertContains(respuesta, "Constancia de devolución")
        self.assertContains(respuesta, reverse("devolucion_print", args=[dev.id]))

    def test_pantalla_de_firma_del_tecnico(self):
        dev = self.devolucion(self.crew)
        url = reverse("tecnico_firmar_nota", args=[dev.id])
        self.client.force_login(self.crew)
        respuesta = self.client.get(url)
        self.assertContains(respuesta, "Firmar constancia de devolución ING-DEV-001")
        self.assertContains(respuesta, "devuelto a almacén")

        respuesta = self.client.post(url, {"trazo": png_base64(), "sha256": hash_documento(dev), "acepto": "1"})
        self.assertRedirects(respuesta, reverse("tecnico_por_firmar"))
        self.assertTrue(dev.firmas.filter(rol=FirmaDocumento.Rol.RECEPTOR, firmante=self.crew).exists())

    def test_pantalla_de_firma_del_almacen(self):
        dev = self.devolucion(self.crew)
        url = reverse("almacen_firmar_nota", args=[dev.id])
        self.client.force_login(self.almacen)
        self.assertContains(self.client.get(url), "recibido conforme")
        respuesta = self.client.post(url, {"trazo": png_base64(), "sha256": hash_documento(dev), "acepto": "1"})
        self.assertRedirects(respuesta, reverse("almacen_por_firmar"))

    def test_constancia_impresa_con_firmas(self):
        dev = self.devolucion(self.crew)
        url = reverse("devolucion_print", args=[dev.id])
        self.client.force_login(self.almacen)
        self.assertContains(self.client.get(url), "Pendiente de firma", count=2)

        self.firmar(dev, self.crew)
        respuesta = self.client.get(url)
        self.assertContains(respuesta, "Firmado electrónicamente por Crew_F (Técnico)")
        self.assertContains(respuesta, "Pendiente de firma", count=1)

    def test_tecnico_solo_ve_sus_devoluciones(self):
        dev = self.devolucion(self.crew)
        url = reverse("devolucion_print", args=[dev.id])
        self.client.force_login(self.crew)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.client.force_login(self.resp)
        self.assertEqual(self.client.get(url).status_code, 302)

    def test_estado_en_el_historial(self):
        dev = self.devolucion(self.crew)
        self.firmar(dev, self.crew)
        self.client.force_login(self.almacen)
        respuesta = self.client.get(reverse("almacen_historial_global"))
        self.assertContains(respuesta, "Firma 1/2")


class LiquidacionFirmaTests(NotaSalidaMixin, TestCase):
    """Acta de liquidación: firma el técnico liquidado (conformidad) y el almacenero que liquidó."""

    def liquidacion(self, tecnico, numero="ING-LIQ-001", **campos):
        """Igual que liquidacion_tecnico: ING LIQ-SEMANAL con solicitante = técnico."""
        doc = DocumentoInventario.objects.create(
            tipo=TipoDocumento.ING, estado=EstadoDocumento.CONFIRMADO, numero=numero,
            sede=self.sede, sede_origen=self.sede, responsable=self.almacen, solicitante=tecnico,
            referencia="LIQ-SEMANAL", **campos,
        )
        DocumentoItem.objects.create(documento=doc, producto=self.producto,
                                     cantidad=2, cantidad_merma=1, cantidad_usada=4)
        return doc

    def test_tecnico_y_almacen_firman_la_liquidacion(self):
        liq = self.liquidacion(self.crew)
        firma = self.firmar(liq, self.crew)
        self.assertEqual(firma.datos_firmante["condicion"], "Técnico")
        self.assertIn("conformidad", firma.datos_firmante["aceptacion"])
        self.firmar(liq, self.almacen, rol=FirmaDocumento.Rol.ALMACEN)
        self.assertEqual(estado_documento(liq), "FIRMADO")

    def test_solo_el_tecnico_liquidado_y_el_almacenero_que_liquido(self):
        liq = self.liquidacion(self.crew)
        with self.assertRaises(PermissionDenied):
            self.firmar(liq, self.resp)
        otro_almacen = self.usuario("almacen_otro_f", UserProfile.Rol.ALMACEN)
        with self.assertRaises(PermissionDenied):
            self.firmar(liq, otro_almacen, rol=FirmaDocumento.Rol.ALMACEN)

    def test_cambiar_merma_o_consumo_despues_de_firmar_se_detecta(self):
        liq = self.liquidacion(self.crew)
        self.firmar(liq, self.crew)
        liq.items.update(cantidad_merma=0, cantidad_usada=5)
        self.assertEqual(estado_firma(liq), "CAMBIADO")

    def test_liquidaciones_viejas_no_se_firman(self):
        vieja = self.liquidacion(self.crew, numero="ING-LIQ-VIEJA",
                                 fecha=timezone.make_aware(datetime(2026, 10, 1, 9, 0)))
        self.assertEqual(estado_documento(vieja), "NO_APLICA")

    def test_aparece_en_por_firmar_y_se_firma_en_pantalla(self):
        liq = self.liquidacion(self.crew)
        self.assertEqual(notas_para_firmar(self.crew)[0], [liq])
        self.assertEqual(notas_para_firmar(self.almacen, FirmaDocumento.Rol.ALMACEN)[0], [liq])

        self.client.force_login(self.crew)
        lista = self.client.get(reverse("tecnico_por_firmar"))
        self.assertContains(lista, reverse("liquidacion_tecnico_print", args=[liq.id]))
        url = reverse("tecnico_firmar_nota", args=[liq.id])
        pantalla = self.client.get(url)
        self.assertContains(pantalla, "Firmar liquidación ING-LIQ-001")
        self.assertContains(pantalla, "<th>Merma</th>", html=False)
        respuesta = self.client.post(url, {"trazo": png_base64(), "sha256": hash_documento(liq), "acepto": "1"})
        self.assertRedirects(respuesta, reverse("tecnico_por_firmar"))

    def test_acta_impresa_con_firmas(self):
        liq = self.liquidacion(self.crew)
        url = reverse("liquidacion_tecnico_print", args=[liq.id])
        self.client.force_login(self.almacen)
        self.assertContains(self.client.get(url), "Pendiente de firma", count=2)
        self.firmar(liq, self.almacen, rol=FirmaDocumento.Rol.ALMACEN)
        respuesta = self.client.get(url)
        self.assertContains(respuesta, "Firmado electrónicamente por Almacen_F (Almacén)")
        self.assertContains(respuesta, "Pendiente de firma", count=1)

    def test_estado_en_el_historial(self):
        liq = self.liquidacion(self.crew)
        self.firmar(liq, self.crew)
        self.firmar(liq, self.almacen, rol=FirmaDocumento.Rol.ALMACEN)
        self.client.force_login(self.almacen)
        self.assertContains(self.client.get(reverse("almacen_historial_global")), "Firmada (2/2)")


class SalidaObraFirmaTests(NotaSalidaMixin, TestCase):
    """Despacho a obra: firma el técnico responsable de la obra y el almacenero que despachó."""

    def setUp(self):
        super().setUp()
        from proyectos.models import EstadoProyecto, Proyecto
        self.obra = Proyecto.objects.create(
            codigo="OBRA-2026-0001", nombre="Obra de prueba firmas", sede=self.sede,
            creado_por=self.almacen, responsable=self.resp, estado=EstadoProyecto.EN_PROCESO,
        )

    def despacho(self, retirado_por=None, numero="SAL-OBRA-001", **campos):
        """Igual que almacen_generar_salida: SAL sin REQ, solicitante = responsable, referencia = obra."""
        doc = DocumentoInventario.objects.create(
            tipo=TipoDocumento.SAL, estado=EstadoDocumento.CONFIRMADO, numero=numero,
            sede=self.sede, responsable=self.almacen, solicitante=self.resp,
            retirado_por=retirado_por or self.resp, referencia=self.obra.codigo, **campos,
        )
        DocumentoItem.objects.create(documento=doc, producto=self.producto, cantidad=7)
        return doc

    def test_firma_el_responsable_aunque_retire_el_crew(self):
        sal = self.despacho(retirado_por=self.crew)
        with self.assertRaises(PermissionDenied):
            self.firmar(sal, self.crew)
        firma = self.firmar(sal, self.resp)
        self.assertEqual(firma.datos_firmante["condicion"], "Responsable de obra")
        self.firmar(sal, self.almacen, rol=FirmaDocumento.Rol.ALMACEN)
        self.assertEqual(estado_documento(sal), "FIRMADO")

    def test_otro_almacenero_no_firma_y_cambios_se_detectan(self):
        sal = self.despacho()
        otro = self.usuario("almacen_otro_f", UserProfile.Rol.ALMACEN)
        with self.assertRaises(PermissionDenied):
            self.firmar(sal, otro, rol=FirmaDocumento.Rol.ALMACEN)
        self.firmar(sal, self.resp)
        sal.items.update(cantidad=8)
        self.assertEqual(estado_firma(sal), "CAMBIADO")

    def test_despachos_viejos_o_sin_obra_no_se_firman(self):
        viejo = self.despacho(numero="SAL-OBRA-VIEJA", fecha=timezone.make_aware(datetime(2026, 10, 1, 9, 0)))
        sin_obra = self.despacho(numero="SAL-SIN-OBRA")
        DocumentoInventario.objects.filter(pk=sin_obra.pk).update(referencia="OBRA-NO-EXISTE")
        sin_obra.refresh_from_db()
        self.assertEqual(estado_documento(viejo), "NO_APLICA")
        self.assertEqual(estado_documento(sin_obra), "NO_APLICA")

    def test_aparece_en_por_firmar_y_no_en_equipo(self):
        sal = self.despacho(retirado_por=self.crew)
        self.assertEqual(notas_para_firmar(self.resp)[0], [sal])
        self.assertEqual(notas_para_firmar(self.crew)[0], [])
        self.assertEqual(notas_para_firmar(self.almacen, FirmaDocumento.Rol.ALMACEN)[0], [sal])

        self.client.force_login(self.resp)
        lista = self.client.get(reverse("tecnico_por_firmar"))
        self.assertContains(lista, "Salida a obra OBRA-2026-0001")
        self.assertContains(lista, reverse("proyecto_pdf_salida", args=[self.obra.id]))
        self.assertNotContains(lista, "Notas de mi equipo")

    def test_pantalla_de_firma_del_responsable(self):
        sal = self.despacho(retirado_por=self.crew)
        url = reverse("tecnico_firmar_nota", args=[sal.id])
        self.client.force_login(self.resp)
        pantalla = self.client.get(url)
        self.assertContains(pantalla, "Firmar salida a obra OBRA-2026-0001 SAL-OBRA-001")
        self.assertContains(pantalla, "Obra: OBRA-2026-0001 - Obra de prueba firmas")
        respuesta = self.client.post(url, {"trazo": png_base64(), "sha256": hash_documento(sal), "acepto": "1"})
        self.assertRedirects(respuesta, reverse("tecnico_por_firmar"))

    def test_vale_impreso_con_firmas_por_despacho(self):
        primero = self.despacho(numero="SAL-OBRA-001")
        self.despacho(numero="SAL-OBRA-002")
        self.firmar(primero, self.resp)
        self.client.force_login(self.almacen)
        vale = self.client.get(reverse("proyecto_pdf_salida", args=[self.obra.id]))
        self.assertContains(vale, "Vale SAL-OBRA-001")
        self.assertContains(vale, "Vale SAL-OBRA-002")
        self.assertContains(vale, "Pendiente de firma", count=3)
        self.assertContains(vale, "Firmado electrónicamente por Resp_F (Responsable de obra)")

    def test_vale_de_obra_vieja_queda_como_antes(self):
        self.despacho(fecha=timezone.make_aware(datetime(2026, 10, 1, 9, 0)))
        self.client.force_login(self.almacen)
        vale = self.client.get(reverse("proyecto_pdf_salida", args=[self.obra.id]))
        self.assertContains(vale, "DNI: _________________")
        self.assertNotContains(vale, "Pendiente de firma")


class EntreSedesMixin(NotaSalidaMixin):
    """Oroya pide a la sede central; la central atiende con un traslado; Oroya recepciona."""

    def setUp(self):
        super().setUp()
        self.oroya = Sede.objects.create(nombre="SEDE-OROYA-F", tipo=Sede.SECUNDARIO)
        self.alm_oroya = self.usuario("almacen_oroya_f", UserProfile.Rol.ALMACEN)
        self.alm_oroya.profile.sede_principal = self.oroya
        self.alm_oroya.profile.save()

    def pedido_atendido(self, numero="REQ-SED-001", recibido=False, **campos):
        req = DocumentoInventario.objects.create(
            tipo=TipoDocumento.REQ, estado=EstadoDocumento.REQ_ATENDIDO, numero=numero,
            tipo_requerimiento=TipoRequerimiento.ENTRE_SEDES, sede=self.oroya, sede_destino=self.sede,
            responsable=self.alm_oroya, **campos,
        )
        DocumentoItem.objects.create(documento=req, producto=self.producto, cantidad=10)
        sal = DocumentoInventario.objects.create(
            tipo=TipoDocumento.SAL, estado=EstadoDocumento.CONFIRMADO, numero=numero.replace("REQ", "SAL"),
            sede=self.sede, sede_destino=self.oroya, responsable=self.almacen, solicitante=self.alm_oroya,
            origen=req, referencia=f"Atención de {numero}", **campos,
        )
        DocumentoItem.objects.create(documento=sal, producto=self.producto, cantidad=10)
        if recibido:
            self.recibir(sal)
        return req, sal

    def recibir(self, sal):
        sal.recibido, sal.recibido_por, sal.recibido_en = True, self.alm_oroya, timezone.now()
        sal.save(update_fields=["recibido", "recibido_por", "recibido_en"])


class TrasladoFirmaTests(EntreSedesMixin, TestCase):
    def test_envia_firma_ya_y_recibe_firma_despues_de_recepcionar(self):
        _, sal = self.pedido_atendido()
        self.firmar(sal, self.almacen, rol=FirmaDocumento.Rol.ALMACEN)
        with self.assertRaises(PermissionDenied):
            self.firmar(sal, self.alm_oroya)  # aún no recepciona
        self.assertNotIn(sal, notas_para_firmar(self.alm_oroya)[0])  # el pedido sí, el traslado aún no

        self.recibir(sal)
        firma = self.firmar(sal, self.alm_oroya)
        self.assertEqual(firma.datos_firmante["condicion"], "Almacén que recibe")
        self.assertEqual(estado_documento(sal), "FIRMADO")

    def test_recibir_no_rompe_la_firma_de_quien_envia(self):
        _, sal = self.pedido_atendido()
        self.firmar(sal, self.almacen, rol=FirmaDocumento.Rol.ALMACEN)
        self.recibir(sal)
        self.assertEqual(estado_firma(sal, FirmaDocumento.Rol.ALMACEN), "FIRMADO")

    def test_traslados_viejos_no_se_firman(self):
        _, sal = self.pedido_atendido(fecha=timezone.make_aware(datetime(2026, 10, 1, 9, 0)))
        self.assertEqual(estado_documento(sal), "NO_APLICA")

    def test_lista_y_pantallas_del_almacen(self):
        _, sal = self.pedido_atendido(recibido=True)
        self.client.force_login(self.alm_oroya)
        lista = self.client.get(reverse("almacen_por_firmar"))
        self.assertContains(lista, "Traslado entre sedes")
        url = reverse("almacen_firmar_recepcion", args=[sal.id])
        self.assertContains(lista, url)
        pantalla = self.client.get(url)
        self.assertContains(pantalla, "Firmar traslado entre sedes")
        self.assertContains(pantalla, "De SEDE-FIRMA a SEDE-OROYA-F")
        respuesta = self.client.post(url, {"trazo": png_base64(), "sha256": hash_documento(sal), "acepto": "1"})
        self.assertRedirects(respuesta, reverse("almacen_por_firmar"))
        self.assertTrue(sal.firmas.filter(rol=FirmaDocumento.Rol.RECEPTOR, firmante=self.alm_oroya).exists())

    def test_nota_impresa_del_traslado(self):
        _, sal = self.pedido_atendido(recibido=True)
        self.firmar(sal, self.almacen, rol=FirmaDocumento.Rol.ALMACEN)
        self.client.force_login(self.almacen)
        respuesta = self.client.get(reverse("sal_print", args=[sal.id]))
        self.assertContains(respuesta, "ENVÍA: ALMACÉN SEDE-FIRMA")
        self.assertContains(respuesta, "RECIBE: ALMACÉN SEDE-OROYA-F")
        self.assertContains(respuesta, "Almacén que envía")
        self.assertContains(respuesta, "Pendiente de firma", count=1)


class PedidoSedesFirmaTests(EntreSedesMixin, TestCase):
    def test_firma_quien_pidio_y_quien_atendio(self):
        req, _ = self.pedido_atendido()
        with self.assertRaises(PermissionDenied):
            self.firmar(req, self.almacen)  # el que atendió no firma como solicitante
        firma = self.firmar(req, self.alm_oroya)
        self.assertEqual(firma.datos_firmante["condicion"], "Sede solicitante")
        firma = self.firmar(req, self.almacen, rol=FirmaDocumento.Rol.ALMACEN)
        self.assertEqual(firma.datos_firmante["condicion"], "Sede que atendió")
        self.assertEqual(estado_documento(req), "FIRMADO")

    def test_pedido_sin_atender_no_se_firma(self):
        req, _ = self.pedido_atendido()
        req.estado = EstadoDocumento.REQ_PENDIENTE
        req.save(update_fields=["estado"])
        self.assertEqual(estado_documento(req), "NO_APLICA")

    def test_aparece_en_la_lista_de_ambos_almacenes(self):
        req, sal = self.pedido_atendido()
        pend_oroya, _ = documentos_del_almacenero(self.alm_oroya)
        pend_central, _ = documentos_del_almacenero(self.almacen)
        self.assertIn(req, pend_oroya)
        self.assertIn(req, pend_central)
        self.assertIn(sal, pend_central)        # el traslado, como quien envía
        self.assertNotIn(sal, pend_oroya)       # aún no lo recepciona

    def test_pedido_impreso_con_firmas(self):
        req, _ = self.pedido_atendido()
        self.firmar(req, self.alm_oroya)
        self.client.force_login(self.almacen)
        respuesta = self.client.get(reverse("req_print", args=[req.id]))
        self.assertContains(respuesta, "SOLICITANTE (SEDE-OROYA-F)")
        self.assertContains(respuesta, "ATENDIDO POR (SEDE-FIRMA)")
        self.assertContains(respuesta, "Firmado electrónicamente por Almacen_Oroya_F (Sede solicitante)")
        self.assertContains(respuesta, "Pendiente de firma", count=1)

    def test_tecnico_no_ve_documentos_entre_sedes(self):
        self.pedido_atendido(recibido=True)
        self.assertEqual(notas_para_firmar(self.crew)[0], [])
        self.client.force_login(self.resp)
        self.assertNotContains(self.client.get(reverse("tecnico_por_firmar")), "entre sedes")


class OrdenProveedorFirmaTests(NotaSalidaMixin, TestCase):
    """Orden de compra: firma quien la pidió y quien recepcionó la compra. La aprobación queda a mano."""

    def setUp(self):
        super().setUp()
        self.alm_2 = self.usuario("almacen_dos_f", UserProfile.Rol.ALMACEN)

    def orden_recibida(self, numero="OC-F-001", recibida=True, **campos):
        req = DocumentoInventario.objects.create(
            tipo=TipoDocumento.REQ, numero=numero, sede=self.sede, responsable=self.almacen,
            tipo_requerimiento=TipoRequerimiento.PROVEEDOR, proveedor_manual="Proveedor SAC",
            estado=EstadoDocumento.REQ_ATENDIDO if recibida else EstadoDocumento.REQ_PENDIENTE, **campos,
        )
        DocumentoItem.objects.create(documento=req, producto=self.producto, cantidad=50)
        if recibida:
            ing = DocumentoInventario.objects.create(
                tipo=TipoDocumento.ING, estado=EstadoDocumento.CONFIRMADO, sede=self.sede,
                responsable=self.alm_2, origen=req, referencia=f"Llegada Compra {numero}", **campos,
            )
            DocumentoItem.objects.create(documento=ing, producto=self.producto, cantidad=50)
        return req

    def test_firma_quien_pidio_y_quien_recepciono(self):
        req = self.orden_recibida()
        with self.assertRaises(PermissionDenied):
            self.firmar(req, self.almacen, rol=FirmaDocumento.Rol.ALMACEN)  # pidió, no recepcionó
        self.assertEqual(self.firmar(req, self.almacen).datos_firmante["condicion"], "Solicitante")
        firma = self.firmar(req, self.alm_2, rol=FirmaDocumento.Rol.ALMACEN)
        self.assertEqual(firma.datos_firmante["condicion"], "Recepción conforme")
        self.assertEqual(estado_documento(req), "FIRMADO")

    def test_orden_sin_recibir_o_vieja_no_se_firma(self):
        pendiente = self.orden_recibida(numero="OC-PEND", recibida=False)
        vieja = self.orden_recibida(numero="OC-VIEJA", fecha=timezone.make_aware(datetime(2026, 10, 1, 9, 0)))
        self.assertEqual(estado_documento(pendiente), "NO_APLICA")
        self.assertEqual(estado_documento(vieja), "NO_APLICA")

    def test_aparece_en_la_lista_de_cada_almacenero(self):
        req = self.orden_recibida()
        self.assertIn(req, documentos_del_almacenero(self.almacen)[0])
        self.assertIn(req, documentos_del_almacenero(self.alm_2)[0])
        self.client.force_login(self.alm_2)
        lista = self.client.get(reverse("almacen_por_firmar"))
        self.assertContains(lista, "Orden de compra")
        self.assertContains(lista, reverse("almacen_firmar_nota", args=[req.id]))

    def test_orden_impresa_con_firmas_y_aprobacion_a_mano(self):
        req = self.orden_recibida()
        self.firmar(req, self.alm_2, rol=FirmaDocumento.Rol.ALMACEN)
        self.client.force_login(self.almacen)
        respuesta = self.client.get(reverse("req_print", args=[req.id]))
        self.assertContains(respuesta, "JEFE DE COMPRAS / APROBACIÓN")
        self.assertContains(respuesta, "Firmado electrónicamente por Almacen_Dos_F (Recepción conforme)")
        self.assertContains(respuesta, "Pendiente de firma", count=1)


class ActaCierreObraFirmaTests(NotaSalidaMixin, TestCase):
    """Acta de cierre: firma el técnico responsable de la obra y el almacenero que la liquidó."""

    def setUp(self):
        super().setUp()
        from proyectos.models import EstadoProyecto, Proyecto, ProyectoMaterial
        self.obra = Proyecto.objects.create(
            codigo="OBRA-2026-0099", nombre="Obra acta de cierre", sede=self.sede,
            creado_por=self.almacen, responsable=self.resp, estado=EstadoProyecto.FINALIZADO,
            fin=timezone.now(), liquidado_por=self.almacen,
        )
        self.material = ProyectoMaterial.objects.create(
            proyecto=self.obra, producto=self.producto, cantidad_planificada=10,
            cantidad_entregada=10, cantidad_devuelta=2, cantidad_merma=1, cantidad_usada=7,
        )

    def firmar_acta(self, usuario, rol=FirmaDocumento.Rol.RECEPTOR, sha=None):
        from inventario.services.firma_service import acta_hash, registrar_firma_acta
        return registrar_firma_acta(
            proyecto_id=self.obra.id, usuario=usuario, rol=rol, trazo=png_base64(),
            sha256_visto=sha or acta_hash(self.obra), acepto=True,
        )

    def estado(self):
        from inventario.services.firma_service import acta_estado
        self.obra.refresh_from_db()
        return acta_estado(self.obra)

    def test_firma_el_responsable_y_quien_liquido(self):
        self.assertEqual(self.estado(), "PENDIENTE")
        with self.assertRaises(PermissionDenied):
            self.firmar_acta(self.crew)
        otro = self.usuario("almacen_otro_f", UserProfile.Rol.ALMACEN)
        with self.assertRaises(PermissionDenied):
            self.firmar_acta(otro, rol=FirmaDocumento.Rol.ALMACEN)
        self.assertEqual(self.firmar_acta(self.resp).datos_firmante["condicion"], "Responsable técnico")
        self.firmar_acta(self.almacen, rol=FirmaDocumento.Rol.ALMACEN)
        self.assertEqual(self.estado(), "FIRMADO")

    def test_cambiar_merma_o_consumo_despues_de_firmar_se_detecta(self):
        self.firmar_acta(self.resp)
        self.material.cantidad_merma, self.material.cantidad_usada = 0, 8
        self.material.save()
        self.assertEqual(self.estado(), "CAMBIADO")

    def test_obras_sin_liquidador_o_viejas_no_se_firman(self):
        self.obra.liquidado_por = None
        self.obra.save()
        self.assertEqual(self.estado(), "NO_APLICA")
        self.obra.liquidado_por = self.almacen
        self.obra.fin = timezone.make_aware(datetime(2026, 10, 1, 9, 0))
        self.obra.save()
        self.assertEqual(self.estado(), "NO_APLICA")

    def test_aparece_en_por_firmar_y_se_firma_en_pantalla(self):
        self.client.force_login(self.resp)
        lista = self.client.get(reverse("tecnico_por_firmar"))
        self.assertContains(lista, "Acta de cierre de obra")
        url = reverse("tecnico_firmar_acta", args=[self.obra.id])
        self.assertContains(lista, url)
        pantalla = self.client.get(url)
        self.assertContains(pantalla, "Firmar acta de cierre de obra OBRA-2026-0099")
        from inventario.services.firma_service import acta_hash
        respuesta = self.client.post(url, {"trazo": png_base64(), "sha256": acta_hash(self.obra), "acepto": "1"})
        self.assertRedirects(respuesta, reverse("tecnico_por_firmar"))
        self.assertTrue(self.obra.firmas_acta.filter(firmante=self.resp).exists())

        self.client.force_login(self.almacen)
        lista = self.client.get(reverse("almacen_por_firmar"))
        self.assertContains(lista, reverse("almacen_firmar_acta", args=[self.obra.id]))

    def test_acta_impresa_con_firmas_y_estado_en_historial(self):
        self.firmar_acta(self.almacen, rol=FirmaDocumento.Rol.ALMACEN)
        self.client.force_login(self.almacen)
        acta = self.client.get(reverse("proyecto_pdf_liquidacion", args=[self.obra.id]))
        self.assertContains(acta, "JEFATURA DE ALMACÉN · V° B° CONFORMIDAD")
        self.assertContains(acta, "Firmado electrónicamente por Almacen_F (Jefatura de almacén)")
        self.assertContains(acta, "Pendiente de firma", count=1)
        historial = self.client.get(reverse("almacen_historial_global"))
        self.assertContains(historial, "Firma 1/2")

    def test_liquidar_la_obra_guarda_quien_la_liquido(self):
        from proyectos.models import EstadoProyecto
        self.obra.estado, self.obra.liquidado_por, self.obra.fin = EstadoProyecto.EN_PROCESO, None, None
        self.obra.save()
        self.material.cantidad_devuelta = self.material.cantidad_merma = self.material.cantidad_usada = 0
        self.material.save()
        self.client.force_login(self.almacen)
        self.client.post(reverse("almacen_liquidar_proyecto", args=[self.obra.id]),
                         {f"input_good_{self.material.id}": 0, f"input_bad_{self.material.id}": 0})
        self.obra.refresh_from_db()
        self.assertEqual(self.obra.estado, EstadoProyecto.FINALIZADO)
        self.assertEqual(self.obra.liquidado_por, self.almacen)
        self.assertEqual(self.estado(), "PENDIENTE")
