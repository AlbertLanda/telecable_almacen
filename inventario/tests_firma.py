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
