"""Automatización con Playwright para registrar/cambiar IPs en el panel."""
import asyncio
import json
import logging
import os
import re
import urllib.request
import urllib.parse
from dataclasses import dataclass

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

log = logging.getLogger(__name__)

DASHBOARD_URL = "https://dashboard-ditxdev.web.app/admin.html"
RESELLER       = "Marke"
USUARIO_FIJO   = "Marke"
GESTOR         = "Marke"

SERVICIO_DEFAULT = "aimdrag_ffth"

HORAS_3_SENTINEL = 0  # dias=0 significa "3 horas"

SERVICIOS = {
    "aimdrag_ffth": {
        "label": "Aimdrag FFTH",
        "duracion_select": "#aimdrag_duration",
        "duraciones": {0: "3h", 1: "1d", 7: "7", 15: "15", 30: "30"},
    },
    "unlock_120fps": {
        "label": "Unlock 120FPS",
        "duracion_select": "#unlock_duration",
        "duraciones": {7: "7", 15: "15", 30: "30"},
    },
    "bypass_login": {
        "label": "Bypass Login",
        "duracion_select": "#bypass_duration",
        "duraciones": {7: "7", 15: "15", 30: "30"},
    },
}

_BROWSER_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
]

# Librerías del sistema que pueden fallar en producción cuando el Nix store
# tiene errores de I/O. Las precargamos vía LD_PRELOAD para que el linker
# las encuentre antes de intentar abrir los paths /nix/store/...
_PRELOAD_LIBS = [
    # ── GLib/GIO — Ubuntu 22.04 (Nix roto en producción) ────────────────────
    # Orden crítico: primero las transitive deps, luego los consumers
    "libpcre.so.3",        # dep de libglib-2.0.so.0
    "libpcre2-8.so.0",     # dep de libselinux.so.1
    "libselinux.so.1",     # dep de libgio-2.0.so.0
    "libffi.so.8",         # dep de libgobject-2.0.so.0
    "libglib-2.0.so.0",
    "libgobject-2.0.so.0",
    "libgmodule-2.0.so.0",
    "libgio-2.0.so.0",
    # ATK / accessibility (at-spi2-core — Nix roto en producción)
    "libatk-1.0.so.0",
    "libatk-bridge-2.0.so.0",
    "libatspi.so.0",
    # X11 stack completo (libx11 y familia — Nix roto en producción)
    "libX11.so.6",
    "libXcomposite.so.1",
    "libXdamage.so.1",
    "libXext.so.6",
    "libXfixes.so.3",
    "libXrender.so.1",     # dep de libXrandr.so.2
    "libXrandr.so.2",
    # XCB + transitive deps de X11/XCB
    "libxcb.so.1",
    "libXau.so.6",
    "libXdmcp.so.6",
    # XKB
    "libxkbcommon.so.0",
    # GBM / Mesa + Wayland (dep transitiva de libgbm en Ubuntu 22.04)
    "libbsd.so.0",             # dep transitiva de libwayland-server
    "libwayland-server.so.0",  # dep de libgbm.so.1
    "libXi.so.6",              # dep de libXext.so.6 / input events
    "libgbm.so.1",
    # Audio
    "libasound.so.2",
    # udev (solo necesita libc — seguro)
    "libudev.so.1",
    # Expat (solo necesita libc — seguro)
    "libexpat.so.1",
    # NSPR / NSS (se auto-satisfacen entre sí + libc)
    "libnspr4.so",
    "libplc4.so",
    "libplds4.so",
    "libnss3.so",
    "libnssutil3.so",
    "libsmime3.so",
    "libssl3.so",
    # libdbus-1.so.3 NO se preloada: necesita libsystemd.so.0 (cadena muy pesada).
    # Chrome la encuentra desde Nix via ld.so.cache (paquete dbus no está roto).
]
_LIB_SEARCH_DIRS = [
    # Libs bakeadas en el repo (siempre disponibles, sin red) — MÁXIMA prioridad.
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "libs"),
    # Copia en /tmp (copiada por start.sh desde libs/)
    "/tmp/pw_libs",
    "/usr/lib/x86_64-linux-gnu",
    "/usr/lib",
    "/lib/x86_64-linux-gnu",
    "/lib",
]

# Ruta caché del libmount compatible descargado
_LIBMOUNT_COMPAT_PATH = "/tmp/pw_libmount_compat.so"
# También buscar libmount bakeado en el repo
_LIBMOUNT_REPO_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "libs", "libmount.so.1"
)
_libmount_cache: str = ""   # "" = sin inicializar; "none" = no encontrado


def _find_compat_libmount() -> str | None:
    """Devuelve path a libmount.so.1 con símbolo MOUNT_2_40.

    Prioridad:
      1. libs/ bakeado en el repo (siempre presente, sin red)
      2. Caché /tmp preparada por start.sh
      3. Búsqueda rápida en el sistema
    """
    global _libmount_cache
    if _libmount_cache == "none":
        return None
    if _libmount_cache:
        return _libmount_cache

    # 1. Lib bakeada en el repo (fuente más fiable)
    if os.path.exists(_LIBMOUNT_REPO_PATH):
        _libmount_cache = _LIBMOUNT_REPO_PATH
        log.debug("libmount desde repo: %s", _LIBMOUNT_REPO_PATH)
        return _LIBMOUNT_REPO_PATH

    # 2. Caché preparada por start.sh en /tmp
    if os.path.exists(_LIBMOUNT_COMPAT_PATH):
        _libmount_cache = _LIBMOUNT_COMPAT_PATH
        log.debug("libmount compat desde /tmp caché: %s", _LIBMOUNT_COMPAT_PATH)
        return _LIBMOUNT_COMPAT_PATH

    # 3. Búsqueda rápida en el sistema
    candidates: list[str] = [
        "/lib/x86_64-linux-gnu/libmount.so.1",
        "/usr/lib/x86_64-linux-gnu/libmount.so.1",
        "/lib/libmount.so.1",
        "/usr/lib/libmount.so.1",
    ]
    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            import subprocess as _sp
            r = _sp.run(["grep", "-c", "MOUNT_2_40", path],
                        capture_output=True, timeout=3)
            if r.returncode == 0:
                log.info("libmount con MOUNT_2_40 en sistema: %s", path)
                _libmount_cache = path
                return path
        except Exception:
            pass

    log.debug("No hay libmount con MOUNT_2_40 disponible")
    _libmount_cache = "none"
    return None


def _get_chromium_env() -> dict:
    """Entorno para el subproceso de chromium con libs del sistema precargadas.

    Estrategia:
    - LD_LIBRARY_PATH: dirs del sistema antes que Nix
    - LD_PRELOAD: libs críticas para evitar I/O error en /nix/store en producción
    - libmount compatible (con MOUNT_2_40) precargado para evitar el mismatch
      entre libgio (Ubuntu 24.04) y libmount (Ubuntu 22.04)
    """
    env = dict(os.environ)

    # ── LD_LIBRARY_PATH: agrega dirs del sistema al inicio ──────────────────
    sys_lib_dirs = ":".join(_LIB_SEARCH_DIRS)
    existing_llp = env.get("LD_LIBRARY_PATH", "")
    env["LD_LIBRARY_PATH"] = sys_lib_dirs + (":" + existing_llp if existing_llp else "")

    # ── LD_PRELOAD: precarga las libs críticas ───────────────────────────────
    preload: list[str] = []
    seen: set[str] = set()

    # 1. Libs estándar del _PRELOAD_LIBS (nspr4, nss3, atk, etc.)
    for lib in _PRELOAD_LIBS:
        if lib in seen:
            continue
        for d in _LIB_SEARCH_DIRS:
            full = os.path.join(d, lib)
            if os.path.exists(full):
                preload.append(full)
                seen.add(lib)
                break

    # 2. libmount compatible con MOUNT_2_40 (evita mismatch libgio↔libmount)
    compat_libmount = _find_compat_libmount()
    if compat_libmount and compat_libmount not in preload:
        preload.insert(0, compat_libmount)   # Va primero para que el linker lo encuentre
        log.debug("LD_PRELOAD libmount compat: %s", compat_libmount)

    if preload:
        existing_lp = env.get("LD_PRELOAD", "")
        env["LD_PRELOAD"] = ":".join(preload) + (":" + existing_lp if existing_lp else "")
        log.debug("LD_PRELOAD para chromium: %s", env["LD_PRELOAD"])

    log.debug("LD_LIBRARY_PATH para chromium: %s", env["LD_LIBRARY_PATH"])
    return env


@dataclass
class RegistroResultado:
    ok: bool
    mensaje: str
    username: str | None = None
    password: str | None = None


def _resolver_duracion(servicio: str, dias: int) -> str:
    cfg = SERVICIOS[servicio]
    duraciones = cfg["duraciones"]
    if dias in duraciones:
        return duraciones[dias]
    disponibles = sorted(duraciones.keys())
    cercano = min(disponibles, key=lambda d: abs(d - dias))
    log.warning("Días=%s no soportado por %s; uso el más cercano: %s", dias, servicio, cercano)
    return duraciones[cercano]


# Opciones del panel por servicio: (umbral_horas, valor_select) ordenadas de mayor a menor
_OPCIONES_PANEL = {
    "aimdrag_ffth":  [(720, "30"), (360, "15"), (168, "7"), (24, "1d"), (3, "3h")],
    "unlock_120fps": [(720, "30"), (360, "15"), (168, "7")],
    "bypass_login":  [(720, "30"), (360, "15"), (168, "7")],
}


def _horas_a_opcion_panel(servicio: str, horas: float) -> str:
    """
    Mapea las horas restantes exactas al valor del select del panel
    sin superar el tiempo que le queda al usuario.
    Ej: 358.5h → '15' (360h) quedaría corto → '7' (168h) es el mayor ≤ 358.5h
    """
    opciones = _OPCIONES_PANEL.get(servicio, _OPCIONES_PANEL["aimdrag_ffth"])
    for umbral, valor in opciones:  # ordenadas de mayor a menor
        if horas >= umbral:
            return valor
    # Menos del mínimo → usar el menor disponible
    return opciones[-1][1]


# ---------------------------------------------------------------------------
# Función interna compartida: abre el panel y registra una IP
# ---------------------------------------------------------------------------
async def _abrir_panel_y_registrar(
    page,
    ip: str,
    dias: int,
    reseller: str,
    servicio: str,
    duracion_override: str | None = None,
) -> RegistroResultado:
    """Navega al dashboard y ejecuta el flujo Add IP.

    Si se pasa ``duracion_override``, se usa directamente ese valor en el
    select de duración en lugar de resolverlo desde ``dias``.
    """
    log.info("Navegando a %s", DASHBOARD_URL)
    await page.goto(DASHBOARD_URL, wait_until="networkidle", timeout=45_000)

    # 1) Abrir modal "Add IP"
    try:
        await page.get_by_text("Add IP", exact=False).first.click(timeout=8_000)
    except PlaywrightTimeoutError:
        await page.locator("button:has-text('Add IP')").first.click(timeout=5_000)

    # 2) Esperar el formulario
    await page.locator("#ipAddress").wait_for(state="visible", timeout=10_000)

    # 3) IP
    await page.locator("#ipAddress").fill(ip)

    # 4) Reseller
    reseller_loc = page.locator("#resellerName")
    await reseller_loc.fill("")
    await reseller_loc.fill(reseller)

    if servicio not in SERVICIOS:
        raise ValueError(f"Servicio desconocido: {servicio}")
    cfg = SERVICIOS[servicio]

    # 5) Checkbox del servicio
    checkbox = page.locator(f"input[type='checkbox'][value='{servicio}']")
    if not await checkbox.is_checked():
        await checkbox.check()

    # 6) Seleccionar duración (dispatch 'change' para que el JS recalcule)
    duracion_value = duracion_override if duracion_override else _resolver_duracion(servicio, dias)
    duracion_loc = page.locator(cfg["duracion_select"])
    await duracion_loc.wait_for(state="visible", timeout=10_000)
    await duracion_loc.select_option(value=duracion_value)
    await duracion_loc.dispatch_event("change")
    await page.wait_for_timeout(1_000)

    # 7) Submit
    await page.locator("#submitBtn").click(timeout=8_000)

    # 8) Leer credenciales generadas
    return await _leer_resultado(page, ip, dias, servicio, duracion_value)


# ---------------------------------------------------------------------------
# registrar_ip — comando /registrar (usa usuario fijo "Marke")
# ---------------------------------------------------------------------------
async def registrar_ip(
    ip: str,
    dias: int,
    usuario: str,
    servicio: str = SERVICIO_DEFAULT,
) -> RegistroResultado:
    """Registra una IP en el panel con el reseller fijo 'Marke'."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=_BROWSER_ARGS, env=_get_chromium_env())
        try:
            context = await browser.new_context()
            page    = await context.new_page()
            return await _abrir_panel_y_registrar(
                page, ip=ip, dias=dias, reseller=RESELLER, servicio=servicio
            )
        except Exception as e:  # noqa: BLE001
            log.exception("Error en automatización Playwright (registrar_ip)")
            return RegistroResultado(ok=False, mensaje=f"Error: {e}")
        finally:
            await browser.close()


# ---------------------------------------------------------------------------
# Firebase RTDB helpers — usados por cambiar_ip
# ---------------------------------------------------------------------------
_FB_API_KEY = "AIzaSyDlWEtmZD2KRektDH2ZPmWHpBqtaMivnRQ"
_FB_EMAIL   = "dinotricka@gmail.com"
_FB_PASS    = "advanced123"
_FB_DB_URL  = "https://proxy-ips-ecd93-default-rtdb.firebaseio.com"

_fb_token: str | None = None


def _fb_login() -> str:
    """Obtiene un idToken de Firebase Auth (con caché simple)."""
    global _fb_token
    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={_FB_API_KEY}"
    payload = json.dumps({"email": _FB_EMAIL, "password": _FB_PASS, "returnSecureToken": True}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        _fb_token = json.loads(resp.read())["idToken"]
    return _fb_token


def _fb_get(path: str, token: str) -> dict | None:
    url = f"{_FB_DB_URL}/{path}.json?auth={token}"
    with urllib.request.urlopen(url, timeout=15) as resp:
        return json.loads(resp.read())


def _fb_put(path: str, token: str, data: dict) -> None:
    url = f"{_FB_DB_URL}/{path}.json?auth={token}"
    payload = json.dumps(data).encode()
    req = urllib.request.Request(url, data=payload, method="PUT",
                                  headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def _fb_delete(path: str, token: str) -> None:
    url = f"{_FB_DB_URL}/{path}.json?auth={token}"
    req = urllib.request.Request(url, method="DELETE")
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def _ip_to_key(ip: str) -> str:
    """Convierte IP a clave Firebase: '190.107.246.13' → '190,107,246,13'"""
    return ip.replace(".", ",")


def _fb_buscar_ip_por_credenciales(token: str, usuario: str, password: str) -> tuple[str | None, dict | None]:
    """
    Busca en Firebase la entrada cuyo bot.username == usuario y bot.password == password.
    Devuelve (ip_key, data) o (None, None) si no encuentra.
    """
    try:
        all_ips = _fb_get("ips_autorizadas", token)
        if not all_ips:
            return None, None
        for key, data in all_ips.items():
            if not isinstance(data, dict):
                continue
            bot = data.get("bot", {})
            if (bot.get("username") == usuario and bot.get("password") == password):
                return key, data
        return None, None
    except Exception:
        log.exception("Error buscando IP por credenciales en Firebase")
        return None, None


# ---------------------------------------------------------------------------
# cambiar_ip — comando /cambiar-ip  (vía Firebase, igual que el bot de Telegram)
# ---------------------------------------------------------------------------
async def cambiar_ip(
    usuario: str,
    password: str,
    nueva_ip: str,
    horas_restantes: float = 3.0,   # solo usado como fallback si no hay expiredAt
    servicio: str = SERVICIO_DEFAULT,
) -> tuple["RegistroResultado", float]:
    """
    Cambia la IP en Firebase RTDB:
      1. Busca la entrada actual por credenciales (usuario/password del bot proxy)
      2. Copia el expiredAt EXACTO de la IP vieja
      3. Borra la entrada vieja
      4. Crea la nueva con el mismo expiredAt (sin sumar ningún tiempo)

    Devuelve (resultado, horas_reales_restantes).
    """
    def _run() -> tuple[RegistroResultado, float]:
        try:
            token = _fb_login()
        except Exception as e:
            return RegistroResultado(ok=False, mensaje=f"Error autenticando en Firebase: {e}"), horas_restantes

        # 1. Buscar entrada vieja por credenciales
        ip_vieja_key, data_vieja = _fb_buscar_ip_por_credenciales(token, usuario, password)

        if not ip_vieja_key or not data_vieja:
            log.warning(
                "cambiar_ip: no encontré entrada en Firebase para usuario=%s — "
                "puede que las credenciales sean incorrectas o no esté registrado.",
                usuario,
            )
            return (
                RegistroResultado(
                    ok=False,
                    mensaje=(
                        "No encontré tu IP registrada en el panel con esas credenciales.\n"
                        "Verificá que el usuario y contraseña sean exactamente los que "
                        "recibiste al registrar tu IP."
                    ),
                ),
                horas_restantes,
            )

        # 2. Extraer el expiredAt exacto (timestamp en ms) de la licencia
        import time as _time
        licencias_viejas = data_vieja.get("licenses", {})
        servicio_data = licencias_viejas.get(servicio, {})
        expired_at_ms = servicio_data.get("expiredAt")
        duration_str  = servicio_data.get("duration", "?")

        now_ms = int(_time.time() * 1000)
        if expired_at_ms:
            ms_restantes = expired_at_ms - now_ms
            horas_reales = ms_restantes / 3_600_000
        else:
            # Fallback: no tenía expiredAt (ej: 1use) — usar horas_restantes del argumento
            horas_reales = horas_restantes
            expired_at_ms = now_ms + int(horas_restantes * 3_600_000)

        log.info(
            "cambiar_ip: ip_vieja=%s usuario=%s → nueva_ip=%s | expiredAt=%s horas_reales=%.2f",
            ip_vieja_key.replace(",", "."), usuario, nueva_ip, expired_at_ms, horas_reales,
        )

        # 3. Construir nueva entrada conservando TODO excepto la IP (la clave)
        nueva_clave = _ip_to_key(nueva_ip)
        nueva_data = {
            "reseller": data_vieja.get("reseller", RESELLER),
            "createdAt": data_vieja.get("createdAt", now_ms),
            "bot": data_vieja.get("bot", {"username": usuario, "password": password}),
            "licenses": {
                servicio: {
                    "expiredAt": expired_at_ms,
                    "duration": duration_str,
                }
            },
        }
        # Conservar otras licencias que pueda tener (unlock_120fps, bypass_login, etc.)
        for lic_key, lic_val in licencias_viejas.items():
            if lic_key != servicio:
                nueva_data["licenses"][lic_key] = lic_val

        # 4. Escribir nueva entrada y borrar la vieja (orden: escribir primero para no perder datos)
        try:
            _fb_put(f"ips_autorizadas/{nueva_clave}", token, nueva_data)
        except Exception as e:
            return RegistroResultado(ok=False, mensaje=f"Error creando nueva IP en Firebase: {e}"), horas_reales

        try:
            _fb_delete(f"ips_autorizadas/{ip_vieja_key}", token)
        except Exception as e:
            log.error("cambiar_ip: nueva IP creada pero no pude borrar la vieja (%s): %s", ip_vieja_key, e)
            # No es fatal — la nueva ya funciona; la vieja expirará sola

        bot_info = nueva_data.get("bot", {})
        return (
            RegistroResultado(
                ok=True,
                mensaje=(
                    f"IP cambiada de {ip_vieja_key.replace(',', '.')} → {nueva_ip}.\n"
                    f"El vencimiento exacto se mantuvo ({horas_reales:.1f}h restantes).\n"
                    f"USERNAME: {bot_info.get('username', usuario)}\n"
                    f"PASSWORD: {bot_info.get('password', password)}"
                ),
                username=bot_info.get("username", usuario),
                password=bot_info.get("password", password),
            ),
            horas_reales,
        )

    return await asyncio.to_thread(_run)


# ---------------------------------------------------------------------------
# get_public_ip — comando /mi-ip
# ---------------------------------------------------------------------------
async def get_public_ip() -> str | None:
    """
    Extrae la IP pública desde ipleak.net (sección 'Your IP addresses').
    Usa la API JSON de ipleak.net para obtener la IPv4 directamente.
    """
    import urllib.request
    import json as _json

    def _fetch() -> str | None:
        try:
            req = urllib.request.Request(
                "https://ipleak.net/json/",
                headers={"User-Agent": "Mozilla/5.0"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = _json.loads(resp.read().decode())
                return data.get("ip") or data.get("query")
        except Exception:  # noqa: BLE001
            return None

    # Intentar primero con la API JSON (rápido, sin Playwright)
    ip = await asyncio.to_thread(_fetch)
    if ip and re.match(r"\d{1,3}(\.\d{1,3}){3}", ip):
        log.info("IP pública obtenida desde ipleak.net/json: %s", ip)
        return ip

    # Fallback: Playwright carga la página completa y espera el div de la IP
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=_BROWSER_ARGS, env=_get_chromium_env())
        try:
            context = await browser.new_context()
            page    = await context.new_page()
            await page.goto("https://ipleak.net/", wait_until="networkidle", timeout=30_000)

            ip = None
            # ipleak.net muestra la IP en un div oscuro dentro de #section_ipv4 o similar
            for selector in ["#section_ipv4 .ipv4", "#ip", ".ip", "h2"]:
                try:
                    el = page.locator(selector).first
                    await el.wait_for(state="visible", timeout=6_000)
                    text = (await el.inner_text()).strip()
                    if re.match(r"\d{1,3}(\.\d{1,3}){3}", text):
                        ip = text
                        break
                except Exception:  # noqa: BLE001
                    continue

            # Último recurso: buscar el patrón IP en el body
            if not ip:
                body = await page.inner_text("body", timeout=5_000)
                match = re.search(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", body)
                if match:
                    ip = match.group(1)

            log.info("IP pública obtenida desde ipleak.net (Playwright): %s", ip)
            return ip
        except Exception as e:  # noqa: BLE001
            log.exception("Error obteniendo IP pública con Playwright: %s", e)
            return None
        finally:
            await browser.close()


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------
async def _leer_resultado(
    page, ip: str, dias: int, servicio: str, duracion_value: str
) -> RegistroResultado:
    """Espera la confirmación del panel y devuelve el resultado."""
    contenedor = page.locator("#generatedIpContainer")
    try:
        await contenedor.wait_for(state="visible", timeout=20_000)
    except PlaywrightTimeoutError:
        await page.wait_for_timeout(2_000)

    texto = ""
    try:
        texto = (await page.locator("#generatedIpList").inner_text(timeout=5_000)).strip()
    except Exception:  # noqa: BLE001
        try:
            texto = (await page.inner_text("body", timeout=3_000)).strip()
        except Exception:  # noqa: BLE001
            pass

    username, password = _extraer_credenciales(texto)
    if username and password:
        log.info("Registro OK ip=%s dias=%s servicio=%s value=%s", ip, dias, servicio, duracion_value)
        return RegistroResultado(
            ok=True,
            mensaje=f"IP registrada correctamente.\nUSERNAME: {username}\nPASSWORD: {password}",
            username=username,
            password=password,
        )
    if texto:
        return RegistroResultado(ok=True, mensaje=texto[:1500])
    return RegistroResultado(
        ok=True,
        mensaje="Registro enviado, pero no pude leer la confirmación de la web.",
    )


_USERNAME_RE = re.compile(
    r"(?:USERNAME|USUARIO|USER)\s*[:=]?\s*([A-Za-z0-9_.\-@]+)", re.IGNORECASE
)
_PASSWORD_RE = re.compile(
    r"(?:PASSWORD|CONTRASE[NÑ]A|CONTRASENIA|PASS)\s*[:=]?\s*(\S+)", re.IGNORECASE
)


def _extraer_credenciales(texto: str) -> tuple[str | None, str | None]:
    if not texto:
        return None, None
    u = _USERNAME_RE.search(texto)
    pw = _PASSWORD_RE.search(texto)
    return (u.group(1) if u else None), (pw.group(1) if pw else None)


DASHBOARD_PUBLIC_URL = "https://dashboard-ditxdev.web.app/dashboard.html"


async def contar_clientes_marke() -> int:
    """Cuenta las IPs en el dashboard con 'Created by: Marke'."""
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(args=_BROWSER_ARGS, env=_get_chromium_env())
            page = await browser.new_page()
            await page.goto(DASHBOARD_PUBLIC_URL, wait_until="networkidle", timeout=25000)
            await asyncio.sleep(4)
            content = await page.content()
            await browser.close()
            matches = re.findall(r"Created by:</strong>\s*Marke\b", content, re.IGNORECASE)
            return len(matches)
    except Exception:
        log.exception("Error contando clientes Marke en dashboard")
        return -1
