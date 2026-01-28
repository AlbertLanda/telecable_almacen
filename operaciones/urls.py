from django.urls import path
from .views import (
    # Técnico
    tecnico_dashboard, 
    tecnico_mis_reqs, 
    tecnico_mis_entregas,
    
    # Liquidación
    liquidacion_dashboard,
    liquidar_sede,
    liquidar_central,
    liquidacion_detalle,
    liquidacion_exportar_excel,
    liquidacion_api_resumen,
    liquidacion_api_graficos,
    LiquidacionListView
)



urlpatterns = [
    # TÉCNICO (Rutas base: /operaciones/tecnico/...)
    path("tecnico/", tecnico_dashboard, name="tecnico_dashboard"),
    path("tecnico/mis-reqs/", tecnico_mis_reqs, name="tecnico_mis_reqs"),
    path("tecnico/mis-entregas/", tecnico_mis_entregas, name="tecnico_mis_entregas"),

    # LIQUIDACIÓN (Rutas base: /operaciones/liquidacion/...)
    path("liquidacion/", liquidacion_dashboard, name="liquidacion_dashboard"),
    path("liquidacion/lista/", LiquidacionListView.as_view(), name="liquidacion_lista"),
    path("liquidacion/sede/<int:sede_id>/", liquidar_sede, name="liquidar_sede"),
    path("liquidacion/central/", liquidar_central, name="liquidar_central"),
    path("liquidacion/<int:liquidacion_id>/", liquidacion_detalle, name="liquidacion_detalle"),
    path("liquidacion/exportar/", liquidacion_exportar_excel, name="liquidacion_exportar_excel"),
    
    # API Liquidación
    path("api/resumen/", liquidacion_api_resumen, name="liquidacion_api_resumen"),
    path("api/graficos/", liquidacion_api_graficos, name="liquidacion_api_graficos"),
]