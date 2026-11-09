import json
import re
import threading
import os
import urllib.request
from todus import ToDusClient2, FileType

PHONE = os.getenv("TODUS_PHONE", "5350155246")
PASSWORD = os.getenv("TODUS_PASSWORD", "fake1234567890abcdef12345678")
TODUS_ID = "toDus"
API_URL = "https://api.apklis.cu/v2/application/?package_name=cu.todus.android"
APK_URL = "https://s3.todus.cu/official/todus/release/rel/toDus_latest.apk"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

client = ToDusClient2(PHONE, PASSWORD)

def descargar_archivo(url, path):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=120) as resp, open(path, 'wb') as f:
        f.write(resp.read())
    return path

def obtener_info():
    req = urllib.request.Request(API_URL, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    
    app = data["results"][0]
    last = app.get("last_release", {})
    nombre = app.get("name", "toDus")
    version = last.get("version_name", "N/A")
    fecha = app.get("updated", "N/A").split("T")[0]
    tamano = f"{app.get('size', 0) / (1024*1024):.2f} MB"
    icon_url = app.get("icon", "")
    
    changelog_raw = last.get("changelog", "")
    texto_limpio = re.sub(r"<li[^>]*>", "\n- ", changelog_raw)
    texto_limpio = re.sub(r"<[^>]+>", "", texto_limpio)
    lineas = [l.strip() for l in texto_limpio.split("\n") if l.strip() and "toDus v" not in l and "Historial" not in l]
    changelog = "\n".join(lineas) if lineas else "No disponible."
    
    texto_info = (
        f"📱 *{nombre}*\n"
        f"🏷️ *Versión:* {version}\n"
        f"📅 *Actualizado:* {fecha}\n"
        f"💾 *Tamaño:* {tamano}\n\n"
        f"📝 *Novedades:*\n{changelog}"
    )
    return texto_info, icon_url, version

def on_message(msg):
    if msg.get("type") != "gc": return
    raw = msg.get("raw", "")
    body = (msg.get("body") or "").lower()
    
    if f"ui='{TODUS_ID}'" in raw or f'ui="{TODUS_ID}"' in raw:
        print(f"\n🔔 Mención detectada con comando: {body}")
        def procesar():
            grupo_id = msg.get("from", "").split("@")[0]
            try:
                client.send_chat_state(grupo_id, "composing")
                texto, icon_url, version = obtener_info()
                
                if "apk" in body:
                    print(f"📦 Enviando APK...")
                    descargar_archivo(APK_URL, "todus_latest.apk")
                    apk_final = client.upload_file(open("todus_latest.apk", 'rb').read(), file_type=FileType.FILE, file_name=f"toDus_{version}.apk")
                    client.send_file_message(grupo_id, apk_final, FileType.FILE, caption=texto, file_name=f"toDus_{version}.apk", file_size=os.path.getsize("todus_latest.apk"))
                    os.remove("todus_latest.apk")
                    print("✅ APK enviado.")
                elif "info" in body:
                    print("🖼️ Enviando Logo + Info...")
                    if icon_url:
                        descargar_archivo(icon_url, "logo.png")
                        img_final = client.upload_file(open("logo.png", 'rb').read(), file_type=FileType.PICTURE, file_name="logo.png")
                        client.send_image_message(grupo_id, img_final, "logo.png", os.path.getsize("logo.png"), caption=texto)
                        os.remove("logo.png")
                    else:
                        client.send_message(grupo_id, texto)
                    print("✅ Info enviada.")
            except Exception as e:
                print(f"❌ Error: {e}")
        threading.Thread(target=procesar, daemon=True).start()

client.login()
print(f"✅ Bot @{TODUS_ID} conectado y escuchando...")
client.listen_messages(on_message)
