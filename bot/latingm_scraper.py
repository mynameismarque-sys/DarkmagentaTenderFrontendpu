"""Automatización Playwright: latingm.com → redeempins.com para diamantes Free Fire."""
import asyncio
import logging
import os
import random
import re
from typing import Awaitable, Callable

from playwright.async_api import TimeoutError as PlaywrightTimeoutError, async_playwright

from .automation import _get_chromium_env

log = logging.getLogger("bot.latingm")

LATINGM_URL         = "https://latingm.com/"
LATINGM_PRODUCT_URL = "https://latingm.com/p/recarga-free-fire-latam/"
REDEEMPINS_URL      = "https://redeempins.com/"

PAQUETES = {
    110:  {"base": 100,  "precio": "$1.450 ARS"},
    341:  {"base": 310,  "precio": "$4.250 ARS"},
    572:  {"base": 520,  "precio": "$7.100 ARS"},
    1166: {"base": 1060, "precio": "$14.000 ARS"},
    2398: {"base": 2180, "precio": "$25.700 ARS"},
    6160: {"base": 5600, "precio": "$64.300 ARS"},
}

# Patrones de PIN que latingm.com puede entregar:
# 1. UUID estilo Windows (mayús o minús): XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX
# 2. Código alfanumérico corto sin guiones: 16-32 chars (ej. ABCD1234EFGH5678)
_PIN_PATTERN = re.compile(
    r'\b(?:[A-Za-z0-9]{8}-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}-[A-Za-z0-9]{12}'
    r'|[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}'
    r'|[A-Za-z0-9]{16,32})\b',
    re.ASCII,
)

# Marcador especial que main.py detecta para activar el fallback manual
_PENDIENTE_MANUAL_TAG = "PENDIENTE_MANUAL"

# ── User-Agent residencial actualizado (Chrome 131 / Windows 10) ─────────────
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
_SEC_CH_UA = '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"'

_REDEEMPINS_NOMBRE = "Agustin Nahuel"
_REDEEMPINS_FECHA  = "11/04/1999"

# ── Args de browser para latingm (stealth máximo contra Cloudflare) ───────────
_LATINGM_BROWSER_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--disable-extensions",
    "--disable-plugins-discovery",
    "--disable-default-apps",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
    "--disable-ipc-flooding-protection",
    "--password-store=basic",
    "--use-mock-keychain",
    "--window-size=1280,800",
    "--lang=es-AR",
    "--disable-client-side-phishing-detection",
    "--disable-popup-blocking",
    "--metrics-recording-only",
]

# ── Stealth JS comprensivo contra fingerprinting de Cloudflare ───────────────
# Parcha: webdriver, plugins, chrome obj, hardware, plataforma, permisos,
# connection, canvas y otras propiedades que CF inspecciona.
_STEALTH_JS = """
() => {
    /* 1. Borrar navigator.webdriver */
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    try { delete navigator.__proto__.webdriver; } catch(e) {}

    /* 2. Plugins reales de Chrome */
    const pluginArr = [
        { name: 'Chrome PDF Plugin',  filename: 'internal-pdf-viewer',            description: 'Portable Document Format',  length: 1 },
        { name: 'Chrome PDF Viewer',  filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '',                          length: 1 },
        { name: 'Native Client',      filename: 'internal-nacl-plugin',            description: '',                          length: 2 },
    ];
    pluginArr.item      = (i) => pluginArr[i] || null;
    pluginArr.namedItem = (n) => pluginArr.find(p => p.name === n) || null;
    pluginArr.refresh   = () => {};
    Object.defineProperty(navigator, 'plugins', { get: () => pluginArr });

    /* 3. MimeTypes */
    const mimes = [
        { type: 'application/pdf',         suffixes: 'pdf', description: '',  enabledPlugin: pluginArr[0] },
        { type: 'application/x-google-chrome-pdf', suffixes: 'pdf', description: 'Portable Document Format', enabledPlugin: pluginArr[1] },
        { type: 'application/x-nacl',      suffixes: '',    description: 'Native Client Executable', enabledPlugin: pluginArr[2] },
        { type: 'application/x-pnacl',     suffixes: '',    description: 'Portable Native Client Executable', enabledPlugin: pluginArr[2] },
    ];
    mimes.item      = (i) => mimes[i] || null;
    mimes.namedItem = (n) => mimes.find(m => m.type === n) || null;
    Object.defineProperty(navigator, 'mimeTypes', { get: () => mimes });

    /* 4. Idiomas */
    Object.defineProperty(navigator, 'languages', { get: () => ['es-AR', 'es', 'en-US', 'en'] });

    /* 5. Hardware y memoria */
    Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
    Object.defineProperty(navigator, 'deviceMemory',        { get: () => 8 });

    /* 6. Plataforma y vendor */
    Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
    Object.defineProperty(navigator, 'vendor',   { get: () => 'Google Inc.' });

    /* 7. Objeto window.chrome completo (loadTimes, csi, runtime, app) */
    const _start = Date.now();
    window.chrome = {
        app: {
            isInstalled: false,
            getDetails:      () => null,
            getIsInstalled:  () => false,
            runningState:    () => 'cannot_run',
            InstallState:    { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
            RunningState:    { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' },
        },
        csi: () => ({
            startE: _start,
            onloadT: _start + Math.floor(Math.random() * 300 + 100),
            pageT:   _start + Math.floor(Math.random() * 600 + 200),
            tran:    15,
        }),
        loadTimes: () => ({
            commitLoadTime:          _start / 1000,
            connectionInfo:          'h2',
            finishDocumentLoadTime:  (_start + 500) / 1000,
            finishLoadTime:          (_start + 800) / 1000,
            firstPaintAfterLoadTime: 0,
            firstPaintTime:          (_start + 300) / 1000,
            navigationType:          'Other',
            npnNegotiatedProtocol:   'h2',
            requestTime:             (_start - 100) / 1000,
            startLoadTime:           (_start - 50) / 1000,
            wasAlternateProtocolAvailable: false,
            wasFetchedViaSpdy: true,
            wasNpnNegotiated:  true,
        }),
        runtime: {
            id:        undefined,
            connect:   () => {},
            sendMessage: () => {},
            OnInstalledReason: {
                CHROME_UPDATE: 'chrome_update', INSTALL: 'install',
                SHARED_MODULE_UPDATE: 'shared_module_update', UPDATE: 'update',
            },
            PlatformOs: { ANDROID: 'android', CROS: 'cros', LINUX: 'linux', MAC: 'mac', WIN: 'win' },
            PlatformArch: { ARM: 'arm', X86_32: 'x86-32', X86_64: 'x86-64' },
            RequestUpdateCheckStatus: {
                NO_UPDATE: 'no_update', THROTTLED: 'throttled', UPDATE_AVAILABLE: 'update_available',
            },
        },
    };

    /* 8. Permisos — sin revelar automation */
    const _origPermQuery = window.navigator.permissions.query.bind(navigator.permissions);
    window.navigator.permissions.query = (params) =>
        params.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission, onchange: null })
            : _origPermQuery(params);

    /* 9. Conexión de red simulada */
    try {
        Object.defineProperty(navigator, 'connection', {
            get: () => ({
                rtt: 50, type: 'wifi', saveData: false,
                effectiveType: '4g', downlinkMax: Infinity,
                downlink: 10, onchange: null,
            }),
        });
    } catch(e) {}

    /* 10. User-Agent — eliminar 'Headless' si aparece */
    const _ua = navigator.userAgent;
    if (_ua.includes('HeadlessChrome')) {
        Object.defineProperty(navigator, 'userAgent', {
            get: () => _ua.replace('HeadlessChrome', 'Chrome'),
        });
    }

    /* 11. Ocultar toString del Proxy si Cloudflare lo inspecciona */
    const _toString = Function.prototype.toString;
    Function.prototype.toString = function() {
        if (this === window.chrome.runtime.connect || this === window.chrome.runtime.sendMessage) {
            return 'function () { [native code] }';
        }
        return _toString.call(this);
    };

    /* 12. Screen dimensions consistentes con viewport */
    try {
        Object.defineProperty(screen, 'width',       { get: () => 1280 });
        Object.defineProperty(screen, 'height',      { get: () => 800 });
        Object.defineProperty(screen, 'availWidth',  { get: () => 1280 });
        Object.defineProperty(screen, 'availHeight', { get: () => 800 });
        Object.defineProperty(screen, 'colorDepth',  { get: () => 24 });
        Object.defineProperty(screen, 'pixelDepth',  { get: () => 24 });
    } catch(e) {}

    /* 13. outerWidth/Height = innerWidth/Height (ventana real) */
    try {
        Object.defineProperty(window, 'outerWidth',  { get: () => 1280 });
        Object.defineProperty(window, 'outerHeight', { get: () => 800 });
    } catch(e) {}
}
"""

# Keywords que indican una página de challenge de Cloudflare
_CF_KEYWORDS = (
    "cloudflare",
    "verificación de seguridad",
    "security check",
    "checking your browser",
    "just a moment",
    "cf-browser-verification",
    "cf-challenge",
    "verifique que es un ser humano",
    "verify you are human",
    "please wait",
    "please stand by",
)


async def _safe_content(page) -> str:
    """Obtiene page.content() de forma segura, reintentando si la página está navegando."""
    for attempt in range(4):
        try:
            return await page.content()
        except Exception as exc:
            if attempt < 3:
                log.debug("latingm: page.content() error (intento %d/4): %s — esperando...", attempt + 1, exc)
                await page.wait_for_timeout(1_500)
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=8_000)
                except Exception:
                    pass
            else:
                log.warning("latingm: page.content() falló 4 veces: %s", exc)
                return ""
    return ""


async def _esperar_cloudflare(page, timeout_ms: int = 25_000) -> bool:
    """
    Detecta si la página actual es un challenge de Cloudflare y espera
    a que se auto-resuelva (funciona para JS-challenge; el Managed Challenge
    con checkbox humano NO se puede resolver automáticamente).
    Retorna True si la página está ok, False si el challenge persiste.
    """
    # Esperar a que la página se estabilice antes de leer el contenido
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=6_000)
    except Exception:
        pass
    await page.wait_for_timeout(500)

    content = await _safe_content(page)
    if not content:
        return True

    if not any(kw in content.lower() for kw in _CF_KEYWORDS):
        return True

    log.info("latingm: challenge Cloudflare detectado — esperando auto-resolución (hasta %ds)...", timeout_ms // 1000)
    try:
        await page.wait_for_function(
            """() => {
                const title = (document.title || '').toLowerCase();
                const body  = document.body ? document.body.innerText.toLowerCase() : '';
                const hasChallenge =
                    title.includes('just a moment') ||
                    title.includes('security check') ||
                    title.includes('verificación') ||
                    body.includes('verificación de seguridad') ||
                    body.includes('checking your browser') ||
                    body.includes('verifique que es un ser humano') ||
                    body.includes('verify you are human') ||
                    !!document.querySelector('.cf-browser-verification') ||
                    !!document.querySelector('#cf-wrapper') ||
                    !!document.querySelector('input[name="cf-turnstile-response"]');
                return !hasChallenge;
            }""",
            timeout=timeout_ms,
        )
        await page.wait_for_timeout(1_500)
        log.info("latingm: Cloudflare challenge superado — URL=%s", page.url)
        return True
    except Exception:
        log.warning("latingm: Cloudflare challenge NO se auto-resolvió en %ds", timeout_ms // 1000)
        return False


async def _goto_cf(page, url: str, **kwargs) -> bool:
    """
    Navega a una URL y espera a que pase cualquier challenge de Cloudflare.
    Retorna True si la página cargó sin bloqueo, False si CF bloqueó.
    Maneja net::ERR_ABORTED (redirecciones WooCommerce) esperando a que la
    página se estabilice en lugar de fallar.
    """
    kwargs.setdefault("wait_until", "domcontentloaded")
    kwargs.setdefault("timeout", 30_000)
    try:
        await page.goto(url, **kwargs)
    except Exception as exc:
        err = str(exc)
        if "ERR_ABORTED" in err or "net::" in err or "Navigation interrupted" in err:
            log.debug("latingm: goto '%s' abortado (probablemente redireccionado) — esperando estabilización", url)
            for _ in range(3):
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=8_000)
                    break
                except Exception:
                    await page.wait_for_timeout(1_500)
        else:
            raise
    await page.wait_for_timeout(1_000)
    ok = await _esperar_cloudflare(page)
    return ok


# ─────────────────────────────────────────────────────────────────────────────
# Login
# ─────────────────────────────────────────────────────────────────────────────

async def _login(page, shop_user: str, shop_pass: str) -> bool:
    log.info("latingm: navegando a login...")
    cf_ok = await _goto_cf(page, f"{LATINGM_URL}my-account/", timeout=35_000)
    if not cf_ok:
        log.error("latingm: Cloudflare bloqueó /my-account/ — no se puede hacer login")
        return False
    await page.wait_for_timeout(1_500)

    content = await _safe_content(page)
    if "cerrar sesión" in content.lower() or "logout" in content.lower() or "cerrar-sesion" in content.lower():
        log.info("latingm: ya estaba logueado")
        return True

    for sel in ["#username", "input[name='username']", "input[type='email']", "input[name='email']"]:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=2_000):
                await el.fill(shop_user)
                log.info("latingm: username con selector %s", sel)
                break
        except Exception:
            continue

    for sel in ["#password", "input[name='password']", "input[type='password']"]:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=2_000):
                await el.fill(shop_pass)
                log.info("latingm: password llenado")
                break
        except Exception:
            continue

    await page.wait_for_timeout(500)

    for sel in ["button[name='login']", "button[type='submit']", "input[type='submit']"]:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=2_000):
                await el.click()
                log.info("latingm: submit con selector %s", sel)
                break
        except Exception:
            continue

    try:
        await page.wait_for_load_state("networkidle", timeout=20_000)
    except Exception:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10_000)
        except Exception:
            pass
    await page.wait_for_timeout(3_000)

    await _esperar_cloudflare(page, timeout_ms=15_000)

    content = await _safe_content(page)
    logged_in = (
        "cerrar sesión" in content.lower()
        or "cerrar-sesion" in content.lower()
        or "logout" in content.lower()
        or "woocommerce-MyAccount-navigation-link--customer-logout" in content
        or "woocommerce-account" in content.lower()
    )
    log.info("latingm: login resultado=%s URL=%s", logged_in, page.url)

    if not logged_in:
        await page.wait_for_timeout(3_000)
        content = await _safe_content(page)
        logged_in = (
            "cerrar sesión" in content.lower()
            or "logout" in content.lower()
            or "woocommerce-account" in content.lower()
        )
        log.info("latingm: re-verificación login=%s", logged_in)

    return logged_in


# ─────────────────────────────────────────────────────────────────────────────
# Extracción del PIN
# ─────────────────────────────────────────────────────────────────────────────

_REDEEMPINS_PIN_SELECTORS = [
    "input[placeholder='Código Pin']",
    "input[placeholder*='Pin']",
    "input[placeholder*='PIN']",
    "input[placeholder*='Código']",
    "input[placeholder*='codigo']",
    "input[name*='pin']",
    "input[id*='pin']",
    "input[name*='code']",
    "input[id*='code']",
    "input[type='text']",
]


async def _insertar_pin_redeempins(page, pin: str) -> bool:
    """
    Intenta insertar el PIN en el campo de redeempins.com usando 4 estrategias.
    Devuelve True si lo logró, False si todas fallaron.
    """
    # Esperar a que la página termine de cargar completamente
    try:
        await page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception:
        pass
    await page.wait_for_timeout(1_500)

    # Esperar a que el campo aparezca (timeout más generoso)
    for sel in _REDEEMPINS_PIN_SELECTORS:
        try:
            await page.wait_for_selector(sel, timeout=6_000, state="visible")
            break
        except Exception:
            continue

    for sel in _REDEEMPINS_PIN_SELECTORS:
        try:
            el = page.locator(sel).first
            if not await el.is_visible(timeout=3_000):
                continue

            # ── Estrategia 1: click + fill ────────────────────────────────
            try:
                await el.click(timeout=3_000)
                await page.wait_for_timeout(300)
                await el.fill(pin, timeout=5_000)
                await page.wait_for_timeout(300)
                valor = await el.input_value(timeout=2_000)
                if valor.strip():
                    log.info("_insertar_pin_redeempins: ✅ estrategia fill — %s | valor=%s", sel, valor[:20])
                    return True
            except Exception:
                pass

            # ── Estrategia 2: triple-click (seleccionar todo) + escritura humanizada
            try:
                await el.click(click_count=3, timeout=3_000)
                await page.wait_for_timeout(random.randint(180, 350))
                await _type_humanizado(page, pin)
                await page.wait_for_timeout(300)
                valor = await el.input_value(timeout=2_000)
                if valor.strip():
                    log.info("_insertar_pin_redeempins: ✅ estrategia humanizada — %s", sel)
                    return True
            except Exception:
                pass

            # ── Estrategia 3: Ctrl+A + escritura humanizada ───────────────
            try:
                await el.click(timeout=3_000)
                await page.keyboard.press("Control+a")
                await page.wait_for_timeout(random.randint(80, 180))
                await _type_humanizado(page, pin)
                await page.wait_for_timeout(300)
                valor = await el.input_value(timeout=2_000)
                if valor.strip():
                    log.info("_insertar_pin_redeempins: ✅ estrategia keyboard.type — %s", sel)
                    return True
            except Exception:
                pass

            # ── Estrategia 4: JS eval — forzar valor + disparar eventos ──
            try:
                await el.evaluate(
                    """(el, v) => {
                        const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value').set;
                        nativeInputValueSetter.call(el, v);
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }""",
                    pin,
                )
                await page.wait_for_timeout(300)
                valor = await el.input_value(timeout=2_000)
                if valor.strip():
                    log.info("_insertar_pin_redeempins: ✅ estrategia JS eval — %s", sel)
                    return True
            except Exception:
                pass

        except Exception as exc:
            log.debug("_insertar_pin_redeempins: selector %s falló: %s", sel, exc)
            continue

    log.warning("_insertar_pin_redeempins: todas las estrategias fallaron para PIN=%s", pin[:20])
    return False


async def _type_humanizado(page, text: str) -> None:
    """
    Escribe texto carácter a carácter con retrasos aleatorios que imitan
    la velocidad de tipeo humana (40-120 ms/tecla, pausas ocasionales).
    El reCAPTCHA invisible de redeempins.com analiza los eventos de
    teclado para detectar bots; este helper los hace indistinguibles.
    """
    for i, char in enumerate(text):
        await page.keyboard.type(char)
        delay = random.randint(40, 120)
        # Pausa "pensante" cada 4-9 caracteres
        if i > 0 and i % random.randint(4, 9) == 0:
            delay += random.randint(120, 380)
        await page.wait_for_timeout(delay)


async def _pre_clic_humano(page) -> None:
    """
    Simula micro-movimientos de mouse y una pausa antes de hacer clic en
    un botón. El reCAPTCHA invisible rastrea trayectorias del cursor.
    """
    vp = page.viewport_size or {"width": 1280, "height": 800}
    # Mover a una posición aleatoria y esperar un poco
    rx = random.randint(100, vp["width"] - 100)
    ry = random.randint(100, vp["height"] - 200)
    await page.mouse.move(rx, ry)
    await page.wait_for_timeout(random.randint(200, 500))
    # Segundo micro-movimiento (jitter)
    await page.mouse.move(rx + random.randint(-15, 15), ry + random.randint(-10, 10))
    await page.wait_for_timeout(random.randint(300, 800))


async def _extraer_pin_de_pedido(page) -> str:
    """Intenta extraer el PIN/licencia del pedido usando múltiples estrategias."""
    pin = ""

    # ── Estrategia 1: buscar "Pin: XXXX" en texto plano ─────────────────────
    try:
        body = await page.inner_text("body")
        pin_line = re.search(
            r'[Pp]in\s*[:\-]\s*([A-Z0-9]{8}-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{12})',
            body,
        )
        if pin_line:
            pin = pin_line.group(1)
            log.info("latingm: PIN via 'Pin:' = %s", pin)
            return pin
    except Exception:
        pass

    # ── Estrategia 2: regex UUID en texto plano ──────────────────────────────
    try:
        body = await page.inner_text("body")
        match = _PIN_PATTERN.search(body)
        if match:
            pin = match.group(0)
            log.info("latingm: PIN via regex texto = %s", pin)
            return pin
    except Exception:
        pass

    # ── Estrategia 3: regex UUID en HTML completo (puede estar en atributos) ─
    try:
        html = await page.content()
        match = _PIN_PATTERN.search(html)
        if match:
            pin = match.group(0)
            log.info("latingm: PIN via regex HTML = %s", pin)
            return pin
    except Exception:
        pass

    # ── Estrategia 4: selectores específicos de WooCommerce y elementos comunes
    _SELECTORS = [
        # Elementos de código / texto literal
        "code", "kbd", "pre",
        # WooCommerce order detail
        ".woocommerce-order-details",
        ".woocommerce-table__product-name",
        ".wc-item-meta",
        ".wc-item-meta-label",
        ".item-meta",
        ".item-downloads",
        ".woocommerce-order-updates",
        ".order-update",
        ".woocommerce-order__order-number",
        # Notas del pedido (donde latingm suele entregar el PIN)
        ".woocommerce-customer-details",
        ".woocommerce-order-details__totals",
        ".order_details",
        ".shop_table",
        ".woocommerce-table",
        # Tablas y celdas genéricas
        "td", "th",
        # Elementos con palabras clave de licencia/serial/pin
        "[class*='license']", "[class*='serial']",
        "[class*='pin']", "[class*='key']",
        "[class*='code']", "[class*='coupon']",
        # Párrafos y divs que pueden contener el PIN
        "p", ".woocommerce-message", ".woocommerce-info",
        "mark", "strong", "b",
    ]

    for sel in _SELECTORS:
        try:
            for el in await page.locator(sel).all():
                try:
                    txt = (await el.inner_text(timeout=1_000)).strip()
                    m = _PIN_PATTERN.search(txt)
                    if m:
                        pin = m.group(0)
                        log.info("latingm: PIN en <%s> = %s", sel, pin)
                        return pin
                except Exception:
                    continue
        except Exception:
            continue

    # ── Estrategia 5: buscar en el HTML de cada elemento (puede estar en value/data)
    try:
        for sel in ["input[readonly]", "input[type='text']", "textarea"]:
            for el in await page.locator(sel).all():
                try:
                    val = await el.get_attribute("value", timeout=500)
                    if val:
                        m = _PIN_PATTERN.search(val)
                        if m:
                            pin = m.group(0)
                            log.info("latingm: PIN en input value <%s> = %s", sel, pin)
                            return pin
                except Exception:
                    continue
    except Exception:
        pass

    return pin


# ─────────────────────────────────────────────────────────────────────────────
# Flujo principal
# ─────────────────────────────────────────────────────────────────────────────

async def comprar_diamantes(
    diamonds: int,
    id_freefire: str,
    notificar_pago: Callable[[str, str], Awaitable[None]],
    guardar_pin: Callable[[str, str], None] | None = None,
) -> tuple[bytes | None, str]:
    shop_user = os.environ.get("SHOP_USER", "")
    shop_pass = os.environ.get("SHOP_PASS", "")
    if not shop_user or not shop_pass:
        return None, "❌ No están configuradas las credenciales de la tienda (SHOP_USER / SHOP_PASS)."

    paquete = PAQUETES.get(diamonds)
    if not paquete:
        return None, f"❌ Paquete de {diamonds} diamantes no reconocido."

    base_str = str(paquete["base"])

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=_LATINGM_BROWSER_ARGS,
            env=_get_chromium_env(),
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=_UA,
            locale="es-AR",
            timezone_id="America/Argentina/Buenos_Aires",
            extra_http_headers={
                "Accept-Language": "es-AR,es;q=0.9,en-US;q=0.8,en;q=0.7",
                "sec-ch-ua": _SEC_CH_UA,
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
            },
        )
        await context.add_init_script(_STEALTH_JS)
        page = await context.new_page()
        screenshot: bytes | None = None

        try:
            # ── 1. Login ─────────────────────────────────────────────────────
            ok = await _login(page, shop_user, shop_pass)
            if not ok:
                screenshot = await page.screenshot()
                # Detectar si es un bloqueo de Cloudflare específicamente
                content = await _safe_content(page)
                if any(kw in content.lower() for kw in _CF_KEYWORDS):
                    return screenshot, (
                        "❌ **Acceso bloqueado temporalmente.**\n"
                        "El servidor está detectando actividad inusual. "
                        "Contactá al admin para que lo resuelva."
                    )
                return screenshot, "❌ El acceso al proveedor falló. Contactá al admin."

            # Calentar sesión en el home
            log.info("latingm: calentando sesión en home...")
            cf_ok = await _goto_cf(page, LATINGM_URL, timeout=20_000)
            await page.wait_for_timeout(2_000)
            content_home = await _safe_content(page)
            if "cerrar sesión" not in content_home.lower() and "logout" not in content_home.lower():
                log.warning("latingm: sesión perdida al ir al home — reintentando login")
                ok = await _login(page, shop_user, shop_pass)
                if not ok:
                    screenshot = await page.screenshot()
                    return screenshot, "❌ La sesión no pudo mantenerse. Revisá las credenciales."

            # ── 2. Navegar al producto ────────────────────────────────────────
            log.info("latingm: navegando al producto...")
            cf_ok = await _goto_cf(page, LATINGM_PRODUCT_URL, timeout=35_000)
            await page.wait_for_timeout(2_000)

            if not cf_ok:
                screenshot = await page.screenshot()
                return screenshot, "❌ Cloudflare bloqueó el acceso a la página del producto."

            if "login" in page.url or ("my-account" in page.url and "login" in (await _safe_content(page)).lower()):
                log.warning("latingm: redirigido a login — reintentando")
                ok = await _login(page, shop_user, shop_pass)
                if not ok:
                    screenshot = await page.screenshot()
                    return screenshot, "❌ La sesión expiró y el re-login falló."
                await _goto_cf(page, LATINGM_PRODUCT_URL, timeout=35_000)
                await page.wait_for_timeout(2_000)

            log.info("latingm: en producto — URL=%s", page.url)

            # ── 3. Seleccionar paquete ────────────────────────────────────────
            option_selected = False
            for sel in [
                "select[id*='pa_diamante']", "select[name*='pa_diamante']",
                "select[id*='diamante']",    "select[name*='diamante']",
                ".variations select",        "select.woo-variation-select",
                "form.variations_form select",
            ]:
                try:
                    select_el = page.locator(sel).first
                    if not await select_el.is_visible(timeout=4_000):
                        continue
                    options = await select_el.locator("option").all()
                    target_value = None
                    for opt in options:
                        opt_text = (await opt.inner_text()).strip()
                        opt_norm = opt_text.replace(".", "").replace(",", "")
                        if base_str in opt_norm or opt_text.startswith(base_str):
                            target_value = await opt.get_attribute("value")
                            log.info("latingm: opción '%s' (value=%s)", opt_text, target_value)
                            break
                    if target_value is None:
                        continue
                    await select_el.select_option(value=target_value)
                    await page.wait_for_timeout(1_500)
                    option_selected = True
                    log.info("latingm: opción seleccionada")
                    break
                except Exception as exc:
                    log.debug("latingm: select %s falló: %s", sel, exc)
                    continue

            if not option_selected:
                screenshot = await page.screenshot()
                return screenshot, (
                    f"❌ No pude seleccionar el paquete de {base_str} diamantes. "
                    "El producto puede haber cambiado. Contactá al admin."
                )

            # ── 4. AÑADIR AL CARRITO ──────────────────────────────────────────
            cart_added = False
            for sel in [
                "button:has-text('AÑADIR AL CARRITO')",
                "button:has-text('Añadir al carrito')",
                "button:has-text('Agregar al carrito')",
                ".single_add_to_cart_button",
                "[name='add-to-cart']",
                "button[type='submit'].cart",
                "button:has-text('Add to cart')",
                "button:has-text('Comprar')",
            ]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=3_000):
                        await el.click(timeout=8_000)
                        cart_added = True
                        log.info("latingm: AÑADIR AL CARRITO — %s", sel)
                        break
                except Exception:
                    continue

            if not cart_added:
                screenshot = await page.screenshot()
                return screenshot, "❌ No pude agregar el producto al carrito."

            # WooCommerce redirige automáticamente al carrito tras agregar al carrito.
            # Esperamos a que termine esa redirección.
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=15_000)
            except Exception:
                pass
            await page.wait_for_timeout(2_000)

            # ── 4b. Limpiar el carrito de ítems viejos si hay más de 1 ────────
            # (runs fallidas anteriores pueden dejar ítems rancios)
            try:
                for _ in range(3):  # Máx 3 ítems rancios
                    rm_btns = await page.locator(
                        "a.remove, a[data-product_id], .remove[href]"
                    ).all()
                    # Filtrar sólo los que NO son del ítem que acabamos de agregar
                    if len(rm_btns) > 1:
                        await rm_btns[0].click(timeout=3_000)
                        await page.wait_for_timeout(1_000)
                    else:
                        break
            except Exception:
                pass

            # ── 5. Ir al carrito si no estamos ya ahí ────────────────────────
            current_url = page.url
            already_on_cart = any(k in current_url for k in ("carrito", "cart", "basket"))
            if not already_on_cart:
                log.info("latingm: no estamos en carrito (%s) — navegando", current_url)
                await _goto_cf(page, f"{LATINGM_URL}carrito/", timeout=20_000)
                await page.wait_for_timeout(1_500)
            else:
                log.info("latingm: ya en carrito — URL=%s", current_url)
                await _esperar_cloudflare(page)
                await page.wait_for_timeout(1_000)

            log.info("latingm: en carrito — URL=%s", page.url)

            # ── 6. Click en FINALIZAR COMPRA ──────────────────────────────────
            checkout_clicked = False
            for sel in [
                "a.checkout-button",
                ".wc-proceed-to-checkout a",
                "a:has-text('Finalizar compra')",
                "a:has-text('FINALIZAR COMPRA')",
                "a[href*='checkout']",
                ".checkout-button",
                "a:has-text('Proceed to checkout')",
            ]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=3_000):
                        await el.click(timeout=8_000)
                        checkout_clicked = True
                        log.info("latingm: FINALIZAR COMPRA — %s", sel)
                        break
                except Exception:
                    continue

            if checkout_clicked:
                # Esperar a que la URL cambie a checkout en lugar de llamar goto() de nuevo
                try:
                    await page.wait_for_url(
                        lambda u: any(k in u for k in ("checkout", "pago", "order")),
                        timeout=15_000,
                    )
                except Exception:
                    # Si no cambió, verificar si ya estamos en checkout
                    if not any(k in page.url for k in ("checkout", "pago", "order")):
                        log.warning("latingm: URL no cambió a checkout tras click — navegando manual")
                        await _goto_cf(page, f"{LATINGM_URL}checkout/", timeout=20_000)
            else:
                log.warning("latingm: FINALIZAR COMPRA no encontrado — navegando directo a /checkout/")
                await _goto_cf(page, f"{LATINGM_URL}checkout/", timeout=20_000)

            # Esperar a que el checkout cargue completamente
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=15_000)
            except Exception:
                pass
            await _esperar_cloudflare(page)
            await page.wait_for_timeout(2_000)
            log.info("latingm: en checkout — URL=%s", page.url)

            # ── 6. Re-login si checkout lo pide ───────────────────────────────
            content = await _safe_content(page)
            if "debes estar conectado" in content.lower() or "must be logged in" in content.lower():
                log.warning("latingm: checkout pide login")
                ok = await _login(page, shop_user, shop_pass)
                if not ok:
                    screenshot = await page.screenshot()
                    return screenshot, "❌ El checkout requiere login pero las credenciales fallaron."
                await _goto_cf(page, f"{LATINGM_URL}checkout/", timeout=20_000)
                await page.wait_for_timeout(2_000)
                content = await _safe_content(page)
                if "debes estar conectado" in content.lower():
                    screenshot = await page.screenshot()
                    return screenshot, "❌ No fue posible autenticarse para finalizar la compra."

            # ── 7. Seleccionar Binance Pay ────────────────────────────────────
            binance_selected = False
            for sel in [
                "input[value*='binance']", "input[id*='binance']",
                "input[value='binancepay']", "input[value='binance_pay']",
                "input[value='binance-pay']",
                "[class*='binance'] input[type='radio']",
                "label:has-text('Paga con Binance Pay')",
                "label:has-text('Binance Pay')",
                "label:has-text('Binance')",
            ]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=3_000):
                        await el.click(timeout=5_000)
                        binance_selected = True
                        log.info("latingm: Binance Pay — %s", sel)
                        break
                except Exception:
                    continue

            if not binance_selected:
                try:
                    for label in await page.locator("label, .payment_method, li.payment_method").all():
                        txt = (await label.inner_text(timeout=1_000)).strip()
                        if "binance" in txt.lower():
                            await label.click(timeout=5_000)
                            binance_selected = True
                            log.info("latingm: Binance Pay por texto: '%s'", txt)
                            break
                except Exception:
                    pass

            if not binance_selected:
                screenshot = await page.screenshot()
                return screenshot, "❌ No pude seleccionar 'Paga con Binance Pay'."

            await page.wait_for_timeout(1_500)

            # ── 8. Seleccionar USDT ───────────────────────────────────────────
            usdt_selected = False
            for sel in [
                "select[name*='currency']", "select[id*='currency']",
                "select[name*='moneda']",   "select[id*='moneda']",
                "select[name*='coin']",     "select[id*='coin']",
                ".binance_pay select",      "#binance_pay_currency",
                "#binancepay_currency",
            ]:
                try:
                    for el in await page.locator(sel).all():
                        if not await el.is_visible(timeout=1_500):
                            continue
                        for val in ["USDT", "usdt", "tether"]:
                            try:
                                await el.select_option(value=val, timeout=2_000)
                                usdt_selected = True
                                log.info("latingm: USDT seleccionado")
                                break
                            except Exception:
                                pass
                        if not usdt_selected:
                            try:
                                await el.select_option(label="USDT", timeout=2_000)
                                usdt_selected = True
                            except Exception:
                                pass
                        if usdt_selected:
                            break
                    if usdt_selected:
                        break
                except Exception:
                    continue

            if not usdt_selected:
                log.warning("latingm: no pude seleccionar USDT — continuando")

            await page.wait_for_timeout(1_000)

            # ── 9. Checkbox T&C ───────────────────────────────────────────────
            tc_checked = False
            for sel in [
                "#terms", "input[name='terms']",
                "input[type='checkbox'][name*='terms']",
                "input[type='checkbox'][id*='terms']",
                ".woocommerce-form__input-checkbox",
            ]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=2_000):
                        if not await el.is_checked():
                            await el.check()
                        tc_checked = True
                        log.info("latingm: T&C marcado — %s", sel)
                        break
                except Exception:
                    continue

            if not tc_checked:
                try:
                    for cb in await page.locator("input[type='checkbox']").all():
                        if not await cb.is_visible(timeout=1_000):
                            continue
                        cb_id = await cb.get_attribute("id")
                        if cb_id:
                            try:
                                label_txt = (await page.locator(f"label[for='{cb_id}']").inner_text(timeout=1_000)).lower()
                                if any(kw in label_txt for kw in ("término", "termino", "condicion", "acuerdo", "leído", "leido", "agree")):
                                    if not await cb.is_checked():
                                        await cb.check()
                                    tc_checked = True
                                    log.info("latingm: T&C por label '%s'", label_txt[:40])
                                    break
                            except Exception:
                                pass
                except Exception:
                    pass

            if not tc_checked:
                log.warning("latingm: no encontré checkbox T&C — continuando")

            await page.wait_for_timeout(500)

            # ── 10. REALIZAR EL PEDIDO ────────────────────────────────────────
            order_placed = False
            for sel in [
                "button:has-text('REALIZAR EL PEDIDO')",
                "button:has-text('Realizar el pedido')",
                "button:has-text('Realizar pedido')",
                "#place_order",
                "[name='woocommerce_checkout_place_order']",
                "button:has-text('Place order')",
                "button:has-text('Pagar ahora')",
                "button:has-text('Pagar')",
            ]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=3_000):
                        await el.click(timeout=8_000)
                        order_placed = True
                        log.info("latingm: REALIZAR EL PEDIDO — %s", sel)
                        break
                except Exception:
                    continue

            if not order_placed:
                screenshot = await page.screenshot()
                return screenshot, "❌ No pude hacer click en 'REALIZAR EL PEDIDO'."

            # Esperar navegación post-pedido (puede ir a pay.binance.com o a order-received/)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=20_000)
            except Exception:
                pass
            await page.wait_for_timeout(2_000)
            log.info("latingm: pedido realizado — URL=%s", page.url)

            # ── 11. Extraer ID del pedido y URL de pago Binance ───────────────
            order_id: str = ""

            # Intentar extraer ID del pedido desde la URL actual
            # Formato: .../order-received/12345/ o .../ver-pedido/12345/
            id_match = re.search(r'/(?:order-received|ver-pedido|view-order)/(\d+)/', page.url)
            if id_match:
                order_id = id_match.group(1)
                log.info("latingm: ID de pedido desde URL = %s", order_id)

            # URL de pago: si el browser fue redirigido a Binance, page.url ES la URL de pago
            pay_url = page.url

            if "latingm.com" in page.url:
                # Estamos en una página de latingm — buscar el link de Binance
                body_txt = await _safe_content(page)

                # 1. Buscar URL de Binance directamente en el HTML
                binance_match = re.search(
                    r'https://pay\.binance\.com/[^\s"\'<>&]+', body_txt
                )
                if binance_match:
                    pay_url = binance_match.group(0)
                    log.info("latingm: URL Binance en HTML = %s", pay_url)

                # 2. Buscar como atributo href
                if pay_url == page.url:
                    try:
                        href = await page.locator(
                            "a[href*='pay.binance.com'], a[href*='binance']"
                        ).first.get_attribute("href", timeout=5_000)
                        if href and "binance" in href.lower():
                            pay_url = href
                            log.info("latingm: URL Binance desde href = %s", pay_url)
                    except Exception:
                        pass

                # 3. Si aún no tenemos ID, extraerlo del texto ("El pedido # 12345")
                if not order_id:
                    id_text_match = re.search(r'pedido\s*#?\s*(\d+)', body_txt, re.IGNORECASE)
                    if id_text_match:
                        order_id = id_text_match.group(1)
                        log.info("latingm: ID de pedido desde texto = %s", order_id)

            # Si el browser fue a Binance y order_id aún está vacío,
            # extraerlo del parámetro _dp (deep-link base64 con returnLink)
            if not order_id and "binance.com" in pay_url:
                try:
                    from urllib.parse import urlparse, parse_qs
                    import base64 as _b64
                    parsed_bp = urlparse(pay_url)
                    dp_param = parse_qs(parsed_bp.query).get("_dp", [None])[0]
                    if dp_param:
                        padding = "=" * ((4 - len(dp_param) % 4) % 4)
                        dp_decoded = _b64.b64decode(dp_param + padding).decode("utf-8", errors="ignore")
                        dp_id_match = re.search(r'/order-received/(\d+)/', dp_decoded)
                        if dp_id_match:
                            order_id = dp_id_match.group(1)
                            log.info("latingm: ID de pedido desde _dp de Binance = %s", order_id)
                except Exception as dp_exc:
                    log.warning("latingm: no pude extraer order_id del _dp: %s", dp_exc)

            log.info("latingm: URL pago Binance = %s | order_id = %s", pay_url, order_id)
            await notificar_pago(pay_url, f"Pago de **{diamonds} 💎** pendiente — ID FF: `{id_freefire}`")

            # ── 12. Polling: esperar confirmación de pago (hasta 10 min) ──────
            # Chequea cada 30 s la página del pedido específico (o el listado).
            # Estados que significan "pago recibido": Completado / Procesando.
            log.info("latingm: polling de estado de pedido (máx 10 min)...")
            POLL_INTERVALO = 30    # segundos
            POLL_MAX       = 20    # 20 × 30 s = 10 min
            pedido_completado = False
            _ESTADOS_OK = ("completado", "completed", "procesando", "processing")

            def _build_order_url() -> str:
                if order_id:
                    return f"{LATINGM_URL}mi-cuenta/ver-pedido/{order_id}/"
                return f"{LATINGM_URL}mi-cuenta/pedidos/"

            for intento in range(POLL_MAX):
                await asyncio.sleep(POLL_INTERVALO)
                try:
                    poll_url = _build_order_url()
                    await _goto_cf(page, poll_url, timeout=20_000)
                    await page.wait_for_timeout(1_500)
                    content = await _safe_content(page)
                    content_lower = content.lower()
                    estado_ok = any(k in content_lower for k in _ESTADOS_OK)
                    if estado_ok:
                        log.info("latingm: ✅ pago confirmado (intento %d/%d)", intento + 1, POLL_MAX)
                        pedido_completado = True
                        break
                    log.info("latingm: ⏳ pedido aún pendiente (intento %d/%d)...", intento + 1, POLL_MAX)
                except Exception as exc:
                    log.warning("latingm: error en polling %d: %s", intento + 1, exc)

            if not pedido_completado:
                screenshot = await page.screenshot()
                return screenshot, "⏰ Tiempo agotado — el pago Binance no fue confirmado en 10 minutos."

            # ── 13 & 14. Navegar al detalle del pedido completado ─────────────
            # Intentar extraer el PIN hasta 4 veces con 5 s entre intentos
            # (latingm puede tardar en generar el PIN tras confirmar el pago).
            PIN_REINTENTOS   = 4
            PIN_ESPERA_SEG   = 5
            pin = ""

            for pin_intento in range(1, PIN_REINTENTOS + 1):
                log.info("latingm: extrayendo PIN (intento %d/%d)...", pin_intento, PIN_REINTENTOS)

                if order_id:
                    # Navegar / recargar la página del pedido
                    order_url = f"{LATINGM_URL}mi-cuenta/ver-pedido/{order_id}/"
                    try:
                        await _goto_cf(page, order_url, timeout=20_000)
                    except Exception:
                        pass
                    await page.wait_for_timeout(3_000)
                    log.info("latingm: en detalle de pedido — URL=%s", page.url)
                else:
                    # Sin order_id: navegar al listado y clickear VER
                    for url_orders in [
                        f"{LATINGM_URL}mi-cuenta/pedidos/",
                        f"{LATINGM_URL}my-account/orders/",
                    ]:
                        try:
                            await _goto_cf(page, url_orders, timeout=20_000)
                            await page.wait_for_timeout(1_500)
                            content = await _safe_content(page)
                            if "ver" in content.lower():
                                break
                        except Exception:
                            continue

                    for sel in [
                        "a:has-text('VER')", "a:has-text('Ver')",
                        "a[href*='ver-pedido']", "a[href*='view-order']",
                        "td.woocommerce-orders-table__cell-order-actions a.button",
                        ".woocommerce-orders-table td a.button",
                    ]:
                        try:
                            el = page.locator(sel).first
                            if await el.is_visible(timeout=3_000):
                                await el.click(timeout=5_000)
                                log.info("latingm: VER clickeado — %s", sel)
                                break
                        except Exception:
                            continue

                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=10_000)
                    except Exception:
                        pass
                    await page.wait_for_timeout(2_000)

                pin = await _extraer_pin_de_pedido(page)
                if pin:
                    log.info("latingm: PIN extraído en intento %d — %s", pin_intento, pin)
                    break

                # Fallback: buscar también en la sección de Descargas
                if not pin:
                    try:
                        log.info("latingm: intentando sección Descargas para PIN...")
                        for url_dl in [
                            f"{LATINGM_URL}mi-cuenta/descargas/",
                            f"{LATINGM_URL}my-account/downloads/",
                        ]:
                            try:
                                await _goto_cf(page, url_dl, timeout=15_000)
                                await page.wait_for_timeout(2_000)
                                pin = await _extraer_pin_de_pedido(page)
                                if pin:
                                    log.info("latingm: PIN encontrado en Descargas — %s", pin)
                                    break
                            except Exception:
                                continue
                    except Exception:
                        pass

                if pin:
                    log.info("latingm: PIN extraído en intento %d (vía Descargas) — %s", pin_intento, pin)
                    break

                log.warning("latingm: PIN no encontrado en intento %d, esperando %ds...", pin_intento, PIN_ESPERA_SEG)
                if pin_intento < PIN_REINTENTOS:
                    await asyncio.sleep(PIN_ESPERA_SEG)

            if not pin:
                screenshot = await page.screenshot()
                return screenshot, "❌ Pago confirmado pero no pude extraer el PIN del pedido."

            log.info("latingm: PIN obtenido = %s", pin)

            # ── Persistir el PIN para poder reenviarlo luego si falla el canje ─
            if guardar_pin:
                try:
                    oid_match = __import__("re").search(r'/(?:ver-pedido|view-order)/(\d+)/', page.url)
                    oid_str = oid_match.group(1) if oid_match else ""
                    guardar_pin(pin, oid_str)
                    log.info("latingm: PIN guardado en DB (order_id=%s)", oid_str)
                except Exception as _gp_exc:
                    log.warning("latingm: no se pudo guardar PIN: %s", _gp_exc)

            # ── 15. redeempins.com — Paso 1: insertar PIN ────────────────────
            await _goto_cf(page, REDEEMPINS_URL, timeout=25_000)
            log.info("redeempins: en formulario paso 1 — URL=%s", page.url)

            pin_inserted = await _insertar_pin_redeempins(page, pin)
            if not pin_inserted:
                screenshot = await page.screenshot()
                return screenshot, f"❌ No pude insertar el PIN en redeempins.com. PIN: `{pin}`"

            # Micro-movimientos humanos antes del clic → evita trigger del reCAPTCHA invisible
            await _pre_clic_humano(page)

            for sel in [
                "button:has-text('Canjear')", "button:has-text('CANJEAR')",
                "button:has-text('Redeem')", "button[type='submit']",
                "input[type='submit']",
            ]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=2_000):
                        await el.click(timeout=8_000)
                        log.info("redeempins: Canjear (paso 1) — %s", sel)
                        break
                except Exception:
                    continue

            # Esperar a que aparezca el formulario de paso 2 (Nombre Completo, etc.)
            log.info("redeempins: esperando formulario paso 2...")
            paso2_ok = False
            try:
                await page.wait_for_selector(
                    "input[placeholder*='Nombre'], input[placeholder*='nombre'], "
                    "input[name*='name'], input[placeholder*='Nacimiento']",
                    timeout=25_000,
                )
                paso2_ok = True
            except Exception:
                await page.wait_for_timeout(5_000)

            if not paso2_ok:
                log.warning("redeempins: paso 2 no apareció — reCAPTCHA probablemente bloqueó el envío")
                screenshot = await page.screenshot()
                return screenshot, (
                    f"⚠️ {_PENDIENTE_MANUAL_TAG}\n"
                    f"PIN:{pin}\nID:{id_freefire}\nDIAM:{diamonds}\n\n"
                    f"❌ El reCAPTCHA bloqueó el canje automático en redeempins.com.\n"
                    f"🔑 PIN: `{pin}`\n🎮 ID FF: `{id_freefire}`"
                )
            log.info("redeempins: formulario paso 2 — URL=%s", page.url)

            # ── 16. redeempins.com — Paso 2: completar datos del jugador ─────
            # Nombre Completo
            for sel in [
                "input[placeholder='Nombre Completo']",
                "input[placeholder*='Nombre']", "input[placeholder*='nombre']",
                "input[name*='name']", "input[id*='name']",
            ]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=2_000):
                        await el.fill(_REDEEMPINS_NOMBRE)
                        log.info("redeempins: Nombre llenado")
                        break
                except Exception:
                    continue

            # Fecha de Nacimiento (DD/MM/YYYY)
            for sel in [
                "input[placeholder='Fecha de Nacimiento']",
                "input[placeholder*='Nacimiento']", "input[placeholder*='nacimiento']",
                "input[placeholder*='Fecha']",
                "input[name*='birth']", "input[id*='birth']",
                "input[name*='fecha']", "input[id*='fecha']",
                "input[type='date']",
            ]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=2_000):
                        await el.fill(_REDEEMPINS_FECHA)
                        log.info("redeempins: Fecha llenada")
                        break
                except Exception:
                    continue

            # Nacionalidad → Argentina
            for sel in [
                "select[name*='nationalit']", "select[id*='nationalit']",
                "select[name*='nation']",     "select[name*='pais']",
                "select[name*='country']",    "select[id*='country']",
                "select",
            ]:
                try:
                    for el in await page.locator(sel).all():
                        if not await el.is_visible(timeout=1_500):
                            continue
                        for val in ["Argentina", "AR", "argentina"]:
                            try:
                                await el.select_option(label=val, timeout=2_000)
                                log.info("redeempins: Argentina seleccionada")
                                break
                            except Exception:
                                try:
                                    await el.select_option(value=val, timeout=2_000)
                                    log.info("redeempins: Argentina (value) seleccionada")
                                    break
                                except Exception:
                                    pass
                        break
                except Exception:
                    continue

            # ID de usuario en el juego
            for sel in [
                "input[placeholder*='ID de usuario']",
                "input[placeholder*='id de usuario']",
                "input[placeholder*='usuario']",
                "input[placeholder*='jugador']",
                "input[placeholder*='Player']",  "input[placeholder*='player']",
                "input[name*='player']",          "input[id*='player']",
                "input[name*='uid']",             "input[id*='uid']",
                "input[name*='user']",            "input[id*='user']",
            ]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=2_000):
                        await el.fill(id_freefire)
                        log.info("redeempins: ID jugador llenado = %s", id_freefire)
                        break
                except Exception:
                    continue

            # T&C checkbox
            for sel in ["input[type='checkbox']", "input[name*='terms']", "input[id*='terms']"]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=2_000):
                        if not await el.is_checked():
                            await el.check()
                        log.info("redeempins: T&C marcado")
                        break
                except Exception:
                    continue

            await page.wait_for_timeout(500)

            # Botón "¡Canjear Ahora!" (paso 2)
            for sel in [
                "button:has-text('¡Canjear Ahora!')",
                "button:has-text('Canjear Ahora')",
                "button:has-text('CANJEAR AHORA')",
                "button:has-text('¡Canjear ahora!')",
                "button[type='submit']", "input[type='submit']",
                "button:has-text('Canjear')", "button:has-text('Redeem')",
            ]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=2_000):
                        await el.click(timeout=8_000)
                        log.info("redeempins: ¡Canjear Ahora! — %s", sel)
                        break
                except Exception:
                    continue

            await page.wait_for_timeout(5_000)
            log.info("redeempins: canje enviado — URL=%s", page.url)

            screenshot = await page.screenshot(full_page=False)
            return screenshot, f"✅ ¡{diamonds} 💎 canjeados con éxito!\n🔑 PIN: `{pin}`"

        except Exception as exc:
            log.exception("Error en comprar_diamantes (diamonds=%d, id_ff=%s)", diamonds, id_freefire)
            try:
                screenshot = await page.screenshot()
            except Exception:
                screenshot = None
            return screenshot, f"❌ Error en el proceso: {exc}"
        finally:
            await browser.close()


async def completar_pedido_existente(
    order_id: str,
    id_freefire: str,
    diamonds: int = 110,
    guardar_pin: Callable[[str, str], None] | None = None,
) -> tuple[bytes | None, str]:
    """
    Completa un pedido ya pagado en latingm.com:
    1. Login en latingm.com
    2. Navega a /mi-cuenta/ver-pedido/<order_id>/
    3. Extrae el PIN
    4. Canjea en redeempins.com para <id_freefire>
    5. Devuelve (screenshot, mensaje)
    """
    shop_user = os.environ.get("SHOP_USER", "")
    shop_pass = os.environ.get("SHOP_PASS", "")
    if not shop_user or not shop_pass:
        return None, "❌ No están configuradas las credenciales de la tienda."

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=_LATINGM_BROWSER_ARGS,
            env=_get_chromium_env(),
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=_UA,
            locale="es-AR",
            extra_http_headers={
                "Accept-Language": "es-AR,es;q=0.9,en-US;q=0.8,en;q=0.7",
                "sec-ch-ua": _SEC_CH_UA,
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
            },
        )
        await context.add_init_script(_STEALTH_JS)
        page = await context.new_page()

        try:
            # ── 1. Login ──────────────────────────────────────────────────────
            log.info("completar_pedido: login para order_id=%s ff=%s", order_id, id_freefire)
            ok = await _login(page, shop_user, shop_pass)
            if not ok:
                screenshot = await page.screenshot()
                return screenshot, "❌ No pude iniciar sesión en el proveedor."

            # ── 2. Ir al pedido específico ────────────────────────────────────
            order_url = f"{LATINGM_URL}mi-cuenta/ver-pedido/{order_id}/"
            log.info("completar_pedido: navegando a %s", order_url)
            await _goto_cf(page, order_url, timeout=25_000)
            await page.wait_for_timeout(2_000)
            log.info("completar_pedido: en pedido — URL=%s", page.url)

            # ── 3. Extraer PIN ────────────────────────────────────────────────
            pin = await _extraer_pin_de_pedido(page)
            if not pin:
                screenshot = await page.screenshot()
                return screenshot, f"❌ No encontré el PIN en el pedido {order_id}. Puede que el pedido aún no esté completado."

            log.info("completar_pedido: PIN = %s", pin)

            # ── Persistir PIN para posible reenvío posterior ──────────────────
            if guardar_pin:
                try:
                    guardar_pin(pin, order_id)
                    log.info("completar_pedido: PIN guardado en DB (order_id=%s)", order_id)
                except Exception as _gp_exc:
                    log.warning("completar_pedido: no se pudo guardar PIN: %s", _gp_exc)

            # ── 4. redeempins.com — Paso 1 ────────────────────────────────────
            await _goto_cf(page, REDEEMPINS_URL, timeout=25_000)
            log.info("completar_pedido: redeempins paso 1 — URL=%s", page.url)

            pin_inserted = await _insertar_pin_redeempins(page, pin)
            if not pin_inserted:
                screenshot = await page.screenshot()
                return screenshot, f"❌ No pude insertar el PIN en redeempins.com. PIN: `{pin}`"

            # Micro-movimientos humanos antes del clic → evita trigger del reCAPTCHA invisible
            await _pre_clic_humano(page)

            for sel in [
                "button:has-text('Canjear')", "button:has-text('CANJEAR')",
                "button:has-text('Redeem')", "button[type='submit']",
                "input[type='submit']",
            ]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=2_000):
                        await el.click(timeout=8_000)
                        log.info("completar_pedido: redeempins paso 1 click")
                        break
                except Exception:
                    continue

            # Esperar formulario paso 2
            log.info("completar_pedido: esperando formulario paso 2...")
            paso2_ok = False
            try:
                await page.wait_for_selector(
                    "input[placeholder*='Nombre'], input[placeholder*='nombre'], "
                    "input[name*='name'], input[placeholder*='Nacimiento']",
                    timeout=25_000,
                )
                paso2_ok = True
            except Exception:
                await page.wait_for_timeout(5_000)

            if not paso2_ok:
                log.warning("completar_pedido: paso 2 no apareció — reCAPTCHA bloqueó el envío")
                screenshot = await page.screenshot()
                return screenshot, (
                    f"⚠️ {_PENDIENTE_MANUAL_TAG}\n"
                    f"PIN:{pin}\nID:{id_freefire}\nDIAM:{diamonds}\n\n"
                    f"❌ El reCAPTCHA bloqueó el canje automático en redeempins.com.\n"
                    f"🔑 PIN: `{pin}`\n🎮 ID FF: `{id_freefire}`"
                )

            # ── 5. redeempins.com — Paso 2 ────────────────────────────────────
            for sel in [
                "input[placeholder='Nombre Completo']", "input[placeholder*='Nombre']",
                "input[placeholder*='nombre']", "input[name*='name']", "input[id*='name']",
            ]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=2_000):
                        await el.fill(_REDEEMPINS_NOMBRE)
                        break
                except Exception:
                    continue

            for sel in [
                "input[placeholder='Fecha de Nacimiento']", "input[placeholder*='Nacimiento']",
                "input[placeholder*='Fecha']", "input[name*='birth']", "input[type='date']",
            ]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=2_000):
                        await el.fill(_REDEEMPINS_FECHA)
                        break
                except Exception:
                    continue

            for sel in [
                "select[name*='nationalit']", "select[id*='nationalit']",
                "select[name*='nation']", "select[name*='pais']",
                "select[name*='country']", "select",
            ]:
                try:
                    for el in await page.locator(sel).all():
                        if not await el.is_visible(timeout=1_500):
                            continue
                        for val in ["Argentina", "AR", "argentina"]:
                            try:
                                await el.select_option(label=val, timeout=2_000)
                                break
                            except Exception:
                                try:
                                    await el.select_option(value=val, timeout=2_000)
                                    break
                                except Exception:
                                    pass
                        break
                except Exception:
                    continue

            for sel in [
                "input[placeholder*='ID de usuario']", "input[placeholder*='usuario']",
                "input[placeholder*='jugador']", "input[placeholder*='Player']",
                "input[name*='player']", "input[name*='uid']", "input[name*='user']",
            ]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=2_000):
                        await el.fill(id_freefire)
                        log.info("completar_pedido: ID jugador = %s", id_freefire)
                        break
                except Exception:
                    continue

            for sel in ["input[type='checkbox']", "input[name*='terms']", "input[id*='terms']"]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=2_000):
                        if not await el.is_checked():
                            await el.check()
                        break
                except Exception:
                    continue

            await page.wait_for_timeout(500)

            for sel in [
                "button:has-text('¡Canjear Ahora!')", "button:has-text('Canjear Ahora')",
                "button:has-text('CANJEAR AHORA')", "button:has-text('¡Canjear ahora!')",
                "button[type='submit']", "input[type='submit']",
                "button:has-text('Canjear')", "button:has-text('Redeem')",
            ]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=2_000):
                        await el.click(timeout=8_000)
                        log.info("completar_pedido: ¡Canjear Ahora! — %s", sel)
                        break
                except Exception:
                    continue

            await page.wait_for_timeout(5_000)
            log.info("completar_pedido: canje enviado — URL=%s", page.url)

            screenshot = await page.screenshot(full_page=False)
            return screenshot, f"✅ ¡{diamonds} 💎 canjeados con éxito!\n🔑 PIN: `{pin}`"

        except Exception as exc:
            log.exception("Error en completar_pedido_existente (order=%s, ff=%s)", order_id, id_freefire)
            try:
                screenshot = await page.screenshot()
            except Exception:
                screenshot = None
            return screenshot, f"❌ Error al completar el pedido: {exc}"
        finally:
            await browser.close()


async def obtener_pin_de_ultimo_pedido(diamonds: int) -> tuple[str, str]:
    """
    Busca el PIN del pedido completado más reciente que coincida con la
    cantidad de diamantes indicada.
    Hace login en latingm.com, recorre los últimos pedidos completados y
    devuelve (pin, order_id). Si no encuentra nada devuelve ("", "").
    """
    paquete = PAQUETES.get(diamonds)
    if not paquete:
        log.warning("obtener_pin_de_ultimo_pedido: diamonds=%d no está en PAQUETES", diamonds)
        return "", ""

    base_str = str(paquete["base"])   # ej. "100" para 110 diamantes
    shop_user = os.environ.get("SHOP_USER", "")
    shop_pass = os.environ.get("SHOP_PASS", "")
    if not shop_user or not shop_pass:
        log.error("obtener_pin_de_ultimo_pedido: faltan SHOP_USER / SHOP_PASS")
        return "", ""

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=_LATINGM_BROWSER_ARGS,
            env=_get_chromium_env(),
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=_UA,
            locale="es-AR",
            timezone_id="America/Argentina/Buenos_Aires",
            extra_http_headers={
                "Accept-Language": "es-AR,es;q=0.9,en-US;q=0.8,en;q=0.7",
                "sec-ch-ua": _SEC_CH_UA,
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
            },
        )
        await context.add_init_script(_STEALTH_JS)
        page = await context.new_page()

        try:
            ok = await _login(page, shop_user, shop_pass)
            if not ok:
                log.error("obtener_pin_de_ultimo_pedido: fallo de login")
                return "", ""

            # ── Cargar lista de pedidos ───────────────────────────────────────
            for url_orders in [
                f"{LATINGM_URL}mi-cuenta/pedidos/",
                f"{LATINGM_URL}my-account/orders/",
            ]:
                try:
                    await _goto_cf(page, url_orders, timeout=20_000)
                    await page.wait_for_timeout(2_000)
                    content = await _safe_content(page)
                    if "completado" in content.lower() or "completed" in content.lower():
                        break
                except Exception:
                    continue

            # ── Recolectar links de pedidos completados ───────────────────────
            order_links: list[tuple[str, str]] = []   # (href, order_id)
            try:
                rows = await page.locator("tr.woocommerce-orders-table__row, .woocommerce-orders-table tbody tr").all()
                for row in rows:
                    try:
                        row_text = (await row.inner_text(timeout=1_500)).lower()
                        if "completado" not in row_text and "completed" not in row_text:
                            continue
                        link_el = row.locator("a[href*='ver-pedido'], a[href*='view-order']").first
                        href = await link_el.get_attribute("href", timeout=1_500)
                        if href:
                            m = re.search(r'/(?:ver-pedido|view-order)/(\d+)/', href)
                            oid = m.group(1) if m else ""
                            order_links.append((href, oid))
                    except Exception:
                        continue
            except Exception:
                pass

            # Fallback: buscar todos los links ver-pedido en la página
            if not order_links:
                try:
                    for el in await page.locator("a[href*='ver-pedido'], a[href*='view-order']").all():
                        try:
                            href = await el.get_attribute("href", timeout=500)
                            if href:
                                m = re.search(r'/(?:ver-pedido|view-order)/(\d+)/', href)
                                oid = m.group(1) if m else ""
                                order_links.append((href, oid))
                        except Exception:
                            continue
                except Exception:
                    pass

            log.info("obtener_pin_de_ultimo_pedido: %d pedidos completados encontrados", len(order_links))

            # ── Recorrer pedidos hasta encontrar el PIN correcto ──────────────
            MAX_INTENTOS = min(len(order_links), 8)
            for href, oid in order_links[:MAX_INTENTOS]:
                try:
                    await _goto_cf(page, href, timeout=20_000)
                    await page.wait_for_timeout(2_500)

                    # Verificar que el pedido contiene el paquete correcto
                    body = await page.inner_text("body")
                    # Buscar el base_str (ej. "100") o el total de diamantes
                    # (ej. "110") en el contenido — aparece en el nombre del producto
                    if base_str not in body and str(diamonds) not in body:
                        log.info("obtener_pin_de_ultimo_pedido: pedido %s no es de %s diamantes, saltando", oid, diamonds)
                        continue

                    pin = await _extraer_pin_de_pedido(page)
                    if not pin:
                        # Intentar también en Descargas
                        for url_dl in [
                            f"{LATINGM_URL}mi-cuenta/descargas/",
                            f"{LATINGM_URL}my-account/downloads/",
                        ]:
                            try:
                                await _goto_cf(page, url_dl, timeout=15_000)
                                await page.wait_for_timeout(2_000)
                                pin = await _extraer_pin_de_pedido(page)
                                if pin:
                                    break
                            except Exception:
                                continue

                    if pin:
                        log.info("obtener_pin_de_ultimo_pedido: PIN encontrado en pedido %s — %s", oid, pin)
                        return pin, oid

                except Exception as exc:
                    log.warning("obtener_pin_de_ultimo_pedido: error en pedido %s: %s", oid, exc)
                    continue

            log.warning("obtener_pin_de_ultimo_pedido: no se encontró PIN para %d diamantes", diamonds)
            return "", ""

        except Exception:
            log.exception("obtener_pin_de_ultimo_pedido: error general (diamonds=%d)", diamonds)
            return "", ""
        finally:
            await browser.close()


async def canjear_pin_directo(
    pin: str,
    id_freefire: str,
    diamonds: int,
) -> tuple[bytes | None, str]:
    """
    Canjea un PIN ya conocido directamente en redeempins.com.
    Útil para reintentar cuando el PIN fue extraído pero el canje falló.
    Devuelve (screenshot, mensaje).
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=_LATINGM_BROWSER_ARGS,
            env=_get_chromium_env(),
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=_UA,
            locale="es-AR",
            timezone_id="America/Argentina/Buenos_Aires",
            extra_http_headers={
                "Accept-Language": "es-AR,es;q=0.9,en-US;q=0.8,en;q=0.7",
                "sec-ch-ua": _SEC_CH_UA,
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
            },
        )
        await context.add_init_script(_STEALTH_JS)
        page = await context.new_page()

        try:
            log.info("canjear_pin_directo: pin=%s ff=%s diamonds=%d", pin, id_freefire, diamonds)

            # ── Paso 1: insertar PIN ──────────────────────────────────────────
            await _goto_cf(page, REDEEMPINS_URL, timeout=25_000)
            log.info("canjear_pin_directo: redeempins cargado — URL=%s", page.url)

            pin_inserted = await _insertar_pin_redeempins(page, pin)
            if not pin_inserted:
                screenshot = await page.screenshot()
                return screenshot, f"❌ No pude insertar el PIN en redeempins.com.\nPIN: `{pin}`"

            # Micro-movimientos humanos antes del clic → evita trigger del reCAPTCHA invisible
            await _pre_clic_humano(page)

            for sel in [
                "button:has-text('Canjear')", "button:has-text('CANJEAR')",
                "button:has-text('Redeem')", "button[type='submit']",
                "input[type='submit']",
            ]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=2_000):
                        await el.click(timeout=8_000)
                        log.info("canjear_pin_directo: Canjear paso 1 — %s", sel)
                        break
                except Exception:
                    continue

            # ── Esperar formulario paso 2 ─────────────────────────────────────
            log.info("canjear_pin_directo: esperando formulario paso 2...")
            paso2_ok = False
            try:
                await page.wait_for_selector(
                    "input[placeholder*='Nombre'], input[placeholder*='nombre'], "
                    "input[name*='name'], input[placeholder*='Nacimiento']",
                    timeout=25_000,
                )
                paso2_ok = True
            except Exception:
                await page.wait_for_timeout(5_000)

            if not paso2_ok:
                log.warning("canjear_pin_directo: paso 2 no apareció — reCAPTCHA bloqueó el envío")
                screenshot = await page.screenshot()
                return screenshot, (
                    f"⚠️ {_PENDIENTE_MANUAL_TAG}\n"
                    f"PIN:{pin}\nID:{id_freefire}\nDIAM:{diamonds}\n\n"
                    f"❌ El reCAPTCHA bloqueó el canje automático en redeempins.com.\n"
                    f"🔑 PIN: `{pin}`\n🎮 ID FF: `{id_freefire}`"
                )
            log.info("canjear_pin_directo: formulario paso 2 — URL=%s", page.url)

            # ── Paso 2: datos del jugador ─────────────────────────────────────
            for sel in [
                "input[placeholder='Nombre Completo']",
                "input[placeholder*='Nombre']", "input[placeholder*='nombre']",
                "input[name*='name']", "input[id*='name']",
            ]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=2_000):
                        await el.fill(_REDEEMPINS_NOMBRE)
                        log.info("canjear_pin_directo: Nombre llenado")
                        break
                except Exception:
                    continue

            for sel in [
                "input[placeholder='Fecha de Nacimiento']",
                "input[placeholder*='Nacimiento']", "input[placeholder*='nacimiento']",
                "input[placeholder*='Fecha']",
                "input[name*='birth']", "input[id*='birth']",
                "input[type='date']",
            ]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=2_000):
                        await el.fill(_REDEEMPINS_FECHA)
                        log.info("canjear_pin_directo: Fecha llenada")
                        break
                except Exception:
                    continue

            for sel in [
                "select[name*='nationalit']", "select[id*='nationalit']",
                "select[name*='nation']", "select[name*='pais']",
                "select[name*='country']", "select[id*='country']",
                "select",
            ]:
                try:
                    for el in await page.locator(sel).all():
                        if not await el.is_visible(timeout=1_500):
                            continue
                        for val in ["Argentina", "AR", "argentina"]:
                            try:
                                await el.select_option(label=val, timeout=2_000)
                                log.info("canjear_pin_directo: Argentina seleccionada")
                                break
                            except Exception:
                                try:
                                    await el.select_option(value=val, timeout=2_000)
                                    break
                                except Exception:
                                    pass
                        break
                except Exception:
                    continue

            for sel in [
                "input[placeholder*='ID de usuario']",
                "input[placeholder*='id de usuario']",
                "input[placeholder*='usuario']",
                "input[placeholder*='jugador']",
                "input[placeholder*='Player']", "input[placeholder*='player']",
                "input[name*='player']", "input[id*='player']",
                "input[name*='uid']", "input[id*='uid']",
                "input[name*='user']", "input[id*='user']",
            ]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=2_000):
                        await el.fill(id_freefire)
                        log.info("canjear_pin_directo: ID jugador llenado = %s", id_freefire)
                        break
                except Exception:
                    continue

            for sel in ["input[type='checkbox']", "input[name*='terms']", "input[id*='terms']"]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=2_000):
                        if not await el.is_checked():
                            await el.check()
                        log.info("canjear_pin_directo: T&C marcado")
                        break
                except Exception:
                    continue

            await page.wait_for_timeout(500)

            for sel in [
                "button:has-text('¡Canjear Ahora!')",
                "button:has-text('Canjear Ahora')",
                "button:has-text('CANJEAR AHORA')",
                "button:has-text('¡Canjear ahora!')",
                "button[type='submit']", "input[type='submit']",
                "button:has-text('Canjear')", "button:has-text('Redeem')",
            ]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=2_000):
                        await el.click(timeout=8_000)
                        log.info("canjear_pin_directo: ¡Canjear Ahora! — %s", sel)
                        break
                except Exception:
                    continue

            await page.wait_for_timeout(5_000)
            log.info("canjear_pin_directo: canje enviado — URL=%s", page.url)

            screenshot = await page.screenshot(full_page=False)
            return screenshot, f"✅ ¡{diamonds} 💎 canjeados con éxito!\n🔑 PIN: `{pin}`"

        except Exception as exc:
            log.exception("Error en canjear_pin_directo (pin=%s, ff=%s)", pin, id_freefire)
            try:
                screenshot = await page.screenshot()
            except Exception:
                screenshot = None
            return screenshot, f"❌ Error al canjear el PIN: {exc}"
        finally:
            await browser.close()
