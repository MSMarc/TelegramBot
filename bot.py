import asyncio
import os
import platform
import requests
import json
import subprocess
from dotenv import load_dotenv
from datetime import datetime
from blinkpy.blinkpy import Blink
from blinkpy.auth import Auth
from datetime import datetime, timedelta
import re

load_dotenv()

IP_DISPOSITIVOS = os.getenv("IP_DISPOSITIVOS", "").split(",")
NOMBRES_DISPOSITIVOS = os.getenv("NOMBRES_DISPOSITIVOS", "").split(",")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
BLINK_USER = os.getenv("BLINK_USER")
BLINK_PASS = os.getenv("BLINK_PASS")
BLINK_MODULE = os.getenv("BLINK_MODULE")
USUARIOS_AUTORIZADOS = os.getenv("USUARIOS_AUTORIZADOS", "").split(",")

REFRESH_SOLICITADO = asyncio.Event()
APAGAR_BOT = asyncio.Event()

CONFIG_PATH = "blink_config.json"

modo_auto = False
ip_router = os.getenv("IP_ROUTER", "192.168.1.1")
tarea_auto_arm = None
blink = None
CHECK_INTERVAL = 600
ULTIMOS_CLIPS = {}
videos_ultimas_24h = []
tarea_vigilancia = None

async def manejar_comando(texto, message_id, chat_id, user_id):
    global modo_auto, tarea_auto_arm, CHECK_INTERVAL
    if str(user_id) not in USUARIOS_AUTORIZADOS:
        telegram_enviar("❌ Acceso denegado. Contacta con el administrador para usarme.", chat_id)
        return
    texto = texto.strip().lower()
    chat_key = str(chat_id)
    if texto == "/start":
        texto_inicio = "🔄 Bot iniciado, monitoreando dispositivos..."
        telegram_enviar(texto_inicio, chat_id)
        REFRESH_SOLICITADO.set()
    elif texto == "/refresh":
        await blink.refresh()
    elif texto == "/stop":
        telegram_enviar("🛑 Bot apagado.", chat_id)
        APAGAR_BOT.set()
    elif texto.startswith("/add"):
        try:
            _, ip, nombre = texto.split()
            if ip not in IP_DISPOSITIVOS:
                IP_DISPOSITIVOS.append(ip)
                NOMBRES_DISPOSITIVOS.append(nombre)
                actualizar_env()
                telegram_enviar(f"✅ Añadido: {nombre} ({ip})", chat_id)
                REFRESH_SOLICITADO.set()
            else:
                telegram_enviar("⚠️ IP ya existe", chat_id)
        except:
            telegram_enviar("❌ Uso: /add 192.168.1.X Nombre", chat_id)
    elif texto.startswith("/delete"):
        try:
            _, ip_o_nombre = texto.split()
            if ip_o_nombre in IP_DISPOSITIVOS:
                idx = IP_DISPOSITIVOS.index(ip_o_nombre)
            elif ip_o_nombre in NOMBRES_DISPOSITIVOS:
                idx = NOMBRES_DISPOSITIVOS.index(ip_o_nombre)
            else:
                telegram_enviar("❌ No encontrado.", chat_id)
                return
            eliminado = NOMBRES_DISPOSITIVOS[idx]
            IP_DISPOSITIVOS.pop(idx)
            NOMBRES_DISPOSITIVOS.pop(idx)
            actualizar_env()
            telegram_enviar(f"🗑️ Eliminado: {eliminado}", chat_id)
            REFRESH_SOLICITADO.set()
        except:
            telegram_enviar("❌ Uso: /delete <IP | Nombre>", chat_id)
    elif texto == "/list":
        asyncio.create_task(enviar_lista_dispositivos(chat_id))
    elif texto.startswith("/interval"):
        try:
            _, segundos = texto.split()
            CHECK_INTERVAL = int(segundos)
            telegram_enviar(f"🕒 Intervalo actualizado a {CHECK_INTERVAL} segundos.", chat_id)
        except:
            telegram_enviar("❌ Uso: /interval <segundos>", chat_id)
    elif texto.startswith("/arm"):
        partes = texto.split()
        if len(partes) == 2:
            if partes[1] == "auto":
                if modo_auto:
                    telegram_enviar("⚠️ Ya estás en modo AUTO.", chat_id)
                else:
                    modo_auto = True
                    telegram_enviar("🤖 Modo AUTO activado. El sistema decidirá armar/desarmar automáticamente.", chat_id)
                    if tarea_auto_arm is None or tarea_auto_arm.done():
                        tarea_auto_arm = asyncio.create_task(auto_arm_loop(chat_id))
            elif partes[1] in ["true", "false"]:
                if modo_auto:
                    modo_auto = False
                    if tarea_auto_arm:
                        tarea_auto_arm.cancel()
                        tarea_auto_arm = None
                    telegram_enviar("⚙️ Modo AUTO desactivado. Armado/desarmado forzado.", chat_id)
                valor = partes[1] == "true"
                asyncio.create_task(comando_arm(valor, chat_id))
            else:
                telegram_enviar("❌ Uso: /arm true|false|auto", chat_id)
        else:
            telegram_enviar("❌ Uso: /arm true|false|auto", chat_id)
    elif texto == "/cams":
        await comando_cams(chat_id)
    elif texto == "/last":
        await comando_last(chat_id)
    elif texto == "/videos":
        await comando_videos(chat_id)
    elif texto.startswith("/video "):
        numero = texto.split(" ")[1]
        await comando_video(chat_id, numero)
    elif texto == "/cap":
        if blink is None:
            telegram_enviar("❌ Blink no conectado.", chat_id)
            return
        await comando_cap(chat_id)
    elif texto == "/test":
        await comando_test(chat_id)
    elif texto == "/help":
        ayuda = (
            "⚙️ *Comandos disponibles:*\n\n"
            "/start                              ▶️ Inicia el bot\n"
            "/refresh                            🔄 Refresca las cámaras\n"
            "/list                                   📋 Lista dispositivos\n"
            "/interval <segundos>    🕒 Tiempo de refresh\n"
            "/clear                                ✨ Limpia el chat\n"
            "/add <IP> <Nombre>     🆕 Añadir dispositivo\n"
            "/delete <IP | Nombre>   🗑️ Eliminar dispositivo\n"
            "/arm true | false | auto   🔒 Cambiar protección\n"
            "/help                                 ❓ Muestra esta ayuda\n"
            "/stop                                 🛑 Apaga el bot\n"
        )
        telegram_enviar(ayuda, chat_id)
    else:
        telegram_enviar("❌ Comando no soportado")

async def comando_test(chat_id):
    if blink is None:
        telegram_enviar("❌ Blink no conectado.", chat_id)
        return
    try:
        sync = blink.sync.get(BLINK_MODULE)
        if not sync:
            telegram_enviar(f"❌ No se encontró módulo Sync llamado '{BLINK_MODULE}'", chat_id)
            return
        funciones = [
            "request_local_storage_manifest",
            "get_local_storage_manifest",
            "request_local_storage_clip_upload",
            "download_local_storage_clip"
        ]
        resultados = []
        for func in funciones:
            soportado = hasattr(sync, func) and callable(getattr(sync, func))
            resultados.append(f"• `{func}()` → {'✅' if soportado else '❌'}")
        for nombre, cam in blink.cameras.items():
            tiene_clip = bool(cam.clip)
            resultados.append(f"\n📷 *{nombre}* → clip directo: {'✅' if tiene_clip else '❌'}")
        telegram_enviar("\n".join(resultados), chat_id)
        if cam.clip:
            video_bytes = cam.clip
            filename = f"{nombre}_clip_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
            path = os.path.join("videos", filename)
            with open(path, "wb") as f:
                f.write(video_bytes)
        path = os.path.join(os.getcwd(), "videos")
        os.makedirs(path, exist_ok=True)
        await blink.download_videos(
            path,
            since='2025/07/04 09:34',
            delay=2
        )
        telegram_enviar(f"✅ Vídeos descargados.", chat_id)
    except Exception as e:
        telegram_enviar(f"❌ Error durante el test: {e}", chat_id)

async def comando_cap(chat_id):
    for nombre, camera in blink.cameras.items():
        try:
            await camera.snap_picture()
            fecha_archivo = datetime.now().strftime("%Y%m%d_%H%M%S")
            fecha_str = datetime.strptime(fecha_archivo, "%Y%m%d_%H%M%S").strftime("%H-%M-%S del %d-%m-%y")
            filename = f"{nombre}_{fecha_str}.jpg"
            path = os.path.join("fotos", filename)
            os.makedirs("fotos", exist_ok=True)
            await camera.image_to_file(path)
            telegram_enviar(f"📸 Foto de *{nombre}* tomada a las {fecha_str}", chat_id)
            telegram_enviar_foto(chat_id, path)
        except Exception as e:
            telegram_enviar(f"❌ Error tomando foto en {nombre}: {e}", chat_id)

def telegram_enviar_foto(chat_id, path):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    with open(path, "rb") as photo:
        files = {"photo": photo}
        data = {"chat_id": chat_id}
        r = requests.post(url, files=files, data=data)
    if not r.ok:
        print(f"Error enviando foto: {r.text}")

async def comando_video(chat_id, numero):
    global videos_ultimas_24h
    if not videos_ultimas_24h:
        telegram_enviar("⚠️ No hay videos almacenados para mostrar. Usa /videos", chat_id)
        return
    try:
        idx = int(numero) - 1
        video = videos_ultimas_24h[idx]
    except (ValueError, IndexError):
        telegram_enviar("❌ Número de video inválido.", chat_id)
        return
    
    ruta_video = video["ruta"]
    if not os.path.exists(ruta_video):
        telegram_enviar("❌ El video solicitado no está disponible localmente.", chat_id)
        return
    telegram_enviar_video(chat_id, ruta_video, f"🎥 Video {numero}: {video['nombre']} ({video['fecha']})")

async def comando_videos(chat_id):
    global videos_ultimas_24h
    videos_ultimas_24h.clear()
    carpeta_videos = "videos"
    os.makedirs(carpeta_videos, exist_ok=True)
    archivos = [f for f in os.listdir(carpeta_videos) if f.endswith(".mp4")]
    lista_mensajes = []
    contador = 1
    for archivo in sorted(archivos, reverse=True):
        match = re.search(r"_(\d{8}_\d{6})\.mp4$", archivo)
        if not match:
            continue
        fecha_archivo_str = match.group(1)
        try:
            fecha_obj = datetime.strptime(fecha_archivo_str, "%Y%m%d_%H%M%S")
            fecha_str = fecha_obj.strftime("%H:%M:%S del %d-%m-%y")
        except Exception:
            fecha_str = "Fecha desconocida"

        nombre_camara = archivo.split("_")[0]
        ruta_video = os.path.join(carpeta_videos, archivo)
        videos_ultimas_24h.append({
            "id": contador,
            "nombre": nombre_camara,
            "fecha": fecha_str,
            "ruta": ruta_video,
        })
        lista_mensajes.append(f"{contador}. {nombre_camara} - {fecha_str}")
        contador += 1
    if lista_mensajes:
        mensaje = "🎞️ Videos de las últimas 24h:\n" + "\n".join(lista_mensajes) + "\n\nUsa /video X para pedir uno."
    else:
        mensaje = "⚠️ No se encontraron videos en las últimas 24h."
    telegram_enviar(mensaje, chat_id)


async def comando_cams(chat_id):
    if blink is None:
        telegram_enviar("❌ Blink no conectado.", chat_id)
        return
    cámaras = []
    for nombre, cam in blink.cameras.items():
        estado = "🔒 Armado" if cam.arm else "🔓 Desarmado"
        attrs = cam.attributes
        estado_bateria = attrs.get("battery_voltage", "N/A")
        ultima_mov = attrs.get("last_recording", "N/A")
        cámaras.append(
            f"*{nombre}*\n"
            f"Estado: {estado}\n"
            f"Batería: {estado_bateria}\n"
            f"Última grabación: {ultima_mov}\n"
        )
    mensaje = "📷 *Cámaras disponibles:*\n\n" + "\n".join(cámaras) if cámaras else "⚠️ No se encontraron cámaras."
    telegram_enviar(mensaje, chat_id)

async def comando_last(chat_id):
    if blink is None:
        telegram_enviar("❌ Blink no conectado.", chat_id)
        return
    any_video = False
    for nombre, cam in blink.cameras.items():
        video_bytes = cam.video_from_cache
        if video_bytes:
            any_video = True
            carpeta_videos = "videos"
            os.makedirs(carpeta_videos, exist_ok=True)
            fecha_str = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = os.path.join(carpeta_videos, f"{nombre}_{fecha_str}.mp4")
            with open(filename, "wb") as f:
                f.write(video_bytes)
            match = re.match(r"(.+)_(\d{8})_(\d{6})\.mp4", os.path.basename(filename))
            if match:
                camara_nombre = match.group(1)
                fecha = match.group(2)
                hora = match.group(3)
                fecha_formateada = f"{fecha[:4]}-{fecha[4:6]}-{fecha[6:]}"
                hora_formateada = f"{hora[:2]}:{hora[2:4]}:{hora[4:]}"
                texto_mensaje = f"🎥 Último vídeo de *{camara_nombre}*\nFecha: {fecha_formateada}\nHora: {hora_formateada}"
            else:
                texto_mensaje = f"🎥 Último vídeo de {nombre}"
            telegram_enviar_video(chat_id, filename, texto_mensaje)
    if not any_video:
        telegram_enviar("⚠️ No hay vídeos recientes disponibles en las cámaras.", chat_id)

async def conectar_blink():
    global blink
    blink = Blink()
    try:
        with open(CONFIG_PATH, "r") as f:
            auth_data = json.load(f)
        blink.auth = Auth(auth_data)
        print("🔄 Intentando restaurar sesión desde archivo...")
    except Exception:
        print("⚠️ No se encontró sesión guardada o está corrupta. Login manual...")
        if not BLINK_USER or not BLINK_PASS:
            raise Exception("❌ No hay usuario o contraseña Blink en variables de entorno")
        blink.auth = Auth({"username": BLINK_USER, "password": BLINK_PASS}, no_prompt=True)
    
    try:
        await blink.start()
        print("✅ Sesión Blink iniciada correctamente.")
        blink.refresh_rate = 30
        blink.no_owls=True
    except Exception as e:
        print(f"❌ Error iniciando Blink: {e}")
        raise e
    try:
        await blink.save(CONFIG_PATH)
        print("💾 Sesión Blink guardada correctamente.")
    except Exception as e:
        print(f"⚠️ No se pudo guardar la sesión: {e}")

async def activar_blink(chat_id):
    try:
        await blink.refresh()
        sync_module = blink.sync.get(BLINK_MODULE)
        if not sync_module:
            telegram_enviar(f"❌ No encontrado módulo Sync llamado '{BLINK_MODULE}'", chat_id)
            return
        await sync_module.async_arm(True)
        telegram_enviar(f"🔒 Blink armado (Sync Module: {BLINK_MODULE})", chat_id)
    except Exception as e:
        telegram_enviar(f"❌ Error activando Blink: {e}", chat_id)

async def desactivar_blink(chat_id):
    try:
        await blink.refresh()
        sync_module = blink.sync.get(BLINK_MODULE)
        if not sync_module:
            telegram_enviar(f"❌ No encontrado módulo Sync llamado '{BLINK_MODULE}'", chat_id)
            return
        await sync_module.async_arm(False)
        telegram_enviar(f"🔓 Blink desarmado (Sync Module: {BLINK_MODULE})", chat_id)
    except Exception as e:
        telegram_enviar(f"❌ Error desactivando Blink: {e}", chat_id)

async def comando_arm(activar: bool, chat_id):
    global tarea_vigilancia
    if blink is None:
        try:
            await conectar_blink()
        except Exception as e:
            telegram_enviar(f"❌ Error conectando Blink: {e}", chat_id)
            return
    if activar:
        await activar_blink(chat_id)
        nadie = not any([await async_ping(ip.strip()) for ip in IP_DISPOSITIVOS])
        if nadie:
            if tarea_vigilancia is None or tarea_vigilancia.done():
                tarea_vigilancia = asyncio.create_task(vigilar_movimiento(chat_id))
    else:
        await desactivar_blink(chat_id)
        if tarea_vigilancia and not tarea_vigilancia.done():
            tarea_vigilancia.cancel()
            tarea_vigilancia = None

async def auto_arm_loop(chat_id):
    global modo_auto, tarea_vigilancia
    armado_actual = None
    while modo_auto and not APAGAR_BOT.is_set():
        router_ok = await async_ping(ip_router)
        if not router_ok:
            texto = f"⚠️ Router ({ip_router}) no responde. No se cambia estado Blink."
            telegram_enviar(texto, chat_id)
            await asyncio.sleep(CHECK_INTERVAL)
            continue
        alguno_conectado = False
        for ip in IP_DISPOSITIVOS:
            if await async_ping(ip.strip()):
                alguno_conectado = True
                break
        armar = not alguno_conectado
        if armado_actual != armar:
            armado_actual = armar
            if armar:
                await activar_blink(chat_id)
                if tarea_vigilancia is None or tarea_vigilancia.done():
                    tarea_vigilancia = asyncio.create_task(vigilar_movimiento(chat_id))
                texto = f"🔒 Sistema armado automáticamente. (Modo AUTO)"
            else:
                await desactivar_blink(chat_id)
                if tarea_vigilancia and not tarea_vigilancia.done():
                    tarea_vigilancia.cancel()
                    tarea_vigilancia = None
                texto = f"🔓 Sistema desarmado automáticamente. (Modo AUTO)"
            telegram_enviar(texto, chat_id)
        await asyncio.sleep(CHECK_INTERVAL)

async def vigilar_movimiento(chat_id):
    global ULTIMOS_CLIPS
    while not APAGAR_BOT.is_set():
        try:
            await blink.refresh()
            for nombre, cam in blink.cameras.items():
                nuevo_clip = cam.clip
                if not nuevo_clip or ULTIMOS_CLIPS.get(nombre) == nuevo_clip:
                    continue
                ULTIMOS_CLIPS[nombre] = nuevo_clip
                r = requests.get(nuevo_clip)
                filename = f"videos/{nombre}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
                os.makedirs("videos", exist_ok=True)
                with open(filename, "wb") as f:
                    f.write(r.content)
                telegram_enviar_video(chat_id, filename, f"🎥 Movimiento detectado en *{nombre}*")
            await asyncio.sleep(30)
        except Exception as e:
            print("❌ Error en vigilancia de movimiento:", e)
            await asyncio.sleep(30)

async def async_ping(ip):
    system = platform.system().lower()
    command = ["ping", "-n", "1", "-w", "1000", ip] if "windows" in system else ["ping", "-c", "1", "-W", "1", ip]
    proc = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await proc.communicate()
    return proc.returncode == 0

def telegram_enviar(text, chat_id=None):
    if chat_id is None:
        print("❌ chat_id no especificado en telegram_enviar")
        return None
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, data=data)
        r.raise_for_status()
        respuesta = r.json()
        return respuesta.get("result", {}).get("message_id")
    except Exception as e:
        print("❌ Error enviando Telegram:", e)
        return None

def telegram_editar(message_id, text, chat_id):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText"
    data = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, data=data)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"❌ Error editando Telegram mensaje {message_id} en chat {chat_id}: {e}")
        print(f"Respuesta: {r.text if 'r' in locals() else 'sin respuesta'}")
        return False

def telegram_eliminar(message_id, chat_id):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteMessage"
    data = {"chat_id": chat_id, "message_id": message_id}
    try:
        r = requests.post(url, data=data)
        r.raise_for_status()
        return True
    except Exception as e:
        print("❌ Error eliminando Telegram:", e)
        return False
    
def telegram_enviar_video(chat_id, path, caption):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendVideo"
    try:
        with open(path, "rb") as video_file:
            files = {"video": video_file}
            data = {"chat_id": chat_id, "caption": caption, "parse_mode": "Markdown"}
            r = requests.post(url, data=data, files=files)
            r.raise_for_status()
    except Exception as e:
        print("❌ Error enviando vídeo por Telegram:", e)

async def enviar_lista_dispositivos(chat_id):
    dispositivos = []
    for ip, nombre in zip(IP_DISPOSITIVOS, NOMBRES_DISPOSITIVOS):
        nombre = nombre.strip()
        ip = ip.strip()
        inicio = datetime.now()
        conectado = await async_ping(ip)
        fin = datetime.now()
        ping_ms = int((fin - inicio).total_seconds() * 1000)
        mac = obtener_mac(ip) if conectado else "❌ No disponible"
        estado = "✅" if conectado else "❌"
        dispositivos.append(f"{estado} *{nombre}*\nIP: `{ip}`\nMAC: `{mac}`\nPing: `{ping_ms}ms`\n")
    mensaje = "📋 *Dispositivos monitoreados:*\n\n" + "\n".join(dispositivos)
    telegram_enviar(mensaje, chat_id)

def obtener_mac(ip):
    try:
        resultado = subprocess.check_output(["arp", "-a"], stderr=subprocess.DEVNULL, text=True)
        for linea in resultado.splitlines():
            if ip in linea:
                partes = linea.split()
                for parte in partes:
                    if "-" in parte and len(parte) == 17:
                        return parte
        return "MAC no encontrada"
    except Exception as e:
        print(f"❌ Error obtener MAC de {ip}: {e}")
        return "Error al obtener MAC"

def actualizar_env():
    with open(".env", "w") as f:
        f.write(f"IP_DISPOSITIVOS={','.join(IP_DISPOSITIVOS)}\n")
        f.write(f"NOMBRES_DISPOSITIVOS={','.join(NOMBRES_DISPOSITIVOS)}\n")
        f.write(f"TELEGRAM_TOKEN={TELEGRAM_TOKEN}\n")

async def recibir_mensajes():
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    last_update = 0
    try:
        r = requests.get(url)
        r.raise_for_status()
        data = r.json()
        updates = data.get("result", [])
        if updates:
            last_update = max(update["update_id"] for update in updates)
            requests.get(url, params={"offset": last_update + 1})
    except Exception as e:
        print("❌ Error limpiando updates antiguos:", e)
    while not APAGAR_BOT.is_set():
        try:
            r = requests.get(url, params={"offset": last_update + 1}, timeout=5)
            r.raise_for_status()
            data = r.json()
            for result in data.get("result", []):
                last_update = result["update_id"]
                mensaje = result.get("message")
                if mensaje:
                    texto = mensaje.get("text", "")
                    mid = mensaje.get("message_id")
                    chat_id = mensaje.get("chat", {}).get("id")
                    user_id = mensaje.get("from", {}).get("id")
                    await manejar_comando(texto, mid, chat_id, user_id)
        except Exception as e:
            print("❌ Error al recibir mensajes:", e)
        for _ in range(20):
            if APAGAR_BOT.is_set():
                return
            await asyncio.sleep(0.1)

async def enviar_estado():
    while not APAGAR_BOT.is_set():
        await REFRESH_SOLICITADO.wait()
        REFRESH_SOLICITADO.clear()
        estados_actuales = {}
        for ip, nombre in zip(IP_DISPOSITIVOS, NOMBRES_DISPOSITIVOS):
            conectado = await async_ping(ip.strip())
            estados_actuales[nombre.strip()] = conectado
        ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lineas = [f"*Dispositivos a las * `{ahora}`"]
        for nombre in NOMBRES_DISPOSITIVOS:
            tick = "✅" if estados_actuales.get(nombre.strip(), False) else "❌"
            lineas.append(f"{tick} {nombre.strip()}")
        lineas.append("\n/help paleta de comandos")
        texto = "\n".join(lineas)
        for chat_id_str, chat_data in data.items():
            principal_id = chat_data.get("principal")
            if principal_id is None:
                continue
            chat_id = int(chat_id_str)
            telegram_enviar(texto, chat_id)
        for _ in range(10):
            if APAGAR_BOT.is_set():
                return
            await asyncio.sleep(0.1)

async def main():
    try:
        await conectar_blink()
    except Exception as e:
        print(f"⚠️ No se pudo conectar a Blink al inicio: {e}")
    REFRESH_SOLICITADO.set()
    tareas = [
        asyncio.create_task(enviar_estado()),
        asyncio.create_task(recibir_mensajes())
    ]
    await asyncio.wait(tareas, return_when=asyncio.FIRST_COMPLETED)
    for t in tareas:
        if not t.done():
            t.cancel()
    await asyncio.gather(*tareas, return_exceptions=True)
    print("Bot apagado correctamente.")

if __name__ == "__main__":
    asyncio.run(main())