from django.urls import path
from django.contrib.auth import views as auth_views

# 1. Importaciones de Auth y Dashboard
from .views.auth import RoleBasedLoginView
from .views.dashboard import (
    dashboard_redirect,
    dash_almacen,
    dash_admin,
    inventory_list,
    almacen_historial_global,
    api_alertas_stock_voz,  # ✅ NUEVO: API para alertas de voz de stock crítico
)

# 2. Importaciones de API
from .views.api import (
    api_dashboard_almacen,
    api_reqs_almacen_list,
    api_reqs_almacen_create,
    api_autodespacho_registrar,
    api_camara_ping,
    api_camara_status,
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
    req_eliminar,
    req_recepcionar_compra,
    req_set_tipo,
    req_atender,
    req_asignacion_directa,
    req_scan_directo,
    almacen_devolucion_rapida,
    devolucion_print,
    req_anular_solicitud,
)

# 5. Importaciones de SAL (Salidas)
from .views.sal import sal_detail, sal_confirmar, sal_print, almacen_recepcionar_traspaso

# 6. Importaciones de CARGA INICIAL
from .views.carga_inicial import (
    carga_inicial_home,
    carga_inicial_registrar_existente,
    carga_inicial_registrar_nuevo,
    producto_etiqueta_print,
    carga_inicial_resumen,
    carga_inicial_cerrar,
    carga_inicial_seriales,
    carga_inicial_serial_registrar,
    carga_inicial_serial_editar,
)

from inventario.views.equipos import (
    equipos_serializados_list,
    equipos_serializados_export,
)

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

    # Paneles específicos
    path("dashboard/almacen/", dash_almacen, name="dash_almacen"),
    path("dashboard/almacen/historial/", almacen_historial_global, name="almacen_historial_global"),
    path("dashboard/admin/", dash_admin, name="dash_admin"),

    # ✅ NUEVO: API para que el panel de la jefa/admin hable cuando haya stock crítico
    path(
        "dashboard/admin/alertas-stock-voz/",
        api_alertas_stock_voz,
        name="api_alertas_stock_voz",
    ),

    # Listado de inventario general
    path("dashboard/inventario/", inventory_list, name="inventory_list"),

    # ==========================
    # API (JSON)
    # ==========================
    path("api/dashboard/almacen/", api_dashboard_almacen, name="api_dashboard_almacen"),
    path("api/almacen/reqs/", api_reqs_almacen_list, name="api_reqs_almacen_list"),
    path("api/almacen/reqs/create/", api_reqs_almacen_create, name="api_reqs_almacen_create"),
    path("api/autodespacho/", api_autodespacho_registrar, name="api_autodespacho_registrar"),

    # Puente de la cámara
    path("api/camara/ping/", api_camara_ping, name="api_camara_ping"),
    path("api/camara/status/", api_camara_status, name="api_camara_status"),

    # ==========================
    # ALMACÉN / ATENCIÓN
    # ==========================
    path("almacen/recepcionar/<int:sal_id>/", almacen_recepcionar_traspaso, name="almacen_recepcionar_traspaso"),
    path("almacen/asignar-directo/", req_asignacion_directa, name="req_asignacion_directa"),
    path("req/<int:req_id>/atender/", req_atender, name="req_atender"),
    

    # ==========================
    # REQ (FLUJO TÉCNICO / GENERAL)
    # ==========================
    path("req/", req_home, name="req_home"),
    path("req/add/", req_add_item, name="req_add_item"),
    path("req/scan-add/", req_scan_add, name="req_scan_add"),
    path("req/scan-directo/", req_scan_directo, name="req_scan_directo"),

    # Carrito y catálogo
    path("req/catalogo/", req_catalogo, name="req_catalogo"),
    path("req/carrito/", req_carrito, name="req_carrito"),
    path("req/add-producto/", req_add_producto, name="req_add_producto"),
    path("req/set-qty/", req_set_qty, name="req_set_qty"),
    path("req/remove-producto/", req_remove_producto, name="req_remove_producto"),

    # Acciones sobre el REQ
    path("req/set-tipo/", req_set_tipo, name="req_set_tipo"),
    path("req/<int:req_id>/enviar/", req_enviar, name="req_enviar"),
    path("req/<int:req_id>/to-sal/", req_convert_to_sal, name="req_to_sal"),
    path("req/<int:req_id>/print/", req_print, name="req_print"),
    path("req/<int:req_id>/recepcionar-compra/", req_recepcionar_compra, name="req_recepcionar_compra"),
    path("req/<int:req_id>/clonar/", req_clonar, name="req_clonar"),
    path("req/<int:req_id>/eliminar/", req_eliminar, name="req_eliminar"),
    path("req/<int:req_id>/set-tipo-doc/", req_set_tipo_doc, name="req_set_tipo_doc"),
    path(
        "req/<int:req_id>/anular/",
        req_anular_solicitud,
        name="req_anular_solicitud",
    ),

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
    path("dashboard/almacen/devolucion/", almacen_devolucion_rapida, name="almacen_devolucion_rapida"),
    path("devolucion/<int:doc_id>/print/", devolucion_print, name="devolucion_print"),

    # ==========================
    # SAL (SALIDAS)
    # ==========================
    path("sal/<int:sal_id>/", sal_detail, name="sal_detail"),
    path("sal/<int:sal_id>/confirmar/", sal_confirmar, name="sal_confirmar"),
    path("sal/<int:sal_id>/print/", sal_print, name="sal_print"),

    # ==========================
    # CARGA INICIAL
    # ==========================
    path(
        "dashboard/almacen/carga-inicial/",
        carga_inicial_home,
        name="carga_inicial_home",
    ),
    path(
        "dashboard/almacen/carga-inicial/registrar-existente/",
        carga_inicial_registrar_existente,
        name="carga_inicial_registrar_existente",
    ),
    path(
        "dashboard/almacen/carga-inicial/registrar-nuevo/",
        carga_inicial_registrar_nuevo,
        name="carga_inicial_registrar_nuevo",
    ),
    path(
        "dashboard/almacen/producto/<int:producto_id>/etiqueta/",
        producto_etiqueta_print,
        name="producto_etiqueta_print",
    ),
    path(
        "dashboard/almacen/carga-inicial/resumen/",
        carga_inicial_resumen,
        name="carga_inicial_resumen",
    ),
    path(
        "dashboard/almacen/carga-inicial/cerrar/",
        carga_inicial_cerrar,
        name="carga_inicial_cerrar",
    ),
    path(
        "dashboard/almacen/carga-inicial/producto/<int:producto_id>/seriales/",
        carga_inicial_seriales,
        name="carga_inicial_seriales",
    ),
    path(
        "dashboard/almacen/carga-inicial/producto/<int:producto_id>/seriales/registrar/",
        carga_inicial_serial_registrar,
        name="carga_inicial_serial_registrar",
    ),
    path(
        "dashboard/almacen/carga-inicial/serial/<int:item_id>/editar/",
        carga_inicial_serial_editar,
        name="carga_inicial_serial_editar",
    ),

    # Filtrado de ONU
    path("equipos-serializados/", equipos_serializados_list, name="equipos_serializados_list"),
    path("equipos-serializados/exportar/", equipos_serializados_export, name="equipos_serializados_export"),
]