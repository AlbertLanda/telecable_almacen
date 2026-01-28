from django.contrib import admin
from .models import Proyecto, ProyectoAsignacion, ProyectoMaterial

class ProyectoMaterialInline(admin.TabularInline):
    model = ProyectoMaterial
    extra = 1
    autocomplete_fields = ['producto']
    readonly_fields = ['costo_unitario']

class ProyectoAsignacionInline(admin.TabularInline):
    model = ProyectoAsignacion
    extra = 1
    autocomplete_fields = ['tecnico']

@admin.register(Proyecto)
class ProyectoAdmin(admin.ModelAdmin):
    list_display = ('codigo', 'nombre', 'sede', 'estado', 'inicio', 'fin')
    list_filter = ('estado', 'sede', 'creado_en')
    search_fields = ('codigo', 'nombre', 'centro_costo')
    inlines = [ProyectoAsignacionInline, ProyectoMaterialInline]
    
    fieldsets = (
        ('Datos Generales', {
            'fields': ('sede', 'codigo', 'nombre', 'descripcion', 'estado', 'centro_costo')
        }),
        ('Fechas', {
            'fields': ('inicio', 'fin')
        }),
        ('Auditoría', {
            'fields': ('creado_por',)
        }),
    )

    def save_model(self, request, obj, form, change):
        if not obj.pk:
            obj.creado_por = request.user
        super().save_model(request, obj, form, change)