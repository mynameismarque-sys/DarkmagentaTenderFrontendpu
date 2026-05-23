"""Servidor Flask que recibe los webhooks de Mercado Pago y sirve archivos estáticos."""
import logging
import os
import threading
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_from_directory

# ── Deduplicación en memoria para webhooks concurrentes ──────────────────────
# MP manda el mismo webhook 2+ veces casi simultáneamente.  El check de DB
# (payment_exists) no protege contra esto porque ambos threads lo leen antes
# de que el primero termine de escribir.  Este lock en memoria cubre ese hueco.
_mp_processing_lock = threading.Lock()
_mp_processing_ids: set[str] = set()   # payment_ids actualmente en proceso

ASSETS_DIR = Path(__file__).parent.parent / "assets"
MEDIA_DIR  = Path(__file__).parent.parent / "tmp_media"
MEDIA_DIR.mkdir(exist_ok=True)

from . import database, payments

log = logging.getLogger(__name__)


def create_app(notify_callback, whatsapp_callback=None) -> Flask:
    """Crea la app Flask.
    - `notify_callback(discord_id, pack, total)` se llama cuando un pago se aprueba.
    - `whatsapp_callback(body, from_number)` se llama cuando llega un mensaje de WhatsApp.
    """
    app = Flask(__name__)

    @app.get("/")
    def index():
        return jsonify(
            {
                "service": "DitxDev Bot",
                "status": "ok",
                "webhook": "/api/mp-webhook",
            }
        )

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok"})

    @app.get("/assets/<path:filename>")
    def serve_asset(filename):
        if not ASSETS_DIR.exists():
            return jsonify({"error": "assets folder not found"}), 404
        return send_from_directory(str(ASSETS_DIR), filename)

    @app.get("/media/<path:filename>")
    def serve_media(filename):
        if not MEDIA_DIR.exists():
            return jsonify({"error": "media folder not found"}), 404
        return send_from_directory(str(MEDIA_DIR), filename)

    @app.get("/api/mp-return")
    def mp_return():
        status = request.args.get("status", "unknown")
        return (
            f"<html><body style='font-family:sans-serif;text-align:center;padding:48px'>"
            f"<h1>Pago {status}</h1>"
            f"<p>Volvé a Discord para ver tus créditos actualizados.</p>"
            f"</body></html>"
        )

    @app.route("/api/mp-webhook", methods=["POST", "GET"])
    def mp_webhook():
        # Mercado Pago manda el evento como query params + JSON body
        data: dict[str, Any] = request.get_json(silent=True) or {}
        topic = (
            request.args.get("topic")
            or request.args.get("type")
            or data.get("type")
            or data.get("topic")
        )
        payment_id = (
            request.args.get("data.id")
            or request.args.get("id")
            or (data.get("data") or {}).get("id")
            or data.get("id")
        )

        log.info("💳 MP webhook recibido — topic=%s payment_id=%s", topic, payment_id)

        if topic not in ("payment", "payments") or not payment_id:
            log.info("💳 MP webhook ignorado (topic=%s no es payment)", topic)
            return jsonify({"received": True}), 200

        pago = payments.obtener_pago(str(payment_id))
        if not pago:
            log.error("💳 MP webhook — no se pudo obtener el pago %s desde la API", payment_id)
            return jsonify({"error": "no se pudo obtener el pago"}), 200

        status = pago.get("status")
        external_reference = pago.get("external_reference") or ""
        amount = float(pago.get("transaction_amount") or 0)
        payer_email = (pago.get("payer") or {}).get("email", "desconocido")
        payment_method = pago.get("payment_method_id", "desconocido")

        log.info(
            "💳 MP pago %s — status=%s monto=$%.2f metodo=%s email=%s ref=%s",
            payment_id, status, amount, payment_method, payer_email, external_reference,
        )

        if status != "approved":
            log.info("💳 MP pago %s — estado '%s', no se acredita", payment_id, status)
            return jsonify({"received": True, "status": status}), 200

        # ── Dedup en memoria (race condition entre threads simultáneos) ──────
        pid_str = str(payment_id)
        with _mp_processing_lock:
            if pid_str in _mp_processing_ids:
                log.info(
                    "💳 MP pago %s ya en proceso en otro thread — ignorando duplicado concurrente",
                    payment_id,
                )
                return jsonify({"received": True, "duplicate": True}), 200
            _mp_processing_ids.add(pid_str)

        try:
            # ── Dedup en DB (para webhooks que llegan después del procesamiento) ──
            if database.payment_exists(pid_str):
                log.info("💳 MP pago %s ya procesado anteriormente, ignorando duplicado", payment_id)
                return jsonify({"received": True, "duplicate": True}), 200

            # external_reference = "discord_id-pack_id"
            try:
                discord_id, pack_id = external_reference.split("-", 1)
            except ValueError:
                log.error("💳 MP pago %s — external_reference inválida: %r", payment_id, external_reference)
                return jsonify({"error": "external_reference inválida"}), 200

            pack = payments.PACKS.get(pack_id)
            if not pack:
                log.error("💳 MP pago %s — pack_id desconocido: %s", payment_id, pack_id)
                return jsonify({"error": "pack desconocido"}), 200

            log.info(
                "💳 MP PAGO APROBADO — payment_id=%s monto=$%.2f pack=%s usuario=%s email=%s",
                payment_id, amount, pack.nombre, discord_id, payer_email,
            )

            # Créditos separados por categoría del pack
            if pack.categoria == "sensi":
                nuevo_total = database.add_sensi_credits(discord_id, pack.creditos)
            elif pack.categoria in ("android", "ios"):
                nuevo_total = 0   # No se suman créditos; entrega automática por DM
            else:
                nuevo_total = database.add_credits(discord_id, pack.creditos)
            database.record_payment(
                payment_id=pid_str,
                discord_id=discord_id,
                pack=pack.id,
                credits_added=pack.creditos,
                amount=amount,
                status=status,
            )

            try:
                notify_callback(discord_id, pack, nuevo_total, amount)
            except Exception:  # noqa: BLE001
                log.exception("💳 MP pago %s — falló la notificación a Discord para %s", payment_id, discord_id)

            return jsonify({"received": True, "credits_total": nuevo_total}), 200

        finally:
            # Liberar el lock en memoria al terminar (éxito o error)
            with _mp_processing_lock:
                _mp_processing_ids.discard(pid_str)

    @app.route("/api/whatsapp-webhook", methods=["POST"])
    def whatsapp_webhook():
        from_number = request.form.get("From", "")
        body = (request.form.get("Body") or "").strip()

        if not body:
            return "", 204

        log.info("WhatsApp recibido de %s: %r", from_number, body[:120])

        if whatsapp_callback:
            try:
                whatsapp_callback(body, from_number)
            except Exception:
                log.exception("Error en whatsapp_callback")

        return "", 204

    return app


def _serve_on(app: Flask, port: int) -> None:
    try:
        app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)
    except OSError as exc:
        log.warning("No pude iniciar Flask en puerto %d: %s", port, exc)


def run_flask(app: Flask) -> None:
    import threading as _t
    # Escuchamos en 5000 (health-check del workflow) y en 8081 (proxy público)
    for port in (5000, 8081):
        log.info("Flask escuchando en 0.0.0.0:%d", port)
        _t.Thread(target=_serve_on, args=(app, port), daemon=True, name=f"flask-{port}").start()
