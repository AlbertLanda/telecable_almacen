from django.contrib import admin
from django import forms
from .models import Proyecto, ProyectoAsignacion, ProyectoMaterial, ProyectoMaterialPendiente, EstadoProyecto


class ProyectoAdminForm(forms.ModelForm):
    class Meta:
        model = Proyecto
        fields = "__all__"

    def clean(self):
        datos = super().clean()
        if self.instance.pk and datos.get("estado") not in (None, EstadoProyecto.FINALIZADO, EstadoProyecto.ANULADO):
            # Django admin ejecuta la validación y el guardado del changeform
            # dentro del mismo atomic: este lock también serializa las firmas.
            proyecto = Proyecto.objects.select_for_update().get(pk=self.instance.pk)
            if proyecto.firmas_acta.exists():
                self.add_error("estado", "El acta ya tiene firmas. Crea una nueva obra o ampliación; esta obra no puede reabrirse.")
        return datos

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
    form = ProyectoAdminForm
    list_display = ('codigo', 'tipo', 'nombre', 'estado', 'responsable', 'creado_por')
    list_filter = ('tipo', 'estado', 'sede')
    search_fields = ('codigo', 'nombre', 'centro_costo')
    inlines = [ProyectoAsignacionInline, ProyectoMaterialInline]
    readonly_fields = ('cierre_registrado', 'mochila_antigua_conciliada')

    fieldsets = (
        ('Datos Generales', {
            'fields': ('sede', 'tipo', 'codigo', 'nombre', 'descripcion', 'estado', 'centro_costo')
        }),
        ('Fechas', {
            'fields': ('inicio', 'fin')
        }),
        ('Auditoría', {
            'fields': ('creado_por', 'cierre_registrado', 'mochila_antigua_conciliada')
        }),
    )

    def save_model(self, request, obj, form, change):
        if not obj.pk:
            obj.creado_por = request.user
        super().save_model(request, obj, form, change)


@admin.register(ProyectoMaterialPendiente)
class ProyectoMaterialPendienteAdmin(admin.ModelAdmin):
    list_display = ('proyecto', 'nombre_solicitado', 'cantidad_estimada', 'resuelto', 'creado_por')
    list_filter = ('resuelto',)
    search_fields = ('nombre_solicitado', 'proyecto__codigo', 'proyecto__nombre')
