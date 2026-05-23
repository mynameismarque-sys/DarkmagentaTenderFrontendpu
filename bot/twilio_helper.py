"""Helpers para enviar mensajes de WhatsApp via Twilio."""
import logging
import os

log = logging.getLogger(__name__)


def _get_credentials() -> tuple[str | None, str | None, str, str | None]:
    sid   = os.environ.get("TWILIO_ACCOUNT_SID")
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    from_ = os.environ.get("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
    to    = os.environ.get("TWILIO_WHATSAPP_TO")
    return sid, token, from_, to


def send_whatsapp(body: str) -> bool:
    """Envía un mensaje de WhatsApp via Twilio. Retorna True si fue exitoso."""
    sid, token, from_, to = _get_credentials()
    if not all([sid, token, to]):
        log.warning("Twilio no configurado — notificación WhatsApp omitida")
        return False
    try:
        from twilio.rest import Client
        c = Client(sid, token)
        c.messages.create(body=body, from_=from_, to=to)
        log.info("WhatsApp enviado OK: %r", body[:80])
        return True
    except Exception:
        log.exception("Error enviando WhatsApp via Twilio")
        return False


def send_whatsapp_media(body: str, media_url: str) -> bool:
    """Envía un mensaje de WhatsApp con imagen adjunta via Twilio."""
    sid, token, from_, to = _get_credentials()
    if not all([sid, token, to]):
        log.warning("Twilio no configurado — notificación WhatsApp omitida")
        return False
    try:
        from twilio.rest import Client
        c = Client(sid, token)
        c.messages.create(body=body, from_=from_, to=to, media_url=[media_url])
        log.info("WhatsApp con imagen enviado OK: %r", body[:80])
        return True
    except Exception:
        log.exception("Error enviando WhatsApp con imagen via Twilio")
        return False
