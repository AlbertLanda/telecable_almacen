from decimal import Decimal
from django.db import models
from django.conf import settings
from django.utils import timezone


from inventario.models import (
    Sede,
    Producto,
    TimeStampedModel,
    ItemSerializado,
)


class TipoProyecto(models.TextChoices):
    PROYECTO = "PROYECTO", "Proyecto"
    AVERIA = "AVERIA", "Avería"


class EstadoProyecto(models.TextChoices):
    # FASE 1: NEGOCIACIÓN JOHN ↔ JILMER
    DISENO = "DISENO", "En Diseño"
    REVISION_TECNICA = "REVISION_TECNICA", "En Revisión Técnica"
    OBSERVADO = "OBSERVADO", "Observado"

    # FASE 2: LISTO PARA EJECUCIÓN
    APROBADO = "APROBADO", "Aprobado / Por Iniciar"
    EN_PROCESO = "EN_PROCESO", "En Ejecución"

    # FASE 3: CIERRE
    FINALIZADO = "FINALIZADO", "Finalizado"
    ANULADO = "ANULADO", "Anulado"


class EstadoTransferenciaCuadrilla(models.TextChoices):
    ENTREGADO = "ENTREGADO", "Entregado"
    DEVUELTO = "DEVUELTO", "Devuelto"
    CONSUMIDO = "CONSUMIDO", "Consumido / Instalado"
    MERMA = "MERMA", "Merma"
    PERDIDO = "PERDIDO", "Perdido"
    ANULADO = "ANULADO", "Anulado"


class Proyecto(TimeStampedModel):
    # Identificación
    codigo = models.CharField(
        max_length=40,
        unique=True,
        verbose_name="Código de Obra",
    )
    nombre = models.CharField(max_length=255)
    tipo = models.CharField(
        max_length=10,
        choices=TipoProyecto.choices,
        default=TipoProyecto.PROYECTO,
        help_text="Proyecto planificado o avería reportada.",
    )
    descripcion = models.TextField(blank=True, default="")
    sede = models.ForeignKey(
        Sede,
        on_delete=models.PROTECT,
        related_name="proyectos",
    )
    centro_costo = models.CharField(max_length=255, blank=True, default="")

    plano = models.FileField(
        upload_to="planos/%Y/%m/",
        null=True,
        blank=True,
        verbose_name="Plano / Diseño",
        help_text="Plano enviado por el diseñador.",
    )

    # Roles del flujo PEX
    creado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="proyectos_creados",
        help_text="Diseñador / planificador que creó el proyecto.",
    )

    responsable = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="proyectos_asignados",
        verbose_name="Responsable PEX",
        help_text="Encargado PEX que revisa, aprueba y recibe materiales.",
        null=True,
        blank=True,
    )

    # Estado y fechas
    estado = models.CharField(
        max_length=25,
        choices=EstadoProyecto.choices,
        default=EstadoProyecto.DISENO,
    )
    inicio = models.DateTimeField(default=timezone.now)
    fin = models.DateTimeField(null=True, blank=True)

    # Trazabilidad del ping-pong John ↔ Jilmer
    fecha_envio_revision = models.DateTimeField(null=True, blank=True)
    fecha_aprobacion = models.DateTimeField(null=True, blank=True)
    fecha_observacion = models.DateTimeField(null=True, blank=True)

    observacion_rechazo = models.TextField(
        blank=True,
        default="",
        help_text="Feedback del responsable PEX al observar el proyecto.",
    )

    class Meta:
        ordering = ["-creado_en"]
        verbose_name = "Proyecto / Obra"
        verbose_name_plural = "Proyectos y Obras"

    def __str__(self):
        return f"{self.codigo} - {self.nombre}"

    @property
    def costo_total_real(self) -> Decimal:
        total = Decimal("0.00")
        for mat in self.materiales.all():
            total += mat.costo_total_real
        return total

    @property
    def total_materiales_planificados(self):
        return sum(int(m.cantidad_planificada or 0) for m in self.materiales.all())

    @property
    def total_materiales_entregados(self):
        return sum(int(m.cantidad_entregada or 0) for m in self.materiales.all())

    @property
    def total_materiales_pendientes(self):
        pendiente = self.total_materiales_planificados - self.total_materiales_entregados
        return max(pendiente, 0)

    @property
    def porcentaje_entrega(self):
        planificado = self.total_materiales_planificados
        if planificado <= 0:
            return 0

        porcentaje = (self.total_materiales_entregados / planificado) * 100
        return min(int(porcentaje), 100)

    @property
    def puede_editar_materiales(self):
        return self.estado in [
            EstadoProyecto.DISENO,
            EstadoProyecto.OBSERVADO,
        ]

    @property
    def puede_enviar_a_revision(self):
        return self.estado in [
            EstadoProyecto.DISENO,
            EstadoProyecto.OBSERVADO,
        ] and self.materiales.exists()

    @property
    def puede_aprobar_pex(self):
        return self.estado == EstadoProyecto.REVISION_TECNICA

    @property
    def puede_despachar_almacen(self):
        return self.estado in [
            EstadoProyecto.APROBADO,
            EstadoProyecto.EN_PROCESO,
        ]


class ProyectoAsignacion(TimeStampedModel):
    proyecto = models.ForeignKey(
        Proyecto,
        on_delete=models.CASCADE,
        related_name="asignaciones_extra",
    )
    tecnico = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="proyectos_colaboracion",
    )
    activo = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Técnico colaborador del proyecto"
        verbose_name_plural = "Técnicos colaboradores del proyecto"
        unique_together = ["proyecto", "tecnico"]

    def __str__(self):
        return f"{self.proyecto.codigo} - {self.tecnico.username}"


class ProyectoMaterial(TimeStampedModel):
    proyecto = models.ForeignKey(
        Proyecto,
        on_delete=models.CASCADE,
        related_name="materiales",
    )
    producto = models.ForeignKey(
        Producto,
        on_delete=models.PROTECT,
    )

    cantidad_planificada = models.PositiveIntegerField(
        default=0,
        verbose_name="Cant. Planificada",
    )
    cantidad_entregada = models.PositiveIntegerField(default=0)
    cantidad_devuelta = models.PositiveIntegerField(default=0)
    cantidad_merma = models.PositiveIntegerField(default=0)
    cantidad_usada = models.PositiveIntegerField(default=0)

    costo_unitario = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )

    observacion_diseno = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Nota del diseñador sobre este material.",
    )

    observacion_revision = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Nota del responsable PEX sobre este material.",
    )

    class Meta:
        unique_together = ["proyecto", "producto"]
        verbose_name = "Material de proyecto"
        verbose_name_plural = "Materiales de proyecto"

    def __str__(self):
        return f"{self.proyecto.codigo} - {self.producto.nombre}"

    def save(self, *args, **kwargs):
        if not self.costo_unitario:
            self.costo_unitario = getattr(self.producto, "costo_unitario", 0)
        super().save(*args, **kwargs)

    @property
    def cantidad_pendiente(self):
        pendiente = int(self.cantidad_planificada or 0) - int(self.cantidad_entregada or 0)
        return max(pendiente, 0)

    @property
    def cantidad_por_liquidar(self):
        pendiente = int(self.cantidad_entregada or 0) - (
            int(self.cantidad_devuelta or 0) +
            int(self.cantidad_merma or 0) +
            int(self.cantidad_usada or 0)
        )
        return max(pendiente, 0)

    @property
    def costo_total_real(self) -> Decimal:
        usado = Decimal(int(self.cantidad_usada or 0))
        merma = Decimal(int(self.cantidad_merma or 0))
        cu = Decimal(self.costo_unitario or 0)
        return (cu * (usado + merma)).quantize(Decimal("0.01"))


class AsignacionCuadrilla(TimeStampedModel):
    proyecto = models.ForeignKey(
        Proyecto,
        on_delete=models.CASCADE,
        related_name="transferencias_cuadrilla",
    )

    # Responsable que reparte. Ej: Jilmer
    entregado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="material_entregado_cuadrilla",
    )

    # Técnico que recibe y se hace responsable. Ej: Kevin
    recibido_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="material_recibido_cuadrilla",
    )

    producto = models.ForeignKey(
        Producto,
        on_delete=models.PROTECT,
    )
    cantidad = models.PositiveIntegerField(
        help_text="Cantidad transferida al técnico.",
    )

    estado = models.CharField(
        max_length=20,
        choices=EstadoTransferenciaCuadrilla.choices,
        default=EstadoTransferenciaCuadrilla.ENTREGADO,
    )

    # Clave para ONUs / routers / equipos serializados
    seriales = models.ManyToManyField(
        ItemSerializado,
        blank=True,
        help_text="MACs/Series específicas entregadas.",
    )

    observaciones = models.CharField(max_length=255, blank=True, default="")
    fecha_entrega = models.DateTimeField(default=timezone.now)
    fecha_cierre = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Transferencia a cuadrilla"
        verbose_name_plural = "Transferencias a cuadrilla"
        ordering = ["-creado_en"]

    def __str__(self):
        return (
            f"{self.cantidad} x {self.producto.nombre} "
            f"de {self.entregado_por.username} a {self.recibido_por.username}"
        )

    @property
    def esta_abierta(self):
        return self.estado == EstadoTransferenciaCuadrilla.ENTREGADO


class ProyectoMaterialPendiente(TimeStampedModel):
    """
    Material que Diseño necesita pero todavía no existe en el catálogo.
    Se registra como texto libre y se vincula a un Producto real una vez
    que Almacén lo da de alta en el sistema.
    """

    proyecto = models.ForeignKey(
        Proyecto,
        on_delete=models.CASCADE,
        related_name="materiales_pendientes",
    )

    nombre_solicitado = models.CharField(max_length=255)
    cantidad_estimada = models.PositiveIntegerField(default=1)
    nota = models.CharField(max_length=255, blank=True, default="")

    creado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="materiales_pendientes_creados",
    )

    resuelto = models.BooleanField(default=False)
    producto_vinculado = models.ForeignKey(
        Producto,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    material_resultante = models.ForeignKey(
        ProyectoMaterial,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    resuelto_en = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Material pendiente de catálogo"
        verbose_name_plural = "Materiales pendientes de catálogo"
        ordering = ["-creado_en"]

    def __str__(self):
        return f"{self.proyecto.codigo} - {self.nombre_solicitado} (pendiente)"
