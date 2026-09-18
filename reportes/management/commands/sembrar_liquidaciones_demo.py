# -*- coding: utf-8 -*-
"""
Data de prueba para las pestañas Liquidaciones y Obras del reporte.

Todo lo que crea lleva el prefijo DEMO-LIQ / DEMO-OBRA, así que se puede
borrar entero con --limpiar sin tocar nada real. Está pensado para
desarrollo: en producción no hace falta y no debería correrse.

Los casos que siembra están elegidos para que se vea cada situación que el
reporte tiene que saber mostrar, no para que los números sean bonitos:
mermas altas y bajas, obras cerradas, obras a medio liquidar y obras que
todavía no despacharon nada.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from inventario.models import (
    DocumentoInventario,
    DocumentoItem,
    EstadoDocumento,
    MovimientoInventario,
    Producto,
    Sede,
    TipoDocumento,
    UserProfile,
)
from proyectos.models import EstadoProyecto, Proyecto, ProyectoMaterial, TipoProyecto

User = get_user_model()

PREFIJO_LIQ = "LIQ-SEMANAL DEMO"
PREFIJO_OBRA = "DEMO-OBRA"

# (semanas atrás, técnico, material, devuelto, usado, merma)
LIQUIDACIONES = [
    (1, 0, 0, 4, 12, 0),
    (1, 0, 1, 0, 3, 1),
    (1, 1, 0, 2, 8, 2),
    (2, 0, 0, 6, 10, 0),
    (2, 1, 1, 1, 5, 0),
    (2, 1, 0, 0, 14, 3),
    (5, 0, 1, 3, 6, 0),
    (5, 1, 0, 5, 9, 1),
]

# (sufijo, nombre, estado, planificado, entregado, devuelto, usado, merma)
OBRAS = [
    # Cerrada y cuadrada: lo entregado se repartió entero.
    ("0001", "Ampliación troncal San Jerónimo", EstadoProyecto.FINALIZADO,
     100, 100, 15, 80, 5),
    # Se gastó más de lo planificado: desvío positivo.
    ("0002", "Avería fibra cortada Av. Huancavelica", EstadoProyecto.FINALIZADO,
     20, 35, 0, 33, 2),
    # En ejecución, con material entregado que nadie cerró todavía.
    ("0003", "Red nueva urbanización Los Andes", EstadoProyecto.EN_PROCESO,
     200, 150, 10, 60, 5),
    # Aprobada pero sin despachar: solo existe el plan.
    ("0004", "Ampliación Sicaya etapa 2", EstadoProyecto.APROBADO,
     80, 0, 0, 0, 0),
]


class Command(BaseCommand):
    help = "Siembra liquidaciones y obras de prueba. Solo para desarrollo."

    def add_arguments(self, parser):
        parser.add_argument(
            "--sede",
            default=None,
            help="Nombre de la sede. Por defecto, la CENTRAL.",
        )
        parser.add_argument(
            "--limpiar",
            action="store_true",
            help="Borra lo sembrado por este comando y sale.",
        )

    # ------------------------------------------------------------------

    def handle(self, *args, **opciones):
        sede = self._resolver_sede(opciones["sede"])
        if not sede:
            return

        if opciones["limpiar"]:
            self._limpiar(sede)
            return

        tecnicos = self._tecnicos(sede)
        if len(tecnicos) < 2:
            self.stderr.write(self.style.ERROR(
                "Hacen falta al menos 2 usuarios con rol SOLICITANTE en la sede."
            ))
            return

        # Se prefieren productos con costo cargado: si se toman los primeros
        # por id pueden salir todos en cero y la columna "Costo real" -que es
        # justamente la que hay que verificar- no probaría nada.
        materiales = list(
            Producto.objects.filter(activo=True).order_by("-costo_unitario", "id")[:2]
        )
        if len(materiales) < 2:
            self.stderr.write(self.style.ERROR("Hacen falta al menos 2 productos activos."))
            return

        with transaction.atomic():
            creadas = self._sembrar_liquidaciones(sede, tecnicos, materiales)
            obras = self._sembrar_obras(sede, tecnicos, materiales)

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"Sembrado en {sede.nombre}: {creadas} liquidaciones y {obras} materiales de obra."
        ))
        self.stdout.write("Revisá las pestañas Liquidaciones y Obras del reporte general.")
        self.stdout.write(self.style.WARNING(
            "Para borrarlo: manage.py sembrar_liquidaciones_demo --limpiar"
        ))

    # ------------------------------------------------------------------

    def _resolver_sede(self, nombre):
        if nombre:
            sede = Sede.objects.filter(nombre__iexact=nombre).first()
            if not sede:
                self.stderr.write(self.style.ERROR(f"No existe la sede {nombre!r}."))
            return sede

        sede = Sede.objects.filter(tipo=Sede.CENTRAL).first() or Sede.objects.first()
        if not sede:
            self.stderr.write(self.style.ERROR("No hay ninguna sede cargada."))
        return sede

    def _tecnicos(self, sede):
        return list(
            User.objects.filter(
                is_active=True,
                profile__rol=UserProfile.Rol.SOLICITANTE,
                profile__sede_principal=sede,
            ).order_by("id")[:2]
        )

    def _almacenero(self, sede):
        return (
            User.objects.filter(
                profile__rol=UserProfile.Rol.ALMACEN, profile__sede_principal=sede
            ).first()
            or User.objects.filter(is_superuser=True).first()
        )

    # ------------------------------------------------------------------

    def _sembrar_liquidaciones(self, sede, tecnicos, materiales):
        """
        Crea un ING de liquidación por técnico y semana.

        Replica lo que hace operaciones.views.liquidar_tecnico, incluida su
        convención: lo devuelto va en `cantidad` y `cantidad_devuelta` queda
        en cero. Si acá se sembrara distinto, el reporte se vería bien con
        datos de prueba y mal con los reales.
        """
        responsable = self._almacenero(sede)
        hoy = timezone.localdate()

        por_documento = {}
        for semanas, idx_tec, idx_mat, devuelto, usado, merma in LIQUIDACIONES:
            por_documento.setdefault((semanas, idx_tec), []).append(
                (materiales[idx_mat], devuelto, usado, merma)
            )

        creadas = 0
        for (semanas, idx_tec), lineas in sorted(por_documento.items()):
            tecnico = tecnicos[idx_tec]
            fecha = timezone.make_aware(
                timezone.datetime.combine(
                    hoy - timedelta(weeks=semanas), timezone.datetime.min.time()
                )
            ) + timedelta(hours=17)

            doc = DocumentoInventario.objects.create(
                tipo=TipoDocumento.ING,
                estado=EstadoDocumento.CONFIRMADO,
                sede=sede,
                responsable=responsable,
                solicitante=tecnico,
                referencia=PREFIJO_LIQ,
                observaciones=f"[DEMO] Liquidación de {tecnico.username}",
                fecha=fecha,
            )
            doc.asignar_numero_si_falta()

            for producto, devuelto, usado, merma in lineas:
                DocumentoItem.objects.create(
                    documento=doc,
                    producto=producto,
                    cantidad=devuelto,
                    cantidad_usada=usado,
                    cantidad_merma=merma,
                    observacion="Liq. Técnico",
                )

                # El movimiento solo existe si algo volvió físicamente, igual
                # que en el flujo real: lo usado y lo mermado no reingresan.
                if devuelto > 0:
                    movimiento = MovimientoInventario.objects.create(
                        producto=producto,
                        sede=sede,
                        tipo=MovimientoInventario.TIPO_IN,
                        qty=devuelto,
                        referencia=doc.numero,
                        usuario=responsable,
                        nota=f"Retorno Liq. {tecnico.username}",
                        creado_en=fecha,
                    )
                    movimiento.aplicar()
                    # creado_en es auto_now_add: hay que reescribirlo para
                    # que el movimiento caiga en la semana que corresponde.
                    MovimientoInventario.objects.filter(pk=movimiento.pk).update(creado_en=fecha)

            creadas += 1
            self.stdout.write(
                f"  {doc.numero}  {fecha:%d/%m/%Y}  {tecnico.username}  "
                f"({len(lineas)} material{'es' if len(lineas) > 1 else ''})"
            )

        return creadas

    def _sembrar_obras(self, sede, tecnicos, materiales):
        responsable = self._almacenero(sede)
        creador = responsable
        total = 0

        for i, (sufijo, nombre, estado, plan, entregado, devuelto, usado, merma) in enumerate(OBRAS):
            tipo = TipoProyecto.AVERIA if "Avería" in nombre else TipoProyecto.PROYECTO

            obra, _ = Proyecto.objects.update_or_create(
                codigo=f"{PREFIJO_OBRA}-{sufijo}",
                defaults={
                    "nombre": nombre,
                    "tipo": tipo,
                    "sede": sede,
                    "estado": estado,
                    "creado_por": creador,
                    "responsable": tecnicos[i % len(tecnicos)],
                    "descripcion": "[DEMO] Obra de prueba para el reporte.",
                },
            )

            producto = materiales[i % len(materiales)]
            ProyectoMaterial.objects.update_or_create(
                proyecto=obra,
                producto=producto,
                defaults={
                    "cantidad_planificada": plan,
                    "cantidad_entregada": entregado,
                    "cantidad_devuelta": devuelto,
                    "cantidad_usada": usado,
                    "cantidad_merma": merma,
                },
            )
            total += 1
            self.stdout.write(f"  {obra.codigo}  {obra.get_estado_display():<22} {producto.nombre}")

        return total

    # ------------------------------------------------------------------

    def _limpiar(self, sede):
        docs = DocumentoInventario.objects.filter(referencia=PREFIJO_LIQ, sede=sede)
        numeros = list(docs.values_list("numero", flat=True))

        movimientos = MovimientoInventario.objects.filter(referencia__in=numeros)
        n_mov = movimientos.count()
        movimientos.delete()

        n_docs = docs.count()
        docs.delete()

        obras = Proyecto.objects.filter(codigo__startswith=PREFIJO_OBRA)
        n_obras = obras.count()
        ProyectoMaterial.objects.filter(proyecto__in=obras).delete()
        obras.delete()

        self.stdout.write(self.style.SUCCESS(
            f"Borrados: {n_docs} liquidaciones, {n_mov} movimientos y {n_obras} obras."
        ))
        self.stdout.write(self.style.WARNING(
            "El stock NO se revirtió: los ingresos que sumaron esas liquidaciones "
            "quedan en las existencias."
        ))
