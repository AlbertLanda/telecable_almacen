/*
 * Captura de firma manuscrita con dedo, lápiz o mouse (Pointer Events).
 * Adaptado de Telecable RRHH (static/js/firma-manuscrita.js).
 *
 * Espera en la página:
 *   <form id="firmaForm"> con <canvas id="firmaLienzo" width="1000" height="360">,
 *   <input type="hidden" name="trazo">, <input type="checkbox" name="acepto">,
 *   botones #firmaBorrar y #firmaGuardar.
 */
(function () {
    'use strict';
    const form = document.getElementById('firmaForm');
    if (!form) return;

    const lienzo = document.getElementById('firmaLienzo');
    const borrar = document.getElementById('firmaBorrar');
    const guardar = document.getElementById('firmaGuardar');
    const acepto = form.querySelector('input[name="acepto"]');
    const trazo = form.querySelector('input[name="trazo"]');
    const ANCHO = lienzo.width, ALTO = lienzo.height;

    const ctx = lienzo.getContext('2d');
    ctx.strokeStyle = '#142033';
    ctx.lineWidth = 3.5;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';

    // pointer: qué dedo/lápiz está dibujando; ultimo: último punto dibujado.
    // puntos y distancia sirven para no aceptar un simple toque como firma.
    let pointer = null, ultimo = null, puntos = 0, distancia = 0, enviando = false;

    function actualizar() {
        guardar.disabled = enviando || !acepto.checked || puntos < 5 || distancia < 30;
    }

    // Convierte la posición en pantalla a coordenadas del lienzo (que puede verse más chico).
    function posicion(e) {
        const r = lienzo.getBoundingClientRect();
        return {
            x: Math.max(0, Math.min(ANCHO, (e.clientX - r.left) * ANCHO / r.width)),
            y: Math.max(0, Math.min(ALTO, (e.clientY - r.top) * ALTO / r.height)),
        };
    }

    lienzo.addEventListener('pointerdown', e => {
        if (enviando || pointer !== null || (e.pointerType === 'mouse' && e.button !== 0)) return;
        e.preventDefault();
        pointer = e.pointerId;
        ultimo = posicion(e);
        lienzo.setPointerCapture(pointer);
    });

    lienzo.addEventListener('pointermove', e => {
        if (e.pointerId !== pointer || !ultimo) return;
        e.preventDefault();
        const p = posicion(e);
        const tramo = Math.hypot(p.x - ultimo.x, p.y - ultimo.y);
        if (tramo < 0.5) return;
        ctx.beginPath();
        ctx.moveTo(ultimo.x, ultimo.y);
        ctx.lineTo(p.x, p.y);
        ctx.stroke();
        distancia += tramo;
        puntos++;
        ultimo = p;
        actualizar();
    });

    function terminar(e) {
        if (e.pointerId !== pointer) return;
        pointer = null;
        ultimo = null;
        actualizar();
    }
    lienzo.addEventListener('pointerup', terminar);
    lienzo.addEventListener('pointercancel', terminar);
    lienzo.addEventListener('lostpointercapture', terminar);

    borrar.addEventListener('click', () => {
        if (enviando) return;
        ctx.clearRect(0, 0, ANCHO, ALTO);
        puntos = 0; distancia = 0; pointer = null; ultimo = null;
        actualizar();
    });

    acepto.addEventListener('change', actualizar);

    // Al enviar, el dibujo se convierte en texto PNG (base64) y viaja en el campo oculto.
    form.addEventListener('submit', e => {
        if (guardar.disabled || enviando) { e.preventDefault(); return; }
        trazo.value = lienzo.toDataURL('image/png');
        enviando = true;
        guardar.textContent = 'Guardando firma…';
        actualizar();
    });

    actualizar();
})();
