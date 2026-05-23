#!/usr/bin/env python3
"""
Generador de sesión Telethon — inicio de sesión desde cero.

Ejecutar desde la raíz del proyecto:
    python tools/gen_telegram_session.py

La conexión se realiza SIEMPRE a través del proxy fijo configurado en los
secrets PROXY_IP / PROXY_PORT / PROXY_USER / PROXY_PASS, de modo que la
sesión queda vinculada a la IP del proxy y no a la IP dinámica de Replit.

Al finalizar guarda el string en:
  - Consola (para copiarlo directamente)
  - telegram_session.txt (para copiarlo sin problemas de wrap de terminal)

Copiá el contenido de telegram_session.txt y guardalo como
secret TELEGRAM_SESSION en Replit Secrets.
"""
import asyncio
import os
import sys

try:
    import socks
except ImportError:
    print("ERROR: PySocks no está instalado. Ejecutá: uv pip install PySocks")
    sys.exit(1)

try:
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError
    from telethon.sessions import StringSession
except ImportError:
    print("ERROR: telethon no está instalado.")
    sys.exit(1)

OUTPUT_FILE = "telegram_session.txt"


def _build_proxy() -> tuple | None:
    """
    Construye la tupla de proxy a partir de los secrets de Replit.
    Devuelve None si algún secret falta (el script sigue sin proxy pero avisa).

    PROXY_TYPE (opcional, default SOCKS5): puede ser HTTP o SOCKS5.
    """
    ip   = os.environ.get("PROXY_IP", "").strip()
    port = os.environ.get("PROXY_PORT", "").strip()
    user = os.environ.get("PROXY_USER", "").strip()
    pwd  = os.environ.get("PROXY_PASS", "").strip()

    if not all([ip, port, user, pwd]):
        print("⚠️  ADVERTENCIA: Faltan secrets de proxy (PROXY_IP/PORT/USER/PASS).")
        print("    La sesión se generará SIN proxy — puede causar conflictos de IP.")
        return None

    proxy_type_str = os.environ.get("PROXY_TYPE", "HTTP").strip().upper()
    proxy_type = socks.SOCKS5 if proxy_type_str == "SOCKS5" else socks.HTTP

    try:
        port_int = int(port)
    except ValueError:
        print(f"ERROR: PROXY_PORT debe ser un número, recibí: {port!r}")
        sys.exit(1)

    return (proxy_type, ip, port_int, True, user, pwd)


async def main() -> None:
    print("=" * 60)
    print("  GENERADOR DE SESIÓN TELETHON — inicio limpio + proxy")
    print("=" * 60)

    api_id_str = os.environ.get("TELEGRAM_API_ID", "").strip()
    api_hash   = os.environ.get("TELEGRAM_API_HASH", "").strip()

    if not api_id_str:
        api_id_str = input("Ingresá tu TELEGRAM_API_ID: ").strip()
    if not api_hash:
        api_hash = input("Ingresá tu TELEGRAM_API_HASH: ").strip()

    if not api_id_str or not api_hash:
        print("ERROR: API_ID y API_HASH son obligatorios.")
        sys.exit(1)

    try:
        api_id = int(api_id_str)
    except ValueError:
        print(f"ERROR: API_ID debe ser un número, recibí: {api_id_str!r}")
        sys.exit(1)

    proxy = _build_proxy()
    if proxy:
        proxy_type_name = "SOCKS5" if proxy[0] == socks.SOCKS5 else "HTTP"
        print(f"\nProxy configurado: {proxy_type_name} → {proxy[1]}:{proxy[2]} (usuario: {proxy[4]})")
    print(f"Usando API_ID={api_id}")
    print("Creando sesión NUEVA desde cero (ignorando sesión anterior)...\n")

    # StringSession() vacío = sesión completamente nueva, sin reutilizar auth key vieja
    client = TelegramClient(StringSession(), api_id, api_hash, proxy=proxy)

    await client.connect()

    if not await client.is_user_authorized():
        phone = input("Número de teléfono (ej: +549...): ").strip()
        await client.send_code_request(phone)
        code = input("Código de verificación que llegó por Telegram: ").strip()
        try:
            await client.sign_in(phone, code)
        except SessionPasswordNeededError:
            password = input("Contraseña 2FA: ").strip()
            await client.sign_in(password=password)

    me = await client.get_me()
    print(f"\n✅ Autenticado como: {me.first_name} (id={me.id})")

    session_string = client.session.save()
    await client.disconnect()

    # Guardar en archivo para evitar problemas de wrap/encoding del terminal
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(session_string)

    print("\n" + "=" * 60)
    print("SESSION STRING (copialo completo, una sola línea):")
    print("=" * 60)
    print(session_string)
    print("=" * 60)
    print(f"\n✅ También guardado en: {OUTPUT_FILE}")
    print("   Abrí ese archivo y copiá todo su contenido.")
    print("\nPasos finales:")
    print("  1. Copiá el contenido de telegram_session.txt")
    print("  2. Guardalo como secret TELEGRAM_SESSION en Replit")
    print("  3. Reiniciá el bot")


if __name__ == "__main__":
    asyncio.run(main())
