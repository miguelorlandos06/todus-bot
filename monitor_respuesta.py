import os
import time
from todus import ToDusClient2, util

PHONE = "5350155246"
PASSWORD = "fake1234567890abcdef12345678"

client = ToDusClient2(PHONE, PASSWORD)
client.login()
print(f"✅ Monitor conectado como {PHONE}")
print("👀 Escuchando TODO el tráfico (Ctrl+C para salir)...\n")

def on_message(msg):
    raw = msg.get("raw", "")
    
    # Ignorar pings/presencias vacías
    if not raw or raw.strip() == " " or ("<p " in raw and "status>Online" in raw):
        return

    timestamp = time.strftime("%H:%M:%S")
    msg_type = msg.get("type", "desconocido")
    
    print(f"\n{'='*60}")
    print(f"⏰ [{timestamp}] TIPO: {msg_type.upper()}")
    print(f"{'='*60}")
    print("--- XML CRUDO ---")
    print(raw)
    print("--- CAMPOS PARSEADOS ---")
    for key, value in msg.items():
        if key == "raw": continue
        if value not in ("", 0, 0.0, False, None, []):
            print(f"  {key}: {value!r}")
    print(f"{'='*60}\n")

try:
    client.listen_messages(on_message)
except TypeError:
    client.listen_messages(client.token, on_message)
except KeyboardInterrupt:
    print("\n🛑 Monitor detenido.")
