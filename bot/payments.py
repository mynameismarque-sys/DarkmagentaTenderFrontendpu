"""Integración con la SDK de Mercado Pago: creación de preferencias de pago."""
import os
import logging
from dataclasses import dataclass

import mercadopago

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Pack:
    id: str
    nombre: str
    precio: float       # ARS
    creditos: float
    categoria: str = "proxy"   # "proxy" | "sensi" | "android" | "ios"


PACKS: dict[str, Pack] = {
    # ── Sensis ──────────────────────────────────────────────────────────────
    "sx":   Pack(id="sx",   nombre="Sensi Xitada",          precio=1000.0,  creditos=0.1,  categoria="sensi"),
    # ── Proxy ───────────────────────────────────────────────────────────────
    "1d":   Pack(id="1d",   nombre="1 Día",                  precio=2500.0,  creditos=1,    categoria="proxy"),
    "7d":   Pack(id="7d",   nombre="7 Días",                 precio=7000.0,  creditos=7,    categoria="proxy"),
    "15d":  Pack(id="15d",  nombre="15 Días",                precio=10000.0, creditos=15,   categoria="proxy"),
    "30d":  Pack(id="30d",  nombre="30 Días",                precio=16500.0, creditos=30,   categoria="proxy"),
    "3d_promo": Pack(id="3d_promo", nombre="3 Días (Promo)", precio=3200.0,  creditos=3,    categoria="proxy"),
    # ── Android regedits ─────────────────────────────────────────────────────
    "lion":       Pack(id="lion",       nombre="Lion Regedit",         precio=2500.0,  creditos=0, categoria="android"),
    "s1mple":     Pack(id="s1mple",     nombre="Regedit S1mple",       precio=3000.0,  creditos=0, categoria="android"),
    "vapev1":     Pack(id="vapev1",     nombre="Vape V1",              precio=5000.0,  creditos=0, categoria="android"),
    "trickff":    Pack(id="trickff",    nombre="Trick FF",             precio=8000.0,  creditos=0, categoria="android"),
    "auxandroid": Pack(id="auxandroid", nombre="Auxilio Android OB53", precio=8500.0,  creditos=0, categoria="android"),
    "klz":        Pack(id="klz",        nombre="KLZ Modded",           precio=9000.0,  creditos=0, categoria="android"),
    "moddedv09":  Pack(id="moddedv09",  nombre="Modded V09",           precio=9500.0,  creditos=0, categoria="android"),
    # ── iOS ──────────────────────────────────────────────────────────────────
    "headtrick": Pack(id="headtrick", nombre="HEADTRICK iOS",       precio=5000.0, creditos=0, categoria="ios"),
    "trickios":  Pack(id="trickios",  nombre="TRICK iOS",           precio=5000.0, creditos=0, categoria="ios"),
    "auxmira":   Pack(id="auxmira",   nombre="Auxilio de Mira iOS", precio=6000.0, creditos=0, categoria="ios"),
    "regedit75": Pack(id="regedit75", nombre="Regedit 75% iOS",     precio=5000.0, creditos=0, categoria="ios"),
}


# ── Catálogo Android: pack_id → (url_descarga, url_tutorial) ─────────────────
ANDROID_CATALOG: dict[str, tuple[str, str]] = {
    "lion": (
        "https://www.mediafire.com/file/4ps6ydnxm5xidkn/Lion_Regedit_%2540markee.4.rar/file",
        "https://youtu.be/neUfRIn_-is",
    ),
    "s1mple": (
        "https://www.mediafire.com/file/uxas8cz9zaorzzb/REGEDIT_S1MPLE_%2540markee.4.rar/file",
        "https://youtu.be/IyQQHE720KM",
    ),
    "vapev1": (
        "https://www.mediafire.com/file/ujssvn7w7pkn3xq/VAPE_V1_%2540markee.4.rar/file",
        "https://youtu.be/c8ZhiBGWSEU",
    ),
    "trickff": (
        "https://www.mediafire.com/file/060uik1xoytlz9q/Trick_FF_%2540markee.4.rar/file",
        "https://youtu.be/ESh0DSWiVOE",
    ),
    "auxandroid": (
        "https://www.mediafire.com/file/t34mgwa4x9txsmj/Auxilio_Android_OB53_%2540markee.4.rar/file",
        "https://youtu.be/2h9Iv68kArQ",
    ),
    "klz": (
        "https://www.mediafire.com/file/kzoj7dzpr58tj44/KLZ_MODDED_%2540markee.4.rar/file",
        "https://youtu.be/5tUCXBjxcZs",
    ),
    "moddedv09": (
        "https://www.mediafire.com/file/obw3tbq7q8lcapd/MODDED_V09_%2540markee.4.rar/file",
        "https://youtu.be/XjKlze7tHf0",
    ),
}

# ── iOS: nombre del producto para el mensaje de WhatsApp ─────────────────────
IOS_PRODUCTOS: dict[str, str] = {
    "headtrick": "HEADTRICK",
    "trickios":  "TRICK",
    "auxmira":   "AUXILIO DE MIRA",
    "regedit75": "REGEDIT 75%",
}
IOS_WA_NUMERO = "5491140501714"


def _sdk() -> mercadopago.SDK:
    token = os.environ.get("MP_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("Falta el secret MP_ACCESS_TOKEN")
    return mercadopago.SDK(token)


def _public_base_url() -> str | None:
    """Devuelve SOLO la URL de producción (.replit.app).
    
    Dominios dev (.replit.dev, kirk.replit.dev, etc.) son rechazados por
    MercadoPago como notification_url → devuelve None para no incluirlos.
    """
    domains = os.environ.get("REPLIT_DOMAINS", "")
    for d in domains.split(","):
        d = d.strip()
        # Solo aceptar dominios de producción (.replit.app)
        if d and d.endswith(".replit.app"):
            return f"https://{d}"
    return None


def crear_preferencia(pack_id: str, discord_user_id: str) -> dict:
    """Crea una preferencia en Mercado Pago para el pack indicado.

    Devuelve un dict con `init_point`, `id` y `pack`.
    """
    pack = PACKS[pack_id]
    sdk = _sdk()
    base_url = _public_base_url()

    token = os.environ.get("MP_ACCESS_TOKEN", "")
    log.info(
        "Creando preferencia MP pack=%s user=%s | token_prefix=%s | base_url=%s",
        pack.id, discord_user_id, token[:12] if token else "MISSING", base_url or "none (sin webhook)",
    )

    preference_data = {
        "items": [
            {
                "title": f"Sensi Marke — {pack.nombre}",
                "quantity": 1,
                "unit_price": float(pack.precio),
                "currency_id": "ARS",
            }
        ],
        "external_reference": f"{discord_user_id}-{pack.id}",
        "statement_descriptor": "SENSIMARKE",
    }

    # notification_url y back_urls solo cuando hay dominio de producción estable.
    # MP rechaza dominios dev (*.replit.dev) y lanza PA_UNAUTHORIZED_RESULT_FROM_POLICIES.
    if base_url:
        preference_data["notification_url"] = f"{base_url}/api/mp-webhook"
        preference_data["back_urls"] = {
            "success": f"{base_url}/api/mp-return?status=success",
            "failure": f"{base_url}/api/mp-return?status=failure",
            "pending": f"{base_url}/api/mp-return?status=pending",
        }
        preference_data["auto_return"] = "approved"

    res = sdk.preference().create(preference_data)
    log.debug("MP response: %s", res)

    if res.get("status") not in (200, 201):
        log.error("Error MP: %s", res)
        raise RuntimeError(
            f"Mercado Pago devolvió status {res.get('status')}: {res.get('response')}"
        )

    body = res["response"]
    init_point = body.get("init_point") or body.get("sandbox_init_point")
    log.info("Preferencia MP creada OK: id=%s init_point=%s", body.get("id"), init_point)
    return {
        "id": body["id"],
        "init_point": init_point,
        "pack": pack,
    }


def obtener_pago(payment_id: str) -> dict | None:
    """Consulta el detalle de un pago por su ID."""
    sdk = _sdk()
    res = sdk.payment().get(payment_id)
    if res.get("status") != 200:
        log.warning("No se pudo obtener pago %s: %s", payment_id, res)
        return None
    return res.get("response")
