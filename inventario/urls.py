from django.urls import path
from django.contrib.auth import views as auth_views

# 1. Importaciones de Auth y Dashboard
from .views.auth import RoleBasedLoginView
from .views.dashboard import (
    dashboard_redirect, 
    dash_almacen, 
    dash_admin, 
    inventory_list
)

# 2. Importaciones de API
from .views.api import (
    api_dashboard_almacen, 
    api_reqs_almacen_list, 
    api_reqs_almacen_create
)

# 3. Importación de SCAN
from .views.scan import scan_view

# 4. Importaciones de REQ (Requerimientos)
from .views.req import (
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
    req_set_tipo_requerimiento, 
    req_print, 
    req_home_almacen,
    req_set_tipo_doc,
    req_clonar,
    req_eliminar
)

# 5. Importaciones de SAL (Salidas)
from .views.sal import sal_detail, sal_confirmar, sal_print


urlpatterns = [
    # ==========================
    # AUTENTICACIÓN
    # ==========================
    path("login/", RoleBasedLoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(next_page="/login/"), name="logout"),

    # ==========================
    # HOME & DASHBOARD
    # ==========================
    path("", dashboard_redirect, name="home"),
    path("scan/", scan_view, name="scan"),
    path("dashboard/", dashboard_redirect, name="dashboard"),
    
    # Paneles Específicos
    path("dashboard/almacen/", dash_almacen, name="dash_almacen"),
    path("dashboard/admin/", dash_admin, name="dash_admin"),
    
    # Listado de inventario general
    path("dashboard/inventario/", inventory_list, name="inventory_list"),

    # ==========================
    # API (JSON)
    # ==========================
    path("api/dashboard/almacen/", api_dashboard_almacen, name="api_dashboard_almacen"),
    path("api/almacen/reqs/", api_reqs_almacen_list, name="api_reqs_almacen_list"),
    path("api/almacen/reqs/create/", api_reqs_almacen_create, name="api_reqs_almacen_create"),

    # ==========================
    # REQ (FLUJO TÉCNICO / GENERAL)
    # ==========================
    path("req/", req_home, name="req_home"),
    path("req/add/", req_add_item, name="req_add_item"),
    path("req/scan-add/", req_scan_add, name="req_scan_add"),

    # Carrito y Catálogo
    path("req/catalogo/", req_catalogo, name="req_catalogo"),
    path("req/carrito/", req_carrito, name="req_carrito"),
    path("req/add-producto/", req_add_producto, name="req_add_producto"),
    path("req/set-qty/", req_set_qty, name="req_set_qty"),
    path("req/remove-producto/", req_remove_producto, name="req_remove_producto"),

    # Acciones sobre el REQ
    path("req/set-tipo/", req_set_tipo_requerimiento, name="req_set_tipo_requerimiento"),
    path("req/<int:req_id>/enviar/", req_enviar, name="req_enviar"),
    path("req/<int:req_id>/to-sal/", req_convert_to_sal, name="req_to_sal"),
    path("req/<int:req_id>/print/", req_print, name="req_print"),
    
    # Clonar pedido (Repetir)
    path("req/<int:req_id>/clonar/", req_clonar, name="req_clonar"),

    path("req/<int:req_id>/eliminar/", req_eliminar, name="req_eliminar"),

    # Acción rápida desde dashboard (cambiar tipo documento)
    path("req/<int:req_id>/set-tipo-doc/", req_set_tipo_doc, name="req_set_tipo_doc"),

    # ==========================
    # REQ (FLUJO ALMACÉN)
    # ==========================
    path("dashboard/almacen/req/", req_home_almacen, name="req_home_almacen"),
    path("dashboard/almacen/req/catalogo/", req_catalogo, name="req_catalogo_almacen"),
    path("dashboard/almacen/req/carrito/", req_carrito, name="req_carrito_almacen"),
    path("dashboard/almacen/req/add-producto/", req_add_producto, name="req_add_producto_almacen"),
    path("dashboard/almacen/req/set-qty/", req_set_qty, name="req_set_qty_almacen"),
    path("dashboard/almacen/req/remove-producto/", req_remove_producto, name="req_remove_producto_almacen"),
    path("dashboard/almacen/req/<int:req_id>/enviar/", req_enviar, name="req_enviar_almacen"),
    path("dashboard/almacen/req/set-tipo/", req_set_tipo_requerimiento, name="req_set_tipo_requerimiento_almacen"),
    path("dashboard/almacen/req/<int:req_id>/print/", req_print, name="req_print_almacen"),

    # ==========================
    # SAL (SALIDAS)
    # ==========================
    path("sal/<int:sal_id>/", sal_detail, name="sal_detail"),
    path("sal/<int:sal_id>/confirmar/", sal_confirmar, name="sal_confirmar"),
    path("sal/<int:sal_id>/print/", sal_print, name="sal_print"),
]