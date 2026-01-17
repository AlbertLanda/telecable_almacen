from django.contrib.auth.views import LoginView
from django.shortcuts import redirect
from django.core.exceptions import PermissionDenied

from inventario.models import UserProfile


class RoleBasedLoginView(LoginView):
    """
    Login que redirige según el rol del usuario.
    """
    template_name = "registration/login.html"
    redirect_authenticated_user = True

    def get_success_url(self):
        user = self.request.user

        # Validación: usuario debe tener perfil
        if not hasattr(user, "profile"):
            raise PermissionDenied("Usuario sin perfil asignado.")

        rol = user.profile.rol

        # Redirecciones por rol
        if rol == UserProfile.Rol.SOLICITANTE:
            return "/dashboard/"

        if rol == UserProfile.Rol.ALMACEN:
            return "/dashboard/"

        if rol in (UserProfile.Rol.JEFA, UserProfile.Rol.ADMIN):
            return "/dashboard/"

        # Fallback seguro
        return "/"
