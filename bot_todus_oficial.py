import json, re, threading, os, urllib.request, traceback
from todus import ToDusClient2, FileType

PHONE = os.getenv("TODUS_PHONE", "5350155246")
PASSWORD = os.getenv("TODUS_PASSWORD", "fake1234567890abcdef12345678")
BOT_JID = f"{PHONE}@im.todus.cu"
API_URL = "https://api.apklis.cu/v2/application/?package_name=cu.todus.android"
APK_URL = "https://s3.todus.cu/official/todus/release/rel/toDus_latest.apk"
HEADERS = {"User-Agent": "Mozilla/5.0"}

client = ToDusClient2(PHONE, PASSWORD)

def get_info():
    req = urllib.request.Request(API_URL, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    app = data["results"][0]
    last = app.get("last_release", {})
    version = last.get("version_name", "N/A")
    fecha = app.get("updated", "N/A").split("T")[0]
    tamano = f"{app.get('size', 0) / (1024*1024):.2f} MB"
    icon_url = app.get("icon", "")
    
    changelog_raw = last.get("changelog", "")
    texto_limpio = re.sub(r"<li[^>]*>", "\n", changelog_raw)
    texto_limpio = re.sub(r"<[^>]+>", "", texto_limpio)
    lineas = [l.strip() for l in texto_limpio.split("\n") if l.strip() and "toDus v" not in l and "Historial" not in l]
    changelog = "\n".join([f"• {l}" for l in lineas])

    texto = (
        f"📦 <b>toDus</b> | <code>{version}</code>\n\n"
        f"📅 <b>Actualizado:</b> {fecha}\n\n"
        f"💾 <b>Tamaño:</b> {tamano}\n\n"
        f"📝 <b>Novedades:</b>\n\n"
        f"{changelog}\n\n"
        f"🔗 <a href='https://test.apklis.cu/es/applications/cu.todus.android'>Ver en Apklis</a>"
    )
    return texto, icon_url, version

def on_message(msg):
    if msg.get("type") != "gc": return
    raw = msg.get("raw", "")
    body = (msg.get("body") or "").lower()

    if f"ui='{BOT_JID}'" in raw or f'ui="{BOT_JID}"' in raw:
        print(f"\n🔔 Mención detectada: {body}")
        def procesar():
            grupo_id = msg.get("from", "").split("@")[0]
            msg_id_original = msg.get("id") # ID del mensaje de la mención
            
            try:
                client.send_chat_state(grupo_id, "composing")
                texto, icon_url, version = get_info()

                if "apk" in body:
                    print("📦 Descargando APK (68MB)...")
                    req_apk = urllib.request.Request(APK_URL, headers=HEADERS)
                    with urllib.request.urlopen(req_apk, timeout=300) as resp, open("todus_latest.apk", 'wb') as f:
                        f.write(resp.read())
                    
                    print("☁️ Subiendo APK a ToDus...")
                    temp_client = ToDusClient2(PHONE, PASSWORD)
                    temp_client.login()
                    with open("todus_latest.apk", "rb") as f:
                        apk_final = temp_client.upload_file(f.read(), file_type=FileType.FILE, file_name=f"toDus_{version}.apk")
                    
                    print("🚀 Enviando APK al grupo (citando)...")
                    # Usamos el cliente de grupos con reply_to_id
                    client.groups.send_file(grupo_id, apk_final, f"toDus_{version}.apk", os.path.getsize("todus_latest.apk"), caption=texto, reply_to=msg_id_original)
                    os.remove("todus_latest.apk")
                    print("✅ APK enviado.")

                elif "info" in body:
                    print("🖼️ Descargando logo...")
                    req_img = urllib.request.Request(icon_url, headers=HEADERS)
                    with urllib.request.urlopen(req_img, timeout=15) as resp, open("logo_pro.png", 'wb') as f:
                        f.write(resp.read())
                    
                    print("☁️ Subiendo logo...")
                    temp_client = ToDusClient2(PHONE, PASSWORD)
                    temp_client.login()
                    with open("logo_pro.png", "rb") as f:
                        img_final = temp_client.upload_file(f.read(), file_type=FileType.PICTURE, file_name="logo_pro.png")
                    
                    print("🚀 Enviando Info al grupo (citando)...")
                    # Usamos el cliente de grupos con reply_to_id
                    client.groups.send_image(grupo_id, img_final, "logo_pro.png", os.path.getsize("logo_pro.png"), 200, 200, "L6PZfSi_.AyE_3t7t7R**0o#DgR4", texto, reply_to=msg_id_original)
                    os.remove("logo_pro.png")
                    print("✅ Info enviada.")
                    
            except Exception as e:
                print("\n❌ ERROR DETALLADO:")
                traceback.print_exc()
        threading.Thread(target=procesar, daemon=True).start()

client.login()
print(f"✅ Bot conectado y escuchando menciones de {BOT_JID}...")
try:
    client.listen_messages(on_message)
except TypeError:
    client.listen_messages(client.token, on_message)
