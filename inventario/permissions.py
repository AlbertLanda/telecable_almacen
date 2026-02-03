# inventario/permissions.py
from functools import wraps
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied

def get_profile(user):
    return getattr(user, "userprofile", None) or getattr(user, "profile", None)

def role_required(*roles, allow_superuser=True):
    def decorator(view_func):
        @login_required
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            user = request.user

            if allow_superuser and getattr(user, "is_superuser", False):
                return view_func(request, *args, **kwargs)

            profile = get_profile(user)
            if not profile:
                raise PermissionDenied("Tu usuario no tiene perfil asignado.")

            if profile.rol not in roles:
                raise PermissionDenied("No tienes permisos para esta acción.")

            request.user_profile = profile
            return view_func(request, *args, **kwargs)
        return _wrapped
    return decorator

def sede_required(view_func):
    @login_required
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        profile = get_profile(request.user)
        if not profile:
            raise PermissionDenied("Tu usuario no tiene perfil asignado.")

        if getattr(profile, "sede_activa", None) is None:
            raise PermissionDenied("No tienes sede activa definida.")

        request.user_profile = profile
        return view_func(request, *args, **kwargs)
    return _wrapped
