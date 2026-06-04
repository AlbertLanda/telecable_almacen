from decimal import Decimal
from django.db import models
from django.conf import settings
from django.utils import timezone
# Importamos modelos base de la app inventario
from inventario.models import Sede, Producto, TimeStampedModel,ItemSerializado, StockTecnico

class EstadoProyecto(models.TextChoices):
    # FASE 1: NEGOCIACIÓN (El Ping-Pong)
    DISENO = "DISENO", "En Diseño"                    # Creador edita
    REVISION_TECNICA = "REVISION_TECNICA", "En Revisión Técnica" # Jilmer revisa
    OBSERVADO = "OBSERVADO", "Observado"              # Jilmer devolvió con notas
    
    # FASE 2: LISTO PARA EJECUCIÓN
    APROBADO = "APROBADO", "Aprobado / Por Iniciar"   # Ambos OK, Almacén ve
    EN_PROCESO = "EN_PROCESO", "En Ejecución"         # Almacén ya despachó algo
    
    # FASE 3: CIERRE
    FINALIZADO = "FINALIZADO", "Finalizado"
    ANULADO = "ANULADO", "Anulado"

class Proyecto(TimeStampedModel):
    # Identificación
    codigo = models.CharField(max_length=40, unique=True, verbose_name="Código de Obra")
    nombre = models.CharField(max_length=255)
    descripcion = models.TextField(blank=True, default="")
    sede = models.ForeignKey(Sede, on_delete=models.PROTECT, related_name="proyectos")
    centro_costo = models.CharField(max_length=255, blank=True, default="")

    plano = models.FileField(
        upload_to='planos/%Y/%m/', 
        null=True, blank=True, verbose_name="Plano (PDF)"
    )

    # Roles
    creado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, 
        on_delete=models.PROTECT, 
        related_name="proyectos_creados",
        help_text="El Diseñador."
    )
    
    # El "Jilmer" (Técnico encargado de aprobar y recibir)
    responsable = models.ForeignKey(
        settings.AUTH_USER_MODEL, 
        on_delete=models.PROTECT, 
        related_name="proyectos_asignados",
        verbose_name="Técnico Responsable",
        null=True, blank=True
    )

    # Estado y Fechas
    estado = models.CharField(max_length=20, choices=EstadoProyecto.choices, default=EstadoProyecto.DISENO)
    inicio = models.DateTimeField(default=timezone.now)
    fin = models.DateTimeField(null=True, blank=True)

    # ✅ CAMPOS DE NEGOCIACIÓN
    observacion_rechazo = models.TextField(blank=True, default="", help_text="Feedback del técnico al observar.")
    fecha_aprobacion = models.DateTimeField(null=True, blank=True)

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


class ProyectoAsignacion(TimeStampedModel):

    proyecto = models.ForeignKey(Proyecto, on_delete=models.CASCADE, related_name="asignaciones_extra")
    tecnico = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="proyectos_colaboracion")
    activo = models.BooleanField(default=True)

class ProyectoMaterial(TimeStampedModel):
    proyecto = models.ForeignKey(Proyecto, on_delete=models.CASCADE, related_name="materiales")
    producto = models.ForeignKey(Producto, on_delete=models.PROTECT)
    
    cantidad_planificada = models.PositiveIntegerField(default=0, verbose_name="Cant. Planificada")
    cantidad_entregada = models.PositiveIntegerField(default=0)
    cantidad_devuelta = models.PositiveIntegerField(default=0)
    cantidad_merma = models.PositiveIntegerField(default=0)
    cantidad_usada = models.PositiveIntegerField(default=0)
    
    costo_unitario = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    class Meta:
        unique_together = ['proyecto', 'producto']

    def save(self, *args, **kwargs):
        if not self.costo_unitario:
            self.costo_unitario = getattr(self.producto, 'costo_unitario', 0)
        super().save(*args, **kwargs)

    @property
    def costo_total_real(self) -> Decimal:
        usado = Decimal(int(self.cantidad_usada or 0))
        merma = Decimal(int(self.cantidad_merma or 0))
        cu = Decimal(self.costo_unitario or 0)
        return (cu * (usado + merma)).quantize(Decimal("0.01"))
    
class AsignacionCuadrilla(TimeStampedModel):
    proyecto = models.ForeignKey(Proyecto, on_delete=models.CASCADE, related_name="transferencias_cuadrilla")
    
    # El responsable que reparte (Ej. Jilmer)
    entregado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="material_entregado_cuadrilla"
    )
    # El técnico que recibe y se hace responsable (Ej. Kevin)
    recibido_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="material_recibido_cuadrilla"
    )
    
    producto = models.ForeignKey(Producto, on_delete=models.PROTECT)
    cantidad = models.PositiveIntegerField(help_text="Cantidad transferida al técnico")
    
    # ✅ CLAVE PARA LAS ONUs: Relación con las series exactas que le dio
    seriales = models.ManyToManyField('inventario.ItemSerializado', blank=True, help_text="Las MACs/Series específicas entregadas")
    
    observaciones = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        verbose_name = "Transferencia a Cuadrilla"
        verbose_name_plural = "Transferencias a Cuadrilla"
        ordering = ["-creado_en"]

    def __str__(self):
        return f"{self.cantidad} x {self.producto.nombre} de {self.entregado_por.username} a {self.recibido_por.username}"