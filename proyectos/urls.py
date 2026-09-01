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

    # Acción: corregir la clasificación Proyecto <-> Avería
    path('<int:proyecto_id>/cambiar-tipo/', views.proyecto_cambiar_tipo, name='proyecto_cambiar_tipo'),

    # Acción: subir o reemplazar el plano PDF de un proyecto ya creado
    path('<int:proyecto_id>/plano/', views.proyecto_reemplazar_plano, name='proyecto_reemplazar_plano'),

    # 3. Paso 2: Gestionar la "Receta" de Materiales (Agregar/Listar)
    path('<int:proyecto_id>/materiales/', views.proyecto_materiales, name='proyecto_materiales'),

    # Acción: Eliminar un material de la lista de planificación
    path('material/eliminar/<int:item_id>/', views.eliminar_material_proyecto, name='eliminar_material_proyecto'),

    path('material/editar/<int:item_id>/', views.editar_cantidad_material, name='editar_cantidad_material'),

    # Materiales pendientes de catálogo (no existen aún como Producto)
    path(
        'material-pendiente/<int:pendiente_id>/vincular/',
        views.proyecto_material_pendiente_vincular,
        name='proyecto_material_pendiente_vincular',
    ),
    path(
        'material-pendiente/<int:pendiente_id>/eliminar/',
        views.proyecto_material_pendiente_eliminar,
        name='proyecto_material_pendiente_eliminar',
    ),

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

    # FLUJO DE APROBACIÓN
    path('proyecto/<int:proyecto_id>/enviar-revision/', views.proyecto_enviar_a_revision, name='proyecto_enviar_revision'),
    path('proyecto/<int:proyecto_id>/aprobar/', views.proyecto_aprobar_tecnico, name='proyecto_aprobar_tecnico'),
    path('proyecto/<int:proyecto_id>/observar/', views.proyecto_observar_tecnico, name='proyecto_observar_tecnico'),
    path(
        "<int:proyecto_id>/asignar-cuadrilla/",
        views.proyecto_asignar_cuadrilla,
        name="proyecto_asignar_cuadrilla",
    ),

    path(
        "ajax/buscar-equipo-proyecto/",
        views.ajax_buscar_equipo_proyecto,
        name="ajax_buscar_equipo_proyecto"
    ),
]