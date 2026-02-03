from django.contrib import admin
from .models import (
    Sede,
    Ubicacion,
    Categoria,
    Producto,
    ProductoSedeInfo,
    Stock,
    MovimientoInventario,
    ItemSerializado,  # ✅ El nuevo protagonista
    UserProfile,
    Proveedor,        # ✅ Agregado
    DocumentoInventario, # ✅ Agregado para ver REQs/SALs
    DocumentoItem,
    StockTecnico      # ✅ Agregado para ver mochilas
)

# ============================================================
# MAESTROS
# ============================================================

@admin.register(Sede)
class SedeAdmin(admin.ModelAdmin):
    search_fields = ("nombre",)
    list_display = ("nombre", "tipo", "activo", "creado_en", "actualizado_en")
    list_filter = ("tipo", "activo")

@admin.register(Ubicacion)
class UbicacionAdmin(admin.ModelAdmin):
    search_fields = ("nombre", "sede__nombre")
    list_filter = ("sede",)
    list_display = ("nombre", "sede", "descripcion", "creado_en", "actualizado_en")
    autocomplete_fields = ("sede",)

@admin.register(Categoria)
class CategoriaAdmin(admin.ModelAdmin):
    search_fields = ("nombre",)
    list_display = ("nombre", "creado_en", "actualizado_en")

@admin.register(Producto)
class ProductoAdmin(admin.ModelAdmin):
    search_fields = ("nombre", "codigo_interno", "barcode")
    list_filter = ("activo", "categoria", "unidad")
    list_display = ("nombre", "codigo_interno", "barcode", "unidad", "costo_unitario", "stock_minimo", "activo")
    autocomplete_fields = ("categoria",)

@admin.register(Proveedor)
class ProveedorAdmin(admin.ModelAdmin):
    search_fields = ("razon_social", "ruc")
    list_display = ("razon_social", "ruc", "telefono", "activo")
    list_filter = ("activo",)

# ============================================================
# INVENTARIO Y STOCK
# ============================================================

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

# ============================================================
# 🔎 TRAZABILIDAD (LO NUEVO)
# ============================================================

@admin.register(ItemSerializado)
class ItemSerializadoAdmin(admin.ModelAdmin):
    # Columnas que verás en la lista
    list_display = (
        'producto', 
        'codigo_trazabilidad',  # 👈 El código "44" pintado
        'serial',               # 👈 El GPON SN
        'mac_address',          # 👈 La MAC
        'estado', 
        'ubicacion',
        'asignado_a'
    )
    
    # Filtros laterales
    list_filter = ('estado', 'ubicacion__sede', 'producto__categoria')
    
    # Barra de búsqueda potente (Busca por todo)
    search_fields = (
        'serial', 
        'codigo_trazabilidad', 
        'mac_address', 
        'serial_secundario',
        'producto__nombre', 
        'producto__codigo_interno'
    )
    
    # Autocompletado para no cargar listas gigantes
    autocomplete_fields = ('producto', 'ubicacion', 'asignado_a')

    # Organización del formulario de edición
    fieldsets = (
        ('Identificación Principal', {
            'fields': ('producto', 'serial', 'codigo_trazabilidad')
        }),
        ('Datos Técnicos', {
            'fields': ('mac_address', 'serial_secundario')
        }),
        ('Situación Actual', {
            'fields': ('ubicacion', 'estado', 'asignado_a')
        }),
    )

@admin.register(StockTecnico)
class StockTecnicoAdmin(admin.ModelAdmin):
    list_display = ('tecnico', 'producto', 'cantidad', 'actualizado_en')
    search_fields = ('tecnico__username', 'producto__nombre')
    list_filter = ('producto',)

# ============================================================
# DOCUMENTOS Y USUARIOS
# ============================================================

class DocumentoItemInline(admin.TabularInline):
    model = DocumentoItem
    extra = 0
    autocomplete_fields = ('producto',)

@admin.register(DocumentoInventario)
class DocumentoInventarioAdmin(admin.ModelAdmin):
    list_display = ('tipo', 'numero', 'sede', 'estado', 'fecha', 'responsable')
    list_filter = ('tipo', 'estado', 'sede')
    search_fields = ('numero', 'responsable__username')
    inlines = [DocumentoItemInline]
    autocomplete_fields = ('sede', 'ubicacion', 'responsable', 'proveedor', 'solicitante')

@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    search_fields = ("user__username", "user__email")
    list_display = ("user", "rol", "sede_principal")
    list_filter = ("rol", "sede_principal")
    autocomplete_fields = ("user", "sede_principal", "sedes_permitidas", "sede_activa")