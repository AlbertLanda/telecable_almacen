from django.urls import path
from . import views

urlpatterns = [
    # ==========================================
    # 🌍 VISTAS GENERALES (Admin / Consulta)
    # ==========================================
    path('', views.proyecto_list, name='proyecto_list'),
    path('<int:pk>/', views.proyecto_detail, name='proyecto_detail'),

    # ==========================================
    # 🎨 FLUJO DEL DISEÑADOR (Creación y Planificación)
    # ==========================================
    
    # 1. Panel Principal del Diseñador
    path('dashboard/', views.disenador_dashboard, name='disenador_dashboard'),
      
    # 2. Paso 1: Crear la "Carpeta" del Proyecto (Datos + Plano)
    path('nuevo/', views.proyecto_create, name='proyecto_create'),

    # 3. Paso 2: Gestionar la "Receta" de Materiales (Agregar/Listar)
    path('<int:proyecto_id>/materiales/', views.proyecto_materiales, name='proyecto_materiales'),

    # Acción: Eliminar un material de la lista de planificación
    path('material/eliminar/<int:item_id>/', views.eliminar_material_proyecto, name='eliminar_material_proyecto'),

    path('material/editar/<int:item_id>/', views.editar_cantidad_material, name='editar_cantidad_material'),

    path('almacen/lista/', views.almacen_proyectos_list, name='almacen_proyectos_list'),

    path('almacen/despacho/<int:proyecto_id>/', views.almacen_proyecto_detalle, name='almacen_proyecto_detalle'),

    path('almacen/generar-salida/<int:proyecto_id>/', views.almacen_generar_salida, name='almacen_generar_salida'),

    path('proyecto/eliminar/<int:pk>/', views.eliminar_proyecto, name='eliminar_proyecto'),

    path('proyecto/pdf/<int:proyecto_id>/', views.proyecto_pdf_salida, name='proyecto_pdf_salida'),

    path('almacen/liquidacion/lista/', views.almacen_liquidacion_lista, name='almacen_liquidacion_lista'),

    path('almacen/liquidar/<int:proyecto_id>/', views.almacen_liquidar_proyecto, name='almacen_liquidar_proyecto'),

    path('proyecto/pdf-cierre/<int:proyecto_id>/', views.proyecto_pdf_liquidacion, name='proyecto_pdf_liquidacion'),

    path('almacen/historial/obras/', views.almacen_historial_obras, name='almacen_historial_obras'),

    path('admin/reportes/obras/', views.admin_reporte_lista, name='admin_reporte_lista'),
    
    path('admin/reportes/detalle/<int:proyecto_id>/', views.admin_detalle_financiero, name='admin_detalle_financiero'),
]