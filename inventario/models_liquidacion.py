from django.db import models
from django.contrib.auth import get_user_model
from django.core.validators import MinValueValidator
from django.utils import timezone

from .models import TimeStampedModel, Sede, Producto

User = get_user_model()


class LiquidacionSemanal(TimeStampedModel):
    """
    Registro de liquidación semanal de inventario por sede y producto
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
    
    sede = models.ForeignKey(Sede, on_delete=models.CASCADE, related_name='liquidaciones')
    producto = models.ForeignKey(Producto, on_delete=models.CASCADE, related_name='liquidaciones')
    
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
            models.Index(fields=['producto']),
        ]
    
    def __str__(self):
        return f"{self.sede.nombre} - {self.producto.nombre} - Sem {self.semana}/{self.anio}"
    
    @property
    def variacion_stock(self):
        """Diferencia entre stock inicial y final"""
        return self.stock_inicial - self.stock_final
    
    @property
    def movimiento_neto(self):
        """Movimiento neto (entregado - devuelto)"""
        return self.cantidad_entregada - self.cantidad_devuelta
    
    @property
    def porcentaje_usado(self):
        """Porcentaje de stock utilizado"""
        if self.stock_inicial > 0:
            return round((self.cantidad_usada / self.stock_inicial) * 100, 2)
        return 0
    
    @property
    def porcentaje_merma(self):
        """Porcentaje de merma"""
        if self.stock_inicial > 0:
            return round((self.cantidad_merma / self.stock_inicial) * 100, 2)
        return 0
    
    @property
    def tipo_diferencia(self):
        """Tipo de diferencia"""
        if self.diferencia == 0:
            return 'BALANCEADO'
        elif self.diferencia > 0:
            return 'SOBRANTE'
        else:
            return 'FALTANTE'
    
    @property
    def estado_display(self):
        """Estado para display"""
        if self.diferencia == 0:
            return 'OK'
        else:
            return 'DISCREPANCIA'


class LiquidacionLog(TimeStampedModel):
    """
    Log de operaciones de liquidación
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
    
    def __str__(self):
        return f"{self.get_tipo_display()} - Sem {self.semana}/{self.anio}"
