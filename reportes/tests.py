# -*- coding: utf-8 -*-
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from inventario.models import (
    Categoria,
    DocumentoInventario,
    ItemSerializado,
    DocumentoItem,
    EstadoDocumento,
    MovimientoInventario,
    Producto,
    Sede,
    Stock,
    TipoDocumento,
    Ubicacion,
    UserProfile,
)


class RegistroMaterialesExportTests(TestCase):
    """
    El archivo exportado tiene que traer lo mismo que el usuario ve filtrado,
    y nada más. Estas pruebas cubren las dos formas en que eso se rompe sin
    hacer ruido: que se filtre por una sede distinta a la del usuario, y que
    las columnas derivadas del documento (Obra, Técnico) dejen de resolver.
    """

    def setUp(self):
        self.jauja = Sede.objects.create(nombre="Jauja", tipo=Sede.CENTRAL)
        self.oroya = Sede.objects.create(nombre="Oroya", tipo=Sede.SECUNDARIO)

        self.producto = Producto.objects.create(nombre="ONU Huawei", unidad="UND")
        self.otro = Producto.objects.create(nombre="Cable drop", unidad="MTR")

        Stock.objects.create(producto=self.producto, sede=self.jauja, cantidad=100)
        Stock.objects.create(producto=self.otro, sede=self.jauja, cantidad=100)
        Stock.objects.create(producto=self.producto, sede=self.oroya, cantidad=100)

        self.almacenero = self._usuario("alm_jauja", UserProfile.Rol.ALMACEN, self.jauja)
        self.tecnico = self._usuario("tec_kevin", UserProfile.Rol.SOLICITANTE, self.jauja)

        self.client.force_login(self.almacenero)

    def _usuario(self, username, rol, sede):
        # El perfil lo crea un signal al guardar el User; acá solo se corrige.
        user = User.objects.create_user(username)
        UserProfile.objects.filter(user=user).update(rol=rol, sede_principal=sede)
        return user

    def _movimiento(self, producto, sede, tipo=MovimientoInventario.TIPO_IN, qty=5, **extra):
        mov = MovimientoInventario.objects.create(
            producto=producto, sede=sede, tipo=tipo, qty=qty,
            usuario=self.almacenero, **extra,
        )
        mov.aplicar()
        return mov

    def _url(self, formato, **params):
        base = reverse("reportes_registro_export", args=[formato])
        params.setdefault("desde", "2000-01-01")
        params.setdefault("hasta", "2100-12-31")
        # El CSV trae la tabla de materiales por defecto; esta clase prueba
        # la de movimientos, así que la pide explícitamente.
        params.setdefault("hoja", "movimientos")
        consulta = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{base}?{consulta}"

    def _filas_csv(self, respuesta):
        texto = respuesta.content.decode("utf-8-sig")
        lineas = [l for l in texto.splitlines() if l.strip()]
        return [l.split(";") for l in lineas[1:]]

    # ---------------------------------------------------------------

    def test_solo_exporta_la_sede_del_usuario(self):
        self._movimiento(self.producto, self.jauja)
        self._movimiento(self.producto, self.oroya)

        filas = self._filas_csv(self.client.get(self._url("csv")))

        self.assertEqual(len(filas), 1)
        self.assertEqual(filas[0][4], "Jauja")

    def test_respeta_el_filtro_de_material(self):
        self._movimiento(self.producto, self.jauja)
        self._movimiento(self.otro, self.jauja)

        filas = self._filas_csv(
            self.client.get(self._url("csv", producto=self.producto.pk))
        )

        self.assertEqual(len(filas), 1)
        self.assertEqual(filas[0][7], "ONU Huawei")

    def test_la_salida_se_exporta_con_cantidad_negativa(self):
        self._movimiento(self.producto, self.jauja, MovimientoInventario.TIPO_OUT, 4)

        fila = self._filas_csv(self.client.get(self._url("csv")))[0]

        self.assertEqual(fila[11], "4")    # Cantidad
        self.assertEqual(fila[12], "-4")   # Cantidad con signo

    def test_obra_y_tecnico_se_resuelven_desde_el_documento(self):
        """
        Ni la obra ni el técnico están en el movimiento: se recuperan uniendo
        `movimiento.referencia` con `documento.numero`. Si esa unión se
        rompiera, el archivo seguiría saliendo, solo que con dos columnas
        vacías y nadie se enteraría.
        """
        from proyectos.models import EstadoProyecto, Proyecto

        obra = Proyecto.objects.create(
            codigo="OB-2026-001",
            nombre="Ampliación San Juan",
            sede=self.jauja,
            creado_por=self.almacenero,
            responsable=self.tecnico,
            estado=EstadoProyecto.APROBADO,
        )

        doc = DocumentoInventario.objects.create(
            tipo=TipoDocumento.SAL,
            estado=EstadoDocumento.BORRADOR,
            sede=self.jauja,
            responsable=self.almacenero,
            solicitante=self.tecnico,
            referencia=obra.codigo,
        )
        DocumentoItem.objects.create(documento=doc, producto=self.producto, cantidad=3)
        doc.confirmar()

        fila = self._filas_csv(self.client.get(self._url("csv")))[0]

        self.assertEqual(fila[16], doc.numero)                        # Documento
        self.assertEqual(fila[19], "OB-2026-001 - Ampliación San Juan")  # Obra
        self.assertEqual(fila[20], "tec_kevin")                       # Técnico

    def test_formato_desconocido_da_404(self):
        self.assertEqual(self.client.get(self._url("docx")).status_code, 404)

    def test_un_tecnico_no_puede_exportar(self):
        self.client.force_login(self.tecnico)
        self.assertEqual(self.client.get(self._url("csv")).status_code, 403)

    def test_los_tres_formatos_responden(self):
        self._movimiento(self.producto, self.jauja)

        for formato in ("xlsx", "csv", "html"):
            with self.subTest(formato=formato):
                self.assertEqual(self.client.get(self._url(formato)).status_code, 200)


class EquiposSerializadosExportTests(TestCase):
    """
    El parque de equipos sale como tabla aparte porque el sistema no guarda
    qué serial salió en qué movimiento. Lo que sí afirma es dónde está cada
    equipo hoy, y eso tiene una trampa: al despacharlo se le borra la
    ubicación, así que filtrar por sede solo por `ubicacion__sede` esconde
    justamente los equipos que están en la calle.
    """

    def setUp(self):
        self.jauja = Sede.objects.create(nombre="Jauja", tipo=Sede.CENTRAL)
        self.oroya = Sede.objects.create(nombre="Oroya", tipo=Sede.SECUNDARIO)
        self.almacen_jauja = Ubicacion.objects.create(nombre="Estante A", sede=self.jauja)
        self.almacen_oroya = Ubicacion.objects.create(nombre="Estante B", sede=self.oroya)

        self.onu = Producto.objects.create(nombre="ONU Huawei", unidad="UND", es_serializado=True)

        self.almacenero = User.objects.create_user("alm_jauja")
        UserProfile.objects.filter(user=self.almacenero).update(
            rol=UserProfile.Rol.ALMACEN, sede_principal=self.jauja
        )
        self.tecnico = User.objects.create_user("tec_kevin")
        UserProfile.objects.filter(user=self.tecnico).update(
            rol=UserProfile.Rol.SOLICITANTE, sede_principal=self.jauja
        )

        self.client.force_login(self.almacenero)

    def _url(self):
        base = reverse("reportes_registro_export", args=["csv"])
        return f"{base}?hoja=equipos&desde=2000-01-01&hasta=2100-12-31"

    def _filas(self):
        texto = self.client.get(self._url()).content.decode("utf-8-sig")
        lineas = [l for l in texto.splitlines() if l.strip()]
        return [l.split(";") for l in lineas[1:]]

    def test_incluye_el_equipo_que_esta_en_manos_del_tecnico(self):
        """
        El caso que la pantalla de equipos hoy se pierde: despachado, sin
        ubicación, y aun así es responsabilidad de esta sede.
        """
        ItemSerializado.objects.create(
            producto=self.onu, serial="SN-EN-ALMACEN",
            mac_address="AABBCCDDEE01", ubicacion=self.almacen_jauja,
            estado=ItemSerializado.Estado.EN_ALMACEN,
        )
        ItemSerializado.objects.create(
            producto=self.onu, serial="SN-DESPACHADO",
            mac_address="AABBCCDDEE02", ubicacion=None,
            estado=ItemSerializado.Estado.ASIGNADO, asignado_a=self.tecnico,
        )

        seriales = {fila[2] for fila in self._filas()}

        self.assertEqual(seriales, {"SN-EN-ALMACEN", "SN-DESPACHADO"})

    def test_no_incluye_equipos_de_otra_sede(self):
        ItemSerializado.objects.create(
            producto=self.onu, serial="SN-OROYA",
            mac_address="AABBCCDDEE03", ubicacion=self.almacen_oroya,
            estado=ItemSerializado.Estado.EN_ALMACEN,
        )

        self.assertEqual(self._filas(), [])

    def test_exporta_mac_y_codigo_pintado(self):
        ItemSerializado.objects.create(
            producto=self.onu, serial="SN-001", mac_address="AABBCCDDEE04",
            codigo_trazabilidad="44", serial_secundario="DSN-99",
            ubicacion=self.almacen_jauja, estado=ItemSerializado.Estado.EN_ALMACEN,
        )

        fila = self._filas()[0]

        self.assertEqual(fila[3], "AABBCCDDEE04")   # MAC
        self.assertEqual(fila[4], "DSN-99")         # Serial secundario
        self.assertEqual(fila[5], "44")             # Código pintado
        self.assertEqual(fila[6], "En almacén")     # Estado


class ReporteMaterialesTests(TestCase):
    """
    La diferencia de fondo entre un kardex y un reporte general.

    El kardex parte de los movimientos, así que un material que nadie tocó
    no existe para él. El reporte parte del catálogo, y por eso lista igual
    los que no se movieron -que son justamente los que hay que mirar para
    reponer o dar de baja-.
    """

    def setUp(self):
        self.jauja = Sede.objects.create(nombre="Jauja", tipo=Sede.CENTRAL)
        self.categoria = Categoria.objects.create(nombre="Routers")

        self.movido = Producto.objects.create(
            nombre="ONU activa", unidad="UND", categoria=self.categoria,
            stock_minimo=5, costo_unitario=Decimal("100.00"),
        )
        self.quieto = Producto.objects.create(
            nombre="Conector olvidado", unidad="UND", stock_minimo=50,
        )
        self.agotado = Producto.objects.create(nombre="Cable agotado", unidad="MTR")

        Stock.objects.create(producto=self.movido, sede=self.jauja, cantidad=20)
        Stock.objects.create(producto=self.quieto, sede=self.jauja, cantidad=3)

        self.almacenero = User.objects.create_user("alm_jauja")
        UserProfile.objects.filter(user=self.almacenero).update(
            rol=UserProfile.Rol.ALMACEN, sede_principal=self.jauja
        )
        self.client.force_login(self.almacenero)

    def _filas(self, **params):
        """
        Filas del CSV como diccionarios, por nombre de columna.

        Leerlas por posición ataba cada prueba al orden de las columnas, y
        reordenar la hoja -algo cosmético- rompía media docena de tests sin
        que nada estuviera mal.
        """
        base = reverse("reportes_registro_export", args=["csv"])
        params.setdefault("hoja", "materiales")
        params.setdefault("desde", "2000-01-01")
        params.setdefault("hasta", "2100-12-31")
        consulta = "&".join(f"{k}={v}" for k, v in params.items())
        texto = self.client.get(f"{base}?{consulta}").content.decode("utf-8-sig")

        lineas = [l for l in texto.splitlines() if l.strip()]
        titulos = lineas[0].split(";")
        return [dict(zip(titulos, l.split(";"))) for l in lineas[1:]]

    def test_lista_materiales_que_no_tuvieron_movimiento(self):
        nombres = {fila["Material"] for fila in self._filas()}

        self.assertEqual(
            nombres, {"ONU activa", "Conector olvidado", "Cable agotado"}
        )

    def test_filtra_por_busqueda(self):
        nombres = {f["Material"] for f in self._filas(q="olvidado")}

        self.assertEqual(nombres, {"Conector olvidado"})

    def test_la_pantalla_lista_todo_el_catalogo_de_una(self):
        respuesta = self.client.get(reverse("reportes_kardex"))

        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.context["total_materiales"], 3)
        self.assertEqual(len(respuesta.context["materiales"]), 3)


class ConsumoPorTecnicoTests(TestCase):
    """
    El corte por técnico se arma sobre el documento del movimiento, no sobre
    el movimiento en sí, y ahí está su punto frágil: un movimiento sin
    documento no se puede atribuir a nadie. Estas pruebas fijan que lo que
    no se puede atribuir quede FUERA en vez de ensuciar las cifras.
    """

    def setUp(self):
        self.jauja = Sede.objects.create(nombre="Jauja", tipo=Sede.CENTRAL)
        self.producto = Producto.objects.create(nombre="ONU", unidad="UND")
        Stock.objects.create(producto=self.producto, sede=self.jauja, cantidad=500)

        self.almacenero = User.objects.create_user("alm_jauja")
        UserProfile.objects.filter(user=self.almacenero).update(
            rol=UserProfile.Rol.ALMACEN, sede_principal=self.jauja
        )
        self.kevin = User.objects.create_user("kevin")
        UserProfile.objects.filter(user=self.kevin).update(
            rol=UserProfile.Rol.SOLICITANTE, sede_principal=self.jauja
        )

        self.client.force_login(self.almacenero)

    def _documento(self, tipo, cantidad, solicitante=None, referencia=""):
        doc = DocumentoInventario.objects.create(
            tipo=tipo,
            estado=EstadoDocumento.BORRADOR,
            sede=self.jauja,
            responsable=self.almacenero,
            solicitante=solicitante,
            referencia=referencia,
        )
        DocumentoItem.objects.create(documento=doc, producto=self.producto, cantidad=cantidad)
        doc.confirmar()
        return doc

    def _filas(self, agrupacion="mes"):
        """
        Va al servicio y no a la pantalla: este corte ya no tiene pestaña
        propia -vive en el Excel-, pero el cálculo sigue importando igual.
        """
        from inventario.models import MovimientoInventario
        from reportes.services.registro_materiales import (
            construir_filas, construir_filas_por_tecnico, movimientos_sin_tecnico,
        )

        movimientos = construir_filas(MovimientoInventario.objects.filter(sede=self.jauja))
        return (
            movimientos_sin_tecnico(movimientos),
            construir_filas_por_tecnico(movimientos, agrupacion),
        )

    def test_suma_entregado_y_devuelto_del_mismo_tecnico(self):
        self._documento(TipoDocumento.SAL, 10, solicitante=self.kevin)
        self._documento(TipoDocumento.ING, 4, solicitante=self.kevin)

        _, filas = self._filas()

        self.assertEqual(len(filas), 1)
        self.assertEqual(filas[0]["tecnico"], "kevin")
        self.assertEqual(filas[0]["entregado"], 10)
        self.assertEqual(filas[0]["devuelto"], 4)
        self.assertEqual(filas[0]["neto"], 6)

    def test_una_compra_no_cuenta_como_devolucion(self):
        """
        Un ingreso sin técnico es una compra, una carga inicial o un
        traslado. Contarlo como "devuelto" inflaría la devolución de alguien
        que nunca recibió ese material.
        """
        self._documento(TipoDocumento.SAL, 10, solicitante=self.kevin)
        self._documento(TipoDocumento.ING, 200)  # sin solicitante

        sin_tecnico, filas = self._filas()

        self.assertEqual(len(filas), 1)
        self.assertEqual(filas[0]["devuelto"], 0)
        self.assertEqual(filas[0]["neto"], 10)
        # Y lo que quedó afuera se cuenta en vez de desaparecer.
        self.assertEqual(sin_tecnico, 1)

    def test_el_periodo_cambia_de_mes_a_semana(self):
        self._documento(TipoDocumento.SAL, 5, solicitante=self.kevin)

        _, por_mes = self._filas("mes")
        _, por_semana = self._filas("semana")

        self.assertRegex(por_mes[0]["periodo"], r"^\d{4}-\d{2}$")
        self.assertRegex(por_semana[0]["periodo"], r"^\d{4}-W\d{2}$")

    def test_una_agrupacion_invalida_cae_en_mes(self):
        self._documento(TipoDocumento.SAL, 5, solicitante=self.kevin)

        _, filas = self._filas("trimestre")

        self.assertRegex(filas[0]["periodo"], r"^\d{4}-\d{2}$")


class LiquidacionesTecnicoTests(TestCase):
    """
    Las liquidaciones traen lo que el técnico DECLARÓ, no una inferencia.

    El punto delicado es la convención del modelo: en una liquidación lo
    devuelto viaja en `DocumentoItem.cantidad` y `cantidad_devuelta` queda
    en cero. Leer el campo de nombre obvio daría devoluciones en cero para
    todos, y nadie lo notaría porque el reporte igual se vería lleno.
    """

    def setUp(self):
        self.jauja = Sede.objects.create(nombre="Jauja", tipo=Sede.CENTRAL)
        self.producto = Producto.objects.create(nombre="ONU", unidad="UND")

        self.almacenero = User.objects.create_user("alm_jauja")
        UserProfile.objects.filter(user=self.almacenero).update(
            rol=UserProfile.Rol.ALMACEN, sede_principal=self.jauja
        )
        self.kevin = User.objects.create_user("kevin")
        UserProfile.objects.filter(user=self.kevin).update(
            rol=UserProfile.Rol.SOLICITANTE, sede_principal=self.jauja
        )

        self.client.force_login(self.almacenero)

    def _liquidar(self, devuelto, usado, merma, tecnico=None, referencia="LIQ-SEMANAL"):
        doc = DocumentoInventario.objects.create(
            tipo=TipoDocumento.ING,
            estado=EstadoDocumento.CONFIRMADO,
            sede=self.jauja,
            responsable=self.almacenero,
            solicitante=tecnico or self.kevin,
            referencia=referencia,
        )
        doc.asignar_numero_si_falta()
        DocumentoItem.objects.create(
            documento=doc,
            producto=self.producto,
            cantidad=devuelto,
            cantidad_usada=usado,
            cantidad_merma=merma,
        )
        return doc

    def _filas(self, agrupacion="mes"):
        from reportes.services.registro_materiales import construir_filas_liquidaciones

        return None, construir_filas_liquidaciones(
            self.jauja, date(2000, 1, 1), date(2100, 12, 31), agrupacion
        )

    def test_lee_lo_devuelto_del_campo_cantidad(self):
        self._liquidar(devuelto=4, usado=10, merma=2)

        _, filas = self._filas()

        self.assertEqual(len(filas), 1)
        self.assertEqual(filas[0]["devuelto"], 4)
        self.assertEqual(filas[0]["usado"], 10)
        self.assertEqual(filas[0]["merma"], 2)
        self.assertEqual(filas[0]["total"], 16)

    def test_calcula_el_porcentaje_de_merma_sobre_el_total(self):
        self._liquidar(devuelto=0, usado=8, merma=2)

        _, filas = self._filas()

        self.assertEqual(filas[0]["pct_merma"], 20.0)

    def test_acumula_varias_liquidaciones_del_mismo_periodo(self):
        self._liquidar(devuelto=1, usado=5, merma=0)
        self._liquidar(devuelto=2, usado=3, merma=1)

        _, filas = self._filas()

        self.assertEqual(len(filas), 1)
        self.assertEqual(filas[0]["devuelto"], 3)
        self.assertEqual(filas[0]["usado"], 8)
        self.assertEqual(filas[0]["documentos"], 2)

    def test_ignora_documentos_que_no_son_liquidaciones(self):
        """
        Una devolución rápida o una compra también son ING con ítems; si se
        colaran, el consumo declarado de alguien subiría sin que lo declare.
        """
        self._liquidar(devuelto=5, usado=0, merma=0, referencia="DEVOLUCION-RAPIDA")

        _, filas = self._filas()

        self.assertEqual(filas, [])

    def test_el_periodo_cambia_de_mes_a_semana(self):
        self._liquidar(devuelto=1, usado=1, merma=0)

        _, por_mes = self._filas("mes")
        _, por_semana = self._filas("semana")

        self.assertRegex(por_mes[0]["periodo"], r"^\d{4}-\d{2}$")
        self.assertRegex(por_semana[0]["periodo"], r"^\d{4}-W\d{2}$")


class MaterialPorObraTests(TestCase):
    """
    El corte por obra es el único que da costo por centro de costo, así que
    sus dos números derivados -desvío y costo real- tienen que estar bien.
    """

    def setUp(self):
        self.jauja = Sede.objects.create(nombre="Jauja", tipo=Sede.CENTRAL)
        self.otra = Sede.objects.create(nombre="Oroya", tipo=Sede.SECUNDARIO)
        self.producto = Producto.objects.create(
            nombre="ONU", unidad="UND", costo_unitario=Decimal("100.00")
        )

        self.almacenero = User.objects.create_user("alm_jauja")
        UserProfile.objects.filter(user=self.almacenero).update(
            rol=UserProfile.Rol.ALMACEN, sede_principal=self.jauja
        )
        self.client.force_login(self.almacenero)

    def _obra(self, codigo, sede=None, **cantidades):
        from proyectos.models import EstadoProyecto, Proyecto, ProyectoMaterial

        obra = Proyecto.objects.create(
            codigo=codigo,
            nombre=f"Obra {codigo}",
            sede=sede or self.jauja,
            creado_por=self.almacenero,
            estado=EstadoProyecto.EN_PROCESO,
        )
        ProyectoMaterial.objects.create(proyecto=obra, producto=self.producto, **cantidades)
        return obra

    def _filas(self):
        from reportes.services.registro_materiales import construir_filas_obras

        filas = construir_filas_obras(self.jauja)
        return round(sum(f["costo_real"] for f in filas), 2), filas

    def test_el_desvio_compara_el_consumo_real_contra_lo_planificado(self):
        """
        Se mide contra usado + merma y no contra lo entregado: el material
        entregado todavía puede volver, el consumido no.
        """
        self._obra(
            "OB-1", cantidad_planificada=10, cantidad_entregada=30,
            cantidad_usada=12, cantidad_merma=3,
        )

        _, filas = self._filas()

        self.assertEqual(filas[0]["desvio"], 5)

    def test_el_costo_real_suma_lo_usado_y_lo_mermado(self):
        self._obra(
            "OB-2", cantidad_planificada=10, cantidad_entregada=10,
            cantidad_usada=6, cantidad_merma=2,
        )

        costo_total, filas = self._filas()

        self.assertEqual(filas[0]["costo_real"], 800.0)
        self.assertEqual(costo_total, 800.0)

    def test_marca_lo_entregado_que_nadie_cerro(self):
        self._obra(
            "OB-3", cantidad_planificada=20, cantidad_entregada=20,
            cantidad_devuelta=2, cantidad_usada=5, cantidad_merma=1,
        )

        _, filas = self._filas()

        self.assertEqual(filas[0]["por_liquidar"], 12)

    def test_no_muestra_obras_de_otra_sede(self):
        self._obra("OB-MIA", cantidad_planificada=5)
        self._obra("OB-AJENA", sede=self.otra, cantidad_planificada=5)

        _, filas = self._filas()

        self.assertEqual([f["obra"] for f in filas], ["OB-MIA"])


class RecepcionSicvTests(TestCase):
    """
    Lo que protege la regla §8.1: ninguna fila se descarta.

    Es el punto donde una integración así se rompe en silencio. Si el
    importador tirara las filas que no entiende, el material declarado en
    campo nunca cuadraría contra el almacén, la pantalla mostraría un total
    menor al real y nada fallaría para avisarlo.
    """

    def setUp(self):
        from inventario.models import Sede as SedeModel

        self.jauja = SedeModel.objects.create(nombre="Jauja", tipo=SedeModel.CENTRAL)
        self.desde = date(2026, 9, 1)
        self.hasta = date(2026, 9, 30)

    def _fila(self, **extra):
        # Las claves son las del reporte real de SICV, con tildes incluidas.
        fila = {
            "Orden": "0007620",
            "Tipo": "Instalación internet",
            "Emisión": "30/08/2026 14:54",
            "Código": "JA01-A0015835",
            "Abonado": "BERROSPI MUÑOZ, ROMULO LUIS",
            "Dirección": "JR SAN MARTIN S/N",
            "Material": "CABLE RG-6",
            "Cantidad": "19.00",
            "Acción": "Utilizado",
            "MAC": "24DO",
            "Atención": "01/09/2026 11:43",
            "Situación": "APROBADO",
            "Técnico": "BRYAN JHOJAN MATEO REYNA",
        }
        fila.update(extra)
        return fila

    def _importar(self, filas, sede=None):
        from reportes.services.sicv import importar_filas

        return importar_filas(
            filas, sede=sede or self.jauja, desde=self.desde, hasta=self.hasta
        )

    # ---------------------------------------------------------------

    def test_guarda_todas_las_columnas_del_reporte(self):
        from reportes.models import DetalleMaterialSicv

        sync = self._importar([self._fila()])

        self.assertEqual(sync.filas_recibidas, 1)
        d = DetalleMaterialSicv.objects.get()
        self.assertEqual(d.orden, "0007620")
        self.assertEqual(d.tipo_orden, "Instalación internet")
        self.assertEqual(d.codigo_abonado, "JA01-A0015835")
        self.assertEqual(d.abonado, "BERROSPI MUÑOZ, ROMULO LUIS")
        self.assertEqual(d.direccion, "JR SAN MARTIN S/N")
        self.assertEqual(d.material, "CABLE RG-6")
        self.assertEqual(d.cantidad, Decimal("19.00"))
        self.assertEqual(d.accion, "Utilizado")
        self.assertEqual(d.mac, "24DO")
        self.assertEqual(d.situacion, "APROBADO")
        self.assertEqual(d.tecnico, "BRYAN JHOJAN MATEO REYNA")
        self.assertIsNotNone(d.emision)
        self.assertIsNotNone(d.atencion)

    def test_guarda_el_texto_sin_normalizar(self):
        """
        El nombre se conserva tal como llega, no en mayúsculas ni recortado:
        es lo que va a permitir interpretarlo cuando se definan las
        equivalencias contra nuestro catálogo.
        """
        from reportes.models import DetalleMaterialSicv

        self._importar([self._fila(Material="  cable RG-6 dúplex  ")])

        self.assertEqual(DetalleMaterialSicv.objects.get().material, "cable RG-6 dúplex")

    def test_la_cantidad_conserva_los_decimales(self):
        """
        SICV declara 19.00 de cable. Truncar metros a unidades enteras haría
        que el consumo de campo nunca cuadre.
        """
        from reportes.models import DetalleMaterialSicv

        self._importar([self._fila(Cantidad="19.75")])

        self.assertEqual(DetalleMaterialSicv.objects.get().cantidad, Decimal("19.75"))

    def test_acepta_la_cantidad_con_coma_decimal(self):
        from reportes.models import DetalleMaterialSicv

        self._importar([self._fila(Cantidad="19,50")])

        self.assertEqual(DetalleMaterialSicv.objects.get().cantidad, Decimal("19.50"))

    def test_reimportar_el_mismo_periodo_no_duplica(self):
        from reportes.models import DetalleMaterialSicv

        self._importar([self._fila()])
        segunda = self._importar([self._fila()])

        self.assertEqual(DetalleMaterialSicv.objects.count(), 1)
        self.assertEqual(segunda.filas_nuevas, 0)
        self.assertEqual(segunda.filas_repetidas, 1)

    def test_dos_materiales_de_la_misma_orden_son_filas_distintas(self):
        """
        Una orden usa varios materiales. Si la huella se armara solo con el
        número de orden, la segunda línea pisaría a la primera y el consumo
        declarado quedaría corto sin que nada avise.
        """
        from reportes.models import DetalleMaterialSicv

        self._importar([
            self._fila(Material="CABLE RG-6", MAC="24DO"),
            self._fila(Material="AISLADOR", MAC="B910"),
        ])

        self.assertEqual(DetalleMaterialSicv.objects.count(), 2)

    def test_dos_sedes_con_el_mismo_numero_de_orden_no_se_pisan(self):
        from inventario.models import Sede as SedeModel
        from reportes.models import DetalleMaterialSicv

        oroya = SedeModel.objects.create(nombre="Oroya", tipo=SedeModel.SECUNDARIO)

        self._importar([self._fila()])
        self._importar([self._fila()], sede=oroya)

        self.assertEqual(DetalleMaterialSicv.objects.count(), 2)

    def test_una_fecha_ilegible_no_pierde_la_fila(self):
        from reportes.models import DetalleMaterialSicv

        sync = self._importar([self._fila(**{"Atención": "el martes pasado"})])

        self.assertEqual(sync.filas_recibidas, 1)
        d = DetalleMaterialSicv.objects.get()
        self.assertIsNone(d.atencion)
        self.assertEqual(d.orden, "0007620")

    def test_un_texto_mas_largo_de_lo_previsto_no_tumba_la_importacion(self):
        from reportes.models import DetalleMaterialSicv

        sync = self._importar([self._fila(Abonado="X" * 400)])

        self.assertEqual(sync.filas_recibidas, 1)
        self.assertEqual(len(DetalleMaterialSicv.objects.get().abonado), 200)

    def test_ignora_las_lineas_vacias_del_archivo(self):
        from reportes.models import DetalleMaterialSicv

        sync = self._importar([
            self._fila(),
            {"Orden": "", "Material": "", "Cantidad": ""},
        ])

        self.assertEqual(DetalleMaterialSicv.objects.count(), 1)
        self.assertEqual(sync.filas_recibidas, 1)

    def test_acepta_las_columnas_sin_tilde(self):
        """
        La API podría mandar claves técnicas en vez de los encabezados del
        reporte. Aceptar ambas evita tener que tocar código cuando se defina
        el formato definitivo.
        """
        from reportes.models import DetalleMaterialSicv

        self._importar([{
            "orden": "999",
            "material": "AISLADOR",
            "cantidad": "2",
            "atencion": "2026-09-10T08:30:00",
            "tecnico": "ANTHONY SOLIS PUENTE",
        }])

        d = DetalleMaterialSicv.objects.get()
        self.assertEqual(d.orden, "999")
        self.assertEqual(d.tecnico, "ANTHONY SOLIS PUENTE")
        self.assertIsNotNone(d.atencion)

    def test_resume_lo_recibido_ordenado_por_volumen(self):
        from reportes.services.sicv import resumen_recibido

        self._importar([
            self._fila(Material="CABLE RG-6", MAC="1", Cantidad="10"),
            self._fila(Material="CABLE RG-6", MAC="2", Cantidad="5"),
            self._fila(Material="AISLADOR", MAC="3", Cantidad="1"),
        ])

        materiales = resumen_recibido(self.jauja)["materiales"]

        self.assertEqual(materiales[0]["material"], "CABLE RG-6")
        self.assertEqual(materiales[0]["filas"], 2)
        self.assertEqual(materiales[0]["total"], Decimal("15"))


class PantallaSicvTests(TestCase):
    """
    La tabla muestra lo recibido, agrupado por técnico.

    El orden de la consulta es parte del contrato de la pantalla:
    {% regroup %} solo junta elementos consecutivos, así que si alguien
    cambia el orden a cronológico el mismo técnico aparecería en varios
    bloques con su nombre repetido.
    """

    def setUp(self):
        from inventario.models import Sede as SedeModel

        self.jauja = SedeModel.objects.create(nombre="Jauja", tipo=SedeModel.CENTRAL)
        self.oroya = SedeModel.objects.create(nombre="Oroya", tipo=SedeModel.SECUNDARIO)

        self.almacenero = User.objects.create_user("alm_jauja")
        UserProfile.objects.filter(user=self.almacenero).update(
            rol=UserProfile.Rol.ALMACEN, sede_principal=self.jauja
        )
        self.client.force_login(self.almacenero)

        self.desde = date(2026, 9, 1)
        self.hasta = date(2026, 9, 30)

    def _importar(self, filas, sede=None):
        from reportes.services.sicv import importar_filas

        return importar_filas(
            filas, sede=sede or self.jauja, desde=self.desde, hasta=self.hasta
        )

    def _fila(self, **extra):
        fila = {
            "Orden": "0007620",
            "Material": "CABLE RG-6",
            "Cantidad": "19.00",
            "Acción": "Utilizado",
            "MAC": "24DO",
            "Atención": "01/09/2026 11:43",
            "Situación": "APROBADO",
            "Técnico": "BRYAN JHOJAN MATEO REYNA",
        }
        fila.update(extra)
        return fila

    def _ver(self, **params):
        """
        Abre la pantalla con un rango que abarca todo.

        La pantalla recorta al mes en curso por defecto, y la mayoría de
        estas pruebas no son sobre el periodo: si dependieran de él, cambiar
        el defecto las rompería a todas. Las que sí lo prueban usan
        `_ver_crudo()` o pasan su propio periodo.
        """
        params.setdefault("filas", 50)
        params.setdefault("periodo", "personalizado")
        params.setdefault("desde", "2000-01-01")
        params.setdefault("hasta", "2100-12-31")
        return self.client.get(reverse("reportes_sicv"), params)

    def _ver_crudo(self, **params):
        """Sin rango impuesto: para probar el recorte por defecto."""
        params.setdefault("filas", 50)
        return self.client.get(reverse("reportes_sicv"), params)

    # ---------------------------------------------------------------

    def test_muestra_las_filas_recibidas(self):
        self._importar([self._fila(MAC="1"), self._fila(MAC="2")])

        respuesta = self._ver()

        self.assertEqual(len(respuesta.context["detalles"]), 2)
        self.assertEqual(respuesta.context["total_filas"], 2)

    def test_no_muestra_filas_de_otra_sede(self):
        self._importar([self._fila(Orden="MIA")])
        self._importar([self._fila(Orden="AJENA")], sede=self.oroya)

        detalles = self._ver().context["detalles"]

        self.assertEqual([d.orden for d in detalles], ["MIA"])

    def test_las_filas_llegan_ordenadas_por_tecnico_para_poder_agrupar(self):
        self._importar([
            self._fila(Orden="1", MAC="1", **{"Técnico": "ZZZ ULTIMO", "Atención": "20/09/2026 10:00"}),
            self._fila(Orden="2", MAC="2", **{"Técnico": "AAA PRIMERO", "Atención": "01/09/2026 10:00"}),
            self._fila(Orden="3", MAC="3", **{"Técnico": "ZZZ ULTIMO", "Atención": "05/09/2026 10:00"}),
        ])

        nombres = [d.tecnico for d in self._ver().context["detalles"]]

        self.assertEqual(nombres, ["AAA PRIMERO", "ZZZ ULTIMO", "ZZZ ULTIMO"])

    def test_filtra_por_tecnico_y_por_material(self):
        self._importar([
            self._fila(Orden="A", MAC="1", Material="CABLE RG-6", **{"Técnico": "UNO"}),
            self._fila(Orden="B", MAC="2", Material="AISLADOR", **{"Técnico": "DOS"}),
            self._fila(Orden="C", MAC="3", Material="AISLADOR", **{"Técnico": "UNO"}),
        ])

        por_tecnico = self._ver(tecnico="UNO").context["detalles"]
        por_material = self._ver(material="AISLADOR").context["detalles"]

        self.assertEqual(sorted(d.orden for d in por_tecnico), ["A", "C"])
        self.assertEqual(sorted(d.orden for d in por_material), ["B", "C"])

    def test_busca_por_orden_abonado_material_y_mac(self):
        self._importar([
            self._fila(Orden="111", Material="CABLE RG-6", MAC="AAAA", Abonado="PEREZ, JUAN"),
            self._fila(Orden="222", Material="AISLADOR", MAC="BBBB", Abonado="LOPEZ, ANA"),
        ])

        for termino, esperado in [("111", "111"), ("AISLADOR", "222"),
                                  ("BBBB", "222"), ("PEREZ", "111")]:
            with self.subTest(termino=termino):
                detalles = self._ver(q=termino).context["detalles"]
                self.assertEqual([d.orden for d in detalles], [esperado])

    def test_un_almacenero_solo_ve_su_sede_aunque_tenga_otras_permitidas(self):
        """
        El caso real de esta base: `personal_jauja` tiene las tres sedes en
        `sedes_permitidas`. Su rol es el que lo limita a una.
        """
        self.almacenero.profile.sedes_permitidas.set([self.jauja, self.oroya])
        self._importar([self._fila()])

        disponibles = self._ver().context["sedes_disponibles"]

        self.assertEqual([s.nombre for s in disponibles], ["Jauja"])

    def test_pedir_otra_sede_por_url_no_muestra_sus_datos(self):
        """
        El selector no puede ser una puerta para leer otro almacén. Una sede
        fuera de las permitidas se ignora y se cae en la propia, en vez de
        devolver sus filas.
        """
        self._importar([self._fila(Orden="MIA")])
        self._importar([self._fila(Orden="AJENA")], sede=self.oroya)

        respuesta = self._ver(sede_id=self.oroya.pk)

        self.assertEqual(respuesta.context["sede"], self.jauja)
        self.assertEqual([d.orden for d in respuesta.context["detalles"]], ["MIA"])

    def test_una_jefa_elige_entre_sus_sedes_aunque_tenga_sede_principal(self):
        """
        El criterio es por ROL, no por tener sede principal. En la base real
        casi todos los usuarios -incluidos técnicos- tienen las tres sedes
        en `sedes_permitidas`, así que usar ese campo como criterio les
        abriría el almacén de las otras sedes.
        """
        from inventario.models import UserProfile as Perfil

        jefa = User.objects.create_user("jefa")
        perfil = Perfil.objects.get(user=jefa)
        perfil.rol = Perfil.Rol.JEFA
        perfil.sede_principal = self.jauja
        perfil.save()
        perfil.sedes_permitidas.set([self.jauja, self.oroya])

        self._importar([self._fila(Orden="MIA")])
        self._importar([self._fila(Orden="AJENA")], sede=self.oroya)

        self.client.force_login(jefa)
        respuesta = self._ver(sede_id=self.oroya.pk)

        self.assertEqual(respuesta.context["sede"], self.oroya)
        self.assertEqual([d.orden for d in respuesta.context["detalles"]], ["AJENA"])

    def test_sin_datos_explica_como_cargarlos(self):
        """
        §8.3: "sin datos" y "conexión apagada" no significan lo mismo y no
        se muestran igual.
        """
        respuesta = self._ver()

        self.assertEqual(respuesta.context["total_filas"], 0)
        self.assertContains(respuesta, "importar_sicv")

    def test_un_tecnico_no_puede_entrar(self):
        tecnico = User.objects.create_user("tec")
        UserProfile.objects.filter(user=tecnico).update(
            rol=UserProfile.Rol.SOLICITANTE, sede_principal=self.jauja
        )
        self.client.force_login(tecnico)

        self.assertEqual(self._ver().status_code, 403)

    def test_los_atajos_de_periodo_acotan_por_fecha_de_atencion(self):
        from django.utils import timezone

        hoy = timezone.localdate()
        ayer = hoy - timedelta(days=1)
        # Dentro del mes pero fuera de los últimos 7 días. Se ancla al día 2
        # para que el caso valga igual cualquier día que corra la prueba.
        hace_rato = hoy.replace(day=2) if hoy.day > 9 else hoy - timedelta(days=10)

        self._importar([
            self._fila(Orden="HOY", MAC="1", **{"Atención": hoy.strftime("%d/%m/%Y 10:00")}),
            self._fila(Orden="AYER", MAC="2", **{"Atención": ayer.strftime("%d/%m/%Y 10:00")}),
            self._fila(Orden="VIEJA", MAC="3", **{"Atención": hace_rato.strftime("%d/%m/%Y 10:00")}),
        ])

        casos = [
            ("hoy", {"HOY"}),
            ("ayer", {"AYER"}),
            ("7dias", {"HOY", "AYER"}),
            ("mes", {"HOY", "AYER", "VIEJA"}),
        ]
        for periodo, esperado in casos:
            with self.subTest(periodo=periodo):
                detalles = self._ver_crudo(periodo=periodo).context["detalles"]
                self.assertEqual({d.orden for d in detalles}, esperado)

    def test_el_periodo_personalizado_usa_las_fechas_del_formulario(self):
        self._importar([
            self._fila(Orden="DENTRO", MAC="1", **{"Atención": "10/09/2026 10:00"}),
            self._fila(Orden="FUERA", MAC="2", **{"Atención": "25/09/2026 10:00"}),
        ])

        detalles = self._ver(
            periodo="personalizado", desde="2026-09-05", hasta="2026-09-15"
        ).context["detalles"]

        self.assertEqual([d.orden for d in detalles], ["DENTRO"])

    def test_un_rango_personalizado_al_reves_se_ordena_solo(self):
        """
        Quien pone 12/09 en "desde" y 05/09 en "hasta" quiso ese rango, no
        un resultado vacío.
        """
        self._importar([self._fila(Orden="DENTRO", **{"Atención": "10/09/2026 10:00"})])

        respuesta = self._ver(periodo="personalizado", desde="2026-09-12", hasta="2026-09-05")

        self.assertEqual(respuesta.context["desde"], date(2026, 9, 5))
        self.assertEqual(respuesta.context["hasta"], date(2026, 9, 12))
        self.assertEqual(len(respuesta.context["detalles"]), 1)

    def test_un_periodo_inventado_cae_en_el_mes_en_curso(self):
        """
        No cae en "sin recorte": SICV acumula una fila por material y por
        orden, así que un ?periodo=cualquiercosa traería meses enteros.
        """
        respuesta = self._ver_crudo(periodo="el-trimestre")

        self.assertEqual(respuesta.context["periodo_sel"], "mes")

    def test_sin_parametros_muestra_el_mes_en_curso(self):
        from django.utils import timezone

        hoy = timezone.localdate()
        respuesta = self._ver_crudo()

        self.assertEqual(respuesta.context["periodo_sel"], "mes")
        self.assertEqual(respuesta.context["desde"], hoy.replace(day=1))
        self.assertEqual(respuesta.context["hasta"], hoy)

    def test_un_personalizado_sin_fechas_no_deja_pasar_todo(self):
        """
        Un ?periodo=personalizado suelto, sin desde ni hasta, no puede
        volverse un "traeme todo" por la puerta de atrás.
        """
        from django.utils import timezone

        hoy = timezone.localdate()
        respuesta = self._ver_crudo(periodo="personalizado")

        self.assertEqual(respuesta.context["desde"], hoy.replace(day=1))
        self.assertEqual(respuesta.context["hasta"], hoy)

    def test_el_periodo_sobrevive_a_la_paginacion(self):
        self._importar([self._fila(**{"Atención": "10/09/2026 10:00"})])

        filtros = self._ver_crudo(periodo="7dias").context["filtros"]

        self.assertIn("periodo=7dias", filtros)


class ClienteSicvTests(TestCase):
    """
    El cliente es la única pieza que habla con SICV, y todavía se apoya en
    supuestos sobre su API. Estas pruebas fijan lo que NO depende de esos
    supuestos: que una falla quede explicada en vez de romper en seco, y que
    la respuesta se entienda venga envuelta o pelada.

    §8.3 pide que "SICV no respondió" se distinga de "no hubo movimientos".
    Eso solo se cumple si cada error deja un mensaje que se pueda guardar.
    """

    def setUp(self):
        from inventario.models import Sede as SedeModel

        self.jauja = SedeModel.objects.create(nombre="Jauja", tipo=SedeModel.CENTRAL)
        self.desde = date(2026, 9, 1)
        self.hasta = date(2026, 9, 30)

    # --- Forma de la respuesta -------------------------------------

    def test_entiende_una_lista_pelada(self):
        from reportes.services.sicv_cliente import extraer_filas

        self.assertEqual(extraer_filas([{"a": 1}]), [{"a": 1}])

    def test_entiende_las_envolturas_habituales(self):
        from reportes.services.sicv_cliente import extraer_filas

        for envoltura in ("data", "results", "rows", "items", "detalles", "registros"):
            with self.subTest(envoltura=envoltura):
                self.assertEqual(extraer_filas({envoltura: [{"a": 1}]}), [{"a": 1}])

    def test_una_forma_desconocida_dice_que_claves_vinieron(self):
        """
        El mensaje tiene que servir para arreglarlo: sin las claves, quien
        lo lea no sabe dónde buscar las filas.
        """
        from reportes.services.sicv_cliente import ErrorSicv, extraer_filas

        with self.assertRaises(ErrorSicv) as caso:
            extraer_filas({"payload": [{"a": 1}], "meta": {}})

        self.assertIn("meta", str(caso.exception))
        self.assertIn("payload", str(caso.exception))

    # --- Fallas de red y de protocolo ------------------------------

    def _consultar_con(self, **kwargs):
        from reportes.services.sicv_cliente import consultar

        with override_settings(SICV_URL="https://sicv.test", SICV_TOKEN="abc", SICV_TIMEOUT=5):
            with mock.patch("reportes.services.sicv_cliente.requests.get", **kwargs) as llamada:
                return consultar(self.jauja, self.desde, self.hasta), llamada

    def test_sin_configurar_lo_dice_antes_de_salir_a_la_red(self):
        from reportes.services.sicv_cliente import ErrorSicv, consultar

        with override_settings(SICV_URL="", SICV_TOKEN=""):
            with self.assertRaises(ErrorSicv) as caso:
                consultar(self.jauja, self.desde, self.hasta)

        self.assertIn("SICV_URL", str(caso.exception))

    def test_un_timeout_dice_cuantos_segundos_espero(self):
        import requests
        from reportes.services.sicv_cliente import ErrorSicv

        with self.assertRaises(ErrorSicv) as caso:
            self._consultar_con(side_effect=requests.Timeout())

        self.assertIn("5 segundos", str(caso.exception))

    def test_un_401_apunta_al_esquema_de_autenticacion(self):
        from reportes.services.sicv_cliente import ErrorSicv

        respuesta = mock.Mock(status_code=401, ok=False, text="no autorizado")

        with self.assertRaises(ErrorSicv) as caso:
            self._consultar_con(return_value=respuesta)

        self.assertIn("SICV_TOKEN", str(caso.exception))

    def test_una_respuesta_que_no_es_json_muestra_con_que_empieza(self):
        from reportes.services.sicv_cliente import ErrorSicv

        respuesta = mock.Mock(status_code=200, ok=True, text="<html>Error 500</html>")
        respuesta.json.side_effect = ValueError()

        with self.assertRaises(ErrorSicv) as caso:
            self._consultar_con(return_value=respuesta)

        self.assertIn("<html>", str(caso.exception))

    def test_un_error_http_no_arrastra_la_pagina_entera_al_mensaje(self):
        """
        Un HTML de error de 40 KB no aporta y llenaría la columna de la base.
        """
        from reportes.services.sicv_cliente import ErrorSicv

        respuesta = mock.Mock(status_code=500, ok=False, text="x" * 5000)

        with self.assertRaises(ErrorSicv) as caso:
            self._consultar_con(return_value=respuesta)

        self.assertLess(len(str(caso.exception)), 400)

    # --- Camino feliz ----------------------------------------------

    def test_devuelve_las_filas_y_manda_el_periodo_en_la_consulta(self):
        respuesta = mock.Mock(status_code=200, ok=True)
        respuesta.json.return_value = {"data": [{"Orden": "1", "Material": "CABLE RG-6"}]}

        filas, llamada = self._consultar_con(return_value=respuesta)

        self.assertEqual(filas, [{"Orden": "1", "Material": "CABLE RG-6"}])
        parametros = llamada.call_args.kwargs["params"]
        self.assertEqual(parametros["desde"], "2026-09-01")
        self.assertEqual(parametros["hasta"], "2026-09-30")
        self.assertEqual(parametros["sede"], "Jauja")

    def test_lo_que_trae_el_cliente_entra_por_el_mismo_camino_que_un_archivo(self):
        """
        La costura del diseño: `importar_filas` recibe diccionarios, así que
        no distingue si vinieron de la API o de un CSV. Lo que se validó
        cargando archivos vale igual para la sincronización.
        """
        from reportes.models import DetalleMaterialSicv, SincronizacionSicv
        from reportes.services.sicv import importar_filas

        respuesta = mock.Mock(status_code=200, ok=True)
        respuesta.json.return_value = [{
            "Orden": "0007620",
            "Material": "CABLE RG-6",
            "Cantidad": "19.00",
            "Atención": "01/09/2026 11:43",
            "Técnico": "BRYAN JHOJAN MATEO REYNA",
        }]

        filas, _ = self._consultar_con(return_value=respuesta)
        sync = importar_filas(
            filas, sede=self.jauja, desde=self.desde, hasta=self.hasta,
            origen=SincronizacionSicv.Origen.API,
        )

        self.assertEqual(sync.filas_nuevas, 1)
        detalle = DetalleMaterialSicv.objects.get()
        self.assertEqual(detalle.orden, "0007620")
        self.assertEqual(detalle.cantidad, Decimal("19.00"))
        self.assertIsNotNone(detalle.atencion)


class ComandoSincronizarTests(TestCase):
    """
    Una corrida que falla tiene que dejar rastro. Si no quedara registrada,
    la pantalla no podría distinguir "SICV no respondió" de "no hubo
    movimientos", que es justo lo que pide §8.3.
    """

    def setUp(self):
        from inventario.models import Sede as SedeModel

        self.jauja = SedeModel.objects.create(nombre="Jauja", tipo=SedeModel.CENTRAL)

    def _correr(self, **extra):
        from django.core.management import call_command

        opciones = {"sede": "Jauja", "desde": "2026-09-01", "hasta": "2026-09-30",
                    "forzar": True, "stdout": StringIO()}
        opciones.update(extra)
        call_command("sincronizar_sicv", **opciones)
        return opciones["stdout"].getvalue()

    def test_apagado_no_corre_salvo_que_se_fuerce(self):
        from reportes.models import SincronizacionSicv

        with override_settings(SICV_SYNC_ENABLED=False):
            salida = self._correr(forzar=False)

        self.assertIn("apagado", salida)
        self.assertFalse(SincronizacionSicv.objects.exists())

    def test_una_falla_queda_registrada_con_su_motivo(self):
        from django.core.management.base import CommandError
        from reportes.models import SincronizacionSicv
        from reportes.services.sicv_cliente import ErrorSicv

        with mock.patch("reportes.management.commands.sincronizar_sicv.consultar",
                        side_effect=ErrorSicv("SICV no respondió en 30 segundos.")):
            with self.assertRaises(CommandError):
                self._correr()

        sync = SincronizacionSicv.objects.get()
        self.assertEqual(sync.estado, SincronizacionSicv.Estado.FALLIDA)
        self.assertIn("no respondió", sync.detalle_error)

    def test_un_periodo_vacio_se_registra_como_completado_no_como_error(self):
        """
        Que SICV no devuelva nada no es una falla: puede que esa semana no
        se haya declarado material. Marcarlo como error haría que alguien
        salga a buscar un problema que no existe.
        """
        from reportes.models import SincronizacionSicv

        with mock.patch("reportes.management.commands.sincronizar_sicv.consultar",
                        return_value=[]):
            salida = self._correr()

        sync = SincronizacionSicv.objects.get()
        self.assertEqual(sync.estado, SincronizacionSicv.Estado.COMPLETADA)
        self.assertEqual(sync.filas_recibidas, 0)
        self.assertIn("no devolvió filas", salida)

    def test_guarda_lo_que_trae_la_api(self):
        from reportes.models import DetalleMaterialSicv, SincronizacionSicv

        filas = [{"Orden": "999", "Material": "AISLADOR", "Cantidad": "2",
                  "Atención": "10/09/2026 08:30", "Técnico": "ANTHONY SOLIS PUENTE"}]

        with mock.patch("reportes.management.commands.sincronizar_sicv.consultar",
                        return_value=filas):
            self._correr()

        sync = SincronizacionSicv.objects.get()
        self.assertEqual(sync.origen, SincronizacionSicv.Origen.API)
        self.assertEqual(sync.filas_nuevas, 1)
        self.assertEqual(DetalleMaterialSicv.objects.get().orden, "999")
