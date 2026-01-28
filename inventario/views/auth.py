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
        if not hasattr(user, "profile"):
            # Si es superusuario sin perfil, que vaya al admin de Django
            if user.is_superuser:
                return "/admin/"
            raise PermissionDenied("Usuario sin perfil asignado.")

        rol = user.profile.rol

        # 1. Técnico
        if rol == UserProfile.Rol.SOLICITANTE:
            return "/operaciones/tecnico/"
        
        # 2. Almacén
        if rol == UserProfile.Rol.ALMACEN:
            return "/dashboard/almacen/"

        # 3. ✅ NUEVO: Diseñador / Planificador
        if rol == UserProfile.Rol.DISENADOR:
            return "/proyectos/dashboard/"

        # 4. Por defecto (Admin / Jefa)
        return "/dashboard/admin/"