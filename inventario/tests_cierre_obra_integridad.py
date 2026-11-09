from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from importlib import import_module
from io import StringIO
from threading import Barrier
from types import SimpleNamespace
from unittest import skipUnless

from django.apps import apps
from django.contrib import admin
from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections, connection, connections
from django.test import Client, RequestFactory, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from inventario.models import (
    DocumentoInventario, DocumentoItem, EstadoDocumento, FirmaDocumento, ItemSerializado, MovimientoInventario,
    Producto, Sede, Stock, StockTecnico, TipoDocumento, Ubicacion, UserProfile,
)
from proyectos.models import EstadoProyecto, Proyecto, ProyectoMaterial
from proyectos.admin import ProyectoAdmin


class CierreObraMixin:
    def setUp(self):
        self.sede = Sede.objects.create(nombre="CIERRE-A", tipo=Sede.CENTRAL)
        self.otra = Sede.objects.create(nombre="CIERRE-B", tipo=Sede.SECUNDARIO)
        for sede in (self.sede, self.otra):
            Ubicacion.objects.create(sede=sede, nombre="Retornos")
        self.almacen = self.usuario("cierre_a", self.sede, UserProfile.Rol.ALMACEN)
        self.almacen_b = self.usuario("cierre_b", self.otra, UserProfile.Rol.ALMACEN)
        self.tecnico = self.usuario("cierre_tecnico", self.sede, UserProfile.Rol.SOLICITANTE)
        self.admin = get_user_model().objects.create_superuser(username="cierre_ti", password="test")
        self.producto = Producto.objects.create(nombre="Cable", codigo_interno="CIERRE-CABLE")
        self.obra = Proyecto.objects.create(
            codigo="CIERRE-001", nombre="Obra cierre", sede=self.sede,
            responsable=self.tecnico, creado_por=self.almacen, estado=EstadoProyecto.EN_PROCESO,
        )
        self.material = ProyectoMaterial.objects.create(
            proyecto=self.obra, producto=self.producto, cantidad_planificada=200, cantidad_entregada=100,
        )
        for sede in (self.sede, self.otra):
            Stock.objects.create(sede=sede, producto=self.producto, cantidad=1000)
        self.mochila = StockTecnico.objects.create(
            tecnico=self.tecnico, producto=self.producto, sede=self.sede, cantidad=50,
        )
        self.salida = DocumentoInventario.objects.create(
            tipo=TipoDocumento.SAL, estado=EstadoDocumento.BORRADOR,
            sede=self.sede, responsable=self.almacen, solicitante=self.tecnico, referencia=self.obra.codigo,
        )
        DocumentoItem.objects.create(documento=self.salida, producto=self.producto, cantidad=100)
        self.salida.confirmar(actualizar_mochila_tecnico=False)
        self.url = reverse("almacen_liquidar_proyecto", args=[self.obra.pk])
        self.client.force_login(self.almacen)

    def usuario(self, nombre, sede, rol):
        user = get_user_model().objects.create_user(username=nombre)
        user.profile.rol = rol
        user.profile.sede_principal = sede
        user.profile.save()
        return user

    def cerrar(self, **extra):
        datos = {f"input_good_{self.material.pk}": 20}
        datos.update(extra)
        return self.client.post(self.url, datos)

    def antigua(self):
        DocumentoInventario.objects.filter(pk=self.salida.pk).update(
            fecha=timezone.make_aware(datetime(2026, 8, 1)),
        )

    def confirmar_revision(self):
        call_command(
            "revisar_mochila_obra", codigo=self.obra.codigo, confirmar=True,
            usuario=self.admin.username, motivo="Conciliación independiente documentada en acta TI-123.",
            stdout=StringIO(),
        )


class CierreObraIntegridadTests(CierreObraMixin, TestCase):
    def test_fecha_fin_estimada_no_impide_primer_cierre(self):
        self.obra.fin = timezone.make_aware(datetime(2030, 12, 1))
        self.obra.save(update_fields=["fin"])
        self.assertEqual(self.cerrar().status_code, 302)
        self.obra.refresh_from_db()
        self.mochila.refresh_from_db()
        self.material.refresh_from_db()
        self.assertTrue(self.obra.cierre_registrado)
        self.assertEqual(self.obra.estado, EstadoProyecto.FINALIZADO)
        self.assertEqual(self.obra.liquidado_por_id, self.almacen.pk)
        self.assertEqual(self.mochila.cantidad, 50)
        self.assertEqual((self.material.cantidad_devuelta, self.material.cantidad_usada), (20, 80))

    def test_antigua_ambigua_bloquea_cierre_sin_cambiar_cantidades(self):
        self.antigua()
        response = self.cerrar()
        self.assertContains(response, "Revisa y concilia su mochila")
        self.obra.refresh_from_db()
        self.material.refresh_from_db()
        self.mochila.refresh_from_db()
        self.assertEqual(self.obra.estado, EstadoProyecto.EN_PROCESO)
        self.assertFalse(self.obra.cierre_registrado)
        self.assertEqual(self.material.cantidad_devuelta, 0)
        self.assertEqual(self.mochila.cantidad, 50)
        self.assertFalse(DocumentoInventario.objects.filter(tipo=TipoDocumento.ING).exists())

    def test_comando_solo_lectura_por_defecto(self):
        self.antigua()
        output = StringIO()
        call_command("revisar_mochila_obra", codigo=self.obra.codigo, stdout=output)
        self.assertIn("Solo lectura", output.getvalue())
        self.obra.refresh_from_db()
        self.assertFalse(self.obra.mochila_antigua_conciliada)
        self.assertFalse(LogEntry.objects.exists())

    def test_revision_exige_superusuario_activo_y_motivo(self):
        for usuario, motivo in ((self.almacen.username, "Revisión previa"), (self.admin.username, " ")):
            with self.subTest(usuario=usuario, motivo=motivo), self.assertRaises(CommandError):
                call_command("revisar_mochila_obra", codigo=self.obra.codigo, confirmar=True,
                             usuario=usuario, motivo=motivo, stdout=StringIO())
        self.admin.is_active = False
        self.admin.save(update_fields=["is_active"])
        with self.assertRaises(CommandError):
            self.confirmar_revision()
        self.obra.refresh_from_db()
        self.assertFalse(self.obra.mochila_antigua_conciliada)
        self.assertFalse(LogEntry.objects.exists())

    def test_revision_audita_sin_cambiar_stock_ni_marcar_cierre(self):
        self.antigua()
        stock_antes = list(Stock.objects.order_by("pk").values_list("pk", "cantidad"))
        movimientos = MovimientoInventario.objects.count()
        self.confirmar_revision()
        self.confirmar_revision()
        self.obra.refresh_from_db()
        self.mochila.refresh_from_db()
        self.assertTrue(self.obra.mochila_antigua_conciliada)
        self.assertFalse(self.obra.cierre_registrado)
        self.assertEqual(self.obra.estado, EstadoProyecto.EN_PROCESO)
        self.assertEqual(self.mochila.cantidad, 50)
        self.assertEqual(list(Stock.objects.order_by("pk").values_list("pk", "cantidad")), stock_antes)
        self.assertEqual(MovimientoInventario.objects.count(), movimientos)
        self.assertEqual(LogEntry.objects.count(), 1)
        self.assertEqual(LogEntry.objects.get().user_id, self.admin.pk)
        self.assertIn("TI-123", LogEntry.objects.get().change_message)
        self.cerrar()
        self.mochila.refresh_from_db()
        self.assertEqual(self.mochila.cantidad, 50)

    def test_doble_post_no_duplica_retorno_ni_cambia_firmante(self):
        self.cerrar()
        self.client.force_login(self.almacen_b)
        self.cerrar()
        self.obra.refresh_from_db()
        self.assertEqual(DocumentoInventario.objects.filter(tipo=TipoDocumento.ING).count(), 1)
        self.assertEqual(Stock.objects.get(sede=self.sede, producto=self.producto).cantidad, 920)
        self.assertEqual(Stock.objects.get(sede=self.otra, producto=self.producto).cantidad, 1000)
        self.assertEqual(self.obra.liquidado_por_id, self.almacen.pk)

    def test_reabrir_solo_liquida_material_nuevo_aunque_se_borre_fin(self):
        self.cerrar()
        Proyecto.objects.filter(pk=self.obra.pk).update(estado=EstadoProyecto.EN_PROCESO, fin=None)
        ProyectoMaterial.objects.filter(pk=self.material.pk).update(cantidad_entregada=150)
        response = self.cerrar(**{f"input_good_{self.material.pk}": 51})
        self.assertContains(response, "supera lo pendiente")
        self.assertEqual(DocumentoInventario.objects.filter(tipo=TipoDocumento.ING).count(), 1)
        self.cerrar(**{f"input_good_{self.material.pk}": 10})
        self.material.refresh_from_db()
        self.mochila.refresh_from_db()
        self.obra.refresh_from_db()
        self.assertEqual((self.material.cantidad_devuelta, self.material.cantidad_usada), (30, 120))
        self.assertEqual(self.mochila.cantidad, 50)
        self.assertTrue(self.obra.cierre_registrado)

    def test_cantidad_invalida_revierte_toda_la_operacion(self):
        otro = Producto.objects.create(nombre="Segundo producto", codigo_interno="CIERRE-2")
        material_2 = ProyectoMaterial.objects.create(
            proyecto=self.obra, producto=otro, cantidad_planificada=20, cantidad_entregada=20,
        )
        for valor in ("-1", "texto", "1.5", "21"):
            with self.subTest(valor=valor):
                self.cerrar(**{f"input_bad_{material_2.pk}": valor})
                self.material.refresh_from_db()
                self.obra.refresh_from_db()
                self.assertEqual(self.material.cantidad_devuelta, 0)
                self.assertEqual(self.obra.estado, EstadoProyecto.EN_PROCESO)
                self.assertFalse(self.obra.cierre_registrado)
                self.assertFalse(DocumentoInventario.objects.filter(tipo=TipoDocumento.ING).exists())
                self.assertEqual(Stock.objects.get(sede=self.sede, producto=self.producto).cantidad, 900)

    def test_despacho_invalido_revierte_otras_lineas_validas(self):
        otro = Producto.objects.create(nombre="Segundo despacho", codigo_interno="CIERRE-DESP-2")
        material_2 = ProyectoMaterial.objects.create(proyecto=self.obra, producto=otro, cantidad_planificada=20)
        Stock.objects.create(sede=self.sede, producto=otro, cantidad=20)
        url = reverse("almacen_generar_salida", args=[self.obra.pk])
        for valor in ("-1", "texto", "1.5"):
            with self.subTest(valor=valor):
                self.client.post(url, {f"input_{self.material.pk}": 5, f"input_{material_2.pk}": valor})
                self.material.refresh_from_db()
                self.assertEqual(self.material.cantidad_entregada, 100)
                self.assertEqual(DocumentoInventario.objects.filter(tipo=TipoDocumento.SAL).count(), 1)
                self.assertEqual(Stock.objects.get(sede=self.sede, producto=self.producto).cantidad, 900)

    def seriales_pendientes(self, cantidad, prefijo="CIERRE-SN"):
        self.producto.es_serializado = True
        self.producto.save(update_fields=["es_serializado"])
        ItemSerializado.objects.bulk_create([
            ItemSerializado(
                producto=self.producto, serial=f"{prefijo}-{i}", proyecto=self.obra,
                estado=ItemSerializado.Estado.ASIGNADO,
                asignado_a=self.tecnico, sede_asignacion=self.sede,
            ) for i in range(cantidad)
        ])

    def test_seriales_faltantes_no_crean_retorno_ficticio(self):
        self.seriales_pendientes(1)
        response = self.cerrar()
        self.assertContains(response, "Concilia los seriales de la obra")
        self.assert_cierre_serial_rechazado(1)

    def test_seriales_excedentes_no_se_instalan_sin_saldo(self):
        self.seriales_pendientes(101)
        response = self.cerrar()
        self.assertContains(response, "Concilia los seriales de la obra")
        self.assert_cierre_serial_rechazado(101)

    def assert_cierre_serial_rechazado(self, cantidad):
        self.obra.refresh_from_db()
        self.material.refresh_from_db()
        self.assertEqual(self.obra.estado, EstadoProyecto.EN_PROCESO)
        self.assertEqual((self.material.cantidad_devuelta, self.material.cantidad_usada), (0, 0))
        self.assertFalse(DocumentoInventario.objects.filter(tipo=TipoDocumento.ING).exists())
        self.assertEqual(Stock.objects.get(sede=self.sede, producto=self.producto).cantidad, 900)
        self.assertEqual(ItemSerializado.objects.filter(estado=ItemSerializado.Estado.ASIGNADO).count(), cantidad)

    def test_reapertura_serial_solo_cierra_equipos_del_nuevo_despacho(self):
        self.seriales_pendientes(100)
        self.assertEqual(self.cerrar().status_code, 302)
        Proyecto.objects.filter(pk=self.obra.pk).update(estado=EstadoProyecto.EN_PROCESO)
        ProyectoMaterial.objects.filter(pk=self.material.pk).update(cantidad_entregada=110)
        self.seriales_pendientes(10, prefijo="CIERRE-SEGUNDO")
        self.assertEqual(self.cerrar(**{f"input_good_{self.material.pk}": 3}).status_code, 302)
        self.material.refresh_from_db()
        self.assertEqual((self.material.cantidad_devuelta, self.material.cantidad_usada), (23, 87))
        self.assertEqual(ItemSerializado.objects.filter(estado=ItemSerializado.Estado.EN_ALMACEN).count(), 23)
        self.assertEqual(ItemSerializado.objects.filter(estado=ItemSerializado.Estado.INSTALADO).count(), 87)
        self.assertFalse(ItemSerializado.objects.filter(estado=ItemSerializado.Estado.ASIGNADO).exists())
        self.assertEqual(DocumentoInventario.objects.filter(tipo=TipoDocumento.ING).count(), 2)

    def registrar_firma(self):
        return FirmaDocumento.objects.create(
            proyecto=self.obra, rol=FirmaDocumento.Rol.ALMACEN,
            firmante=self.almacen, trazo_png=b"firma-de-prueba", sha256_contenido="0" * 64,
        )

    def test_admin_rechaza_reabrir_obra_firmada(self):
        self.cerrar()
        self.registrar_firma()
        self.obra.refresh_from_db()
        request = RequestFactory().post("/admin/proyectos/proyecto/")
        request.user = self.admin
        form_class = ProyectoAdmin(Proyecto, admin.site).get_form(request, obj=self.obra)
        datos = {"codigo": self.obra.codigo, "nombre": self.obra.nombre, "tipo": self.obra.tipo,
                 "sede": self.sede.pk, "estado": EstadoProyecto.EN_PROCESO,
                 "inicio_0": "2026-11-09", "inicio_1": "08:00:00", "creado_por": self.almacen.pk}
        form = form_class(data=datos, instance=self.obra)
        self.assertFalse(form.is_valid())
        self.assertIn("estado", form.errors)
        self.assertIn("no puede reabrirse", str(form.errors["estado"]))
        self.obra.refresh_from_db()
        self.assertEqual(self.obra.estado, EstadoProyecto.FINALIZADO)

    def test_admin_permite_reapertura_no_firmada(self):
        self.cerrar()
        self.obra.refresh_from_db()
        request = RequestFactory().post("/admin/proyectos/proyecto/")
        request.user = self.admin
        form_class = ProyectoAdmin(Proyecto, admin.site).get_form(request, obj=self.obra)
        form = form_class(data={"codigo": self.obra.codigo, "nombre": self.obra.nombre,
                               "tipo": self.obra.tipo, "sede": self.sede.pk,
                               "estado": EstadoProyecto.EN_PROCESO,
                               "inicio_0": "2026-11-09", "inicio_1": "08:00:00",
                               "creado_por": self.almacen.pk}, instance=self.obra)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.obra.refresh_from_db()
        self.assertEqual(self.obra.estado, EstadoProyecto.EN_PROCESO)
        self.assertTrue(self.obra.cierre_registrado)

    def test_reapertura_forzada_firmada_no_permite_despacho_ni_otro_cierre(self):
        self.cerrar()
        self.registrar_firma()
        Proyecto.objects.filter(pk=self.obra.pk).update(estado=EstadoProyecto.EN_PROCESO)
        cantidades = list(Stock.objects.order_by("pk").values_list("pk", "cantidad"))
        documentos = DocumentoInventario.objects.count()
        response = self.client.post(reverse("almacen_generar_salida", args=[self.obra.pk]),
                                    {f"input_{self.material.pk}": 10})
        self.assertContains(response, "ya tiene firmas")
        response = self.cerrar(**{f"input_good_{self.material.pk}": 0})
        self.assertContains(response, "ya tiene firmas")
        self.material.refresh_from_db()
        self.assertEqual((self.material.cantidad_entregada, self.material.cantidad_devuelta,
                          self.material.cantidad_usada), (100, 20, 80))
        self.assertEqual(DocumentoInventario.objects.count(), documentos)
        self.assertEqual(list(Stock.objects.order_by("pk").values_list("pk", "cantidad")), cantidades)
        self.assertEqual(FirmaDocumento.objects.filter(proyecto=self.obra).count(), 1)

    def test_backfill_solo_evidencia_de_cierre_y_no_fecha_estimada(self):
        self.obra.fin = timezone.make_aware(datetime(2030, 12, 1))
        self.obra.save(update_fields=["fin"])
        def nueva(codigo, **campos):
            return Proyecto.objects.create(codigo=codigo, nombre=codigo, sede=self.sede,
                                           creado_por=self.almacen, responsable=self.tecnico, **campos)
        cerrada = nueva("CIERRE-CERRADA", estado=EstadoProyecto.FINALIZADO)
        usada = nueva("CIERRE-USADA", estado=EstadoProyecto.EN_PROCESO)
        ProyectoMaterial.objects.create(proyecto=usada, producto=self.producto, cantidad_entregada=5, cantidad_usada=5)
        retorno = nueva("CIERRE-RETORNO", estado=EstadoProyecto.EN_PROCESO)
        DocumentoInventario.objects.create(tipo=TipoDocumento.ING, estado=EstadoDocumento.CONFIRMADO,
            sede=self.sede, responsable=self.almacen, referencia=f"RETORNO {retorno.codigo}")
        antes = list(Stock.objects.order_by("pk").values_list("pk", "cantidad"))
        migracion = import_module("proyectos.migrations.0011_proyecto_control_cierre")
        migracion.marcar_cierres_con_evidencia(apps, SimpleNamespace(connection=connection))
        for obra in (cerrada, usada, retorno):
            obra.refresh_from_db()
            self.assertTrue(obra.cierre_registrado)
            self.assertTrue(obra.mochila_antigua_conciliada)
        self.obra.refresh_from_db()
        self.assertFalse(self.obra.cierre_registrado)
        self.assertFalse(self.obra.mochila_antigua_conciliada)
        self.assertEqual(list(Stock.objects.order_by("pk").values_list("pk", "cantidad")), antes)
        self.mochila.refresh_from_db()
        self.assertEqual(self.mochila.cantidad, 50)


@skipUnless(connection.vendor == "postgresql", "Concurrencia de cierres: requiere PostgreSQL")
class CierreObraConcurrenteTests(CierreObraMixin, TransactionTestCase):
    def simultaneas(self, acciones):
        barrera = Barrier(2)
        def enviar(accion):
            close_old_connections()
            try:
                cliente = Client()
                cliente.force_login(get_user_model().objects.get(pk=accion[0]))
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    backend_pid = cursor.fetchone()[0]
                def esperar_bloqueo(execute, sql, params, many, context):
                    if '"proyectos_proyecto"' in sql and "FOR UPDATE" in sql:
                        barrera.wait(timeout=10)
                    return execute(sql, params, many, context)
                with connection.execute_wrapper(esperar_bloqueo):
                    respuesta = cliente.post(accion[1], accion[2])
                return respuesta.status_code, backend_pid
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as executor:
            resultados = list(executor.map(enviar, acciones))
        self.assertEqual(len({pid for _, pid in resultados}), 2)
        return resultados

    def test_dos_sedes_solo_generan_un_retorno(self):
        datos = {f"input_good_{self.material.pk}": 20}
        resultado = self.simultaneas([
            (self.almacen.pk, self.url, datos), (self.almacen_b.pk, self.url, datos),
        ])
        self.assertEqual([status for status, _ in resultado], [302, 302])
        self.assertEqual(DocumentoInventario.objects.filter(tipo=TipoDocumento.ING).count(), 1)
        self.material.refresh_from_db()
        self.assertEqual((self.material.cantidad_devuelta, self.material.cantidad_usada), (20, 80))
        self.assertEqual(sum(Stock.objects.filter(producto=self.producto).values_list("cantidad", flat=True)), 1920)

    def test_despacho_y_cierre_comparten_bloqueo_de_obra(self):
        resultado = self.simultaneas([
            (self.almacen.pk, self.url, {f"input_good_{self.material.pk}": 20}),
            (self.almacen_b.pk, reverse("almacen_generar_salida", args=[self.obra.pk]),
             {f"input_{self.material.pk}": 50}),
        ])
        self.obra.refresh_from_db()
        self.material.refresh_from_db()
        self.assertEqual(self.obra.estado, EstadoProyecto.FINALIZADO, resultado)
        self.assertIn(self.material.cantidad_entregada, (100, 150))
        self.assertEqual(self.material.cantidad_por_liquidar, 0)
        self.assertEqual(self.material.cantidad_usada, self.material.cantidad_entregada - 20)
