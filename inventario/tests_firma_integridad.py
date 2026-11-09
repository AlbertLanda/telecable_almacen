"""La firma cubre el contenido histórico, sin congelar el inventario actual."""
import hashlib
import json

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from inventario.models import (
    DocumentoInventario, DocumentoItem, DocumentoItemSerializado, EstadoDocumento,
    FirmaDocumento, ItemSerializado, MovimientoInventario, Sede, Stock,
    StockTecnico, TipoDocumento, Ubicacion,
)
from inventario.services.firma_service import (
    ALMACEN, RECEPTOR, acta_estado_firma, acta_hash, contenido_documento,
    estado_documento, estado_firma, hash_documento, registrar_firma_acta,
)
from inventario.tests_firma import NotaSalidaMixin, png_base64
from proyectos.models import EstadoProyecto, Proyecto, ProyectoMaterial


@override_settings(FIRMA_NOTAS_DESDE="2026-01-01")
class FirmaContenidoHistoricoTests(NotaSalidaMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.producto.es_serializado = True
        self.producto.save(update_fields=["es_serializado"])
        self.doc = DocumentoInventario.objects.create(
            tipo=TipoDocumento.ING, estado=EstadoDocumento.CONFIRMADO,
            numero="LIQ-INTEGRIDAD", referencia="LIQ-SEMANAL", sede=self.sede,
            sede_origen=self.sede, responsable=self.almacen, solicitante=self.crew,
            observaciones="Equipo recibido y revisado.",
        )
        self.item = DocumentoItem.objects.create(
            documento=self.doc, producto=self.producto, cantidad=1,
            observacion="Devuelto completo.",
        )
        self.equipo = ItemSerializado.objects.create(
            producto=self.producto, serial="SN-FIRMA-ORIGINAL", codigo_trazabilidad="F-001",
        )
        self.vinculo = DocumentoItemSerializado.objects.create(
            documento_item=self.item, item_serializado=self.equipo, estado_resultante="EN_ALMACEN",
        )

    def firmar_ambas(self):
        self.firmar(self.doc, self.crew)
        self.firmar(self.doc, self.almacen, rol=ALMACEN)

    def test_cambiar_serial_con_totales_iguales_invalida_ambas_firmas_en_impresion(self):
        otro = ItemSerializado.objects.create(producto=self.producto, serial="SN-FIRMA-SUSTITUTO")
        self.firmar_ambas()
        self.client.force_login(self.almacen)
        url = reverse("liquidacion_tecnico_print", args=[self.doc.id])
        antes = self.client.get(url)
        self.assertContains(antes, self.equipo.serial)
        sha_antes = hash_documento(self.doc)

        DocumentoItemSerializado.objects.filter(pk=self.vinculo.pk).update(item_serializado=otro)
        despues = self.client.get(url)

        self.assertContains(despues, otro.serial)
        self.assertNotContains(despues, self.equipo.serial)
        self.assertEqual(self.doc.items.get().cantidad, 1)
        self.assertNotEqual(hash_documento(self.doc), sha_antes)
        self.assertEqual(estado_documento(self.doc), "CAMBIADO")
        self.assertFalse(despues.context["firmas"][RECEPTOR]["vigente"])
        self.assertFalse(despues.context["firmas"][ALMACEN]["vigente"])

    def test_cambiar_resultado_historico_del_equipo_invalida_firma(self):
        self.firmar(self.doc, self.crew)
        DocumentoItemSerializado.objects.filter(pk=self.vinculo.pk).update(estado_resultante="INSTALADO")
        self.assertEqual(estado_firma(self.doc), "CAMBIADO")

    def test_corregir_numero_serial_o_trazabilidad_impresos_invalida_firma(self):
        self.firmar(self.doc, self.crew)
        for campo, nuevo in (("serial", "SN-CORREGIDO"), ("codigo_trazabilidad", "F-999")):
            with self.subTest(campo=campo):
                original = getattr(self.equipo, campo)
                ItemSerializado.objects.filter(pk=self.equipo.pk).update(**{campo: nuevo})
                self.assertEqual(estado_firma(self.doc), "CAMBIADO")
                ItemSerializado.objects.filter(pk=self.equipo.pk).update(**{campo: original})
                self.assertEqual(estado_firma(self.doc), "FIRMADO")

    def test_despachar_de_nuevo_el_equipo_no_invalida_constancia_anterior(self):
        self.firmar_ambas()
        sha = hash_documento(self.doc)
        otra = Sede.objects.create(nombre="SEDE-CUSTODIA-POSTERIOR", tipo=Sede.SECUNDARIO)
        ubicacion = Ubicacion.objects.create(nombre="Nueva ubicacion", sede=otra)
        obra = Proyecto.objects.create(
            codigo="OBRA-CUSTODIA-POSTERIOR", nombre="Nueva obra", sede=otra,
            creado_por=self.almacen, responsable=self.resp,
        )
        ItemSerializado.objects.filter(pk=self.equipo.pk).update(
            estado="ASIGNADO", asignado_a=self.resp, sede_asignacion=otra,
            ubicacion=ubicacion, proyecto=obra,
        )
        self.assertEqual(hash_documento(self.doc), sha)
        self.assertEqual(estado_documento(self.doc), "FIRMADO")
        self.vinculo.refresh_from_db()
        self.assertEqual(self.vinculo.estado_resultante, "EN_ALMACEN")

    def test_orden_y_ids_de_vinculos_no_alteran_el_contenido_firmado(self):
        otro = ItemSerializado.objects.create(producto=self.producto, serial="SN-FIRMA-DOS")
        self.item.cantidad = 2
        self.item.save(update_fields=["cantidad"])
        DocumentoItemSerializado.objects.create(
            documento_item=self.item, item_serializado=otro, estado_resultante="EN_ALMACEN",
        )
        self.firmar(self.doc, self.crew)
        sha = hash_documento(self.doc)
        self.item.seriales_seleccionados.all().delete()
        for equipo in (otro, self.equipo):
            DocumentoItemSerializado.objects.create(
                documento_item=self.item, item_serializado=equipo, estado_resultante="EN_ALMACEN",
            )
        self.assertEqual(hash_documento(self.doc), sha)
        self.assertEqual(estado_firma(self.doc), "FIRMADO")

    def test_cambiar_observaciones_de_la_constancia_invalida_firma(self):
        self.firmar(self.doc, self.crew)
        self.doc.observaciones = "Se recibio equipo incompleto."
        self.doc.save(update_fields=["observaciones"])
        self.assertEqual(estado_firma(self.doc), "CAMBIADO")

    def test_cambiar_nota_de_linea_impresa_invalida_nota_de_salida(self):
        sal = self.nota_salida(retirado_por=self.crew)
        self.firmar(sal, self.crew)
        sal.items.update(observacion="Equipo sin fuente de poder.")
        self.assertEqual(estado_firma(sal), "CAMBIADO")

    def test_cambiar_descripcion_o_unidad_del_producto_invalida_firma(self):
        self.firmar(self.doc, self.crew)
        for campo, nuevo in (("nombre", "Equipo diferente"), ("unidad", "CAJA")):
            with self.subTest(campo=campo):
                original = getattr(self.producto, campo)
                type(self.producto).objects.filter(pk=self.producto.pk).update(**{campo: nuevo})
                self.assertEqual(estado_firma(self.doc), "CAMBIADO")
                type(self.producto).objects.filter(pk=self.producto.pk).update(**{campo: original})

    def test_firmar_solo_agrega_firmas_sin_modificar_inventario_ni_documento(self):
        Stock.objects.create(producto=self.producto, sede=self.sede, cantidad=8)
        StockTecnico.objects.create(tecnico=self.crew, producto=self.producto, sede=self.sede, cantidad=2)
        modelos = (
            Stock, StockTecnico, ItemSerializado, MovimientoInventario,
            DocumentoInventario, DocumentoItem, DocumentoItemSerializado,
        )
        antes = {modelo: list(modelo.objects.order_by("pk").values()) for modelo in modelos}
        self.firmar_ambas()
        despues = {modelo: list(modelo.objects.order_by("pk").values()) for modelo in modelos}
        self.assertEqual(antes, despues)
        self.assertEqual(FirmaDocumento.objects.count(), 2)

    def test_huella_legada_se_conserva_sin_inventar_vigencia(self):
        # Replica exactamente el contenido LIQ que sellaba la version anterior.
        legado = {
            "tipo": self.doc.tipo, "numero": self.doc.numero,
            "fecha": self.doc.fecha.isoformat(), "estado": self.doc.estado,
            "sede_id": self.doc.sede_id, "sede_origen_id": self.doc.sede_origen_id,
            "almacen_id": self.doc.responsable_id, "tecnico_id": self.doc.solicitante_id,
            "items": [[self.producto.codigo_interno, 1, 0, 0]],
        }
        sha_legado = hashlib.sha256(json.dumps(legado, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        firma = FirmaDocumento.objects.create(
            documento=self.doc, rol=RECEPTOR, firmante=self.crew,
            trazo_png=b"trazo-legado", sha256_contenido=sha_legado,
            datos_firmante={"nombre": "Nombre conservado"},
        )
        antes = FirmaDocumento.objects.filter(pk=firma.pk).values().get()
        self.assertEqual(estado_firma(self.doc), "CAMBIADO")
        self.assertEqual(FirmaDocumento.objects.filter(pk=firma.pk).values().get(), antes)
        self.assertEqual(contenido_documento(self.doc)["version_contenido"], 2)


@override_settings(FIRMA_NOTAS_DESDE="2026-01-01")
class FirmaActaContenidoTests(NotaSalidaMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.obra = Proyecto.objects.create(
            codigo="OBRA-ACTA-INTEGRIDAD", nombre="Obra firmada", sede=self.sede,
            creado_por=self.almacen, responsable=self.resp, estado=EstadoProyecto.FINALIZADO,
            fin=timezone.now(), liquidado_por=self.almacen,
        )
        ProyectoMaterial.objects.create(
            proyecto=self.obra, producto=self.producto,
            cantidad_entregada=3, cantidad_devuelta=1, cantidad_usada=2,
        )
        self.salida = DocumentoInventario.objects.create(
            tipo=TipoDocumento.SAL, estado=EstadoDocumento.CONFIRMADO, numero="SAL-ACTA-INTEGRIDAD",
            referencia=self.obra.codigo, sede=self.sede, responsable=self.almacen,
            solicitante=self.resp, retirado_por=self.crew,
        )

    def firmar_acta(self):
        return registrar_firma_acta(
            proyecto_id=self.obra.id, usuario=self.resp, rol=RECEPTOR, trazo=png_base64(),
            sha256_visto=acta_hash(self.obra), acepto=True,
        )

    def test_cambiar_nombre_de_obra_impreso_invalida_acta(self):
        self.firmar_acta()
        self.obra.nombre = "Otra obra"
        self.obra.save(update_fields=["nombre"])
        self.assertEqual(acta_estado_firma(self.obra), "CAMBIADO")

    def test_cambiar_despacho_impreso_invalida_acta(self):
        self.firmar_acta()
        self.salida.retirado_por = self.resp
        self.salida.save(update_fields=["retirado_por"])
        self.assertEqual(acta_estado_firma(self.obra), "CAMBIADO")
