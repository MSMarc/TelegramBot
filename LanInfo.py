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
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BLINK_USER = os.getenv("BLINK_USER")
BLINK_PASS = os.getenv("BLINK_PASS")

CHECK_INTERVAL = 600
MENSAJES_GUARDADOS_FILE = "telegram_messages.json"
REFRESH_SOLICITADO = asyncio.Event()
APAGAR_BOT = asyncio.Event()

telegram_message_id = None

CONFIG_PATH = "blink_config.json"
SYNC_MODULE_NAME = os.getenv("BLINK_MODULE")

blink = None

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

async def activar_blink():
    try:
        await blink.refresh()
        sync_module = blink.sync.get(SYNC_MODULE_NAME)
        if not sync_module:
            telegram_send_y_guardar(f"❌ No encontrado módulo Sync llamado '{SYNC_MODULE_NAME}'")
            return
        await sync_module.async_arm(True)
        telegram_send_y_guardar(f"🔒 Blink armado (Sync Module: {SYNC_MODULE_NAME})")
    except Exception as e:
        telegram_send_y_guardar(f"❌ Error activando Blink: {e}")

async def desactivar_blink():
    try:
        await blink.refresh()
        sync_module = blink.sync.get(SYNC_MODULE_NAME)
        if not sync_module:
            telegram_send_y_guardar(f"❌ No encontrado módulo Sync llamado '{SYNC_MODULE_NAME}'")
            return
        await sync_module.async_arm(False)
        telegram_send_y_guardar(f"🔓 Blink desarmado (Sync Module: {SYNC_MODULE_NAME})")
    except Exception as e:
        telegram_send_y_guardar(f"❌ Error desactivando Blink: {e}")

async def async_ping(ip):
    system = platform.system().lower()
    command = ["ping", "-n", "1", "-w", "1000", ip] if "windows" in system else ["ping", "-c", "1", "-W", "1", ip]
    proc = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await proc.communicate()
    return proc.returncode == 0

def cargar_mensajes_guardados():
    if not os.path.exists(MENSAJES_GUARDADOS_FILE):
        return {"principal": None, "otros": []}
    try:
        with open(MENSAJES_GUARDADOS_FILE, "r") as f:
            return json.load(f)
    except Exception as e:
        print("❌ Error cargando mensajes guardados:", e)
        return {"principal": None, "otros": []}

def guardar_mensajes_guardados(data):
    try:
        with open(MENSAJES_GUARDADOS_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print("❌ Error guardando mensajes:", e)

def telegram_send(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, data=data)
        r.raise_for_status()
        return r.json().get("result", {}).get("message_id")
    except Exception as e:
        print("❌ Error enviando Telegram:", e)
        return None

def telegram_send_y_guardar(text):
    mid = telegram_send(text)
    if mid:
        data = cargar_mensajes_guardados()
        if mid not in data["otros"]:
            data["otros"].append(mid)
            guardar_mensajes_guardados(data)
    return mid

def telegram_edit(message_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText"
    data = {"chat_id": TELEGRAM_CHAT_ID, "message_id": message_id, "text": text, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, data=data)
        r.raise_for_status()
        return True
    except Exception as e:
        print("❌ Error editando Telegram:", e)
        return False

def telegram_delete(message_id):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "message_id": message_id}
    try:
        r = requests.post(url, data=data)
        r.raise_for_status()
        return True
    except Exception as e:
        print("❌ Error eliminando Telegram:", e)
        return False

async def limpiar_chat_completo():
    """Intenta eliminar todos los mensajes del chat excepto el principal"""
    data = cargar_mensajes_guardados()
    principal = data.get("principal")
    otros = data.get("otros", [])
    for msg_id in otros:
        telegram_delete(msg_id)
    data["otros"] = []
    # if principal:
    #     telegram_delete(principal)
    #     data["principal"] = None
    guardar_mensajes_guardados(data)

def actualizar_env():
    with open(".env", "w") as f:
        f.write(f"IP_DISPOSITIVOS={','.join(IP_DISPOSITIVOS)}\n")
        f.write(f"NOMBRES_DISPOSITIVOS={','.join(NOMBRES_DISPOSITIVOS)}\n")
        f.write(f"TELEGRAM_TOKEN={TELEGRAM_TOKEN}\n")
        f.write(f"TELEGRAM_CHAT_ID={TELEGRAM_CHAT_ID}\n")

async def enviar_lista_dispositivos():
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
    telegram_send_y_guardar(mensaje)

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

async def comando_arm(activar: bool):
    if blink is None:
        try:
            await conectar_blink()
        except Exception as e:
            telegram_send_y_guardar(f"❌ Error conectando Blink: {e}")
            return
    if activar:
        await activar_blink()
    else:
        await desactivar_blink()

def manejar_comando(texto, message_id):
    texto = texto.strip().lower()
    data = cargar_mensajes_guardados()
    if message_id not in data["otros"] and message_id != data.get("principal") and texto != "/refresh":
        data["otros"].append(message_id)
        guardar_mensajes_guardados(data)
    if texto == "/refresh":
        telegram_delete(message_id)
        REFRESH_SOLICITADO.set()
    elif texto == "/off":
        telegram_send_y_guardar("🛑 Bot apagado.")
        APAGAR_BOT.set()
    elif texto.startswith("/add"):
        try:
            _, ip, nombre = texto.split()
            if ip not in IP_DISPOSITIVOS:
                IP_DISPOSITIVOS.append(ip)
                NOMBRES_DISPOSITIVOS.append(nombre)
                actualizar_env()
                telegram_send_y_guardar(f"✅ Añadido: {nombre} ({ip})")
                REFRESH_SOLICITADO.set()
            else:
                telegram_send_y_guardar("⚠️ IP ya existe")
        except:
            telegram_send_y_guardar("❌ Uso: /add 192.168.1.X Nombre")
    elif texto == "/clear":
        telegram_delete(message_id)  # Borra el comando mismo
        asyncio.create_task(limpiar_chat_completo())
    elif texto.startswith("/delete"):
        try:
            _, ip_o_nombre = texto.split()
            if ip_o_nombre in IP_DISPOSITIVOS:
                idx = IP_DISPOSITIVOS.index(ip_o_nombre)
            elif ip_o_nombre in NOMBRES_DISPOSITIVOS:
                idx = NOMBRES_DISPOSITIVOS.index(ip_o_nombre)
            else:
                telegram_send_y_guardar("❌ No encontrado.")
                return
            eliminado = NOMBRES_DISPOSITIVOS[idx]
            IP_DISPOSITIVOS.pop(idx)
            NOMBRES_DISPOSITIVOS.pop(idx)
            actualizar_env()
            telegram_send_y_guardar(f"🗑️ Eliminado: {eliminado}")
            REFRESH_SOLICITADO.set()
        except:
            telegram_send_y_guardar("❌ Uso: /delete <IP | Nombre>")
    elif texto == "/list":
        asyncio.create_task(enviar_lista_dispositivos())
    elif texto.startswith("/interval"):
        try:
            _, segundos = texto.split()
            CHECK_INTERVAL = int(segundos)
            telegram_send_y_guardar(f"🕒 Intervalo actualizado a {CHECK_INTERVAL} segundos.")
        except:
            telegram_send_y_guardar("❌ Uso: /interval <segundos>")
    elif texto.startswith("/arm"):
        partes = texto.split()
        if len(partes) == 2 and partes[1] in ["true", "false"]:
            valor = partes[1] == "true"
            asyncio.create_task(comando_arm(valor))
        else:
            telegram_send_y_guardar("❌ Uso: /arm true|false")
    elif texto == "/help":
        ayuda = (
            "⚙️ *Comandos disponibles:*\n\n"
            "/refresh                            🔄 Actualiza la lista\n"
            "/list                                   📋 Lista dispositivos\n"
            "/interval <segundos>    🕒 Tiempo de refresh\n"
            "/clear                                ✨ Limpia el chat\n"
            "/add <IP> <Nombre>     🆕 Añadir dispositivo\n"
            "/delete <IP|Nombre>     🗑️ Eliminar dispositivo\n"
            "/help                                 ❓ Muestra esta ayuda\n"
            "/off                                    🛑 Apaga el bot\n"
        )
        telegram_send_y_guardar(ayuda)

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
                    manejar_comando(texto, mid)
        except Exception as e:
            print("❌ Error al recibir mensajes:", e)
        for _ in range(20):
            if APAGAR_BOT.is_set():
                return
            await asyncio.sleep(0.1)

async def enviar_estado():
    global telegram_message_id
    while not APAGAR_BOT.is_set():
        await REFRESH_SOLICITADO.wait()
        REFRESH_SOLICITADO.clear()
        estados_actuales = {}
        for ip, nombre in zip(IP_DISPOSITIVOS, NOMBRES_DISPOSITIVOS):
            conectado = await async_ping(ip.strip())
            estados_actuales[nombre.strip()] = conectado
        ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lineas = [f"*Último /refresh a las * `{ahora}`"]
        for nombre in NOMBRES_DISPOSITIVOS:
            tick = "✅" if estados_actuales.get(nombre.strip(), False) else "❌"
            lineas.append(f"{tick} {nombre.strip()}")
        lineas.append("\n/help paleta de comandos")
        texto = "\n".join(lineas)
        data = cargar_mensajes_guardados()
        if telegram_message_id is None:
            telegram_message_id = telegram_send(texto)
            if telegram_message_id:
                data["principal"] = telegram_message_id
                guardar_mensajes_guardados(data)
        else:
            exito = telegram_edit(telegram_message_id, texto)
            if not exito:
                telegram_delete(telegram_message_id)
                telegram_message_id = telegram_send(texto)
                if telegram_message_id:
                    data["principal"] = telegram_message_id
                    guardar_mensajes_guardados(data)
        for _ in range(10):
            if APAGAR_BOT.is_set():
                return
            await asyncio.sleep(0.1)

async def limpiar_mensajes_anteriores():
    data = cargar_mensajes_guardados()
    for mid in data.get("otros", []):
        telegram_delete(mid)
    if data.get("principal"):
        telegram_delete(data["principal"])
    guardar_mensajes_guardados({"principal": None, "otros": []})

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