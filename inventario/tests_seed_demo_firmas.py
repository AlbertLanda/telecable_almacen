from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from inventario.models import DocumentoInventario, FirmaDocumento, Stock, StockTecnico, TipoRequerimiento
from inventario.services.firma_service import acta_estado, estado_documento
from proyectos.models import Proyecto


class SeedDemoFirmasTests(TestCase):
    def setUp(self):
        call_command("seed_demo_firmas", stdout=StringIO())

    def estados(self):
        firmables = [d for d in DocumentoInventario.objects.all() if estado_documento(d) != "NO_APLICA"]
        return sorted(estado_documento(d) for d in firmables)

    def test_hay_documentos_en_todos_los_estados(self):
        estados = self.estados()
        for estado in ("PENDIENTE", "PARCIAL", "FIRMADO", "CAMBIADO"):
            self.assertIn(estado, estados)
        self.assertEqual(acta_estado(Proyecto.objects.get(codigo="OBRA-JAUJA-002")), "PARCIAL")

    def test_cada_tipo_de_documento_esta_presente(self):
        from inventario.services import firma_service as fs
        clases = {fs.clase_documento(d) for d in DocumentoInventario.objects.all() if fs.requiere_firma(d)}
        self.assertEqual(clases, {fs.NOTA_SALIDA, fs.DEVOLUCION, fs.LIQUIDACION, fs.SALIDA_OBRA,
                                  fs.TRASLADO, fs.PEDIDO_SEDES, fs.ORDEN_PROVEEDOR})

    def test_stock_y_mochila_sin_negativos(self):
        self.assertFalse(Stock.objects.filter(cantidad__lt=0).exists())
        self.assertFalse(StockTecnico.objects.filter(cantidad__lt=0).exists())
        traslado = DocumentoInventario.objects.get(tipo="SAL", sede_destino__isnull=False)
        self.assertTrue(traslado.recibido)

    def test_no_se_carga_dos_veces(self):
        with self.assertRaises(CommandError):
            call_command("seed_demo_firmas", stdout=StringIO())
