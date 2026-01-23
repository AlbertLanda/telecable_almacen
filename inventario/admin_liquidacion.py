from django.contrib import admin
from django.utils.html import format_html
from django.db.models import Sum, Count, Q

from .models_liquidacion import LiquidacionSemanal, LiquidacionLog


@admin.register(LiquidacionSemanal)
class LiquidacionSemanalAdmin(admin.ModelAdmin):
    list_display = [
        'fecha_liquidacion', 'sede', 'producto', 'semana', 'anio',
        'stock_inicial', 'stock_final', 'diferencia', 'estado_badge'
    ]
    list_filter = [
        'fecha_liquidacion', 'semana', 'anio', 'sede', 'estado',
        'sede__tipo'
    ]
    search_fields = [
        'producto__nombre', 'producto__codigo_interno', 
        'sede__nombre', 'observaciones'
    ]
    readonly_fields = [
        'creado_en', 'actualizado_en', 'variacion_stock', 
        'movimiento_neto', 'porcentaje_usado', 'porcentaje_merma',
        'tipo_diferencia'
    ]
    
    fieldsets = (
        ('Información General', {
            'fields': (
                'fecha_liquidacion', 'semana', 'anio', 
                'sede', 'producto', 'estado'
            )
        }),
        ('Stock y Cantidades', {
            'fields': (
                'stock_inicial', 'stock_final',
                'cantidad_entregada', 'cantidad_usada',
                'cantidad_devuelta', 'cantidad_merma',
                'diferencia'
            )
        }),
        ('Cálculos Automáticos', {
            'fields': (
                'variacion_stock', 'movimiento_neto',
                'porcentaje_usado', 'porcentaje_merma',
                'tipo_diferencia'
            ),
            'classes': ('collapse',)
        }),
        ('Auditoría', {
            'fields': (
                'liquidado_por', 'observaciones',
                'creado_en', 'actualizado_en'
            )
        }),
    )
    
    def estado_badge(self, obj):
        colors = {
            'PENDIENTE': 'gray',
            'LIQUIDADO': 'blue',
            'CONSISTENTE': 'green',
            'INCONSISTENTE': 'red',
            'REVISAR': 'orange'
        }
        color = colors.get(obj.estado, 'gray')
        return format_html(
            '<span style="background-color: {}; color: white; '
            'padding: 2px 8px; border-radius: 12px; font-size: 11px;">'
            '{}</span>', color, obj.get_estado_display()
        )
    estado_badge.short_description = 'Estado'
    
    def get_queryset(self, request):
        return super().get_queryset(request).select_related(
            'sede', 'producto', 'liquidado_por'
        )
    
    def changelist_view(self, request, extra_context=None):
        # Agregar estadísticas al changelist
        queryset = self.get_queryset(request)
        
        total_productos = queryset.count()
        total_diferencia = queryset.aggregate(
            total=Sum('diferencia')
        )['total'] or 0
        
        discrepancias = queryset.filter(diferencia__ne=0).count()
        
        extra_context = extra_context or {}
        extra_context.update({
            'total_productos': total_productos,
            'total_diferencia': total_diferencia,
            'discrepancias': discrepancias,
            'porcentaje_discrepancias': (
                (discrepancias / total_productos * 100) if total_productos > 0 else 0
            )
        })
        
        return super().changelist_view(request, extra_context)


@admin.register(LiquidacionLog)
class LiquidacionLogAdmin(admin.ModelAdmin):
    list_display = [
        'creado_en', 'tipo', 'semana', 'anio', 'sede',
        'usuario', 'productos_procesados', 'discrepancias_detectadas'
    ]
    list_filter = [
        'tipo', 'semana', 'anio', 'sede', 'usuario'
    ]
    search_fields = [
        'descripcion', 'usuario__username', 'sede__nombre'
    ]
    readonly_fields = ['creado_en', 'actualizado_en']
    
    fieldsets = (
        ('Información General', {
            'fields': (
                'tipo', 'semana', 'anio', 'sede', 'usuario'
            )
        }),
        ('Resultados', {
            'fields': (
                'productos_procesados', 'discrepancias_detectadas'
            )
        }),
        ('Descripción', {
            'fields': ('descripcion',)
        }),
        ('Auditoría', {
            'fields': ('creado_en', 'actualizado_en'),
            'classes': ('collapse',)
        }),
    )
    
    def get_queryset(self, request):
        return super().get_queryset(request).select_related(
            'sede', 'usuario'
        )
