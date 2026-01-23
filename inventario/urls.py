from django.urls import path
from django.contrib.auth import views as auth_views

from inventario.views_auth import RoleBasedLoginView
from .views_api import api_reqs_almacen_list, api_reqs_almacen_create

from .views_dashboard import (
    dashboard_redirect,
    dash_almacen,
    dash_solicitante,
    dash_admin,
    inventory_list,
)

from .views_api import api_dashboard_almacen
from .views import scan_view

# REQ (funciones)
from .views_req import (
    req_home,
    req_add_item,
    req_scan_add,
    req_enviar,
    req_convert_to_sal,
    req_catalogo,
    req_add_producto,
    req_carrito,
    req_set_qty,
    req_remove_producto,
)

# Importamos el módulo para usar views_req.req_set_tipo_requerimiento y views_req.req_print
from . import views_req

from .views_sal import sal_detail, sal_confirmar, sal_print
from .views_tecnico import tecnico_dashboard, tecnico_mis_reqs, tecnico_mis_entregas

# Importar views de liquidación
from .views_liquidacion import (
    liquidacion_dashboard,
    liquidar_sede,
    liquidar_central,
    liquidacion_detalle,
    liquidacion_api_resumen,
    liquidacion_api_graficos,
    liquidacion_exportar_excel,
    LiquidacionListView,
)


urlpatterns = [
    # AUTH
    path("login/", RoleBasedLoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(next_page="/login/"), name="logout"),

    # HOME (logueado)
    path("", dashboard_redirect, name="home"),
    path("scan/", scan_view, name="scan"),

    # REQ
    path("req/", req_home, name="req_home"),
    path("req/add/", req_add_item, name="req_add_item"),
    path("req/scan-add/", req_scan_add, name="req_scan_add"),
    path("req/<int:req_id>/enviar/", req_enviar, name="req_enviar"),

    path("req/catalogo/", req_catalogo, name="req_catalogo"),
    path("req/add-producto/", req_add_producto, name="req_add_producto"),
    path("req/carrito/", req_carrito, name="req_carrito"),
    path("req/set-qty/", req_set_qty, name="req_set_qty"),
    path("req/remove-producto/", req_remove_producto, name="req_remove_producto"),

    # ✅ NUEVO: tipo de requerimiento (proveedor / entre sedes)
    path("req/set-tipo/", views_req.req_set_tipo_requerimiento, name="req_set_tipo_requerimiento"),

    # ✅ NUEVO: impresión de REQ (2 formatos)
    path("req/<int:req_id>/print/", views_req.req_print, name="req_print"),

    # REQ → SAL
    path("req/<int:req_id>/to-sal/", req_convert_to_sal, name="req_to_sal"),

    # SAL
    path("sal/<int:sal_id>/", sal_detail, name="sal_detail"),
    path("sal/<int:sal_id>/confirmar/", sal_confirmar, name="sal_confirmar"),
    path("sal/<int:sal_id>/print/", sal_print, name="sal_print"),

    # DASHBOARD
    path("dashboard/", dashboard_redirect, name="dashboard"),
    path("dashboard/almacen/", dash_almacen, name="dash_almacen"),
    path("dashboard/solicitante/", dash_solicitante, name="dash_solicitante"),
    path("dashboard/admin/", dash_admin, name="dash_admin"),

    # Inventario listado
    path("dashboard/inventario/", inventory_list, name="inventory_list"),

    # API
    path("api/dashboard/almacen/", api_dashboard_almacen, name="api_dashboard_almacen"),

    # TECNICO
    path("tecnico/", tecnico_dashboard, name="tecnico_dashboard"),
    path("tecnico/mis-reqs/", tecnico_mis_reqs, name="tecnico_mis_reqs"),
    path("tecnico/mis-entregas/", tecnico_mis_entregas, name="tecnico_mis_entregas"),
    path("req/<int:req_id>/set-tipo-doc/", views_req.req_set_tipo_doc, name="req_set_tipo_doc"),

    # LIQUIDACIÓN
    path("liquidacion/", liquidacion_dashboard, name="liquidacion_dashboard"),
    path("liquidacion/sede/<int:sede_id>/", liquidar_sede, name="liquidar_sede"),
    path("liquidacion/central/", liquidar_central, name="liquidar_central"),
    path("liquidacion/<int:liquidacion_id>/", liquidacion_detalle, name="liquidacion_detalle"),
    path("liquidacion/lista/", LiquidacionListView.as_view(), name="liquidacion_lista"),
    
    # API Liquidación
    path("api/liquidacion/resumen/", liquidacion_api_resumen, name="liquidacion_api_resumen"),
    path("api/liquidacion/graficos/", liquidacion_api_graficos, name="liquidacion_api_graficos"),
    path("liquidacion/exportar/", liquidacion_exportar_excel, name="liquidacion_exportar_excel"),


    path("api/almacen/reqs/", api_reqs_almacen_list, name="api_reqs_almacen_list"),
    path("api/almacen/reqs/create/", api_reqs_almacen_create, name="api_reqs_almacen_create"),
]