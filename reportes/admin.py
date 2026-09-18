# -*- coding: utf-8 -*-
"""
Admin de la recepción de SICV.

Es solo de lectura: lo que llegó del sistema de campo es evidencia de lo
que SICV declaró, y corregirlo a mano acá haría imposible distinguir un
dato recibido de uno editado. Si algo llegó mal, se corrige en SICV y se
vuelve a importar -la huella hace que la reimportación actualice en vez de
duplicar-.
"""
from django.contrib import admin

from .models import DetalleMaterialSicv, SincronizacionSicv


@admin.register(SincronizacionSicv)
class SincronizacionSicvAdmin(admin.ModelAdmin):
    list_display = (
        "iniciada_en", "sede", "desde", "hasta", "origen", "estado",
        "filas_recibidas", "filas_nuevas", "filas_repetidas",
    )
    list_filter = ("estado", "origen", "sede")
    date_hierarchy = "iniciada_en"
    readonly_fields = ("iniciada_en", "terminada_en")

    def has_add_permission(self, request):
        # Una sincronización la crea el importador, no una persona: creada a
        # mano quedaría sin filas y ensuciaría el historial.
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(DetalleMaterialSicv)
class DetalleMaterialSicvAdmin(admin.ModelAdmin):
    list_display = (
        "orden", "atencion", "material", "cantidad", "accion",
        "tecnico", "situacion", "sede",
    )
    list_filter = ("sede", "situacion", "accion", "tipo_orden")
    search_fields = ("orden", "material", "tecnico", "abonado", "codigo_abonado", "mac")
    date_hierarchy = "atencion"
    list_select_related = ("sede",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
