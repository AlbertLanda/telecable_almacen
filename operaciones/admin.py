from django.contrib import admin
from .models import LiquidacionSemanal, LiquidacionLog

@admin.register(LiquidacionSemanal)
class LiquidacionSemanalAdmin(admin.ModelAdmin):
    list_display = (
        'sede', 
        'producto', 
        'semana', 
        'anio', 
        'stock_inicial', 
        'stock_final', 
        'diferencia', 
        'estado'
    )
    list_filter = ('anio', 'semana', 'sede', 'estado')
    search_fields = ('producto__nombre', 'producto__codigo_interno', 'sede__nombre')
    readonly_fields = ('creado_en', 'actualizado_en')
    ordering = ('-anio', '-semana', 'sede')
    
    fieldsets = (
        ('Información Principal', {
            'fields': ('sede', 'producto', 'semana', 'anio', 'estado')
        }),
        ('Métricas de Stock', {
            'fields': (
                'stock_inicial', 'stock_final', 
                'cantidad_entregada', 'cantidad_usada', 
                'cantidad_devuelta', 'cantidad_merma', 
                'diferencia'
            )
        }),
        ('Auditoría', {
            'fields': ('liquidado_por', 'observaciones', 'creado_en', 'actualizado_en')
        }),
    )

@admin.register(LiquidacionLog)
class LiquidacionLogAdmin(admin.ModelAdmin):
    list_display = ('tipo', 'usuario', 'sede', 'semana', 'anio', 'creado_en')
    list_filter = ('tipo', 'creado_en', 'sede')
    search_fields = ('descripcion', 'usuario__username')
    readonly_fields = ('creado_en', 'actualizado_en')