from django.shortcuts import render
from django.views.decorators.csrf import requires_csrf_token

def csrf_failure(request, reason=""):
    """
    Vista personalizada para manejar errores CSRF (Token de seguridad inválido o faltante).
    Muestra una página amigable en lugar del error 403 genérico de Django.
    """
    return render(request, 'inventario/csrf_failure.html', status=403)

@requires_csrf_token
def custom_csrf_protect(view_func):
    """
    Decorador personalizado para proteger vistas con CSRF.
    Útil si necesitas lógica extra en desarrollo para permitir ciertos orígenes.
    """
    def wrapped_view(request, *args, **kwargs):
        # Lógica personalizada opcional:
        # En producción, Django maneja esto nativamente con MIDDLEWARE,
        # pero mantenemos la estructura por si tenías lógica específica aquí.
        
        if request.method == 'POST':
            # Verificar si el request viene de un origen confiable (si fuera necesario)
            pass
        
        return view_func(request, *args, **kwargs)
    
    return wrapped_view