from django.urls import path
from django.contrib.auth import views as auth_views
from inventario.views_auth import RoleBasedLoginView

# Vistas del Dashboard
from .views_dashboard import (
    dashboard_redirect, dash_almacen, dash_solicitante, dash_admin, inventory_list,
    create_product_simple, add_stock_simple, update_stock_simple, get_product_by_code,
)
from .views_api import api_dashboard_almacen
from .views import scan_view

# Vistas de Usuarios (SIN usuario_create)
from .views_users import usuarios_list, usuario_edit, usuario_delete

# Vistas de Requerimientos
from .views_req import (
    req_home, req_add_item, req_scan_add, req_enviar, req_convert_to_sal,
    req_catalogo, req_add_producto, req_carrito, req_set_qty, req_remove_producto,
)
from . import views_req
from .views_sal import sal_detail, sal_confirmar, sal_print
from .views_tecnico import tecnico_dashboard, tecnico_mis_reqs, tecnico_mis_entregas

urlpatterns = [
    # AUTH
    path("login/", RoleBasedLoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(next_page="/login/"), name="logout"),

    # HOME
    path("", dashboard_redirect, name="home"),
    
    # SCAN & STOCK
    path("scan/", scan_view, name="scan"),
    path("product/create/simple/", create_product_simple, name="create_product_simple"),
    path("stock/add/simple/", add_stock_simple, name="add_stock_simple"),
    path("stock/update/simple/", update_stock_simple, name="update_stock_simple"),
    path("api/get-product-by-code/", get_product_by_code, name="get_product_by_code"),

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
    path("req/set-tipo/", views_req.req_set_tipo_requerimiento, name="req_set_tipo_requerimiento"),
    path("req/<int:req_id>/print/", views_req.req_print, name="req_print"),
    path("req/<int:req_id>/to-sal/", req_convert_to_sal, name="req_to_sal"),
    path("req/<int:req_id>/set-tipo-doc/", views_req.req_set_tipo_doc, name="req_set_tipo_doc"),

    # SAL
    path("sal/<int:sal_id>/", sal_detail, name="sal_detail"),
    path("sal/<int:sal_id>/confirmar/", sal_confirmar, name="sal_confirmar"),
    path("sal/<int:sal_id>/print/", sal_print, name="sal_print"),

    # DASHBOARD
    path("dashboard/", dashboard_redirect, name="dashboard"),
    path("dashboard/almacen/", dash_almacen, name="dash_almacen"),
    path("dashboard/solicitante/", dash_solicitante, name="dash_solicitante"),
    path("dashboard/admin/", dash_admin, name="dash_admin"),
    path("dashboard/inventario/", inventory_list, name="inventory_list"),

    # API
    path("api/dashboard/almacen/", api_dashboard_almacen, name="api_dashboard_almacen"),

    # TECNICO
    path("tecnico/", tecnico_dashboard, name="tecnico_dashboard"),
    path("tecnico/mis-reqs/", tecnico_mis_reqs, name="tecnico_mis_reqs"),
    path("tecnico/mis-entregas/", tecnico_mis_entregas, name="tecnico_mis_entregas"),

    # GESTIÓN DE USUARIOS (SIMPLIFICADA)
    path('usuarios/', usuarios_list, name='usuarios_list'),
    path('usuarios/editar/<int:user_id>/', usuario_edit, name='usuario_edit'),
    path('usuarios/eliminar/<int:user_id>/', usuario_delete, name='usuario_delete'),
]