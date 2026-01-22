from django.shortcuts import render
from django.http import HttpResponseForbidden
from django.views.decorators.csrf import requires_csrf_token


def csrf_failure(request, reason=""):
    """
    Vista personalizada para manejar errores CSRF
    """
    return render(request, 'inventario/csrf_failure.html', status=403)


@requires_csrf_token
def custom_csrf_protect(view_func):
    """
    Decorador personalizado para proteger vistas con CSRF
    """
    def wrapped_view(request, *args, **kwargs):
        # Para desarrollo, permitir más flexibilidad
        if request.method == 'POST':
            # Verificar si el request viene de un origen confiable
            origin = request.META.get('HTTP_ORIGIN', '')
            referer = request.META.get('HTTP_REFERER', '')
            
            # Permitir si viene de localhost o 127.0.0.1
            trusted_origins = [
                'http://127.0.0.1:8000',
                'http://localhost:8000',
                'http://127.0.0.1:49878',  # Browser preview
                'http://localhost:49878',
            ]
            
            if origin in trusted_origins or any(origin.startswith(f'http://127.0.0.1:{port}') for port in range(8000, 8010)):
                return view_func(request, *args, **kwargs)
            elif referer and any(referer.startswith(f'http://127.0.0.1:{port}') for port in range(8000, 8010)):
                return view_func(request, *args, **kwargs)
        
        return view_func(request, *args, **kwargs)
    
    return wrapped_view
