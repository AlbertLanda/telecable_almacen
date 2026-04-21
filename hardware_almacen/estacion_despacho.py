import cv2
import face_recognition
import os
import numpy as np
import datetime
import threading
import requests  # <--- IMPORTANTE: La librería para hablar con Django
from dotenv import load_dotenv, find_dotenv
from PIL import Image

print("Iniciando Sistema de Auto-Despacho TeleCable v1.0...")

# ==========================================
# --- 1. SEGURIDAD Y CONFIGURACIÓN ---
# ==========================================
load_dotenv(find_dotenv(), override=True)  
CAMERA_URL = os.getenv("CAMERA_URL")

if not CAMERA_URL:
    print("❌ ERROR: No se encontró CAMERA_URL en el archivo .env")
    exit()

# ==========================================
# --- 2. ENTRENAMIENTO DE ROSTROS ---
# ==========================================
print("Cargando base de datos de rostros de técnicos...")
rostros_conocidos_encodings = []
nombres_conocidos = []

directorio_actual = os.path.dirname(os.path.abspath(__file__))
carpeta_rostros = os.path.join(directorio_actual, "rostros")

if not os.path.exists(carpeta_rostros):
    os.makedirs(carpeta_rostros)
    print(f"⚠️ Se creó la carpeta '{carpeta_rostros}'. Mete algunas fotos ahí y reinicia.")

for archivo in os.listdir(carpeta_rostros):
    if archivo.lower().endswith((".jpg", ".jpeg", ".png")):
        ruta_imagen = os.path.join(carpeta_rostros, archivo)
        try:
            img_pil = Image.open(ruta_imagen).convert('RGB')
            imagen = np.array(img_pil, dtype=np.uint8)
            imagen = np.ascontiguousarray(imagen)
            
            encodings = face_recognition.face_encodings(imagen)
            
            if len(encodings) > 0:
                rostros_conocidos_encodings.append(encodings[0])
                nombre = os.path.splitext(archivo)[0].replace("_", " ").upper()
                nombres_conocidos.append(nombre)
                print(f"   -> ✅ Técnico cargado: {nombre}")
            else:
                print(f"   -> ⚠️ No se detectó rostro en: {archivo}")
        except Exception as e:
            print(f"   -> ❌ Error con {archivo}: {e}")

print(f"✅ ¡Sistema listo con {len(nombres_conocidos)} técnicos!")

# ==========================================
# --- 3. CONEXIÓN AL VIDEO (MOTOR TURBO) ---
# ==========================================
class CamaraTiempoReal:
    def __init__(self, url):
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
        self.cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        
        if not self.cap.isOpened():
            print("❌ Error crítico: Sin señal de la cámara Tapo.")
            exit()
            
        self.ret, self.frame = self.cap.read()
        self.corriendo = True
        self.hilo = threading.Thread(target=self.actualizar, daemon=True)
        self.hilo.start()

    def actualizar(self):
        while self.corriendo:
            try:
                ret, frame = self.cap.read()
                if ret:
                    self.frame = frame
            except Exception as e:
                pass

    def read(self):
        return True, self.frame.copy()

    def release(self):
        self.corriendo = False
        self.cap.release()

cap = CamaraTiempoReal(CAMERA_URL)

# ==========================================
# --- 4. IA ASÍNCRONA (PROCESO DE FONDO) ---
# ==========================================
ubicaciones_rostros = []
nombres_detectados = []
ia_ocupada = False

def procesar_ia_en_fondo(frame_para_ia):
    global ubicaciones_rostros, nombres_detectados, ia_ocupada
    
    frame_pequeno = cv2.resize(frame_para_ia, (0, 0), fx=0.25, fy=0.25)
    rgb_frame_pequeno = cv2.cvtColor(frame_pequeno, cv2.COLOR_BGR2RGB)
    rgb_frame_pequeno = np.ascontiguousarray(rgb_frame_pequeno, dtype=np.uint8)

    locs = face_recognition.face_locations(rgb_frame_pequeno)
    encs = face_recognition.face_encodings(rgb_frame_pequeno, locs)

    nombres = []
    for face_encoding in encs:
        coincidencias = face_recognition.compare_faces(rostros_conocidos_encodings, face_encoding, tolerance=0.5)
        nombre = "DESCONOCIDO"

        if True in coincidencias:
            distancias = face_recognition.face_distance(rostros_conocidos_encodings, face_encoding)
            mejor_indice = np.argmin(distancias)
            if coincidencias[mejor_indice]:
                nombre = nombres_conocidos[mejor_indice]

        nombres.append(nombre)

    ubicaciones_rostros = locs
    nombres_detectados = nombres
    ia_ocupada = False

# ==========================================
# --- 5. BUCLE VISUAL Y LÓGICA DE CARRITO ---
# ==========================================
codigo_escaneado = ""
carrito = []  # <--- EL CARRITO DE COMPRAS

while True:
    ret, frame_crudo = cap.read()
    if not ret: 
        break

    if not ia_ocupada:
        ia_ocupada = True
        threading.Thread(target=procesar_ia_en_fondo, args=(frame_crudo.copy(),), daemon=True).start()

    tecnico_actual = nombres_detectados[0] if len(nombres_detectados) > 0 else "DESCONOCIDO"

    # --- 6. DIBUJAR LA INTERFAZ ---
    for (top, right, bottom, left), nombre in zip(ubicaciones_rostros, nombres_detectados):
        top *= 4; right *= 4; bottom *= 4; left *= 4
        color = (0, 255, 0) if nombre != "DESCONOCIDO" else (0, 0, 255)

        cv2.rectangle(frame_crudo, (left, top), (right, bottom), color, 2)
        cv2.rectangle(frame_crudo, (left, bottom - 35), (right, bottom), color, cv2.FILLED)
        cv2.putText(frame_crudo, nombre, (left + 6, bottom - 6), cv2.FONT_HERSHEY_DUPLEX, 0.6, (255, 255, 255), 1)

    # UI DEL CARRITO
    if tecnico_actual != "DESCONOCIDO":
        cv2.putText(frame_crudo, f"SESION: {tecnico_actual}", (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
        cv2.putText(frame_crudo, f"CARRITO: {len(carrito)} equipos", (20, 80), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
        cv2.putText(frame_crudo, "[C] Confirmar  |  [X] Vaciar  |  [Q] Salir", (20, 110), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 1)
    else:
        cv2.putText(frame_crudo, "BLOQUEADO - ACERQUE SU ROSTRO", (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)

    hora = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    cv2.putText(frame_crudo, f"TeleCable - Almacen | {hora}", (20, frame_crudo.shape[0] - 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)

    cv2.imshow('Estacion de Auto-Despacho', frame_crudo)

    # ==========================================
    # --- 7. ESCUCHADOR DEL LECTOR Y TECLADO ---
    # ==========================================
    key = cv2.waitKey(1) & 0xFF
    
    if key == ord('q'):
        break
        
    # --- LA LETRA 'C' DISPARA LA API A DJANGO ---
    elif key == ord('c') or key == ord('C'):
        if len(carrito) > 0 and tecnico_actual != "DESCONOCIDO":
            print("\n" + "🟢"*20)
            print("🚀 Enviando paquete de despacho a la nube de TeleCable...")
            
            # ADVERTENCIA: Asegúrate de que Django esté corriendo en este puerto y tu IP sea correcta
            url_django = "http://127.0.0.1:8000/api/almacen/auto-despacho/" 
            
            # Convertimos "ALBERT LANDA" a "albertlanda" para que coincida con el username de Django
            username_limpio = tecnico_actual.lower().replace(" ", "")
            
            payload = {
                "username": username_limpio, 
                "carrito": carrito,
                "sede_id": 1  # ID de tu Sede Principal (Ej: 1)
            }
            
            try:
                respuesta = requests.post(url_django, json=payload)
                data = respuesta.json()
                
                if data.get("ok"):
                    print(f"✅ ¡ÉXITO! {data.get('mensaje')}")
                    for p in data.get("productos", []):
                        print(f"  -> {p}")
                else:
                    print(f"❌ Error del servidor: {data.get('error')}")
            except Exception as e:
                print(f"❌ Error de conexión: {e}")
                print("⚠️ Asegúrate de que el servidor Django esté corriendo (python manage.py runserver).")
            
            print("🟢"*20 + "\n")
            carrito = [] # Vaciamos el carrito tras procesar
        else:
            print("⚠️ Error: Carrito vacío o técnico no reconocido.")
            
    # --- LA LETRA 'X' VACÍA EL CARRITO ---
    elif key == ord('x') or key == ord('X'):
        print("🗑️ Carrito vaciado por el usuario.")
        carrito = []
        codigo_escaneado = ""
        
    # --- EL LECTOR DE BARRAS LLENA EL CARRITO ---
    elif key != 255:
        if key == 13: # El Enter de la pistola
            if codigo_escaneado != "":
                if tecnico_actual != "DESCONOCIDO":
                    carrito.append(codigo_escaneado)
                    print(f"➕ Agregado: {codigo_escaneado} | Llevas: {len(carrito)} items")
                else:
                    print("❌ Escaneo rechazado: Técnico no reconocido.")
                codigo_escaneado = ""
        else:
            try:
                codigo_escaneado += chr(key)
            except ValueError:
                pass

# Limpieza
cap.release()
cv2.destroyAllWindows()