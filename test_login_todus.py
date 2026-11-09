import os, urllib.request, json
from todus import ToDusClient2

# Forzamos el "teléfono" a toDus
PHONE = "toDus" 
PASSWORD = "fake1234567890abcdef12345678"

print(f"Intentando login con el identificador: {PHONE}")
try:
    client = ToDusClient2(PHONE, PASSWORD)
    client.login()
    print("🎉 ¡El servidor aceptó el login con 'toDus'!")
    print("Token:", client.token[:50])
except Exception as e:
    print(f"❌ El servidor rechazó el login (como era de esperar): {e}")
    print("La API de ToDus exige un número de teléfono válido para autenticar.")
