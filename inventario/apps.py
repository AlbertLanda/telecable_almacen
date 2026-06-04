from django.apps import AppConfig

class InventarioConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'inventario'
    verbose_name = 'Inventario General'

    def ready(self):
        try:
            # Intentamos importar las señales
            import inventario.signals
        except ImportError:
            # Si falla, no rompemos todo el sistema (útil para debug)
            pass