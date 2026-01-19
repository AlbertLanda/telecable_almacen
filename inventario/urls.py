from django.urls import path
from django.contrib.auth import views as auth_views
from inventario.views_auth import RoleBasedLoginView

# --- IMPORTS DEL DASHBOARD ---
from .views_dashboard import (
    dashboard_redirect,
    dash_almacen,
    dash_solicitante,
    dash_admin,
    inventory_list,
    update_stock_simple,   # Editar manual
    add_stock_simple,      # Sumar stock (Escáner)
    get_product_by_code,   # Buscar producto API
    create_product_simple  # <--- NUEVO: Crear producto (FALTABA ESTE)
)

from .views_api import api_dashboard_almacen
from .views import scan_view

# Imports de Requerimientos
from .views_req import (
    req_home, req_add_item, req_scan_add, req_enviar, req_convert_to_sal,
    req_catalogo, req_add_producto, req_carrito,
    req_set_qty, req_remove_producto
)

from .views_sal import sal_detail, sal_confirmar
from .views_tecnico import tecnico_dashboard, tecnico_mis_reqs, tecnico_mis_entregas


urlpatterns = [
    # AUTH
    path("login/", RoleBasedLoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(next_page="/login/"), name="logout"),

    # HOME
    path("", dashboard_redirect, name="home"),
    path("scan/", scan_view, name="scan"), # Escáner antiguo

    # REQ
    path("req/", req_home, name="req_home"),
    path("req/add/", req_add_item, name="req_add_item"),
    path("req/scan-add/", req_scan_add, name="req_scan_add"),
    path("req/<int:req_id>/enviar/", req_enviar, name="req_enviar"),
    path("req/set-qty/", req_set_qty, name="req_set_qty"),
    path("req/remove-producto/", req_remove_producto, name="req_remove_producto"),
    path("req/catalogo/", req_catalogo, name="req_catalogo"),
    path("req/add-producto/", req_add_producto, name="req_add_producto"),
    path("req/carrito/", req_carrito, name="req_carrito"),
    path("req/<int:req_id>/to-sal/", req_convert_to_sal, name="req_to_sal"),

    # SAL
    path("sal/<int:sal_id>/", sal_detail, name="sal_detail"),
    path("sal/<int:sal_id>/confirmar/", sal_confirmar, name="sal_confirmar"),

    # DASHBOARD
    path("dashboard/", dashboard_redirect, name="dashboard"),
    path("dashboard/almacen/", dash_almacen, name="dash_almacen"),
    path("dashboard/solicitante/", dash_solicitante, name="dash_solicitante"),
    path("dashboard/admin/", dash_admin, name="dash_admin"),

    # --- INVENTARIO & ACCIONES ---
    path("dashboard/inventario/", inventory_list, name="inventory_list"),
    
    # Acciones de Stock
    path('stock/update/simple/', update_stock_simple, name='update_stock_simple'),
    path('stock/add/simple/', add_stock_simple, name='add_stock_simple'),
    
    # <--- RUTA NUEVA PARA CREAR PRODUCTOS DESDE EL ESCÁNER --->
    path('product/create/simple/', create_product_simple, name='create_product_simple'),
    
    # API Interna
    path('api/product/lookup/', get_product_by_code, name='get_product_by_code'),
    
    # API Generales
    path("api/dashboard/almacen/", api_dashboard_almacen, name="api_dashboard_almacen"),

    # TECNICO
    path("tecnico/", tecnico_dashboard, name="tecnico_dashboard"),
    path("tecnico/mis-reqs/", tecnico_mis_reqs, name="tecnico_mis_reqs"),
    path("tecnico/mis-entregas/", tecnico_mis_entregas, name="tecnico_mis_entregas"),
]