import asyncio
import os
import platform
import requests
import json
import subprocess
from dotenv import load_dotenv
from datetime import datetime
from blinkpy.blinkpy import Blink

load_dotenv()

IP_DISPOSITIVOS = os.getenv("IP_DISPOSITIVOS", "").split(",")
NOMBRES_DISPOSITIVOS = os.getenv("NOMBRES_DISPOSITIVOS", "").split(",")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
BLINK_USER = os.getenv("BLINK_USER")
BLINK_PASS = os.getenv("BLINK_PASS")
BLINK_MODULE = os.getenv("BLINK_MODULE")
USUARIOS_AUTORIZADOS = os.getenv("USUARIOS_AUTORIZADOS", "").split(",")

MENSAJES_GUARDADOS_FILE = "telegram_messages.json"
REFRESH_SOLICITADO = asyncio.Event()
APAGAR_BOT = asyncio.Event()

CONFIG_PATH = "blink_config.json"

modo_auto = False
ip_router = os.getenv("IP_ROUTER", "192.168.1.1")
tarea_auto_arm = None
blink = None
CHECK_INTERVAL = 600
ULTIMOS_CLIPS = {}
tarea_vigilancia = None

def manejar_comando(texto, message_id, chat_id, user_id):
    global modo_auto, tarea_auto_arm, CHECK_INTERVAL
    if str(user_id) not in USUARIOS_AUTORIZADOS:
        telegram_enviar("❌ Acceso denegado. Contacta con el administrador para usarme.", chat_id)
        return
    texto = texto.strip().lower()
    data = cargar_mensajes_guardados()
    chat_key = str(chat_id)
    if chat_key not in data:
        data[chat_key] = {"principal": None, "otros": []}
    if message_id not in data[chat_key]["otros"] and message_id != data[chat_key].get("principal") and texto != "/refresh":
        data[chat_key]["otros"].append(message_id)
        guardar_mensajes_guardados(data)
    if texto == "/start":
        texto_inicio = "🔄 Bot iniciado, monitoreando dispositivos..."
        nuevo_id = telegram_enviar(texto_inicio, chat_id)
        if nuevo_id:
            data = cargar_mensajes_guardados()
            chat_key = str(chat_id)
            if chat_key not in data:
                data[chat_key] = {"principal": None, "otros": []}
            data[chat_key]["principal"] = nuevo_id
            guardar_mensajes_guardados(data)
            REFRESH_SOLICITADO.set()
    elif texto == "/refresh":
        telegram_eliminar(message_id, chat_id)
        REFRESH_SOLICITADO.set()
    elif texto == "/stop":
        telegram_enviar_guardar("🛑 Bot apagado.", chat_id)
        APAGAR_BOT.set()
    elif texto.startswith("/add"):
        try:
            _, ip, nombre = texto.split()
            if ip not in IP_DISPOSITIVOS:
                IP_DISPOSITIVOS.append(ip)
                NOMBRES_DISPOSITIVOS.append(nombre)
                actualizar_env()
                telegram_enviar_guardar(f"✅ Añadido: {nombre} ({ip})", chat_id)
                REFRESH_SOLICITADO.set()
            else:
                telegram_enviar_guardar("⚠️ IP ya existe", chat_id)
        except:
            telegram_enviar_guardar("❌ Uso: /add 192.168.1.X Nombre", chat_id)
    elif texto == "/clear":
        telegram_eliminar(message_id, chat_id)
        asyncio.create_task(limpiar_chat_completo(chat_id))
    elif texto.startswith("/delete"):
        try:
            _, ip_o_nombre = texto.split()
            if ip_o_nombre in IP_DISPOSITIVOS:
                idx = IP_DISPOSITIVOS.index(ip_o_nombre)
            elif ip_o_nombre in NOMBRES_DISPOSITIVOS:
                idx = NOMBRES_DISPOSITIVOS.index(ip_o_nombre)
            else:
                telegram_enviar_guardar("❌ No encontrado.", chat_id)
                return
            eliminado = NOMBRES_DISPOSITIVOS[idx]
            IP_DISPOSITIVOS.pop(idx)
            NOMBRES_DISPOSITIVOS.pop(idx)
            actualizar_env()
            telegram_enviar_guardar(f"🗑️ Eliminado: {eliminado}", chat_id)
            REFRESH_SOLICITADO.set()
        except:
            telegram_enviar_guardar("❌ Uso: /delete <IP | Nombre>", chat_id)
    elif texto == "/list":
        asyncio.create_task(enviar_lista_dispositivos(chat_id))
    elif texto.startswith("/interval"):
        try:
            _, segundos = texto.split()
            CHECK_INTERVAL = int(segundos)
            telegram_enviar_guardar(f"🕒 Intervalo actualizado a {CHECK_INTERVAL} segundos.", chat_id)
        except:
            telegram_enviar_guardar("❌ Uso: /interval <segundos>", chat_id)
    elif texto.startswith("/arm"):
        partes = texto.split()
        if len(partes) == 2:
            if partes[1] == "auto":
                if modo_auto:
                    telegram_enviar_guardar("⚠️ Ya estás en modo AUTO.", chat_id)
                else:
                    modo_auto = True
                    telegram_enviar_guardar("🤖 Modo AUTO activado. El sistema decidirá armar/desarmar automáticamente.", chat_id)
                    if tarea_auto_arm is None or tarea_auto_arm.done():
                        tarea_auto_arm = asyncio.create_task(auto_arm_loop(chat_id))
            elif partes[1] in ["true", "false"]:
                if modo_auto:
                    modo_auto = False
                    if tarea_auto_arm:
                        tarea_auto_arm.cancel()
                        tarea_auto_arm = None
                    telegram_enviar_guardar("⚙️ Modo AUTO desactivado. Armado/desarmado forzado.", chat_id)
                valor = partes[1] == "true"
                asyncio.create_task(comando_arm(valor, chat_id))
            else:
                telegram_enviar_guardar("❌ Uso: /arm true|false|auto", chat_id)
        else:
            telegram_enviar_guardar("❌ Uso: /arm true|false|auto", chat_id)
    elif texto == "/help":
        ayuda = (
            "⚙️ *Comandos disponibles:*\n\n"
            "/start                              ▶️ Inicia el bot\n"
            "/refresh                            🔄 Actualiza la lista\n"
            "/list                                   📋 Lista dispositivos\n"
            "/interval <segundos>    🕒 Tiempo de refresh\n"
            "/clear                                ✨ Limpia el chat\n"
            "/add <IP> <Nombre>     🆕 Añadir dispositivo\n"
            "/delete <IP | Nombre>   🗑️ Eliminar dispositivo\n"
            "/arm true | false | auto   🔒 Cambiar protección\n"
            "/help                                 ❓ Muestra esta ayuda\n"
            "/stop                                 🛑 Apaga el bot\n"
        )
        telegram_enviar_guardar(ayuda, chat_id)

async def conectar_blink():
    global blink
    blink = Blink()
    session_cargada = False
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                saved_auth = json.load(f)
            if hasattr(blink.auth, "set_auth"):
                blink.auth.set_auth(saved_auth)
                print("🔐 Sesión Blink restaurada desde archivo con set_auth()")
            else:
                blink.auth.token = saved_auth.get("token")
                blink.auth.user_id = saved_auth.get("user_id")
                print("🔐 Sesión Blink restaurada parcialmente desde archivo")
            session_cargada = True
        except Exception as e:
            print("⚠️ Error cargando sesión Blink:", e)
    if not session_cargada or not blink.auth.token:
        if not BLINK_USER or not BLINK_PASS:
            raise Exception("⚠️ No hay usuario o contraseña Blink en variables de entorno")
        blink.auth.login_data = {"username": BLINK_USER, "password": BLINK_PASS}
        await blink.auth.login()
    await blink.start()
    to_save = {
        "token": str(blink.auth.token) if blink.auth.token else None,
        "refresh_token": str(blink.auth.refresh_token) if hasattr(blink.auth, "refresh_token") else None,
        "access_token": str(blink.auth.access_token) if hasattr(blink.auth, "access_token") else None,
        "token_expiry": blink.auth.token_expiry if hasattr(blink.auth, "token_expiry") else None,
        "user_id": blink.auth.user_id,
    }
    with open(CONFIG_PATH, "w") as f:
        json.dump(to_save, f)
    print("💾 Sesión Blink guardada en disco")

async def activar_blink(chat_id):
    try:
        await blink.refresh()
        sync_module = blink.sync.get(BLINK_MODULE)
        if not sync_module:
            telegram_enviar_guardar(f"❌ No encontrado módulo Sync llamado '{BLINK_MODULE}'", chat_id)
            return
        await sync_module.async_arm(True)
        telegram_enviar_guardar(f"🔒 Blink armado (Sync Module: {BLINK_MODULE})", chat_id)
    except Exception as e:
        telegram_enviar_guardar(f"❌ Error activando Blink: {e}", chat_id)

async def desactivar_blink(chat_id):
    try:
        await blink.refresh()
        sync_module = blink.sync.get(BLINK_MODULE)
        if not sync_module:
            telegram_enviar_guardar(f"❌ No encontrado módulo Sync llamado '{BLINK_MODULE}'", chat_id)
            return
        await sync_module.async_arm(False)
        telegram_enviar_guardar(f"🔓 Blink desarmado (Sync Module: {BLINK_MODULE})", chat_id)
    except Exception as e:
        telegram_enviar_guardar(f"❌ Error desactivando Blink: {e}", chat_id)

async def comando_arm(activar: bool, chat_id):
    global tarea_vigilancia
    if blink is None:
        try:
            await conectar_blink()
        except Exception as e:
            telegram_enviar_guardar(f"❌ Error conectando Blink: {e}", chat_id)
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
            actualizar_mensaje_principal(texto, chat_id)
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
            actualizar_mensaje_principal(texto, chat_id)
        await asyncio.sleep(CHECK_INTERVAL)

async def vigilar_movimiento(chat_id):
    global ULTIMOS_CLIPS
    while not APAGAR_BOT.is_set():
        try:
            await blink.refresh()
            nadie = not any([await async_ping(ip.strip()) for ip in IP_DISPOSITIVOS])
            if nadie:
                for nombre, cam in blink.cameras.items():
                    cam.refresh()
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

def cargar_mensajes_guardados():
    if not os.path.exists(MENSAJES_GUARDADOS_FILE):
        return {}
    try:
        with open(MENSAJES_GUARDADOS_FILE, "r") as f:
            return json.load(f)
    except Exception as e:
        print("❌ Error cargando mensajes guardados:", e)
        return {}

def guardar_mensajes_guardados(data):
    try:
        with open(MENSAJES_GUARDADOS_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print("❌ Error guardando mensajes:", e)

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

def telegram_enviar_guardar(text, chat_id):
    mid = telegram_enviar(text, chat_id)
    if mid:
        data = cargar_mensajes_guardados()
        chat_key = str(chat_id)
        if chat_key not in data:
            data[chat_key] = {"principal": None, "otros": []}
        if mid not in data[chat_key]["otros"]:
            data[chat_key]["otros"].append(mid)
            guardar_mensajes_guardados(data)
    return mid

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

async def limpiar_chat_completo(chat_id):
    """Intenta eliminar todos los mensajes del chat excepto el principal"""
    data = cargar_mensajes_guardados()
    if str(chat_id) not in data:
        return
    principal = data[str(chat_id)].get("principal")
    otros = data[str(chat_id)].get("otros", [])
    for msg_id in otros:
        telegram_eliminar(msg_id, chat_id)
    data[str(chat_id)]["otros"] = []
    # if principal:
    #     telegram_eliminar(principal, chat_id)
    #     data[str(chat_id)]["principal"] = None
    guardar_mensajes_guardados(data)

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
    telegram_enviar_guardar(mensaje, chat_id)

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

def actualizar_mensaje_principal(texto, chat_id):
    ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    texto_completo = f"{texto}"
    data = cargar_mensajes_guardados()
    chat_key = str(chat_id)
    if chat_key not in data:
        data[chat_key] = {"principal": None, "otros": []}
    principal_id = data.get(chat_key, {}).get("principal")
    if principal_id:
        exito = telegram_editar(principal_id, texto_completo, chat_id)
        if not exito:
            telegram_eliminar(principal_id, chat_id)
            nuevo_id = telegram_enviar(texto_completo, chat_id)
            if nuevo_id:
                data[chat_key]["principal"] = nuevo_id
                guardar_mensajes_guardados(data)
    else:
        nuevo_id = telegram_enviar(texto_completo, chat_id)
        if nuevo_id:
            data[chat_key]["principal"] = nuevo_id
            guardar_mensajes_guardados(data)

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
                    manejar_comando(texto, mid, chat_id, user_id)
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
        lineas = [f"*Último /refresh a las* `{ahora}`"]
        for nombre in NOMBRES_DISPOSITIVOS:
            tick = "✅" if estados_actuales.get(nombre.strip(), False) else "❌"
            lineas.append(f"{tick} {nombre.strip()}")
        lineas.append("\n/help paleta de comandos")
        texto = "\n".join(lineas)
        data = cargar_mensajes_guardados()
        for chat_id_str, chat_data in data.items():
            principal_id = chat_data.get("principal")
            if principal_id is None:
                continue
            chat_id = int(chat_id_str)
            actualizar_mensaje_principal(texto, chat_id)
        for _ in range(10):
            if APAGAR_BOT.is_set():
                return
            await asyncio.sleep(0.1)

async def limpiar_mensajes_anteriores():
    if not os.path.exists(MENSAJES_GUARDADOS_FILE):
        guardar_mensajes_guardados({})
        return
    try:
        data = cargar_mensajes_guardados()
        nuevos_datos = {}
        for chat_id_str, ids in data.items():
            try:
                chat_id = int(chat_id_str)
            except ValueError:
                print(f"⚠️ Chat ID inválido en mensajes guardados: {chat_id_str}")
                continue
            if isinstance(ids, dict):
                mensajes = [ids.get("principal")] + ids.get("otros", [])
            elif isinstance(ids, list):
                mensajes = ids
            else:
                mensajes = []
            for mid in mensajes:
                if mid:
                    telegram_eliminar(mid, chat_id)
            nuevos_datos[str(chat_id)] = {"principal": None, "otros": []}
        guardar_mensajes_guardados(nuevos_datos)
    except Exception as e:
        print(f"⚠️ Error limpiando mensajes anteriores: {e}")
        guardar_mensajes_guardados({})

async def main():
    await limpiar_mensajes_anteriores()
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