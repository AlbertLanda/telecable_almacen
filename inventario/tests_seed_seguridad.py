"""Los comandos demo se rechazan antes de leer o escribir la base de datos."""
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings


class SeedSeguridadTests(TestCase):
    comandos = ("seed_demo_firmas", "seed_sedes_prueba")

    def comprobar_bloqueo(self):
        for comando in self.comandos:
            with self.subTest(comando=comando):
                with self.assertNumQueries(0):
                    with self.assertRaisesMessage(CommandError, "DEBUG=True y ALLOW_DEMO_DATA=True"):
                        call_command(comando, stdout=StringIO())

    @override_settings(DEBUG=False, ALLOW_DEMO_DATA=True)
    def test_produccion_rechaza_demo_aunque_la_bandera_este_activada(self):
        self.comprobar_bloqueo()

    @override_settings(DEBUG=True, ALLOW_DEMO_DATA=False)
    def test_desarrollo_requiere_activacion_explicita(self):
        self.comprobar_bloqueo()

    @override_settings(DEBUG=True, ALLOW_DEMO_DATA="false")
    def test_valor_de_texto_no_se_interpreta_como_permiso(self):
        self.comprobar_bloqueo()
