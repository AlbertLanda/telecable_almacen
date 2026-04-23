// 1. Función para obtener el valor de la cookie CSRF (estándar de Django)
function getCookie(name) {
    let cookieValue = null;
    if (document.cookie && document.cookie !== '') {
        const cookies = document.cookie.split(';');
        for (let i = 0; i < cookies.length; i++) {
            const cookie = cookies[i].trim();
            if (cookie.substring(0, name.length + 1) === (name + '=')) {
                cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
                break;
            }
        }
    }
    return cookieValue;
}

const input = document.getElementById("scanInput");
if (input) {
    input.addEventListener("focus", () => input.select());

    // 2. Escuchar cuando se escanea (Enter)
    input.addEventListener("keypress", async (e) => {
        if (e.key === "Enter") {
            const code = input.value.trim();
            const tecnicoId = document.getElementById("tecnicoSelect")?.value;

            if (!code || !tecnicoId) return;

            const fd = new FormData();
            fd.append("codigo_escaneado", code);
            fd.append("tecnico_id", tecnicoId);

            try {
                // 3. LA SOLUCIÓN: Enviar el X-CSRFToken en los headers
                const res = await fetch("/req/scan-directo/", { 
                    method: 'POST', 
                    body: fd, 
                    headers: {
                        'X-Requested-With': 'XMLHttpRequest',
                        'X-CSRFToken': getCookie('csrftoken') // <--- CLAVE PARA LA NUBE
                    } 
                });

                const data = await res.json();
                
                if (data.ok) {
                    showToast(`✅ ${data.mensaje}`); 
                    if(typeof renderCart === 'function') renderCart(data.items); 
                    input.value = ""; // Limpiar para el siguiente escaneo
                } else {
                    showToast(`❌ ${data.error}`, 'error'); 
                }
            } catch (err) { 
                console.error(err);
                showToast("Error de conexión al escanear", 'error'); 
            }
        }
    });
}