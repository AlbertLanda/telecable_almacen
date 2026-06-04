import cv2
import face_recognition
import os
import numpy as np
import datetime
import threading
import requests
import time # <--- NUEVO: Para controlar los 2 segundos del Ping
from dotenv import load_dotenv, find_dotenv
from PIL import Image

print("Iniciando Vigía Inteligente TeleCable v2.0...")

# ==========================================
# --- 1. SEGURIDAD Y CONFIGURACIÓN ---
# ==========================================
load_dotenv(find_dotenv(), override=True)  

CAMERA_URL = os.getenv("CAMERA_URL")
SEDE_ID = os.getenv("SEDE_ID")
# Ahora usamos la ruta del Ping
PING_URL = os.getenv("PING_URL", "http://127.0.0.1:8000/api/camara/ping/") 

if not CAMERA_URL or not SEDE_ID:
    print("❌ ERROR: Faltan variables en el archivo .env (CAMERA_URL o SEDE_ID)")
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

print(f"✅ ¡Vigía listo con {len(nombres_conocidos)} técnicos!")

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
# --- 5. BUCLE PRINCIPAL (EL VIGÍA) ---
# ==========================================
ultimo_ping = 0

while True:
    ret, frame_crudo = cap.read()
    if not ret: 
        break

    if not ia_ocupada:
        ia_ocupada = True
        threading.Thread(target=procesar_ia_en_fondo, args=(frame_crudo.copy(),), daemon=True).start()

    tecnico_actual = nombres_detectados[0] if len(nombres_detectados) > 0 else "DESCONOCIDO"

    # --- ENVIAR PING AL SERVIDOR (MÁXIMO CADA 2 SEGUNDOS) ---
    tiempo_actual = time.time()
    if tecnico_actual != "DESCONOCIDO" and (tiempo_actual - ultimo_ping) > 2.0:
        username_limpio = tecnico_actual.lower().replace(" ", "")
        
        payload = {
            "username": username_limpio, 
            "sede_id": int(SEDE_ID)
        }
        
        try:
            # Enviamos el dato a Django. Usamos timeout=1 para que el video no se trabe si el wifi está lento.
            requests.post(PING_URL, json=payload, timeout=1) 
            print(f"📡 Ping enviado a Django: {tecnico_actual} en ventanilla.")
        except Exception as e:
            pass # Ignoramos errores de red momentáneos
            
        ultimo_ping = tiempo_actual

    # --- DIBUJAR LA INTERFAZ ---
    for (top, right, bottom, left), nombre in zip(ubicaciones_rostros, nombres_detectados):
        top *= 4; right *= 4; bottom *= 4; left *= 4
        color = (0, 255, 0) if nombre != "DESCONOCIDO" else (0, 0, 255)

        cv2.rectangle(frame_crudo, (left, top), (right, bottom), color, 2)
        cv2.rectangle(frame_crudo, (left, bottom - 35), (right, bottom), color, cv2.FILLED)
        cv2.putText(frame_crudo, nombre, (left + 6, bottom - 6), cv2.FONT_HERSHEY_DUPLEX, 0.6, (255, 255, 255), 1)

    # UI Minimalista (Ya no hay carrito)
    if tecnico_actual != "DESCONOCIDO":
        cv2.putText(frame_crudo, f"DETECTADO: {tecnico_actual}", (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
        cv2.putText(frame_crudo, "Sincronizando con Web...", (20, 80), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 1)
    else:
        cv2.putText(frame_crudo, "ESPERANDO ROSTRO...", (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)

    hora = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    cv2.putText(frame_crudo, f"TeleCable - Vigia | Sede ID: {SEDE_ID} | {hora}", (20, frame_crudo.shape[0] - 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)

    cv2.imshow('Vigia Inteligente - TeleCable', frame_crudo)

    # ==========================================
    # --- 6. ESCUCHADOR DE TECLADO ---
    # ==========================================
    key = cv2.waitKey(1) & 0xFF
    
    if key == ord('q'):
        print("Cerrando Vigía...")
        break

# Limpieza
cap.release()
cv2.destroyAllWindows()