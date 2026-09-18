from django.urls import path

from . import views

urlpatterns = [
    path("", views.reportes_home, name="reportes_home"),
    path("sicv/", views.reportes_sicv, name="reportes_sicv"),
    path("kardex/", views.reportes_kardex, name="reportes_kardex"),
    # El formato va en la ruta y se valida contra FORMATOS en la vista.
    path(
        "kardex/exportar/<str:formato>/",
        views.reportes_registro_export,
        name="reportes_registro_export",
    ),
]
