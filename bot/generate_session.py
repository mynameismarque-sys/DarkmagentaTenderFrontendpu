"""
Script para generar una sesión limpia de Telegram con el proxy.

Pasos:
  1. Hace login fresco (sin sesión previa) usando el proxy configurado
  2. Termina TODAS las otras sesiones activas (elimina el AuthKeyDuplicatedError)
  3. Imprime el string de sesión para pegar en el secret TELEGRAM_SESSION

Cómo usarlo:
  cd /home/runner/workspace
  python bot/generate_session.py

Después de completarlo, copiá el string que aparece al final
y pegalo en el secret TELEGRAM_SESSION del proyecto.
"""

import asyncio
import os
import sys

# Agregar el directorio raíz al path para poder importar desde bot/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import socks
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError
    from telethon.sessions import StringSession
    from telethon.tl.functions.auth import ResetAuthorizationsRequest
except ImportError as e:
    print(f"ERROR: Falta una dependencia: {e}")
    print("Corré: pip install telethon pysocks")
    sys.exit(1)


def _get_env(key: str, required: bool = True) -> str:
    val = os.environ.get(key, "").strip()
    if required and not val:
        print(f"ERROR: falta la variable de entorno {key!r}")
        sys.exit(1)
    return val


def _build_proxy() -> tuple | None:
    ip   = _get_env("PROXY_IP",   required=False) or "108.181.215.247"
    port = _get_env("PROXY_PORT", required=False) or "10065"
    user = _get_env("PROXY_USER", required=False) or "DGZADAXFF"
    pwd  = _get_env("PROXY_PASS", required=False)

    if not pwd:
        print("⚠  PROXY_PASS no está configurado — conectando sin proxy.")
        return None

    try:
        port_int = int(port)
    except ValueError:
        print(f"ERROR: PROXY_PORT inválido ({port!r})")
        sys.exit(1)

    proxy_type_str = _get_env("PROXY_TYPE", required=False).upper() or "HTTP"
    proxy_type = socks.SOCKS5 if proxy_type_str == "SOCKS5" else socks.HTTP
    type_name  = "SOCKS5" if proxy_type == socks.SOCKS5 else "HTTP"
    print(f"   Proxy: {type_name} → {ip}:{port_int} (user={user})")
    return (proxy_type, ip, port_int, True, user, pwd)


async def main() -> None:
    api_id   = int(_get_env("TELEGRAM_API_ID"))
    api_hash = _get_env("TELEGRAM_API_HASH")
    proxy    = _build_proxy()

    print("\n=== GENERADOR DE SESIÓN TELEGRAM ===\n")

    phone = input("Número de teléfono (con código de país, ej: +5491112345678): ").strip()
    if not phone:
        print("ERROR: número vacío.")
        sys.exit(1)

    client = TelegramClient(
        StringSession(),
        api_id,
        api_hash,
        proxy=proxy,
    )

    await client.connect()
    print(f"\nConectado a Telegram. Enviando código a {phone}...")

    result = await client.send_code_request(phone)
    phone_code_hash = result.phone_code_hash

    code = input("Código recibido por SMS/Telegram: ").strip().replace(" ", "")
    if not code:
        print("ERROR: código vacío.")
        await client.disconnect()
        sys.exit(1)

    try:
        await client.sign_in(
            phone=phone,
            code=code,
            phone_code_hash=phone_code_hash,
        )
    except SessionPasswordNeededError:
        password = input("Contraseña 2FA: ").strip()
        if not password:
            print("ERROR: contraseña vacía.")
            await client.disconnect()
            sys.exit(1)
        try:
            await client.sign_in(password=password)
        except Exception as e:
            print(f"ERROR en 2FA: {e}")
            await client.disconnect()
            sys.exit(1)
    except PhoneCodeInvalidError:
        print("ERROR: código incorrecto. Volvé a ejecutar el script.")
        await client.disconnect()
        sys.exit(1)
    except Exception as e:
        print(f"ERROR al verificar código: {e}")
        await client.disconnect()
        sys.exit(1)

    me = await client.get_me()
    print(f"\n✅ Login exitoso como @{me.username} ({me.first_name})")

    print("\nTerminando todas las otras sesiones activas...")
    try:
        await client(ResetAuthorizationsRequest())
        print("✅ Todas las otras sesiones terminadas.")
    except Exception as e:
        print(f"⚠  No se pudieron terminar otras sesiones: {e}")

    session_string = client.session.save()
    await client.disconnect()

    print("\n" + "=" * 60)
    print("STRING DE SESIÓN (copiá todo lo de abajo):")
    print("=" * 60)
    print(session_string)
    print("=" * 60)
    print()
    print("Pasos siguientes:")
    print("  1. Copiá el string de arriba")
    print("  2. En Replit → Secrets → TELEGRAM_SESSION → pegá el string")
    print("  3. Hacé un nuevo deployment de producción")
    print("  4. Reiniciá el workflow de desarrollo")
    print()


if __name__ == "__main__":
    asyncio.run(main())
