from django.test import TestCase
from django.core.exceptions import ValidationError
from django.db import transaction
from decimal import Decimal

from .models import Producto, Sede, Categoria, Stock, MovimientoInventario, Ubicacion

class InventarioCoreTests(TestCase):

    def setUp(self):
        """Configuración inicial para las pruebas"""
        # Crear datos base
        self.sede = Sede.objects.create(nombre="Sede Test", tipo=Sede.CENTRAL)
        self.ubicacion = Ubicacion.objects.create(nombre="Estante A", sede=self.sede)
        self.categoria = Categoria.objects.create(nombre="Materiales")
        
        # Producto de prueba
        self.producto = Producto.objects.create(
            nombre="Cable UTP",
            categoria=self.categoria,
            unidad="METROS",
            costo_unitario=Decimal("1.50")
        )

    def test_codigo_interno_automatico(self):
        """Prueba que se genere TC-ALM-000001 si no se envía código"""
        p2 = Producto.objects.create(nombre="Conector RJ45")
        self.assertTrue(p2.codigo_interno.startswith("TC-ALM-"))
        print(f"✅ Código generado correctamente: {p2.codigo_interno}")

    def test_movimiento_entrada_sube_stock(self):
        """Prueba que un movimiento de entrada (IN) aumente el stock"""
        cantidad_inicial = 0
        
        # 1. Crear movimiento de entrada
        mov = MovimientoInventario.objects.create(
            producto=self.producto,
            sede=self.sede,
            ubicacion=self.ubicacion,
            tipo=MovimientoInventario.TIPO_IN,
            qty=100,
            costo_unitario=Decimal("1.50")
        )
        
        # 2. Aplicar movimiento (simulando lo que hacen los signals o servicios)
        mov.aplicar()
        
        # 3. Verificar stock
        stock = Stock.objects.get(producto=self.producto, sede=self.sede)
        self.assertEqual(stock.cantidad, 100)
        print("✅ Entrada de stock procesada correctamente (0 -> 100)")

    def test_movimiento_salida_baja_stock(self):
        """Prueba que una salida (OUT) descuente el stock"""
        # 1. Primero damos stock inicial
        Stock.objects.create(producto=self.producto, sede=self.sede, cantidad=50)
        
        # 2. Crear salida
        mov = MovimientoInventario.objects.create(
            producto=self.producto,
            sede=self.sede,
            ubicacion=self.ubicacion,
            tipo=MovimientoInventario.TIPO_OUT,
            qty=20
        )
        mov.aplicar()
        
        # 3. Verificar resta
        stock = Stock.objects.get(producto=self.producto, sede=self.sede)
        self.assertEqual(stock.cantidad, 30) # 50 - 20 = 30
        print("✅ Salida de stock procesada correctamente (50 -> 30)")

    def test_validacion_stock_insuficiente(self):
        """Prueba que no deje sacar más de lo que hay"""
        Stock.objects.create(producto=self.producto, sede=self.sede, cantidad=10)
        
        mov = MovimientoInventario(
            producto=self.producto,
            sede=self.sede,
            tipo=MovimientoInventario.TIPO_OUT,
            qty=50 # Pedimos 50, solo hay 10
        )
        
        # Esperamos que levante ValidationError
        with self.assertRaises(ValidationError):
            mov.aplicar()
            
        print("✅ Validación de stock insuficiente funciona correctamente")

    def test_ajuste_inventario(self):
        """Prueba de ajuste (ADJ) negativo y positivo"""
        stock = Stock.objects.create(producto=self.producto, sede=self.sede, cantidad=100)
        
        # Ajuste de -5 (pérdida o corrección)
        mov = MovimientoInventario.objects.create(
            producto=self.producto,
            sede=self.sede,
            tipo=MovimientoInventario.TIPO_ADJ,
            qty=-5
        )
        mov.aplicar()
        
        stock.refresh_from_db()
        self.assertEqual(stock.cantidad, 95)
        print("✅ Ajuste de inventario procesado correctamente")