from django.http import HttpResponse

def render_to_pdf(template_src, context_dict={}):
    return HttpResponse("PDF Desactivado por ahora")