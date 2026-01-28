from decimal import Decimal
from django.db import models
from django.conf import settings
from django.utils import timezone
# Importamos modelos base de la app inventario
from inventario.models import Sede, Producto, TimeStampedModel

class EstadoProyecto(models.TextChoices):
    PENDIENTE = "PENDIENTE", "Pendiente / Diseño"  # Creado por Diseñador
    EN_PROCESO = "EN_PROCESO", "En Ejecución"      # Material Entregado
    FINALIZADO = "FINALIZADO", "Finalizado"        # Liquidado
    ANULADO = "ANULADO", "Anulado"

class Proyecto(TimeStampedModel):
    # Identificación
    codigo = models.CharField(max_length=40, unique=True, verbose_name="Código de Obra")
    nombre = models.CharField(max_length=255)
    descripcion = models.TextField(blank=True, default="")
    sede = models.ForeignKey(Sede, on_delete=models.PROTECT, related_name="proyectos")
    centro_costo = models.CharField(max_length=255, blank=True, default="")

    # ✅ NUEVO: El Plano del Diseñador
    plano = models.FileField(
        upload_to='planos/%Y/%m/', 
        null=True, 
        blank=True, 
        verbose_name="Plano (PDF)"
    )

    # Roles
    creado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, 
        on_delete=models.PROTECT, 
        related_name="proyectos_creados",
        help_text="El Diseñador o Planificador que creó el proyecto."
    )
    
    # ✅ NUEVO: Responsable Directo (Para que le salga en su panel)
    responsable = models.ForeignKey(
        settings.AUTH_USER_MODEL, 
        on_delete=models.PROTECT, 
        related_name="proyectos_asignados",
        verbose_name="Técnico Responsable",
        null=True, 
        blank=True
    )

    # Estado y Fechas
    estado = models.CharField(max_length=20, choices=EstadoProyecto.choices, default=EstadoProyecto.PENDIENTE)
    inicio = models.DateTimeField(default=timezone.now)
    fin = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-creado_en"]
        verbose_name = "Proyecto / Obra"
        verbose_name_plural = "Proyectos y Obras"

    def __str__(self):
        return f"{self.codigo} - {self.nombre}"

    # ✅ LÓGICA FINANCIERA (Para el Admin)
    @property
    def costo_total_real(self) -> Decimal:
        """Suma el costo real de todos los materiales usados en el proyecto."""
        total = Decimal("0.00")
        for mat in self.materiales.all():
            total += mat.costo_total_real
        return total


class ProyectoAsignacion(TimeStampedModel):
    """
    Tabla opcional por si quieres asignar ayudantes o una cuadrilla extra
    además del responsable principal.
    """
    proyecto = models.ForeignKey(Proyecto, on_delete=models.CASCADE, related_name="asignaciones_extra")
    tecnico = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="proyectos_colaboracion")
    activo = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.tecnico.username} en {self.proyecto.codigo}"


class ProyectoMaterial(TimeStampedModel):
    proyecto = models.ForeignKey(Proyecto, on_delete=models.CASCADE, related_name="materiales")
    producto = models.ForeignKey(Producto, on_delete=models.PROTECT)
    
    # ✅ NUEVO: Lo que el Diseñador calculó en la "Receta"
    cantidad_planificada = models.PositiveIntegerField(default=0, verbose_name="Cant. Planificada")
    
    # Lo que realmente pasó (se llena con Almacén y Liquidación)
    cantidad_entregada = models.PositiveIntegerField(default=0)  # Suma de SALs
    cantidad_devuelta = models.PositiveIntegerField(default=0, help_text="Material que regresó al almacén")
    cantidad_merma = models.PositiveIntegerField(default=0)
    cantidad_usada = models.PositiveIntegerField(default=0)
    
    # Costo histórico (se guarda al momento de asignar para no variar si sube el precio después)
    costo_unitario = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    class Meta:
        unique_together = ['proyecto', 'producto'] # Evita duplicados del mismo producto en un proyecto

    def __str__(self):
        return f"{self.producto.nombre} - Plan: {self.cantidad_planificada}"

    # 👇 AQUÍ ESTÁ LA MAGIA: COPIA EL PRECIO AUTOMÁTICAMENTE
    def save(self, *args, **kwargs):
        # Si el costo es nulo o cero, intentamos buscarlo en el Producto original
        if not self.costo_unitario:
            # Intentamos obtener 'costo_unitario' o 'precio' del modelo Producto
            precio_maestro = getattr(self.producto, 'costo_unitario', 0)
            
            # Si en tu modelo Producto se llama 'precio', descomenta la línea de abajo:
            # precio_maestro = getattr(self.producto, 'precio', 0)
            
            self.costo_unitario = precio_maestro
            
        super().save(*args, **kwargs)

    @property
    def costo_total_real(self) -> Decimal:
        """
        Costo = (Usado + Merma) * Costo Unitario
        """
        usado = Decimal(int(self.cantidad_usada or 0))
        merma = Decimal(int(self.cantidad_merma or 0))
        cu = Decimal(self.costo_unitario or 0)
        
        # El costo real es lo que se gastó (instalado + desperdicio)
        return (cu * (usado + merma)).quantize(Decimal("0.01"))