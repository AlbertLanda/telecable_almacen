"""Regresiones de atención de obras incluidas en el descubrimiento global de tests."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from inventario.models import (
    DocumentoInventario, EstadoDocumento, MovimientoInventario, Producto,
    Sede, Stock, TipoDocumento, UserProfile,
)
from proyectos.models import EstadoProyecto, Proyecto, ProyectoMaterial, ProyectoMaterialPendiente


class AtencionAlmacenTests(TestCase):
    def setUp(self):
        self.sede = Sede.objects.create(nombre="ATENCION-A", tipo=Sede.CENTRAL)
        self.otra_sede = Sede.objects.create(nombre="ATENCION-B", tipo=Sede.SECUNDARIO)
        self.almacen = self.usuario("almacen_atencion", self.sede, UserProfile.Rol.ALMACEN)
        self.otro_almacen = self.usuario("almacen_atencion_b", self.otra_sede, UserProfile.Rol.ALMACEN)
        self.tecnico = self.usuario("tecnico_atencion", self.sede, UserProfile.Rol.SOLICITANTE)
        self.producto = Producto.objects.create(nombre="Cable de atención", codigo_interno="ATENCION-1")
        self.producto_b = Producto.objects.create(nombre="Conector de atención", codigo_interno="ATENCION-2")
        self.client.force_login(self.almacen)

    def usuario(self, nombre, sede, rol):
        usuario = get_user_model().objects.create_user(username=nombre)
        usuario.profile.rol = rol
        usuario.profile.sede_principal = sede
        usuario.profile.save()
        return usuario

    def obra(self, codigo, estado=EstadoProyecto.EN_PROCESO, sede=None):
        return Proyecto.objects.create(
            codigo=codigo, nombre=codigo, sede=sede or self.sede, responsable=self.tecnico,
            creado_por=self.almacen, estado=estado,
        )

    def material(self, obra, planificado=10, entregado=0, producto=None):
        return ProyectoMaterial.objects.create(
            proyecto=obra, producto=producto or self.producto,
            cantidad_planificada=planificado, cantidad_entregada=entregado,
        )

    def catalogo(self, obra, nombre="Caja sin catálogo", resuelto=False):
        return ProyectoMaterialPendiente.objects.create(
            proyecto=obra, nombre_solicitado=nombre, cantidad_estimada=3,
            creado_por=self.almacen, resuelto=resuelto,
        )

    def listado(self):
        response = self.client.get(reverse("almacen_proyectos_list"))
        self.assertEqual(response.status_code, 200)
        return response, [obra.pk for obra in response.context["proyectos"]]

    def detalle(self, obra):
        response = self.client.get(reverse("almacen_proyecto_detalle", args=[obra.pk]))
        self.assertEqual(response.status_code, 200)
        return response

    def test_entrega_completa_sale_de_atencion_y_conserva_consulta_y_liquidacion(self):
        obra = self.obra("ATENCION-COMPLETA")
        material = self.material(obra, entregado=10)
        _, ids = self.listado()
        self.assertNotIn(obra.pk, ids)
        detalle = self.detalle(obra)
        self.assertContains(detalle, "100% de materiales catalogados completos")
        self.assertNotContains(detalle, "Generar Salida")
        liquidaciones = self.client.get(reverse("almacen_liquidacion_lista"))
        self.assertIn(obra, liquidaciones.context["proyectos"])
        obra.refresh_from_db()
        material.refresh_from_db()
        self.assertEqual(obra.estado, EstadoProyecto.EN_PROCESO)
        self.assertIsNone(obra.fin)
        self.assertEqual((material.cantidad_planificada, material.cantidad_entregada,
                          material.cantidad_devuelta, material.cantidad_usada), (10, 10, 0, 0))
        self.assertFalse(DocumentoInventario.objects.exists())
        self.assertFalse(MovimientoInventario.objects.exists())

    def test_parcial_muestra_cantidad_pendiente_y_stock_contra_lo_que_falta(self):
        obra = self.obra("ATENCION-PARCIAL", estado=EstadoProyecto.APROBADO)
        self.material(obra, entregado=3)
        Stock.objects.create(sede=self.sede, producto=self.producto, cantidad=7)
        response, ids = self.listado()
        self.assertIn(obra.pk, ids)
        self.assertContains(response, "Aprobado / Por Iniciar")
        detalle = self.detalle(obra)
        self.assertContains(detalle, "7 Pendiente")
        self.assertContains(detalle, "Generar Salida")
        self.assertNotContains(detalle, "Falta Stock")
        self.assertContains(detalle, '<span class="stock-badge sb-ok">7</span>', html=True)

    def test_exceso_de_un_producto_no_compensa_faltante_de_otro(self):
        obra = self.obra("ATENCION-NO-COMPENSA")
        self.material(obra, planificado=10, entregado=20)
        self.material(obra, planificado=10, entregado=0, producto=self.producto_b)
        _, ids = self.listado()
        self.assertIn(obra.pk, ids)
        detalle = self.detalle(obra)
        self.assertEqual(detalle.context["progreso"], 50)
        self.assertContains(detalle, "10 Pendiente")

    def test_catalogo_pendiente_conserva_atencion_y_aclara_porcentaje(self):
        obra = self.obra("ATENCION-CATALOGO")
        self.material(obra, entregado=10)
        self.catalogo(obra)
        listado, ids = self.listado()
        self.assertIn(obra.pk, ids)
        self.assertContains(listado, "Pendiente de catálogo")
        detalle = self.detalle(obra)
        self.assertContains(detalle, "100% de materiales catalogados completos")
        self.assertContains(detalle, "Pendientes de catálogo (1)")
        self.assertContains(detalle, "Caja sin catálogo")
        self.assertContains(detalle, "no están incluidos en el porcentaje de entrega")
        self.assertNotContains(detalle, "Generar Salida")
        response = self.client.get(reverse("almacen_generar_salida", args=[obra.pk]), follow=True)
        self.assertContains(response, "aún quedan materiales pendientes de catálogo")
        self.assertNotContains(response, "Todos los materiales planificados ya fueron entregados")
        self.assertFalse(DocumentoInventario.objects.exists())

    def test_catalogo_resuelto_deja_de_retener_obra_completa(self):
        obra = self.obra("ATENCION-RESUELTA")
        self.material(obra, entregado=10)
        pendiente = self.catalogo(obra)
        self.assertIn(obra.pk, self.listado()[1])
        pendiente.resuelto = True
        pendiente.save(update_fields=["resuelto"])
        self.assertNotIn(obra.pk, self.listado()[1])
        self.assertNotContains(self.detalle(obra), "Caja sin catálogo")
        # Si al vincularlo se agrega material todavía no entregado, sigue en la bandeja.
        self.material(obra, planificado=3, producto=self.producto_b)
        self.assertIn(obra.pk, self.listado()[1])

    def test_solo_catalogo_y_obra_vacia_no_se_confunden_con_entrega_completa(self):
        catalogo = self.obra("ATENCION-SOLO-CATALOGO", estado=EstadoProyecto.APROBADO)
        self.catalogo(catalogo)
        vacia = self.obra("ATENCION-VACIA", estado=EstadoProyecto.APROBADO)
        self.assertEqual(self.listado()[1], [catalogo.pk])
        self.assertContains(self.detalle(catalogo), "Sin materiales catalogados")
        response = self.client.get(reverse("almacen_generar_salida", args=[vacia.pk]), follow=True)
        self.assertContains(response, "La obra no tiene materiales planificados para despachar")
        self.assertNotContains(response, "Todos los materiales planificados ya fueron entregados")

    def test_kpi_y_listado_coinciden_en_estados_sedes_y_sin_duplicados(self):
        local = self.obra("ATENCION-LOCAL", estado=EstadoProyecto.APROBADO)
        self.material(local, entregado=1)
        self.material(local, producto=self.producto_b)
        self.catalogo(local, nombre="Pendiente uno")
        self.catalogo(local, nombre="Pendiente dos")
        externa = self.obra("ATENCION-EXTERNA", sede=self.otra_sede)
        self.material(externa, entregado=2)
        completa = self.obra("ATENCION-COMPLETA-KPI")
        self.material(completa, entregado=10)
        for estado in (EstadoProyecto.DISENO, EstadoProyecto.OBSERVADO,
                       EstadoProyecto.REVISION_TECNICA, EstadoProyecto.FINALIZADO, EstadoProyecto.ANULADO):
            excluida = self.obra(f"ATENCION-{estado}", estado=estado)
            self.material(excluida)
            self.catalogo(excluida)
        for usuario in (self.almacen, self.otro_almacen):
            with self.subTest(usuario=usuario.username):
                self.client.force_login(usuario)
                _, ids = self.listado()
                self.assertCountEqual(ids, [local.pk, externa.pk])
                self.assertEqual(len(ids), 2)
                dashboard = self.client.get(reverse("dash_almacen"))
                self.assertEqual(dashboard.status_code, 200)
                self.assertEqual(dashboard.context["notificacion_proyectos"], 2)
                self.assertEqual(dashboard.context["kpi_pendientes"], 2)

    def test_kpi_conserva_requerimientos_pendientes_de_la_sede(self):
        obra = self.obra("ATENCION-KPI-REQ")
        self.material(obra)
        DocumentoInventario.objects.create(tipo=TipoDocumento.REQ, estado=EstadoDocumento.REQ_PENDIENTE,
            sede=self.sede, solicitante=self.tecnico, responsable=self.almacen)
        dashboard = self.client.get(reverse("dash_almacen"))
        self.assertEqual(dashboard.context["notificacion_proyectos"], 1)
        self.assertEqual(dashboard.context["kpi_pendientes"], 2)

    def test_tecnico_no_accede_a_bandeja_ni_detalle_de_almacen(self):
        obra = self.obra("ATENCION-PERMISOS")
        self.material(obra)
        self.client.force_login(self.tecnico)
        self.assertEqual(self.client.get(reverse("almacen_proyectos_list")).status_code, 302)
        self.assertEqual(self.client.get(reverse("almacen_proyecto_detalle", args=[obra.pk])).status_code, 302)

    def test_acceso_directo_tecnico_no_expone_contador_de_otras_sedes(self):
        local = self.obra("ATENCION-KPI-LOCAL", estado=EstadoProyecto.APROBADO)
        externa = self.obra("ATENCION-KPI-EXTERNA", sede=self.otra_sede)
        self.material(local)
        self.material(externa)
        self.client.force_login(self.tecnico)
        response = self.client.get(reverse("dash_almacen"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["notificacion_proyectos"], 1)
        self.assertEqual(response.context["kpi_pendientes"], 1)
