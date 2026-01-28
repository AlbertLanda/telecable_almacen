from django.db import models
from django.contrib.auth import get_user_model
from django.core.validators import MinValueValidator
# Importamos los modelos base desde la app 'inventario'
from inventario.models import Sede, Producto, TimeStampedModel

User = get_user_model()

class LiquidacionSemanal(TimeStampedModel):
    """
    Registro de liquidación semanal de inventario por sede y producto (Ciclo Lunes-Lunes).
    """
    ESTADO_CHOICES = [
        ('PENDIENTE', 'Pendiente'),
        ('LIQUIDADO', 'Liquidado'),
        ('CONSISTENTE', 'Consistente'),
        ('INCONSISTENTE', 'Inconsistente'),
        ('REVISAR', 'Requerir Revisión'),
    ]
    
    fecha_liquidacion = models.DateField()
    semana = models.IntegerField()
    anio = models.IntegerField()
    
    sede = models.ForeignKey(Sede, on_delete=models.CASCADE, related_name='liquidaciones_semanales')
    producto = models.ForeignKey(Producto, on_delete=models.CASCADE, related_name='liquidaciones_semanales')
    
    stock_inicial = models.IntegerField(default=0, validators=[MinValueValidator(0)])
    stock_final = models.IntegerField(default=0, validators=[MinValueValidator(0)])
    
    cantidad_entregada = models.IntegerField(default=0, validators=[MinValueValidator(0)])
    cantidad_usada = models.IntegerField(default=0, validators=[MinValueValidator(0)])
    cantidad_devuelta = models.IntegerField(default=0, validators=[MinValueValidator(0)])
    cantidad_merma = models.IntegerField(default=0, validators=[MinValueValidator(0)])
    
    diferencia = models.IntegerField(default=0)
    estado = models.CharField(max_length=20, choices=ESTADO_CHOICES, default='PENDIENTE')
    
    liquidado_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    observaciones = models.TextField(blank=True, default='')
    
    class Meta:
        verbose_name = "Liquidación Semanal"
        verbose_name_plural = "Liquidaciones Semanales"
        unique_together = ['semana', 'anio', 'sede', 'producto']
        ordering = ['-fecha_liquidacion', 'sede__nombre', 'producto__nombre']
        indexes = [
            models.Index(fields=['fecha_liquidacion']),
            models.Index(fields=['semana', 'anio']),
            models.Index(fields=['sede']),
        ]
    
    def __str__(self):
        return f"{self.sede.nombre} - {self.producto.nombre} - Sem {self.semana}/{self.anio}"
    
    @property
    def variacion_stock(self):
        return self.stock_inicial - self.stock_final

class LiquidacionLog(TimeStampedModel):
    """
    Log de auditoría para las liquidaciones.
    """
    TIPO_CHOICES = [
        ('LIQUIDACION_SEDE', 'Liquidación de Sede'),
        ('LIQUIDACION_CENTRAL', 'Liquidación Central'),
        ('CORRECCION', 'Corrección Manual'),
        ('VERIFICACION', 'Verificación'),
    ]
    
    tipo = models.CharField(max_length=30, choices=TIPO_CHOICES)
    semana = models.IntegerField()
    anio = models.IntegerField()
    sede = models.ForeignKey(Sede, on_delete=models.CASCADE, null=True, blank=True)
    
    usuario = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    descripcion = models.TextField()
    productos_procesados = models.IntegerField(default=0)
    discrepancias_detectadas = models.IntegerField(default=0)
    
    class Meta:
        verbose_name = "Log de Liquidación"
        verbose_name_plural = "Logs de Liquidación"
        ordering = ['-creado_en']