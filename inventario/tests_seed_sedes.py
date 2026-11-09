from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, override_settings
from io import StringIO

from inventario.models import Sede, Stock, UserProfile
from proyectos.models import EstadoProyecto, Proyecto


@override_settings(DEBUG=True, ALLOW_DEMO_DATA=True)
class SeedSedesPruebaTests(TestCase):
    def correr(self):
        call_command("seed_sedes_prueba", stdout=StringIO())

    def test_crea_sedes_almacenes_y_obras_por_despachar(self):
        self.correr()
        for nombre in ("Sede Jauja", "Sede Oroya"):
            sede = Sede.objects.get(nombre=nombre)
            almaceneros = User.objects.filter(profile__rol=UserProfile.Rol.ALMACEN, profile__sede_principal=sede)
            self.assertEqual(almaceneros.count(), 2)
        self.assertEqual(Sede.objects.get(nombre="Sede Jauja").tipo, Sede.CENTRAL)

        obras = Proyecto.objects.filter(codigo__in=["OBRA-JAUJA-001", "OBRA-OROYA-001"])
        self.assertEqual(obras.count(), 2)
        for obra in obras:
            self.assertEqual(obra.estado, EstadoProyecto.APROBADO)
            for m in obra.materiales.all():
                self.assertGreater(m.cantidad_planificada, m.cantidad_entregada)
                stock = Stock.objects.get(producto=m.producto, sede=obra.sede).cantidad
                self.assertGreaterEqual(stock, m.cantidad_planificada)

        self.assertTrue(User.objects.get(username="almacen_oroya_2").check_password("test1234"))

    def test_correrlo_dos_veces_no_duplica(self):
        self.correr()
        stock = Stock.objects.get(producto__codigo_interno="PRB-001", sede__nombre="Sede Jauja").cantidad
        self.correr()
        self.assertEqual(Stock.objects.get(producto__codigo_interno="PRB-001", sede__nombre="Sede Jauja").cantidad, stock)
        self.assertEqual(Proyecto.objects.filter(codigo="OBRA-JAUJA-001").count(), 1)
