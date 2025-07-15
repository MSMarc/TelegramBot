import asyncio
import os
import platform
import requests
import json
import subprocess
from dotenv import load_dotenv
from blinkpy.blinkpy import Blink
from blinkpy.auth import Auth
from datetime import datetime, timedelta
import re
import aiohttp
import aiofiles

load_dotenv()

IP_DISPOSITIVOS = os.getenv("IP_DISPOSITIVOS", "").split(",")
NOMBRES_DISPOSITIVOS = os.getenv("NOMBRES_DISPOSITIVOS", "").split(",")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
BLINK_USER = os.getenv("BLINK_USER")
BLINK_PASS = os.getenv("BLINK_PASS")
BLINK_MODULE = os.getenv("BLINK_MODULE")
USUARIOS_AUTORIZADOS = os.getenv("USUARIOS_AUTORIZADOS", "").split(",")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
REFRESH_SOLICITADO = asyncio.Event()
APAGAR_BOT = asyncio.Event()

CONFIG_PATH = "blink_config.json"
RUTA_ETIQUETAS = "etiquetas_videos.json"

modo_home = "auto"
modo_arm = "true"
armado_actual = None
IP_ROUTER = os.getenv("IP_ROUTER", "192.168.1.1")
tarea_auto_arm = None
blink = None
CHECK_INTERVAL = 600
ULTIMOS_CLIPS = {}
videos_ultimas_24h = []
tarea_vigilancia = None
tarea_principal = None
session = None
presencia_anterior = None

def cargar_max_id():
    if not os.path.exists(RUTA_ETIQUETAS):
        return 0
    try:
        with open(RUTA_ETIQUETAS, "r") as f:
            etiquetas = json.load(f)
        max_id = max(e["id"] for e in etiquetas) if etiquetas else 0
        return max_id
    except Exception as e:
        print(f"⚠️ Error leyendo etiquetas para max id: {e}")
        return 0

contador_videos = cargar_max_id() + 1

async def detectar_presencia(chat_id=None):
    global presencia_anterior
    router_ok = await async_ping(IP_ROUTER)
    if not router_ok:
        return None
    presencia_actual = False
    for ip in IP_DISPOSITIVOS:
        if await async_ping(ip.strip()):
            presencia_actual = True
            break
    if chat_id is not None and presencia_anterior is not None:
        if presencia_anterior == True and presencia_actual == False:
            await telegram_enviar(f"🏠 /home auto ha detectado casa vacía.", chat_id)
        elif presencia_anterior == False and presencia_actual == True:
            await telegram_enviar(f"🏠 /home auto ha detectado alguien en casa.", chat_id)
    presencia_anterior = presencia_actual
    return presencia_actual

async def crear_sesion():
    global session
    if session is None:
        session = aiohttp.ClientSession()
    return session

async def cerrar_sesion():
    global session
    if session:
        await session.close()
        session = None

async def manejar_comando(texto, message_id, chat_id, user_id):
    global tarea_auto_arm, CHECK_INTERVAL, modo_home, tarea_auto_arm, modo_arm, tarea_principal
    if str(user_id) not in USUARIOS_AUTORIZADOS:
        telegram_enviar("❌ Acceso denegado. Contacta con el administrador para usarme.", chat_id)
        return
    texto = texto.strip().lower()
    texto.replace("@Sky_Blink_Bot","")
    if texto == "/start":
        telegram_enviar("🏁 Bot iniciado. Usa /help para ver comandos.", chat_id)
    elif texto.startswith("/") and texto[1:].split()[0].isdigit():
        partes = texto[1:].split(maxsplit=1)
        if len(partes) != 2:
            telegram_enviar("❌ Uso: /<número> <etiqueta>", chat_id)
            return
        id_str, etiqueta = partes
        try:
            vid_id = int(id_str)
            video = next((v for v in videos_ultimas_24h if v["id"] == vid_id), None)
            if not video:
                telegram_enviar("❌ No se encontró vídeo con ese número.", chat_id)
                return
            nueva_entrada = {
                "id": vid_id,
                "ruta": video["ruta"],
                "nombre": video["nombre"],
                "fecha": video["fecha"],
                "etiqueta": etiqueta.strip()
            }
            etiquetas = []
            if os.path.exists(RUTA_ETIQUETAS):
                with open(RUTA_ETIQUETAS, "r") as f:
                    etiquetas = json.load(f)
            ya_existia = any(e["id"] == vid_id for e in etiquetas)
            etiquetas = [e for e in etiquetas if e["id"] != vid_id]
            etiquetas.append(nueva_entrada)
            with open(RUTA_ETIQUETAS, "w") as f:
                json.dump(etiquetas, f, indent=2, ensure_ascii=False)
            if ya_existia:
                telegram_enviar(f"♻️ Etiqueta para vídeo {vid_id} actualizada: *{etiqueta.strip()}*", chat_id)
            else:
                telegram_enviar(f"🏷️ Etiqueta para vídeo {vid_id} guardada: *{etiqueta.strip()}*", chat_id)
        except Exception as e:
            telegram_enviar(f"❌ Error etiquetando vídeo: {e}", chat_id)
    elif texto == "/status":
        await enviar_estado(chat_id)
    elif texto == "/refresh":
        await blink.refresh()
    elif texto == "/stop":
        telegram_enviar("🛑 Bot apagado.", chat_id)
        APAGAR_BOT.set()
        if tarea_principal and not tarea_principal.done():
            tarea_principal.cancel()
        if tarea_vigilancia and not tarea_vigilancia.done():
            tarea_vigilancia.cancel()
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
    elif texto.startswith("/arm"):
        partes = texto.split()
        if len(partes) == 2 and partes[1] in ["auto", "true", "false"]:
            nuevo_valor = partes[1]
            if modo_arm != nuevo_valor:
                modo_arm = nuevo_valor
                if tarea_principal and not tarea_principal.done():
                    tarea_principal.cancel()
                    try:
                        await tarea_principal
                    except asyncio.CancelledError:
                        print("♻️ loop_principal reiniciado por /arm")
                tarea_principal = asyncio.create_task(loop_principal(chat_id))
            else:
                telegram_enviar(f"🔒 Modo /arm ya estaba en *{modo_arm}*", chat_id)
        elif len(partes) == 1:
            telegram_enviar(f"🔒 Estado actual /arm *{modo_arm}*", chat_id)
        else:
            telegram_enviar("❌ Uso: /arm true | false | auto", chat_id)
    elif texto.startswith("/home"):
        partes = texto.split()
        if len(partes) == 2 and partes[1] in ["auto", "true", "false"]:
            nuevo_valor = partes[1]
            if modo_home != nuevo_valor:
                modo_home = nuevo_valor
                telegram_enviar(f"🏠 Modo HOME actualizado a *{modo_home}*", chat_id)
                if tarea_principal and not tarea_principal.done():
                    tarea_principal.cancel()
                    try:
                        await tarea_principal
                    except asyncio.CancelledError:
                        print("♻️ loop_principal reiniciado por /home")
                tarea_principal = asyncio.create_task(loop_principal(chat_id))
            else:
                telegram_enviar(f"🏠 Modo HOME ya estaba en *{modo_home}*", chat_id)
        elif len(partes) == 1:
            telegram_enviar(f"🏠 Estado actual /home *{modo_home}*", chat_id)
        else:
            telegram_enviar("❌ Uso: /home true | false | auto", chat_id)
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
    elif texto.startswith("/authorize"):
        await manejar_autorize(texto, chat_id)
    elif texto == "/help":
        ayuda = (
            "⚙️ *Comandos disponibles:*\n\n"
            "▶️ /start – Inicia el bot\n"
            "❓ /help – Muestra esta ayuda\n"
            "🔄 /refresh – Refresca las cámaras\n"
            "📋 /list – Lista dispositivos\n"
            "🆕 /add <IP> <Nombre> – Añadir dispositivo\n"
            "🗑️ /delete <IP | Nombre> – Eliminar dispositivo\n"
            "🔒 /arm true|false|auto – Cambiar protección\n"
            "🏠 /home true|false|auto – Control modo hogar\n"
            "📷 /cams – Info de cámaras\n"
            "🎞️ /last – Descarga últimos vídeos\n"
            "📼 /videos – Lista vídeos recientes\n"
            "🎬 /video <nº> – Envía vídeo concreto\n"
            "📸 /cap – Foto actual de cámaras\n"
            "🧪 /test – Test de compatibilidad\n"
            "💎 /authorize – Autiroza a otro usuario"
            "🛑 /stop – Apaga el bot\n"
        )
        telegram_enviar(ayuda, chat_id)
    else:
        telegram_enviar("❌ Comando no soportado", chat_id)

async def comando_test(chat_id):
    if blink is None:
        telegram_enviar("❌ Blink no conectado.", chat_id)
        return
    try:
        sync = blink.sync.get(BLINK_MODULE)
        if not sync:
            telegram_enviar(f"❌ No se encontró módulo Sync llamado '{BLINK_MODULE}'", chat_id)
            return
        resultados = []
        for nombre, cam in blink.cameras.items():
            tiene_clip = bool(cam.clip)
            resultados.append(f"\n📷 *{nombre}* → clip directo: {'✅' if tiene_clip else '❌'}")
        telegram_enviar("\n".join(resultados), chat_id)
        if cam.clip:
            url_clip = cam.clip
            r = requests.get(url_clip)
            if r.ok:
                video_bytes = r.content
                filename = f"{nombre}_clip_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
                path = os.path.join("videos", filename)
                os.makedirs("videos", exist_ok=True)
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
            fecha_str = datetime.strptime(fecha_archivo_str, "%Y%m%d_%H%M%S").strftime("%H:%M:%S del %d-%m-%y")
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
            async with aiofiles.open(filename, "wb") as f:
                await f.write(video_bytes)
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
            await telegram_enviar_video(chat_id, filename, texto_mensaje)
    if not any_video:
        telegram_enviar("⚠️ No hay vídeos recientes disponibles en las cámaras.", chat_id)

async def telegram_enviar_video(chat_id, path, caption):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendVideo"
    global session
    try:
        mpwriter = aiohttp.MultipartWriter('form-data')
        part = mpwriter.append(str(chat_id))
        part.set_content_disposition('form-data', name='chat_id')
        part = mpwriter.append(caption)
        part.set_content_disposition('form-data', name='caption')
        part = mpwriter.append('Markdown')
        part.set_content_disposition('form-data', name='parse_mode')
        async with aiofiles.open(path, 'rb') as f:
            video_data = await f.read()
        video_part = mpwriter.append(video_data)
        video_part.set_content_disposition('form-data', name='video', filename=os.path.basename(path))
        video_part.headers['Content-Type'] = 'video/mp4'
        async with session.post(url, data=mpwriter) as resp:
            if resp.status != 200:
                text = await resp.text()
                print(f"❌ Error enviando vídeo por Telegram: {resp.status} {text}")
    except Exception as e:
        print(f"❌ Error enviando vídeo por Telegram: {e}")


async def conectar_blink():
    global blink
    blink = Blink()
    sesion_restaurada = False
    try:
        with open(CONFIG_PATH, "r") as f:
            auth_data = json.load(f)
        blink.auth = Auth(auth_data)
        sesion_restaurada = True
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
        blink.no_owls = True
    except Exception as e:
        print(f"❌ Error iniciando Blink: {e}")
        raise e
    if not sesion_restaurada:
        try:
            await blink.save(CONFIG_PATH)
            print("💾 Sesión Blink guardada correctamente.")
        except Exception as e:
            print(f"⚠️ No se pudo guardar la sesión: {e}")

async def activar_blink(chat_id):
    try:
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
        sync_module = blink.sync.get(BLINK_MODULE)
        if not sync_module:
            telegram_enviar(f"❌ No encontrado módulo Sync llamado '{BLINK_MODULE}'", chat_id)
            return
        await sync_module.async_arm(False)
        telegram_enviar(f"🔓 Blink desarmado (Sync Module: {BLINK_MODULE})", chat_id)
    except Exception as e:
        telegram_enviar(f"❌ Error desactivando Blink: {e}", chat_id)

async def comando_arm(activar: bool, chat_id):
    global tarea_vigilancia, modo_home, modo_arm
    if blink is None:
        try:
            await conectar_blink()
        except Exception as e:
            telegram_enviar(f"❌ Error conectando Blink: {e}", chat_id)
            return
    if activar:
        await activar_blink(chat_id)
        if tarea_vigilancia is None or tarea_vigilancia.done():
            tarea_vigilancia = asyncio.create_task(vigilar_movimiento(chat_id))
    else:
        await desactivar_blink(chat_id)
        if tarea_vigilancia and not tarea_vigilancia.done():
            tarea_vigilancia.cancel()
            tarea_vigilancia = None

async def loop_principal(chat_id):
    global modo_home, modo_arm, armado_actual, APAGAR_BOT
    while not APAGAR_BOT.is_set():
        try:
            if modo_arm == "true":
                armar = True
            elif modo_arm == "false":
                armar = False
            elif modo_arm == "auto":
                if modo_home == "auto":
                    presencia = await detectar_presencia()
                    if presencia is None:
                        telegram_enviar(f"⚠️ No se detecta el router {IP_ROUTER}", chat_id)
                        await asyncio.sleep(CHECK_INTERVAL)
                        continue
                else:
                    presencia = (modo_home == "true")
                armar = not presencia
            else:
                telegram_enviar(f"❌ Valor de /arm desconocido: {modo_arm}", chat_id)
                await asyncio.sleep(CHECK_INTERVAL)
                continue
            if armado_actual != armar:
                armado_actual = armar
                await comando_arm(armar, chat_id)
            await asyncio.sleep(CHECK_INTERVAL)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"❌ Error en loop_principal: {e}")
            await asyncio.sleep(10)
    print("loop_principal terminado")

async def vigilar_movimiento(chat_id):
    global ULTIMOS_CLIPS, videos_ultimas_24h, contador_videos
    while not APAGAR_BOT.is_set():
        try:
            await blink.refresh()
            for nombre, cam in blink.cameras.items():
                video_bytes = cam.video_from_cache
                if not video_bytes:
                    continue
                nuevo_hash = hash(video_bytes)
                if ULTIMOS_CLIPS.get(nombre) == nuevo_hash:
                    continue
                ULTIMOS_CLIPS[nombre] = nuevo_hash
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                fecha_str = datetime.strptime(timestamp, "%Y%m%d_%H%M%S").strftime("%H:%M:%S del %d-%m-%y")
                filename = f"videos/{nombre}_{timestamp}.mp4"
                os.makedirs("videos", exist_ok=True)
                async with aiofiles.open(filename, "wb") as f:
                    await f.write(video_bytes)
                video_info = {
                    "id": contador_videos,
                    "ruta": filename,
                    "nombre": nombre,
                    "fecha": fecha_str,
                }
                videos_ultimas_24h.append(video_info)
                caption = (
                    f"🎥 *Vídeo {contador_videos}*\n"
                    f"Cámara: *{nombre}*\n"
                    f"Fecha: {fecha_str}\n"
                )
                await telegram_enviar_video(chat_id, filename, caption)
                contador_videos += 1
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
        f.write(f"USUARIOS_AUTORIZADOS={','.join(USUARIOS_AUTORIZADOS)}\n")

async def manejar_autorize(texto, chat_id, user, bot):
    global USUARIOS_AUTORIZADOS
    usuario_id = str(user.id)
    if usuario_id != USUARIOS_AUTORIZADOS[0]:
        await telegram_enviar("❌ Solo el usuario principal puede usar /autorize", chat_id)
        return
    partes = texto.split()
    if len(partes) != 2:
        await telegram_enviar("❌ Uso correcto: /autorize @username", chat_id)
        return
    username = partes[1].lstrip("@")
    try:
        chat_info = await bot.get_chat(f"@{username}")
        nuevo_usuario_id = str(chat_info.id)
    except Exception as e:
        await telegram_enviar(f"❌ No se pudo obtener el user_id de @{username}: {e}", chat_id)
        return
    if nuevo_usuario_id in USUARIOS_AUTORIZADOS:
        await telegram_enviar("⚠️ Usuario ya autorizado.", chat_id)
        return
    USUARIOS_AUTORIZADOS.append(nuevo_usuario_id)
    actualizar_env()
    await telegram_enviar(f"✅ Usuario @{username} autorizado correctamente.", chat_id)

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

async def enviar_estado(chat_id):
    estados_actuales = {}
    for ip, nombre in zip(IP_DISPOSITIVOS, NOMBRES_DISPOSITIVOS):
        conectado = await async_ping(ip.strip())
        estados_actuales[nombre.strip()] = conectado
    ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lineas = [f"*Dispositivos a las* `{ahora}`"]
    for nombre in NOMBRES_DISPOSITIVOS:
        tick = "✅" if estados_actuales.get(nombre.strip(), False) else "❌"
        lineas.append(f"{tick} {nombre.strip()}")
    texto = "\n".join(lineas)
    telegram_enviar(texto, chat_id)

async def main():
    global tarea_principal
    await crear_sesion()
    try:
        await conectar_blink()
    except Exception as e:
        print(f"⚠️ No se pudo conectar a Blink al inicio: {e}")
    tarea_principal = asyncio.create_task(loop_principal(TELEGRAM_CHAT_ID))
    tareas = [
        asyncio.create_task(recibir_mensajes()),
        tarea_principal
    ]
    print("🚀 Bot iniciado")
    try:
        await asyncio.gather(*tareas)
    except Exception as e:
        print(f"❌ Error en tareas principales: {e}")
    await cerrar_sesion()
    print("Bot apagado correctamente.")

if __name__ == "__main__":
    asyncio.run(main())