// Opcional: seleccionar todo el input al enfocar
const input = document.getElementById("scanInput");
if (input) {
  input.addEventListener("focus", () => input.select());
}
