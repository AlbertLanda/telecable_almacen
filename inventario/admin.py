from django.contrib import admin
from .models import (
    Categoria,
    Ubicacion,
    Producto,
    ProductoSedeInfo,
    Stock,
    MovimientoInventario,
    Sede,
    UserProfile,
)

@admin.register(Categoria)
class CategoriaAdminedeInfoAdmin(admin.ModelAdmin):
    search_fields = ("nombre",)
    list_display = ("nombre", "creado_en", "actualizado_en")


@admin.register(Ubicacion)
class UbicacionAdmin(admin.ModelAdmin):
    search_fields = ("nombre", "sede__nombre")
    list_filter = ("sede",)
    list_display = ("nombre", "sede", "descripcion", "creado_en", "actualizado_en")
    autocomplete_fields = ("sede",)


@admin.register(Producto)
class ProductoAdmin(admin.ModelAdmin):
    search_fields = ("nombre", "codigo_interno", "barcode")
    list_filter = ("activo", "categoria", "unidad")
    list_display = ("nombre", "codigo_interno", "barcode", "unidad", "costo_unitario", "stock_minimo", "activo")
    autocomplete_fields = ("categoria",)


@admin.register(ProductoSedeInfo)
class ProductoSedeInfoAdmin(admin.ModelAdmin):
    search_fields = ("producto__nombre", "producto__codigo_interno", "sede__nombre", "ubicacion_referencial__nombre")
    list_filter = ("sede",)
    list_display = ("producto", "sede", "ubicacion_referencial", "creado_en", "actualizado_en")
    autocomplete_fields = ("producto", "sede", "ubicacion_referencial")


@admin.register(Stock)
class StockAdmin(admin.ModelAdmin):
    search_fields = ("producto__nombre", "producto__codigo_interno", "sede__nombre")
    list_filter = ("sede",)
    list_display = ("producto", "sede", "cantidad", "actualizado_en_operacion")
    autocomplete_fields = ("producto", "sede")


@admin.register(MovimientoInventario)
class MovimientoInventarioAdmin(admin.ModelAdmin):
    search_fields = (
        "producto__nombre",
        "producto__codigo_interno",
        "sede__nombre",
        "ubicacion__nombre",
        "referencia",
    )
    list_filter = ("tipo", "sede", "ubicacion", "producto")
    list_display = ("tipo", "producto", "sede", "ubicacion", "qty", "costo_unitario", "costo_total", "referencia", "creado_en")
    autocomplete_fields = ("producto", "sede", "ubicacion")


@admin.register(Sede)
class SedeAdmin(admin.ModelAdmin):
    search_fields = ("nombre",)
    list_display = ("nombre", "tipo", "activo", "creado_en", "actualizado_en")
    list_filter = ("tipo", "activo")


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    search_fields = ("user__username", "user__email")
    list_display = ("user", "rol", "sede_principal")
    list_filter = ("rol", "sede_principal")
    autocomplete_fields = ("user", "sede_principal", "sedes_permitidas", "sede_activa")
