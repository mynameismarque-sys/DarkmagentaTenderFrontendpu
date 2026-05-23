"""Entrypoint del bot: arranca Discord + Flask en el mismo proceso."""
import asyncio
import base64
import collections
import datetime
import email as email_lib
import imaplib
import io
import logging
import os
import random
import re
import string
import time
import threading
from email.header import decode_header
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
import openai as _openai_lib

from . import automation, comprobante_ai, database, latingm_scraper, payments, telegram_client, webhook_server, twilio_helper

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("bot")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_ID = os.environ.get("DISCORD_GUILD_ID")
ADMIN_ROLE_ID = os.environ.get("ADMIN_ROLE_ID")

# ---------------------------------------------------------------------------
# Binance Pay — configuración manual
# ---------------------------------------------------------------------------
BINANCE_ID = "1037986525"
_PROD_URL = "https://registro-automatizado.replit.app"


def _public_base_url() -> str:
    """URL pública del bot. REPLIT_DOMAINS se setea en producción; DEV_DOMAIN en dev."""
    domains = os.environ.get("REPLIT_DOMAINS", "")
    if domains:
        return f"https://{domains.split(',')[0].strip()}"
    dev = os.environ.get("REPLIT_DEV_DOMAIN", "")
    if dev:
        return f"https://{dev}"
    return _PROD_URL


_REPLIT_DOMAIN = _public_base_url().replace("https://", "")
BINANCE_QR_PUBLIC_URL = f"{_public_base_url()}/assets/binance_qr.png"

# Naranja X / alias bancario — datos de transferencia
# ---------------------------------------------------------------------------
NARANJA_X_CBU   = "4530000800012463323851"
NARANJA_X_ALIAS = "agusmarquesini.nx"
NARANJA_X_LOGO_URL = (
    f"https://{_REPLIT_DOMAIN}/assets/naranjax_logo.png" if _REPLIT_DOMAIN else None
)
# Horario en que se muestran los links de Mercado Pago (hora Argentina UTC-3)
# Fuera de ese horario se muestra el alias Naranja X para transferencia manual
import datetime as _dt
_HORA_MP_DESDE = _dt.time(4, 0)   # 04:00 AM
_HORA_MP_HASTA = _dt.time(9, 30)  # 09:30 AM


_TZ_ARG = _dt.timezone(_dt.timedelta(hours=-3))


def _ahora_arg() -> _dt.datetime:
    """Hora actual en Argentina (UTC-3)."""
    return _dt.datetime.now(_TZ_ARG)


def _en_horario_mp() -> bool:
    """True si ahora es entre las 4:00 AM y 9:30 AM hora Argentina (UTC-3)."""
    t = _ahora_arg().time()
    return _HORA_MP_DESDE <= t <= _HORA_MP_HASTA


# ---------------------------------------------------------------------------
# Variable de estado global para automatización de Mercado Pago
# ---------------------------------------------------------------------------
MODO_AUTO_MP: bool = False

# ID del emoji personalizado de la bandera argentina (se carga en on_ready)
_ARG_EMOJI_ID: int | None = None


# ---------------------------------------------------------------------------
# Cliente OpenAI (Replit AI Integrations proxy — no requiere clave propia)
# ---------------------------------------------------------------------------
_openai_client = _openai_lib.AsyncOpenAI(
    base_url=os.environ.get("AI_INTEGRATIONS_OPENAI_BASE_URL"),
    api_key=os.environ.get("AI_INTEGRATIONS_OPENAI_API_KEY", "placeholder"),
)


# ---------------------------------------------------------------------------
# Discord client
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.invites = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


class _DeferFailed(Exception):
    """Se lanza cuando defer() no pudo responder a Discord.

    Propagada automáticamente a on_app_command_error (slash commands)
    o View.on_error (botones). No hay que manejarla en cada handler.
    """


class _SafeViewMixin:
    """Mixin para discord.ui.View que maneja _DeferFailed silenciosamente."""

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item,
    ) -> None:
        if isinstance(error, _DeferFailed):
            log.warning(
                "Botón '%s' de %s: interacción expirada antes del defer.",
                getattr(item, "custom_id", getattr(item, "label", "?")),
                interaction.user,
            )
            return
        log.exception(
            "Error inesperado en botón '%s' para %s",
            getattr(item, "custom_id", getattr(item, "label", "?")),
            interaction.user,
        )
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ Ocurrió un error. Intentá de nuevo.", ephemeral=True
                )
            else:
                # Ya fue deferida → usar followup para que el usuario vea el error
                await interaction.followup.send(
                    "❌ Ocurrió un error. Intentá de nuevo.", ephemeral=True
                )
        except Exception:
            pass


async def _safe_defer(
    interaction: discord.Interaction,
    ephemeral: bool = True,
    thinking: bool = False,
) -> None:
    """Defer la interacción con manejo de errores.

    Lanza _DeferFailed si no se pudo responder a Discord (interacción
    expirada, red lenta, etc.). Se propaga automáticamente al error handler.

    NOTA: sin asyncio.wait_for — ese wrapper cancelaba el HTTP request antes
    de que Discord lo procesara, causando 'La aplicación no respondió'.
    """
    try:
        await interaction.response.defer(ephemeral=ephemeral, thinking=thinking)
    except discord.InteractionResponded:
        pass   # ya respondida (p.ej. doble click), followup funciona igual
    except Exception as e:
        log.warning(
            "defer() falló para %s en '%s': %s",
            interaction.user,
            getattr(interaction.command, "name", "button/modal"),
            e,
        )
        raise _DeferFailed(str(e)) from e


@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    cmd_name = interaction.command.name if interaction.command else "desconocido"
    # Unwrap el error real si viene envuelto en CommandInvokeError
    real_error = getattr(error, "original", error)

    # _DeferFailed = interacción expirada antes de poder responder.
    # No intentamos enviar nada (la interacción ya está muerta) y no logueamos
    # como error (es esperado cuando hay lag de red o event loop ocupado).
    if isinstance(real_error, _DeferFailed):
        log.warning(
            "Interacción '/%s' de %s expiró antes del defer: %s",
            cmd_name, interaction.user, real_error,
        )
        return

    # Sin permisos → mensaje claro en lugar del error genérico
    if isinstance(error, app_commands.MissingPermissions):
        try:
            await interaction.response.send_message(
                "🚫 No tenés permisos para usar este comando. Se requiere **Administrador**.",
                ephemeral=True,
            )
        except Exception:
            pass
        return

    log.error(
        "Error en slash command '/%s' invocado por %s (%s): %s",
        cmd_name, interaction.user, interaction.user.id, real_error,
        exc_info=real_error,
    )
    msg = (
        f"⚠️ Ocurrió un error inesperado en `/{cmd_name}`.\n"
        "Intentá de nuevo en unos segundos. Si el problema persiste, abrí un ticket."
    )
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass


def _generar_key_proxy() -> str:
    """Genera una key única con formato MARKE + 6 chars alfanuméricos mayúsculas."""
    chars = string.ascii_uppercase + string.digits
    suffix = "".join(random.choices(chars, k=6))
    return f"MARKE{suffix}"


def _puede_registrar(interaction: discord.Interaction) -> bool:
    """True si el usuario tiene rol de administrador (para comandos admin)."""
    if isinstance(interaction.user, discord.Member):
        if interaction.user.guild_permissions.administrator:
            return True
        if ADMIN_ROLE_ID:
            try:
                rid = int(ADMIN_ROLE_ID)
                if any(r.id == rid for r in interaction.user.roles):
                    return True
            except ValueError:
                pass
        # Si no hay rol configurado y no es admin → permitido sólo si no se exige rol
        return not bool(ADMIN_ROLE_ID)
    return False


def _es_verificado(interaction: discord.Interaction) -> bool:
    """True si el usuario está autorizado a usar comandos de proxy.

    Autorizamos en cualquiera de estos casos (cualquiera de los 3 alcanza):
      1. Es administrador (rol admin o permisos de admin del guild).
      2. Tiene el rol Verificado en Discord.
      3. Tiene créditos (de proxy o sensi) en la DB del bot. Si pagó,
         está verificado: el rol es decoración y a veces falla en asignarse.

    Independiente de si interaction.user es Member o User: lo único que
    necesitamos es el id para chequear créditos en la DB.
    """
    user = interaction.user
    user_id = getattr(user, "id", None)
    if user_id is None:
        log.warning("_es_verificado: interaction.user sin id (tipo=%s)", type(user).__name__)
        return False

    # 1) Admin
    try:
        if _puede_registrar(interaction):
            log.info("_es_verificado(%s): OK por admin/rol-admin", user_id)
            return True
    except Exception:
        log.exception("_es_verificado: error chequeando admin para %s", user_id)

    # 2) Rol Verificado (solo si es Member, los User no tienen .roles)
    try:
        roles = getattr(user, "roles", None)
        if roles and any(r.id == ROL_VERIFICADO_ID for r in roles):
            log.info("_es_verificado(%s): OK por rol Verificado", user_id)
            return True
    except Exception:
        log.exception("_es_verificado: error leyendo roles de %s", user_id)

    # 3) Pagó alguna vez (existe en users o tiene pago aprobado) → verificado.
    #    Esto cubre tanto a quien tiene saldo como a quien lo gastó pero ya
    #    es cliente histórico. El control de saldo lo hace cada comando aparte.
    try:
        discord_id = str(user_id)
        if database.ha_pagado_alguna_vez(discord_id):
            creditos_proxy = database.get_credits(discord_id)
            creditos_sensi = database.get_sensi_credits(discord_id)
            log.info(
                "_es_verificado(%s): OK por historial de pago (proxy=%s sensi=%s) — "
                "intento asignar rol en background",
                user_id, creditos_proxy, creditos_sensi,
            )
            try:
                asyncio.create_task(
                    _asignar_rol_verificado(
                        user_id,
                        motivo="Auto-fix: pagó alguna vez pero no tiene rol",
                    )
                )
            except Exception:
                log.exception(
                    "_es_verificado: no pude lanzar asignación de rol bg para %s",
                    user_id,
                )
            return True
    except Exception:
        log.exception(
            "_es_verificado: error chequeando historial de pago para %s", user_id
        )

    log.warning(
        "_es_verificado(%s) → False (no admin, sin rol Verificado, nunca pagó)",
        user_id,
    )
    return False


# ---------------------------------------------------------------------------
# /registrar — DESACTIVADO (sistema de keys reemplaza el de créditos)
# ---------------------------------------------------------------------------
@tree.command(name="registrar", description="[Desactivado] El sistema de proxy ahora funciona por keys")
async def registrar(
    interaction: discord.Interaction,
):
    await _safe_defer(interaction, ephemeral=True)
    await interaction.followup.send(
        "❌ Este comando fue desactivado. El proxy ahora funciona con **keys**.\n"
        "Usá `/comprar` para adquirir tu plan y `/key` para activarlo.",
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# /gratismarke — key gratuita de 3 días (una vez por persona, ventana de 42h)
# ---------------------------------------------------------------------------
@tree.command(
    name="gratismarke",
    description="[Desactivado] Promo gratuita no disponible por el momento",
)
async def gratismarke(interaction: discord.Interaction):
    await _safe_defer(interaction, ephemeral=True)
    await interaction.followup.send(
        "❌ El comando `/gratismarke` está desactivado por el momento.\n"
        "Usá `/comprar` para adquirir tu plan.",
        ephemeral=True,
    )
    return
    await _safe_defer(interaction, ephemeral=True, thinking=True)

    es_admin = _puede_registrar(interaction)

    # Verificar que la ventana de 42h esté activa (admins la saltan)
    if not es_admin:
        expiry_str = database.get_config("gratismarke_expiry")
        if not expiry_str or time.time() > float(expiry_str):
            await interaction.followup.send(
                "⏰ La promo gratuita no está activa en este momento.\n"
                "Cuando se active una nueva promo te avisamos por el servidor.",
                ephemeral=True,
            )
            return

    discord_id = str(interaction.user.id)

    if not es_admin and database.has_used_free_trial(discord_id):
        await interaction.followup.send(
            "Ya usaste tu key gratuita. Para seguir usando el proxy, usá `/comprar`.",
            ephemeral=True,
        )
        return

    if not telegram_client.is_ready():
        await interaction.followup.send(
            "❌ El sistema de generación de keys no está disponible ahora. Intentá en unos minutos.",
            ephemeral=True,
        )
        return

    key = _generar_key_proxy()
    try:
        await telegram_client.cmd_gen(key, 3)
        log.info("🔑 KEY GRATIS — key=%s dias=3 usuario=%s (%s)", key, interaction.user, interaction.user.id)
        asyncio.create_task(_log_key(
            "gen_ok", key, interaction.user.id,
            dias=3, metodo="Key gratis (prueba)",
        ))
    except Exception as exc:
        log.exception("Error generando key gratuita via Telegram para %s", discord_id)
        asyncio.create_task(_log_key(
            "gen_error", key, interaction.user.id,
            dias=3, metodo="Key gratis (prueba)", error=str(exc),
        ))
        await interaction.followup.send(
            f"❌ No se pudo generar tu key en este momento. Intentá de nuevo en unos minutos.\n"
            f"```{exc}```",
            ephemeral=True,
        )
        return

    database.mark_free_trial_used(discord_id)

    embed = discord.Embed(
        title="🎁 Tu key gratuita de 3 días",
        description=(
            f"¡Listo! Tu key de prueba por **3 días** está activa.\n\n"
            f"🔑 **Tu key:**\n```\n{key}\n```\n"
            f"Usá el comando `/key` en el servidor para activarla con tu IP.\n\n"
            f"🌐 **Servidor:** `108.181.215.247`\n"
            f"👔 **Puerto Cuello:** `10065`\n"
            f"👕 **Puerto Pecho:** `10066`\n"
            f"👤 **Login:** ||DGZADAXFF||\n"
            f"🔒 **Contraseña:** ||DGZADAXFF||\n\n"
            + (f"▶️ **Tutorial de configuración:**\n{PROXY_TUTORIAL_URL}\n\n" if PROXY_TUTORIAL_URL else "")
            + "📲 **Grupo de WhatsApp:**\nhttps://chat.whatsapp.com/DQxndyWBG860vpaVcxam3s"
        ),
        color=0xF1C40F,
    )
    embed.set_footer(text="Esta prueba solo se puede usar una vez por cuenta.")

    try:
        await interaction.user.send(embed=embed)
        await interaction.followup.send(
            "✅ Te mandé tu key por mensaje privado. Usá `/key` para activarla.",
            ephemeral=True,
        )
    except discord.Forbidden:
        await interaction.followup.send(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# /comprar — menú desplegable con los packs
# ---------------------------------------------------------------------------
def _fmt_creditos(c: float) -> str:
    """Formatea créditos: enteros sin decimal, decimales con 1 cifra."""
    return f"{c:g}"


# ── Nota de soporte que se incluye en cada entrega de regedit ────────────────
NOTA_SOPORTE = (
    "⚠️ *Este archivo optimiza la estabilidad de la mira para mejorar "
    "la precisión. No es un aimbot.*"
)

# ── Tutorial del proxy ──
PROXY_TUTORIAL_URL: str = "https://www.youtube.com/watch?v=bNxxIBlbWWka"


async def _enviar_entrega_android(user: discord.User, pack: "payments.Pack") -> None:
    """Envía el link de descarga y tutorial por DM para packs Android."""
    import urllib.parse as _urlparse
    catalog = payments.ANDROID_CATALOG.get(pack.id)
    if not catalog:
        log.warning("No encontré catálogo Android para pack %s", pack.id)
        return
    download_url, tutorial_url = catalog
    embed = discord.Embed(
        title=f"📦 Tu archivo Android: {pack.nombre}",
        description=(
            f"¡Gracias por tu compra! Acá está tu descarga:\n\n"
            f"**📥 Descarga:**\n{download_url}\n\n"
            f"**▶️ Tutorial de instalación:**\n{tutorial_url}\n\n"
            f"{NOTA_SOPORTE}"
        ),
        color=0x2ECC71,
    )
    embed.set_footer(text="Marke Panel • Soporte disponible en el servidor")
    try:
        await user.send(embed=embed)
    except discord.Forbidden:
        log.warning("DM bloqueado para %s en entrega Android", user.id)
    except Exception:
        log.exception("Error enviando DM Android a %s", user.id)


async def _enviar_entrega_ios(user: discord.User, pack: "payments.Pack") -> None:
    """Envía el link de WhatsApp por DM para packs iOS."""
    import urllib.parse as _urlparse
    producto = payments.IOS_PRODUCTOS.get(pack.id, pack.nombre)
    texto_wa = _urlparse.quote(f"Hola Markee, vengo por el archivo {producto} de iOS")
    wa_url = f"https://wa.me/{payments.IOS_WA_NUMERO}?text={texto_wa}"
    embed = discord.Embed(
        title=f"🍎 Tu archivo iOS: {pack.nombre}",
        description=(
            f"¡Gracias por tu compra! Para recibir tu archivo, contactá a Markee por WhatsApp:\n\n"
            f"**📱 WhatsApp:**\n{wa_url}\n\n"
            f"Escribí desde ese link y Markee te enviará el archivo.\n\n"
            f"{NOTA_SOPORTE}"
        ),
        color=0x3498DB,
    )
    embed.set_footer(text="Marke Panel • Soporte disponible en el servidor")
    try:
        await user.send(embed=embed)
    except discord.Forbidden:
        log.warning("DM bloqueado para %s en entrega iOS", user.id)
    except Exception:
        log.exception("Error enviando DM iOS a %s", user.id)


class PackSelect(discord.ui.Select):
    def __init__(self, modo: str = "proxy"):
        """
        modo = "sensi"   → solo el pack Sensi Xitada
        modo = "proxy"   → solo los packs de proxy
        modo = "android" → solo los regedits Android
        modo = "ios"     → solo los packs iOS
        modo = "regedit" → Android + iOS juntos
        custom_id fijo por modo para que el View sobreviva reinicios del bot.
        """
        self._modo = modo
        options = []
        promo_activa = database.get_config("proxy_promo_active") == "1"
        for p in payments.PACKS.values():
            if modo == "sensi"   and p.categoria != "sensi":   continue
            if modo == "proxy"   and p.categoria != "proxy":   continue
            if modo == "android" and p.categoria != "android": continue
            if modo == "ios"     and p.categoria != "ios":     continue
            if modo == "regedit" and p.categoria not in ("android", "ios"): continue
            # El pack de promo solo se muestra cuando está activa
            if p.id == "3d_promo" and not promo_activa:        continue

            if p.categoria == "sensi":
                label = f"🎯 {p.nombre} — ${p.precio:,.0f} ARS"
                desc  = f"1 consulta de sensibilidad ({_fmt_creditos(p.creditos)} crédito)"
            elif p.categoria == "android":
                label = f"🤖 {p.nombre} — ${p.precio:,.0f} ARS"
                desc  = "Descarga + tutorial automático por DM"
            elif p.categoria == "ios":
                label = f"🍎 {p.nombre} — ${p.precio:,.0f} ARS"
                desc  = "Link de contacto por WhatsApp vía DM"
            elif p.id == "3d_promo":
                label = f"🔥 {p.nombre} — ${p.precio:,.0f} ARS (PROMO)"
                desc  = f"Key de {int(p.creditos)} días enviada por DM al instante"
            else:
                dias = int(p.creditos)
                label = f"🌐 {p.nombre} — ${p.precio:,.0f} ARS"
                desc  = f"Key de {dias} día{'s' if dias != 1 else ''} enviada por DM al instante"
            options.append(discord.SelectOption(label=label, description=desc, value=p.id))

        placeholder_map = {
            "sensi":   "Comprá tu Sensi Xitada...",
            "proxy":   "Elegí un pack de proxy...",
            "android": "Elegí tu regedit Android...",
            "ios":     "Elegí tu archivo iOS...",
            "regedit": "Elegí tu regedit Android o iOS...",
        }
        placeholder = placeholder_map.get(modo, "Elegí un pack...")
        super().__init__(
            custom_id=f"pack_select_{modo}",   # ID fijo → View persistente
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        pack_id = self.values[0]
        await _safe_defer(interaction, ephemeral=True, thinking=True)

        # Intento crear preferencia de Mercado Pago. Si falla NO interrumpo
        # el flujo: el user igual podrá pagar por Naranja X o Binance.
        pack = payments.PACKS[pack_id]
        mp_url: str | None = None
        try:
            pref = await asyncio.to_thread(
                payments.crear_preferencia, pack_id, str(interaction.user.id)
            )
            pack = pref["pack"]
            mp_url = pref.get("init_point")
        except Exception:
            log.exception(
                "Error creando preferencia MP para pack=%s user=%s — "
                "sigo igual con NX/Binance",
                pack_id, interaction.user.id,
            )

        # Texto del embed: armado dinámico según categoría y métodos disponibles.
        if pack.categoria in ("android", "ios"):
            entrega_txt = (
                "Descarga + tutorial por DM 🤖" if pack.categoria == "android"
                else "Link de WhatsApp por DM 🍎"
            )
            partes = [
                f"Precio: **${pack.precio:,.0f} ARS**",
                f"📦 Entrega: **{entrega_txt}**",
                f"_(automática al confirmar el pago)_",
                "",
            ]
        elif pack.categoria == "sensi":
            partes = [
                f"Precio: **${pack.precio:,.0f} ARS**",
                f"Créditos a sumar: **{_fmt_creditos(pack.creditos)}**",
                "",
            ]
        else:
            dias = int(pack.creditos)
            partes = [
                f"Precio: **${pack.precio:,.0f} ARS**",
                f"🔑 Entrega: **Key de {dias} día{'s' if dias != 1 else ''}** enviada por DM al confirmar el pago.",
                "",
            ]
        if MODO_AUTO_MP:
            partes += [
                "**Mercado Pago** → link de pago automático.",
                "Aceptamos todas las billeteras argentinas: Ualá, Naranja X, Brubank, "
                "Cuenta DNI, Personal Pay y cualquier home banking.",
                "Tu key llega al instante después del pago. ✅",
                "",
                "**Transferencia 🇦🇷** → transferencia manual al alias. "
                "Mandás el comprobante y se acredita al confirmar.",
                "",
            ]
        else:
            partes += [
                "**Naranja X** → transferencia manual al alias. "
                "Mandás el comprobante y se acredita al confirmar.",
                "",
            ]
        partes.append(
            "**Binance** → pagá en USDT y mandanos el comprobante para aprobación manual."
        )

        embed = discord.Embed(
            title=f"Pack {pack.nombre}",
            description="\n".join(partes),
            color=0x00B0F4,
        )
        view = MetodoPagoView(
            mp_url=mp_url,
            pack=pack,
            discord_id=str(interaction.user.id),
            user_id=interaction.user.id,
            username=interaction.user.display_name,
            channel_id=interaction.channel_id,
        )
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


class PackView(_SafeViewMixin, discord.ui.View):
    def __init__(self, modo: str = "proxy"):
        super().__init__(timeout=None)  # Sin timeout → sobrevive reinicios
        self.add_item(PackSelect(modo=modo))


# ---------------------------------------------------------------------------
# Botón MP interactivo (fallback cuando la preferencia no pudo generarse antes)
# ---------------------------------------------------------------------------
class MPGenerarPagoButton(discord.ui.Button):
    """Genera la preferencia de Mercado Pago al hacer clic y envía el link ephemeralmente."""
    def __init__(self, pack, discord_id: str, user_id: int, row: int = 0):
        super().__init__(
            label="Mercado Pago",
            style=discord.ButtonStyle.primary,
            emoji=discord.PartialEmoji(name="mercadopago", id=1499197027903344811),
            row=row,
        )
        self.pack = pack
        self.discord_id = discord_id
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            pref = await asyncio.to_thread(
                payments.crear_preferencia, self.pack.id, self.discord_id
            )
            mp_url = pref.get("init_point")
            if mp_url:
                await interaction.followup.send(
                    f"💳 **Pagá con Mercado Pago**\n\n"
                    f"Hacé clic en el siguiente link para completar tu pago de "
                    f"**${self.pack.precio:,.0f} ARS**:\n{mp_url}\n\n"
                    f"Los créditos se acreditan automáticamente al confirmar el pago. ✅",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    "❌ No pude generar el link de Mercado Pago. "
                    "Usá **Transferencia** o **Binance** como alternativa.",
                    ephemeral=True,
                )
        except Exception as exc:
            log.exception("MPGenerarPagoButton: error generando preferencia pack=%s user=%s",
                          self.pack.id, self.discord_id)
            await interaction.followup.send(
                f"❌ No pude conectar con Mercado Pago en este momento.\n"
                f"```{type(exc).__name__}: {exc}```\n"
                "Usá **Transferencia** o **Binance** como alternativa.",
                ephemeral=True,
            )


# ---------------------------------------------------------------------------
# Vista de selección de método de pago (MP / NX + Binance en la misma fila)
# ---------------------------------------------------------------------------
class MetodoPagoView(_SafeViewMixin, discord.ui.View):
    def __init__(
        self,
        mp_url: str | None,
        pack: payments.Pack,
        discord_id: str,
        user_id: int,
        username: str,
        channel_id: int,
    ):
        super().__init__(timeout=None)
        self.pack = pack
        self.discord_id = discord_id
        self.user_id = user_id
        self.username = username
        self.channel_id = channel_id

        # Decisión de botones según MODO_AUTO_MP:
        #  - True (activado): botón MP siempre (link directo si hay url, interactivo si no) + botón NX
        #  - False (desactivado): solo botón NX
        if MODO_AUTO_MP:
            if mp_url:
                self.add_item(
                    discord.ui.Button(
                        label="Mercado Pago",
                        style=discord.ButtonStyle.link,
                        url=mp_url,
                        emoji=discord.PartialEmoji(name="mercadopago", id=1499197027903344811),
                        row=0,
                    )
                )
            else:
                self.add_item(
                    MPGenerarPagoButton(pack=pack, discord_id=discord_id, user_id=user_id, row=0)
                )
        self.add_item(
            NXIniciarPago(
                pack=pack,
                discord_id=discord_id,
                user_id=user_id,
                username=username,
                channel_id=channel_id,
            )
        )

        # Binance siempre disponible
        self.add_item(
            BinanceIniciarPago(
                pack=pack,
                discord_id=discord_id,
                user_id=user_id,
                username=username,
                channel_id=channel_id,
            )
        )


class NXIniciarPago(discord.ui.Button):
    """Botón Transferencia Bancaria (Naranja X) con bandera argentina."""
    def __init__(self, pack, discord_id, user_id, username, channel_id):
        super().__init__(
            label="Transferencia",
            style=discord.ButtonStyle.success,
            emoji="🇦🇷",
            row=0,
        )
        self.pack = pack
        self.discord_id = discord_id
        self.user_id = user_id
        self.username = username
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction):
        # Responder a Discord PRIMERO para evitar el timeout de 3 segundos
        await _safe_defer(interaction, ephemeral=True, thinking=False)

        op_id = f"NX-{random.randint(1000, 9999)}"
        with _pending_binance_lock:
            _pending_binance[op_id] = {
                "discord_id": self.discord_id,
                "user_id":    self.user_id,
                "pack":       self.pack,
                "channel_id": self.channel_id,
                "username":   self.username,
                "metodo":     "NaranjaX",
            }
        # Persistir en DB en un thread para no bloquear el event loop
        try:
            await asyncio.to_thread(
                database.save_pending_payment,
                payment_id=op_id, discord_id=self.discord_id, user_id=self.user_id,
                pack_id=self.pack.id, channel_id=self.channel_id,
                username=self.username, metodo="NaranjaX",
            )
        except Exception:
            log.exception("No pude persistir pending NX %s", op_id)

        embed = discord.Embed(
            title="🟠 Pago vía Naranja X / Alias",
            description=(
                f"**Operación:** `#{op_id}`\n\n"
                f"Realizá una transferencia por **${self.pack.precio:,.0f} ARS** "
                f"usando los siguientes datos:\n\n"
                f"🏦 **Plataforma:** Naranja X\n"
                f"👤 **Alias:** `{NARANJA_X_ALIAS}`\n"
                f"🔢 **CBU:** `{NARANJA_X_CBU}`\n"
                f"🪪 **Titular:** Agustín Nahuel Marquesini\n\n"
                f"Una vez transferido, presioná **Subir Comprobante** "
                f"y mandame la captura de la transferencia por privado."
            ),
            color=0xFF5A00,
        )
        if NARANJA_X_LOGO_URL:
            embed.set_thumbnail(url=NARANJA_X_LOGO_URL)
        embed.set_footer(text="Marke Panel • Transferencia manual — se acredita al confirmar")

        view = BinanceComprobanteView(op_id=op_id, user_id=self.user_id)
        try:
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        except Exception as e:
            log.warning("NX followup.send falló: %s", e)


class BinanceIniciarPago(discord.ui.Button):
    def __init__(
        self,
        pack: payments.Pack,
        discord_id: str,
        user_id: int,
        username: str,
        channel_id: int,
    ):
        super().__init__(
            label="Binance",
            style=discord.ButtonStyle.secondary,
            emoji=discord.PartialEmoji(name="binance", id=1499205940220526642),
            row=0,
        )
        self.pack = pack
        self.discord_id = discord_id
        self.user_id = user_id
        self.username = username
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction):
        # Responder a Discord PRIMERO para evitar el timeout de 3 segundos
        await _safe_defer(interaction, ephemeral=True, thinking=False)

        op_id = f"BN-{random.randint(1000, 9999)}"
        with _pending_binance_lock:
            _pending_binance[op_id] = {
                "discord_id": self.discord_id,
                "user_id": self.user_id,
                "pack": self.pack,
                "channel_id": self.channel_id,
                "username": self.username,
                "metodo": "Binance",
            }
        # Persistir en DB en un thread para no bloquear el event loop
        try:
            await asyncio.to_thread(
                database.save_pending_payment,
                payment_id=op_id, discord_id=self.discord_id, user_id=self.user_id,
                pack_id=self.pack.id, channel_id=self.channel_id,
                username=self.username, metodo="Binance",
            )
        except Exception:
            log.exception("No pude persistir pending BN %s", op_id)

        embed = discord.Embed(
            title="💰 Pago con Binance Pay",
            description=(
                f"**Operación:** `#{op_id}`\n\n"
                f"Transferí el equivalente a **${self.pack.precio:,.0f} ARS** "
                f"en USDT a este Binance ID:\n\n"
                f"```\n{BINANCE_ID}\n```\n"
                "Una vez que pagaste, presioná **Subir Comprobante** y enviá "
                "la captura de pantalla de la transferencia."
            ),
            color=0xF0B90B,
        )

        if BINANCE_QR_PUBLIC_URL:
            embed.set_image(url=BINANCE_QR_PUBLIC_URL)

        view = BinanceComprobanteView(op_id=op_id, user_id=self.user_id)
        try:
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        except Exception as e:
            log.warning("Binance followup.send falló: %s", e)


class BinanceComprobanteView(_SafeViewMixin, discord.ui.View):
    def __init__(self, op_id: str, user_id: int):
        super().__init__(timeout=None)
        self.add_item(BinanceSubirComprobanteButton(op_id=op_id, user_id=user_id))


class BinanceSubirComprobanteButton(discord.ui.Button):
    def __init__(self, op_id: str, user_id: int):
        super().__init__(
            label="Subir Comprobante 📎",
            style=discord.ButtonStyle.primary,
        )
        self.op_id = op_id
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        # ── 1. Responder a Discord PRIMERO (obligatorio antes de los 3 s) ──────
        await _safe_defer(interaction, ephemeral=True, thinking=False)

        # ── 2. Verificar que la operación todavía está pendiente ───────────────
        with _pending_binance_lock:
            if self.op_id not in _pending_binance:
                await interaction.followup.send(
                    "Esta operación ya fue procesada o expiró.", ephemeral=True
                )
                return
            _waiting_comprobante[self.user_id] = self.op_id

        # ── 3. Preparar el texto del DM según el método ────────────────────────
        if self.op_id.startswith("NX-"):
            dm_texto = (
                f"📸 **Enviame el comprobante de la transferencia realizada aquí.**\n"
                f"🆔 Operación: `#{self.op_id}` (Naranja X)\n\n"
                "Mandá la captura de la transferencia (con monto, fecha y nombre del destinatario "
                "visibles). Una vez que la reciba, la paso a revisión y te aviso cuando esté aprobada."
            )
            confirm_texto = "📩 Te mandé un mensaje privado. Enviame el comprobante de la transferencia por ahí."
        else:
            dm_texto = (
                f"📸 **Enviame la captura de pantalla de tu pago Binance aquí.**\n"
                f"🆔 Operación: `#{self.op_id}` (Binance Pay)\n\n"
                "Una vez que la reciba, la mando para revisión y te aviso cuando sea aprobada."
            )
            confirm_texto = "📩 Te mandé un mensaje privado. Enviame el comprobante por ahí."

        # ── 4. Enviar el DM (red IO separado de la respuesta a Discord) ────────
        try:
            await interaction.user.send(dm_texto)
            await interaction.followup.send(confirm_texto, ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ No te pude escribir por DM. Activá los mensajes directos del servidor e intentá de nuevo.",
                ephemeral=True,
            )
            with _pending_binance_lock:
                _waiting_comprobante.pop(self.user_id, None)


@tree.command(name="comprar", description="Comprar créditos, regedits Android o iOS")
async def comprar(interaction: discord.Interaction):
    await _safe_defer(interaction, ephemeral=True, thinking=True)
    uid = str(interaction.user.id)

    # Detectar canal para mostrar solo los packs relevantes
    ch = interaction.channel_id
    ch_name = (getattr(interaction.channel, "name", "") or "").lower()

    if (CANAL_SENSI_ID and ch == CANAL_SENSI_ID) or "sensi" in ch_name:
        modo   = "sensi"
        saldo  = database.get_sensi_credits(uid)
        titulo = "🎯 Comprá tu Sensi Xitada"
        desc   = (
            f"Saldo actual: **{_fmt_creditos(saldo)}** crédito(s) de sensi\n\n"
            "Cada **Sensi Xitada** tiene un costo de **0.1 crédito de sensi** (= $1.000 ARS).\n"
            "⚠️ Estos créditos son exclusivos del canal de sensis — no sirven para el proxy.\n\n"
            "Comprá el pack y usá `/sensibilidad` con el modelo de tu celular."
        )
        color = 0xFF4500
    elif "android" in ch_name:
        modo   = "android"
        titulo = "🤖 Regedits Android — Sensi Marke"
        desc   = (
            "Elegí tu regedit para **Android**.\n\n"
            "📥 Descarga + tutorial automático por DM al confirmar el pago.\n\n"
            f"{NOTA_SOPORTE}"
        )
        color = 0x2ECC71
    elif "ios-" in ch_name or ch_name.startswith("ios"):
        modo   = "ios"
        titulo = "🍎 Archivos iOS — Sensi Marke"
        desc   = (
            "Elegí tu archivo para **iOS**.\n\n"
            "📱 Recibirás el link de WhatsApp con Markee automáticamente por DM.\n\n"
            f"{NOTA_SOPORTE}"
        )
        color = 0x3498DB
    elif any(kw in ch_name for kw in ("regedit", "regedits")):
        modo   = "regedit"
        titulo = "📦 Catálogo de Regedits — Sensi Marke"
        desc   = (
            "Elegí tu regedit para **Android** o **iOS**.\n\n"
            "🤖 **Android:** descarga + tutorial automático por DM.\n"
            "🍎 **iOS:** link de contacto con Markee por WhatsApp vía DM.\n\n"
            f"{NOTA_SOPORTE}"
        )
        color = 0x9B59B6
    else:
        modo   = "proxy"
        titulo = "Tienda de keys Sensi Marke"
        desc   = (
            "Elegí el plan de proxy que querés comprar.\n"
            "Tu key llegará por DM al instante una vez confirmado el pago. 🔑"
            + (f"\n\n[TUTORIAL AQUÍ]({PROXY_TUTORIAL_URL})" if PROXY_TUTORIAL_URL else "")
        )
        color = 0xF1C40F

    embed = discord.Embed(title=titulo, description=desc, color=color)
    await interaction.followup.send(embed=embed, view=PackView(modo=modo), ephemeral=True)


# ---------------------------------------------------------------------------
# /mercadoactivar y /mercadodesactivar — control de automatización MP (admins)
# ---------------------------------------------------------------------------
@tree.command(name="mercadoactivar", description="Activar Mercado Pago como opción de compra automática")
@app_commands.checks.has_permissions(administrator=True)
async def mercadoactivar(interaction: discord.Interaction):
    global MODO_AUTO_MP
    MODO_AUTO_MP = True
    await interaction.response.send_message(
        "🟢 Automatización ACTIVADA. Mercado Pago ahora aparecerá como opción de compra junto a Transferencia Bancaria.",
        ephemeral=True,
    )
    await asyncio.to_thread(database.set_config, "MODO_AUTO_MP", "1")


@tree.command(name="mercadodesactivar", description="Desactivar Mercado Pago como opción de compra automática")
@app_commands.checks.has_permissions(administrator=True)
async def mercadodesactivar(interaction: discord.Interaction):
    global MODO_AUTO_MP
    MODO_AUTO_MP = False
    await interaction.response.send_message(
        "🔴 Automatización DESACTIVADA. Mercado Pago ha sido removido de las opciones; ahora los usuarios solo verán la opción de Transferencia o aviso manual.",
        ephemeral=True,
    )
    await asyncio.to_thread(database.set_config, "MODO_AUTO_MP", "0")


@tree.command(name="mercadostatus", description="Ver el estado actual de la automatización de Mercado Pago")
@app_commands.checks.has_permissions(administrator=True)
async def mercadostatus(interaction: discord.Interaction):
    if MODO_AUTO_MP:
        estado = "🟢 **ACTIVADA**"
        detalle = "Mercado Pago aparece como opción de compra junto a Transferencia Bancaria."
    else:
        estado = "🔴 **DESACTIVADA**"
        detalle = "Solo se muestra la opción de Transferencia Bancaria. Mercado Pago está oculto."
    await interaction.response.send_message(
        f"**Estado actual de la automatización de Mercado Pago:** {estado}\n{detalle}",
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# /gen y /key — integración con @FFPROXYCHEAT_BOT via Telethon
# ---------------------------------------------------------------------------
@tree.command(name="gen", description="(Admin) Generar una key en el bot de FF Proxy")
@app_commands.describe(key="La key a generar", dias="Cantidad de días de acceso")
async def gen_cmd(interaction: discord.Interaction, key: str, dias: int):
    await _safe_defer(interaction, ephemeral=True, thinking=True)
    roles = getattr(interaction.user, "roles", [])
    tiene_rol_key = any(r.id == KEY_ALLOWED_ROLE_ID for r in roles)
    if not (_puede_registrar(interaction) or tiene_rol_key):
        await interaction.followup.send("No tenés permiso para usar este comando.", ephemeral=True)
        return
    try:
        respuesta = await telegram_client.cmd_gen(key, dias)
        log.info("🔑 KEY /gen — key=%s dias=%d admin=%s (%s)", key, dias, interaction.user, interaction.user.id)
        await interaction.followup.send(
            f"✅ **Respuesta del bot de FF Proxy:**\n```\n{respuesta}\n```",
            ephemeral=True,
        )
        asyncio.create_task(_log_key(
            "gen_ok", key, interaction.user.id,
            dias=dias, metodo="Admin /gen", respuesta_tg=respuesta,
            admin_id=interaction.user.id,
        ))
    except asyncio.TimeoutError:
        await interaction.followup.send(
            "⏱️ El bot de Telegram no respondió a tiempo. Intentá de nuevo.",
            ephemeral=True,
        )
        asyncio.create_task(_log_key(
            "gen_error", key, interaction.user.id,
            dias=dias, metodo="Admin /gen",
            error="Timeout: el bot de Telegram no respondió a tiempo",
        ))
    except Exception as exc:
        log.exception("Error en /gen key=%s dias=%s", key, dias)
        await interaction.followup.send(f"❌ Error: {exc}", ephemeral=True)
        asyncio.create_task(_log_key(
            "gen_error", key, interaction.user.id,
            dias=dias, metodo="Admin /gen", error=str(exc),
        ))


KEY_ALLOWED_ROLE_ID = 1505803027188289679  # Rol con acceso a /key

@tree.command(name="key", description="Activar tu key de FF Proxy con tu IP")
@app_commands.describe(key="Tu key de acceso", ip="Tu IP pública")
async def key_cmd(interaction: discord.Interaction, key: str, ip: str):
    await _safe_defer(interaction, ephemeral=True, thinking=True)
    # Permitir: verificados, admins, o quien tenga el rol KEY_ALLOWED_ROLE_ID
    roles = getattr(interaction.user, "roles", [])
    tiene_rol_key = any(r.id == KEY_ALLOWED_ROLE_ID for r in roles)
    if not (_es_verificado(interaction) or tiene_rol_key):
        await interaction.followup.send(
            "❌ No tenés permiso para usar `/key`. Necesitás estar verificado o tener el rol correspondiente.",
            ephemeral=True,
        )
        return
    # Sanitizar IP: quitar espacios, saltos de línea y caracteres invisibles
    import ipaddress as _ipaddress
    ip_clean = re.sub(r"[^\d.:]", "", ip.strip())
    try:
        _ipaddress.ip_address(ip_clean)
    except ValueError:
        await interaction.followup.send(
            f"❌ La IP **`{ip}`** no tiene un formato válido.\n"
            "Usá el formato `192.168.1.1` (solo números y puntos, sin espacios).",
            ephemeral=True,
        )
        return

    # Verificar si la key está baneada
    ban_info = database.is_key_banned(key)
    if ban_info:
        razon_txt = f"\nMotivo: *{ban_info['reason']}*" if ban_info.get("reason") else ""
        await interaction.followup.send(
            f"🚫 La key `{key}` fue **baneada** y no puede usarse más.{razon_txt}\n"
            "Si creés que es un error, contactá a un administrador.",
            ephemeral=True,
        )
        return

    try:
        respuesta = await telegram_client.cmd_key(key, ip_clean)
        await interaction.followup.send(
            f"✅ **Tu key fue activada:**\n```\n{respuesta}\n```\n\n"
            "¡Muchísimas gracias por comprar en **Sensi Marke**! 🖤\n"
            "Recordá siempre estar atento a mi grupo:\n"
            "https://chat.whatsapp.com/DQxndyWBG860vpaVcxam3s",
            ephemeral=True,
        )
        database.record_key_activation(key, str(interaction.user.id), ip_clean)
        asyncio.create_task(_log_key(
            "activada_ok", key, interaction.user.id,
            ip=ip_clean, respuesta_tg=respuesta,
        ))
    except ValueError as exc:
        exc_str = str(exc)
        # Detectar si es "Invalid key or network error" del bot → puede ser temporal
        if "network" in exc_str.lower():
            msg = (
                f"⚠️ **El servidor de proxy no respondió correctamente:**\n```\n{exc_str}\n```\n"
                "Esto puede ser un problema temporal del servidor. "
                "**Esperá 1-2 minutos e intentá de nuevo.** "
                "Si sigue fallando, contactá a un administrador."
            )
        else:
            msg = f"❌ **El bot de FF Proxy respondió con un error:**\n```\n{exc_str}\n```"
        await interaction.followup.send(msg, ephemeral=True)
        asyncio.create_task(_log_key(
            "activada_error", key, interaction.user.id,
            ip=ip_clean, error=exc_str,
        ))
    except asyncio.TimeoutError:
        await interaction.followup.send(
            "⏱️ El bot de Telegram no respondió a tiempo. Intentá de nuevo en unos segundos.",
            ephemeral=True,
        )
        asyncio.create_task(_log_key(
            "activada_error", key, interaction.user.id,
            ip=ip_clean, error="Timeout: el bot de Telegram no respondió a tiempo",
        ))
    except Exception as exc:
        exc_str = str(exc)
        log.exception("Error en /key key=%s ip=%s", key, ip)
        # Si el error final contiene "network error" (tras 3 reintentos fallidos)
        if "network" in exc_str.lower():
            msg = (
                "⚠️ **El servidor de proxy no está respondiendo** después de varios intentos.\n"
                "Esto es un problema temporal del servidor de proxy. "
                "**Esperá 2-5 minutos e intentá de nuevo.**\n"
                "Si sigue fallando, contactá a un administrador con tu key."
            )
        else:
            msg = f"❌ Error al activar la key: {exc_str}"
        await interaction.followup.send(msg, ephemeral=True)
        asyncio.create_task(_log_key(
            "activada_error", key, interaction.user.id,
            ip=ip_clean, error=exc_str,
        ))


# ---------------------------------------------------------------------------
# /ban-key y /unban-key — banear/desbanear keys de proxy
# ---------------------------------------------------------------------------
@tree.command(
    name="ban-key",
    description="(Admin) Banear una key para que no se pueda usar nunca más",
)
@app_commands.describe(
    key="La key a banear (ej: MARKEXYZ123)",
    razon="Motivo del ban (opcional)",
)
async def ban_key_cmd(
    interaction: discord.Interaction,
    key: str,
    razon: str | None = None,
):
    await _safe_defer(interaction, ephemeral=True, thinking=True)
    if not _puede_registrar(interaction):
        await interaction.followup.send("❌ Solo administradores.", ephemeral=True)
        return

    key_clean = key.strip().upper()

    ya_baneada = database.is_key_banned(key_clean)
    if ya_baneada:
        await interaction.followup.send(
            f"⚠️ La key `{key_clean}` **ya estaba baneada** (baneada por `{ya_baneada.get('banned_by', '?')}` el {str(ya_baneada.get('banned_at', '?'))[:16]}).",
            ephemeral=True,
        )
        return

    # Guardar ban en DB
    database.ban_key(
        key=key_clean,
        discord_id=None,
        reason=razon,
        banned_by=str(interaction.user),
    )

    # Buscar si la key tiene IPs activas en Firebase y eliminarlas
    activaciones = database.get_key_activations(key_clean)
    ips_eliminadas: list[str] = []
    ips_no_encontradas: list[str] = []

    if activaciones:
        ips_vistas: set[str] = set()
        for act in activaciones:
            ip = act.get("ip", "")
            if ip and ip not in ips_vistas:
                ips_vistas.add(ip)
                eliminada = await asyncio.to_thread(automation.eliminar_ip_en_firebase, ip)
                if eliminada:
                    ips_eliminadas.append(ip)
                else:
                    ips_no_encontradas.append(ip)

    log.warning(
        "BAN KEY — key=%s admin=%s (%s) razon=%r IPs_eliminadas=%s",
        key_clean, interaction.user, interaction.user.id, razon, ips_eliminadas,
    )

    # Notificar en #ventas
    try:
        canal_ventas = await _obtener_canal_ventas()
        if canal_ventas:
            embed = discord.Embed(
                title="🚫 Key Baneada",
                description=(
                    f"🔑 **Key:** `{key_clean}`\n"
                    f"🛡️ **Admin:** {interaction.user.mention}\n"
                    + (f"📝 **Motivo:** {razon}\n" if razon else "")
                    + (f"🌐 **IPs eliminadas de Firebase:** {', '.join(f'`{ip}`' for ip in ips_eliminadas)}\n" if ips_eliminadas else "")
                    + (f"⚠️ **IPs no encontradas en Firebase:** {', '.join(f'`{ip}`' for ip in ips_no_encontradas)}\n" if ips_no_encontradas else "")
                ),
                color=0xE74C3C,
            )
            embed.timestamp = discord.utils.utcnow()
            await canal_ventas.send(embed=embed)
    except Exception:
        log.exception("Error posteando ban de key en #ventas")

    # Armar respuesta al admin
    lineas = [f"✅ Key `{key_clean}` **baneada correctamente**."]
    if razon:
        lineas.append(f"📝 Motivo: {razon}")
    if ips_eliminadas:
        lineas.append(f"🌐 IPs eliminadas de Firebase: {', '.join(f'`{ip}`' for ip in ips_eliminadas)}")
    if ips_no_encontradas:
        lineas.append(f"⚠️ IPs no encontradas en Firebase (puede que ya hayan expirado): {', '.join(f'`{ip}`' for ip in ips_no_encontradas)}")
    if not activaciones:
        lineas.append("ℹ️ No había activaciones registradas para esta key (nunca se usó o fue activada antes de esta versión).")

    await interaction.followup.send("\n".join(lineas), ephemeral=True)


@tree.command(
    name="unban-key",
    description="(Admin) Desbanear una key para que pueda volver a usarse",
)
@app_commands.describe(key="La key a desbanear (ej: MARKEXYZ123)")
async def unban_key_cmd(interaction: discord.Interaction, key: str):
    await _safe_defer(interaction, ephemeral=True)
    if not _puede_registrar(interaction):
        await interaction.followup.send("❌ Solo administradores.", ephemeral=True)
        return

    key_clean = key.strip().upper()
    eliminada = database.unban_key(key_clean)

    if eliminada:
        log.info("UNBAN KEY — key=%s admin=%s (%s)", key_clean, interaction.user, interaction.user.id)
        await interaction.followup.send(
            f"✅ Key `{key_clean}` **desbaneada**. Ya puede volver a activarse.",
            ephemeral=True,
        )
    else:
        await interaction.followup.send(
            f"⚠️ La key `{key_clean}` no estaba en la lista de baneadas.",
            ephemeral=True,
        )


@tree.command(
    name="keys-baneadas",
    description="(Admin) Ver la lista de todas las keys baneadas",
)
async def keys_baneadas_cmd(interaction: discord.Interaction):
    await _safe_defer(interaction, ephemeral=True)
    if not _puede_registrar(interaction):
        await interaction.followup.send("❌ Solo administradores.", ephemeral=True)
        return

    baneadas = database.list_banned_keys()

    if not baneadas:
        await interaction.followup.send("✅ No hay ninguna key baneada actualmente.", ephemeral=True)
        return

    lineas = []
    for idx, entry in enumerate(baneadas, 1):
        key_txt   = entry.get("key", "?")
        motivo    = entry.get("reason") or "—"
        by        = entry.get("banned_by") or "?"
        when      = str(entry.get("banned_at", "?"))[:16]
        lineas.append(f"`{idx}.` 🔑 `{key_txt}` — 📝 {motivo} — 🛡️ {by} — 📅 {when}")

    # Discord no permite embeds con description > 4096 chars — paginar si hace falta
    BLOQUE = 30
    paginas = [lineas[i:i + BLOQUE] for i in range(0, len(lineas), BLOQUE)]

    for num, pagina in enumerate(paginas, 1):
        encabezado = f"🚫 **Keys baneadas ({len(baneadas)} total)**" if num == 1 else f"_(continuación {num}/{len(paginas)})_"
        embed = discord.Embed(
            title=encabezado if num == 1 else None,
            description="\n".join(pagina),
            color=0xE74C3C,
        )
        if num == 1:
            embed.set_footer(text=f"Total baneadas: {len(baneadas)}")
        await interaction.followup.send(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# /tg_relogin y /tg_codigo — regenerar sesión de Telegram desde producción
# ---------------------------------------------------------------------------
@tree.command(
    name="tg_relogin",
    description="(Admin) Iniciar re-autenticación de Telegram: envía un código SMS al teléfono.",
)
@app_commands.describe(telefono="Número de teléfono con código de país (ej: +5491112345678)")
async def tg_relogin_cmd(interaction: discord.Interaction, telefono: str):
    await _safe_defer(interaction, ephemeral=True, thinking=True)
    if not _puede_registrar(interaction):
        await interaction.followup.send("❌ Solo admins pueden usar este comando.", ephemeral=True)
        return
    try:
        msg = await telegram_client.relogin_start(telefono)
        await interaction.followup.send(msg, ephemeral=True)
    except Exception as exc:
        log.exception("Error en /tg_relogin telefono=%s", telefono)
        await interaction.followup.send(f"❌ Error: {exc}", ephemeral=True)


@tree.command(
    name="tg_codigo",
    description="(Admin) Completar re-autenticación de Telegram con el código SMS recibido.",
)
@app_commands.describe(
    codigo="Código recibido por SMS (ej: 12345)",
    password="Contraseña 2FA (solo si tu cuenta tiene verificación en dos pasos)",
)
async def tg_codigo_cmd(
    interaction: discord.Interaction,
    codigo: str,
    password: str = "",
):
    await _safe_defer(interaction, ephemeral=True, thinking=True)
    if not _puede_registrar(interaction):
        await interaction.followup.send("❌ Solo admins pueden usar este comando.", ephemeral=True)
        return
    try:
        msg = await telegram_client.relogin_verify(codigo, password)
        await interaction.followup.send(msg, ephemeral=True)
    except Exception as exc:
        log.exception("Error en /tg_codigo")
        await interaction.followup.send(f"❌ {exc}", ephemeral=True)


# ---------------------------------------------------------------------------
# /saldo — utilidad
# ---------------------------------------------------------------------------
@tree.command(name="invitar", description="Cómo ganar comisiones invitando gente al servidor con tu link")
async def invitar(interaction: discord.Interaction):
    await _safe_defer(interaction, ephemeral=True, thinking=True)
    embed = discord.Embed(
        title="🔗 Sistema de Afiliados — Link de Invitación",
        description=(
            "Ganás el **30% de comisión** por cada venta que concrete alguien que vos invitaste al servidor. "
            "El sistema es **100% automático** — no hace falta que la persona haga nada extra.\n\n"
            "**¿Cómo funciona?**\n"
            "1️⃣ Creá un **link de invitación** de este servidor:\n"
            "   → Clic derecho en cualquier canal → **Invitar gente**\n"
            "   → O desde ⚙️ Configuración del servidor → **Invitaciones**\n\n"
            "2️⃣ Compartí ese link con quien quieras (redes, amigos, etc.).\n\n"
            "3️⃣ Cuando alguien entre al servidor usando **tu link**, el bot lo detecta automáticamente "
            "y lo registra como tu referido. ✅ No hay código que ingresar.\n\n"
            "4️⃣ Cada vez que esa persona compre cualquier producto, "
            "vas a recibir **30% de comisión** directo a tu saldo. 💰\n\n"
            "Usá `/perfil` para ver tus referidos y saldo acumulado."
        ),
        color=0xF1C40F,
    )
    embed.set_footer(text="Marke Panel • Sistema de Afiliados Sensi Marke")
    await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(name="referido", description="Registrá manualmente a quien te invitó (solo si entraste sin usar su link)")
@app_commands.describe(codigo="Código de afiliado de quien te invitó (pedíselo a esa persona con /perfil)")
async def referido_cmd(interaction: discord.Interaction, codigo: str):
    await _safe_defer(interaction, ephemeral=True, thinking=True)
    discord_id = str(interaction.user.id)

    # Verificar si ya tiene referidor
    if database.get_referrer(discord_id):
        await interaction.followup.send(
            "❌ Ya tenés un referido registrado (probablemente porque entraste con el link de invitación de alguien). "
            "Solo se puede registrar una vez.",
            ephemeral=True,
        )
        return

    # Verificar si el código existe
    referrer_id = database.get_referrer_by_code(codigo.strip())
    if not referrer_id:
        await interaction.followup.send(
            "❌ Código inválido. Pedile su código a la persona que te invitó — "
            "lo puede ver usando `/perfil`.",
            ephemeral=True,
        )
        return

    # No puede referirse a sí mismo
    if referrer_id == discord_id:
        await interaction.followup.send(
            "❌ No podés registrarte como referido de vos mismo.",
            ephemeral=True,
        )
        return

    ok = database.register_referral(referrer_id, discord_id)
    if ok:
        await interaction.followup.send(
            "✅ ¡Listo! Quedaste registrado como referido.\n"
            "Cada vez que compres en el servidor, quien te invitó va a recibir el **30%** de comisión. 🙌",
            ephemeral=True,
        )
        try:
            referrer_user = await client.fetch_user(int(referrer_id))
            await referrer_user.send(
                f"🎉 **¡Nuevo referido registrado!**\n"
                f"Un usuario ingresó tu código y quedó registrado como tu referido.\n"
                f"Cada vez que compre cualquier producto vas a ganar **30%** de comisión automáticamente. 💰\n\n"
                f"Usá `/perfil` para ver tu saldo."
            )
        except Exception:
            pass
    else:
        await interaction.followup.send(
            "❌ No pude registrar el referido. Intentá de nuevo más tarde.",
            ephemeral=True,
        )


@tree.command(name="perfil", description="Ver tu perfil de afiliado: código, referidos y saldo de comisiones")
async def perfil(interaction: discord.Interaction):
    await _safe_defer(interaction, ephemeral=True, thinking=True)
    discord_id = str(interaction.user.id)

    code        = database.get_or_create_referral_code(discord_id)
    balance     = database.get_referral_balance(discord_id)
    count       = database.get_referral_count(discord_id)
    sales_count = database.get_referral_sales_count(discord_id)
    rate        = database.get_commission_rate(discord_id)
    pct         = int(rate * 100)

    embed = discord.Embed(
        title=f"👤 Perfil de Afiliado — {interaction.user.display_name}",
        color=0x9B59B6,
    )
    embed.add_field(name="🔗 Tu código de afiliado", value=f"```{code}```", inline=False)
    embed.add_field(name="👥 Referidos totales",     value=str(count),             inline=True)
    embed.add_field(name="🛒 Ventas generadas",      value=str(sales_count),       inline=True)
    embed.add_field(name="💰 Saldo de comisiones",   value=f"${balance:,.0f} ARS", inline=True)
    embed.add_field(
        name="📊 Comisión: **30%**",
        value="Ganás el **30%** de cada venta que concrete alguien que entró al servidor con tu link de invitación.",
        inline=False,
    )
    embed.add_field(
        name="ℹ️ ¿Cómo funciona?",
        value=(
            "• Creá un **link de invitación** del servidor (clic derecho en un canal → Invitar gente).\n"
            "• Compartilo — quien entre con ese link queda registrado como tu referido **automáticamente**.\n"
            "• No hace falta que hagan nada extra. El bot lo detecta solo al unirse. ✅\n"
            "• Cada compra que hagan te genera **30%** de comisión en todos los productos. 💰"
        ),
        inline=False,
    )
    embed.set_footer(text="Marke Panel • Sistema de Afiliados Sensi Marke")
    await interaction.followup.send(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
@tree.command(name="saldo", description="Consultar tus créditos disponibles")
async def saldo(interaction: discord.Interaction):
    await _safe_defer(interaction, ephemeral=True, thinking=True)
    uid = str(interaction.user.id)
    proxy_c = database.get_credits(uid)
    sensi_c = database.get_sensi_credits(uid)
    embed = discord.Embed(
        title="💰 Tu saldo de créditos",
        color=0xF1C40F,
    )
    embed.add_field(
        name="🌐 Créditos de Proxy",
        value=f"**{_fmt_creditos(proxy_c)}** crédito(s)\n_Sirven para `/registrar`_",
        inline=True,
    )
    embed.add_field(
        name="🎯 Créditos de Sensis",
        value=f"**{_fmt_creditos(sensi_c)}** crédito(s)\n_Sirven para `/sensibilidad`_",
        inline=True,
    )
    embed.set_footer(text="Los créditos de proxy y sensis no son intercambiables")
    await interaction.followup.send(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# /regalar — admin suma créditos a otro usuario
# ---------------------------------------------------------------------------
@tree.command(
    name="regalar",
    description="(Admin) Sumar créditos manualmente a un usuario",
)
@app_commands.describe(
    usuario="Usuario de Discord al que querés sumarle créditos",
    cantidad="Cantidad de créditos a sumar (puede ser negativa para descontar)",
    tipo="Tipo de créditos: proxy (por defecto) o sensi",
    motivo="Motivo opcional (queda en el log)",
)
@app_commands.choices(tipo=[
    app_commands.Choice(name="Proxy", value="proxy"),
    app_commands.Choice(name="Sensi", value="sensi"),
])
async def regalar(
    interaction: discord.Interaction,
    usuario: discord.User,
    cantidad: float,
    tipo: app_commands.Choice[str] = None,
    motivo: str = "",
):
    await _safe_defer(interaction, ephemeral=True, thinking=True)

    if not _puede_registrar(interaction):
        await interaction.followup.send(
            "No tenés permiso para usar este comando.", ephemeral=True
        )
        return

    if cantidad == 0:
        await interaction.followup.send(
            "La cantidad no puede ser 0.", ephemeral=True
        )
        return

    tipo_val = (tipo.value if tipo else "proxy")
    es_sensi = tipo_val == "sensi"
    destino_id = str(usuario.id)

    if es_sensi:
        nuevo_total = database.add_sensi_credits(destino_id, cantidad)
        if nuevo_total < 0:
            database.add_sensi_credits(destino_id, -cantidad)
            await interaction.followup.send(
                f"No puedo descontar {abs(cantidad)} créditos sensi: {usuario.mention} "
                f"solo tiene {nuevo_total - cantidad} disponibles.",
                ephemeral=True,
            )
            return
        tipo_label = "sensi"
    else:
        nuevo_total = database.add_credits(destino_id, cantidad)
        if nuevo_total < 0:
            database.add_credits(destino_id, -cantidad)
            await interaction.followup.send(
                f"No puedo descontar {abs(cantidad)} créditos: {usuario.mention} "
                f"solo tiene {nuevo_total - cantidad} disponibles.",
                ephemeral=True,
            )
            return
        tipo_label = "proxy"

    log.info(
        "REGALO admin=%s destino=%s tipo=%s cantidad=%s motivo=%r nuevo_total=%s",
        interaction.user.id, destino_id, tipo_label, cantidad, motivo, nuevo_total,
    )

    accion = "sumaron" if cantidad > 0 else "descontaron"
    icono = "🎯" if es_sensi else "🌐"
    embed = discord.Embed(
        title=f"{icono} Créditos {tipo_label} actualizados",
        description=(
            f"Se {accion} **{abs(cantidad)}** crédito(s) de **{tipo_label}** a {usuario.mention}.\n"
            f"Nuevo saldo {tipo_label}: **{nuevo_total}**."
            + (f"\n\n*Motivo:* {motivo}" if motivo else "")
        ),
        color=0x2ECC71 if cantidad > 0 else 0xE67E22,
    )
    await interaction.followup.send(embed=embed, ephemeral=True)

    # Aviso por DM al usuario
    try:
        dm_embed = discord.Embed(
            title=f"{icono} Tus créditos {tipo_label} cambiaron",
            description=(
                f"Un admin te {accion} **{abs(cantidad)}** crédito(s) de **{tipo_label}**.\n"
                f"Tu nuevo saldo {tipo_label}: **{nuevo_total}**."
                + (f"\n\n*Motivo:* {motivo}" if motivo else "")
            ),
            color=0x2ECC71 if cantidad > 0 else 0xE67E22,
        )
        await usuario.send(embed=dm_embed)
    except (discord.Forbidden, discord.HTTPException):
        log.info("No pude mandar DM al usuario %s (DMs cerrados)", destino_id)


# ---------------------------------------------------------------------------
# /historial — últimos pagos y registros del usuario
# ---------------------------------------------------------------------------
def _fmt_fecha(valor: str) -> str:
    """Recorta el timestamp ISO a 'YYYY-MM-DD HH:MM' para que se lea más prolijo."""
    if not valor:
        return "—"
    return str(valor)[:16].replace("T", " ")


@tree.command(name="historial", description="Ver tus últimos pagos y registros de IP")
async def historial(interaction: discord.Interaction):
    await _safe_defer(interaction, ephemeral=True, thinking=True)
    discord_id = str(interaction.user.id)
    pagos = database.get_payments(discord_id, limit=5)
    registros = database.get_registrations(discord_id, limit=5)
    saldo_actual = database.get_credits(discord_id)

    embed = discord.Embed(
        title="Tu historial",
        description=f"Saldo actual: **{saldo_actual}** crédito(s)",
        color=0x9B59B6,
    )

    if pagos:
        lineas = []
        for p in pagos:
            pack = payments.PACKS.get(p["pack"])
            nombre_pack = pack.nombre if pack else p["pack"]
            lineas.append(
                f"`{_fmt_fecha(p['created_at'])}` · **{nombre_pack}** · "
                f"+{p['credits_added']} créd · ${p['amount']:,.0f} · {p['status']}"
            )
        embed.add_field(
            name=f"Últimos pagos ({len(pagos)})",
            value="\n".join(lineas)[:1024],
            inline=False,
        )
    else:
        embed.add_field(
            name="Últimos pagos",
            value="Todavía no compraste ningún pack. Usá `/comprar`.",
            inline=False,
        )

    if registros:
        lineas = []
        for r in registros:
            lineas.append(
                f"`{_fmt_fecha(r['created_at'])}` · **{r['ip']}** · "
                f"{r['dias']} día(s) · usuario *{r['usuario']}*"
            )
        embed.add_field(
            name=f"Últimas IPs registradas ({len(registros)})",
            value="\n".join(lineas)[:1024],
            inline=False,
        )
    else:
        embed.add_field(
            name="Últimas IPs registradas",
            value="Todavía no registraste ninguna IP. Usá `/registrar`.",
            inline=False,
        )

    await interaction.followup.send(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# /cambiar-ip — DESACTIVADO (sistema de keys reemplaza el de créditos)
# ---------------------------------------------------------------------------
@tree.command(
    name="cambiar-ip",
    description="[Desactivado] El sistema de proxy ahora funciona por keys",
)
async def cambiar_ip_cmd(interaction: discord.Interaction):
    await _safe_defer(interaction, ephemeral=True)
    await interaction.followup.send(
        "❌ Este comando fue desactivado. El proxy ahora funciona con **keys**.\n"
        "Usá `/key` para activar tu key con tu IP.",
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# /mi-ip — muestra la IP pública del bot/servidor (útil para VPN)
# ---------------------------------------------------------------------------
@tree.command(name="mi-ip", description="Obtené tu IP pública actual")
async def mi_ip(interaction: discord.Interaction):
    await _safe_defer(interaction, ephemeral=True, thinking=False)
    await interaction.followup.send(
        "🌐 **Consultá tu IP pública acá:**\nhttps://ipleak.net/\n\n"
        "La IP que aparece en el recuadro verde es la que tenés que registrar.",
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# /certificado — descarga el certificado mitmproxy (sin necesitar el proxy)
# ---------------------------------------------------------------------------
CERT_PATH = Path(__file__).parent.parent / "attached_assets" / "mitm_cert.pem"

INSTRUCCIONES = (
    "\n\n**Cómo instalar en iPhone/iPad:**\n"
    "Abrí el archivo → tap *Permitir* → "
    "andá a *Configuración › General › Admon. Dispositivos y VPN* "
    "→ tocá el perfil → tocá *Instalar*."
)


@tree.command(name="certificado", description="Descargá el certificado para usar el proxy")
async def certificado(interaction: discord.Interaction):
    await _safe_defer(interaction, ephemeral=True, thinking=True)

    if not CERT_PATH.exists() or CERT_PATH.stat().st_size == 0:
        await interaction.followup.send(
            "⚠️ El certificado no está disponible todavía. "
            "Pedíselo al admin.",
            ephemeral=True,
        )
        return

    await interaction.followup.send(
        f"🔐 **Certificado mitmproxy**{INSTRUCCIONES}",
        file=discord.File(str(CERT_PATH), filename="mitmproxy-ca-cert.pem"),
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# /appdatos — link directo a la app Potatso en la App Store
# ---------------------------------------------------------------------------
APP_POTATSO_URL = "https://apps.apple.com/ar/app/potatso/id1239860606"

@tree.command(name="appdatos", description="Descargá la app necesaria para usar el proxy en iPhone/iPad")
async def appdatos(interaction: discord.Interaction):
    await _safe_defer(interaction, ephemeral=True)
    embed = discord.Embed(
        title="📱 App requerida para datos — Potatso",
        description=(
            "Para usar el proxy de datos en **iPhone / iPad** necesitás instalar **Potatso** desde la App Store.\n\n"
            f"🔗 [Descargar Potatso]({APP_POTATSO_URL})"
        ),
        color=0x1DB954,
        url=APP_POTATSO_URL,
    )
    await interaction.followup.send(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# /admin-cert — (admin) sube el archivo .pem al bot para distribuirlo
# ---------------------------------------------------------------------------
@tree.command(
    name="admin-cert",
    description="(Admin) Subí el certificado .pem para que los usuarios puedan descargarlo",
)
@app_commands.describe(archivo="Archivo .pem descargado de mitm.it con el proxy activo")
async def admin_cert(
    interaction: discord.Interaction,
    archivo: discord.Attachment,
):
    await _safe_defer(interaction, ephemeral=True, thinking=True)

    if not _puede_registrar(interaction):
        await interaction.followup.send("No tenés permiso.", ephemeral=True)
        return

    if not archivo.filename.lower().endswith((".pem", ".crt", ".cer")):
        await interaction.followup.send(
            "El archivo debe tener extensión .pem, .crt o .cer.", ephemeral=True
        )
        return

    content = await archivo.read()
    if len(content) < 100:
        await interaction.followup.send("El archivo parece estar vacío o corrupto.", ephemeral=True)
        return

    CERT_PATH.parent.mkdir(parents=True, exist_ok=True)
    CERT_PATH.write_bytes(content)
    log.info("Certificado actualizado por %s (%d bytes)", interaction.user, len(content))

    await interaction.followup.send(
        f"✅ Certificado guardado correctamente ({len(content):,} bytes). "
        "Los usuarios ya pueden usar `/certificado` para descargarlo.",
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# /promoproxy — (admin) gestionar promo de proxy y ventana gratismarke
# ---------------------------------------------------------------------------
@tree.command(
    name="promoproxy",
    description="(Admin) Activar/desactivar promo proxy o resetear ventana /gratismarke",
)
@app_commands.describe(
    accion="Qué querés hacer",
)
@app_commands.choices(accion=[
    app_commands.Choice(name="Activar promo proxy (pack 3d_promo visible)", value="activar"),
    app_commands.Choice(name="Desactivar promo proxy",                       value="desactivar"),
    app_commands.Choice(name="Resetear ventana /gratismarke (nueva ventana 42h)", value="reset_gratismarke"),
])
async def promoproxy(
    interaction: discord.Interaction,
    accion: app_commands.Choice[str],
):
    await _safe_defer(interaction, ephemeral=True)

    if not _puede_registrar(interaction):
        await interaction.followup.send("No tenés permiso.", ephemeral=True)
        return

    val = accion.value
    if val == "activar":
        database.set_config("proxy_promo_active", "1")
        await interaction.followup.send(
            "✅ **Promo proxy activada.** El pack `3d_promo` ya aparece en `/comprar`.",
            ephemeral=True,
        )
    elif val == "desactivar":
        database.set_config("proxy_promo_active", "0")
        await interaction.followup.send(
            "✅ **Promo proxy desactivada.** El pack `3d_promo` ya no aparece en `/comprar`.",
            ephemeral=True,
        )
    elif val == "reset_gratismarke":
        nueva_exp = int(time.time()) + 42 * 3600
        database.set_config("gratismarke_expiry", str(nueva_exp))
        database.reset_free_trial_all()
        import datetime as _dt
        exp_hr = _dt.datetime.fromtimestamp(nueva_exp).strftime("%d/%m/%Y %H:%M")
        await interaction.followup.send(
            f"✅ **Ventana /gratismarke reseteada.**\n"
            f"Nueva expiración: **{exp_hr}** (42 h desde ahora).\n"
            f"Todos los usuarios pueden volver a usar `/gratismarke` dentro de esa ventana.",
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# Actualización del perfil del bot (avatar + banner + bio)
# ---------------------------------------------------------------------------
_perfil_actualizado = False  # solo lo hacemos una vez por sesión

BOT_BIO = (
    "💜 Comprá créditos y registrá tus IPs al instante. "
    "Comprá tu plan de proxy con /comprar y activá tu key con /key."
)

# Preferimos GIF animado; si no existe usamos PNG estático como fallback
AVATAR_PATH = Path("attached_assets/gengar_profile_animated.gif")
AVATAR_FALLBACK = Path("attached_assets/IMG_0738_1777448862841.jpeg")
BANNER_PATH = Path("attached_assets/C7286697-AF87-40EA-BFD8-BDB58D48FF51_1777448994438.gif")
BANNER_FALLBACK = Path("attached_assets/C7286697-AF87-40EA-BFD8-BDB58D48FF51_1777448994438.gif")


async def _actualizar_perfil() -> None:
    """Sube avatar, banner y bio al perfil del bot. Se ejecuta una vez por sesión."""
    global _perfil_actualizado
    if _perfil_actualizado:
        return
    _perfil_actualizado = True

    # --- Avatar (GIF animado preferido, PNG como fallback) ---
    avatar_file = AVATAR_PATH if AVATAR_PATH.exists() else AVATAR_FALLBACK
    if avatar_file.exists():
        try:
            await client.user.edit(avatar=avatar_file.read_bytes())
            log.info("Avatar del bot actualizado (%s).", avatar_file.name)
        except discord.HTTPException as e:
            log.warning("No pude cambiar el avatar: %s", e)
    else:
        log.warning("No encontré ningún archivo de avatar.")

    # --- Banner (GIF animado preferido, PNG como fallback) ---
    # Discord limita los cambios de banner; esperamos y reintentamos si hace falta
    banner_file = BANNER_PATH if BANNER_PATH.exists() else BANNER_FALLBACK
    mime = "image/gif" if banner_file.suffix == ".gif" else "image/png"
    if banner_file.exists():
        b64 = base64.b64encode(banner_file.read_bytes()).decode()
        from discord.http import Route  # noqa: PLC0415
        for attempt in range(4):
            wait = [35, 60, 120, 180][attempt]
            await asyncio.sleep(wait)
            try:
                await client.http.request(
                    Route("PATCH", "/users/@me"),
                    json={"banner": f"data:{mime};base64,{b64}"},
                )
                log.info("Banner del bot actualizado (%s).", banner_file.name)
                break
            except Exception as e:  # noqa: BLE001
                if attempt < 3:
                    log.warning("Banner rate-limited (intento %d), reintentando en %ds…",
                                attempt + 1, [60, 120, 180][attempt])
                else:
                    log.warning("No pude cambiar el banner tras 4 intentos: %s", e)
    else:
        log.warning("No encontré ningún archivo de banner.")


# ---------------------------------------------------------------------------
# Comando: verificar_ahora
# ---------------------------------------------------------------------------
@tree.command(
    name="verificar-ahora",
    description="(Admin) Asigna el rol Verificado a todos los miembros que no lo tengan",
)
async def verificar_ahora(interaction: discord.Interaction):
    if not _puede_registrar(interaction):
        try:
            await interaction.response.send_message("No tenés permiso.", ephemeral=True)
        except Exception:
            pass
        return

    await _safe_defer(interaction, ephemeral=True, thinking=True)
    guild = interaction.guild
    rol_verificado = guild.get_role(ROL_VERIFICADO_ID)
    rol_usuario    = guild.get_role(ROL_USUARIO_ID)

    if rol_verificado is None:
        await interaction.followup.send("No encontré el rol Verificado.", ephemeral=True)
        return

    verificados = 0
    async for member in guild.fetch_members(limit=None):
        if member.bot:
            continue
        if rol_verificado not in member.roles:
            roles_a_dar = [r for r in [rol_verificado, rol_usuario] if r and r not in member.roles]
            if roles_a_dar:
                try:
                    await member.add_roles(*roles_a_dar, reason="Verificación masiva /verificar-ahora")
                    await _enviar_bienvenida(member)
                    verificados += 1
                    await asyncio.sleep(0.5)   # evitar rate-limit
                except discord.HTTPException as e:
                    log.warning("No pude verificar a %s: %s", member, e)

    await interaction.followup.send(
        f"✅ Verificación completa — {verificados} miembro(s) verificado(s).",
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# Comando: /banear  (admin)
# ---------------------------------------------------------------------------
@tree.command(name="banear", description="(Admin) Banear a un usuario del servidor")
@app_commands.describe(
    usuario="Usuario a banear",
    razon="Motivo del ban",
    borrar_dias="Días de historial de mensajes a borrar (0-7, default 1)",
)
async def banear_cmd(
    interaction: discord.Interaction,
    usuario: discord.Member,
    razon: str = "Sin motivo especificado",
    borrar_dias: int = 1,
):
    if not _puede_registrar(interaction):
        try:
            await interaction.response.send_message("❌ No tenés permiso para usar este comando.", ephemeral=True)
        except Exception:
            pass
        return

    borrar_dias = max(0, min(7, borrar_dias))

    await _safe_defer(interaction, ephemeral=True, thinking=True)

    try:
        await interaction.guild.ban(
            usuario,
            reason=f"[Admin: {interaction.user}] {razon}",
            delete_message_seconds=borrar_dias * 86400,
        )
    except discord.Forbidden:
        await interaction.followup.send("❌ No tengo permisos para banear a ese usuario.", ephemeral=True)
        return
    except Exception as e:
        await interaction.followup.send(f"❌ Error al banear: {e}", ephemeral=True)
        return

    log.warning("BAN manual: %s (%s) baneado por %s — %s", usuario, usuario.id, interaction.user, razon)

    # Log en #logs-registros
    try:
        canal_logs = await _obtener_canal_logs()
        if canal_logs:
            embed = discord.Embed(title="🔨 Ban manual", color=0xFF6600)
            embed.set_author(
                name=f"{usuario} ({usuario.id})",
                icon_url=getattr(usuario.display_avatar, "url", None),
            )
            embed.add_field(name="Baneado por", value=str(interaction.user), inline=True)
            embed.add_field(name="Motivo", value=razon, inline=False)
            embed.add_field(name="Historial borrado", value=f"{borrar_dias} día(s)", inline=True)
            embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
            await canal_logs.send(embed=embed)
    except Exception:
        log.exception("Error posteando log de ban manual")

    await interaction.followup.send(
        f"✅ **{usuario}** fue baneado correctamente.\n📝 Motivo: {razon}",
        ephemeral=True,
    )


@tree.command(name="unban", description="(Admin) Desbanear a un usuario por su ID")
@app_commands.describe(
    user_id="ID numérico del usuario a desbanear",
    razon="Motivo del desban (opcional)",
)
async def unban_cmd(interaction: discord.Interaction, user_id: str, razon: str = "Sin motivo especificado"):
    if not _puede_registrar(interaction):
        await interaction.response.send_message("❌ No tenés permiso para usar este comando.", ephemeral=True)
        return

    await _safe_defer(interaction, ephemeral=True, thinking=True)

    try:
        uid = int(user_id.strip())
    except ValueError:
        await interaction.followup.send("❌ El ID ingresado no es válido. Tiene que ser un número.", ephemeral=True)
        return

    try:
        user = discord.Object(id=uid)
        await interaction.guild.unban(user, reason=f"[Admin: {interaction.user}] {razon}")
    except discord.NotFound:
        await interaction.followup.send("❌ No encontré a ese usuario en la lista de baneados.", ephemeral=True)
        return
    except discord.Forbidden:
        await interaction.followup.send("❌ No tengo permisos para desbanear usuarios.", ephemeral=True)
        return
    except Exception as exc:
        await interaction.followup.send(f"❌ Error al desbanear: {exc}", ephemeral=True)
        return

    log.info("UNBAN: id=%s desbaneado por %s — %s", uid, interaction.user, razon)

    try:
        canal_logs = await _obtener_canal_logs()
        if canal_logs:
            embed = discord.Embed(title="✅ Desban", color=0x2ECC71)
            embed.add_field(name="ID del usuario", value=str(uid), inline=True)
            embed.add_field(name="Desbaneado por", value=str(interaction.user), inline=True)
            embed.add_field(name="Motivo", value=razon, inline=False)
            embed.timestamp = discord.utils.utcnow()
            await canal_logs.send(embed=embed)
    except Exception:
        log.exception("Error posteando log de unban")

    await interaction.followup.send(
        f"✅ El usuario con ID `{uid}` fue **desbaneado** correctamente.\n📝 Motivo: {razon}",
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# Sistema de notificación de lives de TikTok
# ---------------------------------------------------------------------------
CANAL_TIKTOK_ID  = 1304582052045258752
TIKTOK_USUARIOS  = ["shutupmarke", "shutupmarke2"]
TIKTOK_INTERVALO = 180  # segundos entre cada chequeo

MENSAJES_LIVE = [
    "🔴 **¡{usuario} está EN VIVO en TikTok!** 🎮\n¡Vengan a jugar con nosotros!\n👉 {url}",
    "🚨 **¡DIRECTO AHORA!** @{usuario} prendió el live 🔥\n¡No se lo pierdan, están en vivo!\n👉 {url}",
    "🎮 **¡LIVE!** {usuario} está jugando en TikTok ahora mismo\n¡Súmanse al directo, van a pasarla genial!\n👉 {url}",
    "⚡ **¡ATENCIÓN!** {usuario} acaba de prender el live 🎉\n¡No se pierdan la acción en vivo!\n👉 {url}",
    "🎯 **¡EN VIVO AHORA!** {usuario} está en TikTok\n¡Entren rápido antes de que se pierdan todo! 🔴\n👉 {url}",
    "🌟 **¡{usuario} está en directo!** 🎮🔴\n¡Los espera para jugar juntos, vengan!\n👉 {url}",
    "📢 **¡AVISO!** {usuario} arrancó su live en TikTok\n¡El directo está a full, entren ya! 🚀\n👉 {url}",
    "🏆 **¡Hora del live!** {usuario} está jugando en vivo\n¡No falten, siempre hay buena onda! 🔴🎮\n👉 {url}",
]

_lives_activos: set[str] = set()   # usuarios cuya notificación ya se mandó esta sesión


def _check_tiktok_live(usuario: str) -> bool:
    """Devuelve True si el usuario de TikTok está actualmente en vivo."""
    import requests as req
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "es-AR,es;q=0.9",
        "Referer": "https://www.tiktok.com/",
    }
    try:
        # Paso 1: obtener el roomId de la página del usuario
        r = req.get(
            f"https://www.tiktok.com/@{usuario}/live",
            headers=headers, timeout=15, allow_redirects=True,
        )
        m = re.search(r'"roomId":"(\d+)"', r.text)
        if not m:
            return False
        room_id = m.group(1)

        # Paso 2: verificar si ese room está realmente activo via webcast API
        api = req.get(
            f"https://webcast.tiktok.com/webcast/room/check_alive/?aid=1988&room_ids={room_id}",
            headers=headers, timeout=10,
        )
        data = api.json()
        rooms = data.get("data", [])
        return bool(rooms and rooms[0].get("alive", False))
    except Exception as exc:
        log.warning("Error chequeando live de @%s: %s", usuario, exc)
        return False


async def _loop_tiktok_live():
    """Tarea en segundo plano que monitorea los lives de TikTok."""
    await client.wait_until_ready()
    log.info("Monitor TikTok iniciado (intervalo=%ds, usuarios=%s)", TIKTOK_INTERVALO, TIKTOK_USUARIOS)
    while not client.is_closed():
        for usuario in TIKTOK_USUARIOS:
            try:
                en_vivo = await asyncio.to_thread(_check_tiktok_live, usuario)
                if en_vivo and usuario not in _lives_activos:
                    _lives_activos.add(usuario)
                    log.info("@%s está en VIVO — enviando notificación", usuario)
                    channel = client.get_channel(CANAL_TIKTOK_ID)
                    if channel:
                        url = f"https://www.tiktok.com/@{usuario}/live"
                        texto = random.choice(MENSAJES_LIVE).format(usuario=f"@{usuario}", url=url)
                        await channel.send(f"@everyone\n\n{texto}")
                elif not en_vivo and usuario in _lives_activos:
                    _lives_activos.discard(usuario)
                    log.info("@%s terminó el live", usuario)
            except Exception:
                log.exception("Error en monitor TikTok para @%s", usuario)
            await asyncio.sleep(5)   # pausa entre usuarios
        await asyncio.sleep(TIKTOK_INTERVALO)


# ---------------------------------------------------------------------------
# Sistema de sincronización Gmail → Discord
# ---------------------------------------------------------------------------
CANAL_ANUNCIOS_ID   = 1305902996538003557
CANAL_REFERENCIAS_ID = 1498962751475814480

PALABRAS_REFERENCIA = [
    "gracias", "recomendado", "recomendada", "llegó", "llego", "llegaron",
    "referencia", "comprobante", "pagué", "pague", "recibí", "recibi",
    "satisfecho", "satisfecha", "excelente servicio", "lo recomiendo",
    "muy bueno", "funciona", "anda bien",
]
PALABRAS_ANUNCIO = [
    "oferta", "disponible", "actualización", "actualizacion", "precio",
    "diamantes", "pack", "nuevo", "nuevos", "lista", "$", "promo",
    "promocion", "descuento", "gratis", "recarga", "servicio",
    "anuncio", "importante", "atención", "atencion", "novedad",
]


def _decodificar_header(valor: str) -> str:
    partes = decode_header(valor or "")
    resultado = []
    for raw, enc in partes:
        if isinstance(raw, bytes):
            resultado.append(raw.decode(enc or "utf-8", errors="ignore"))
        else:
            resultado.append(raw or "")
    return " ".join(resultado)


def _clasificar(subject: str, body: str) -> str:
    """Devuelve 'anuncio', 'referencia' según el contenido."""
    texto = (subject + " " + body).lower()
    pts_ref  = sum(1 for p in PALABRAS_REFERENCIA if p in texto)
    pts_anun = sum(1 for p in PALABRAS_ANUNCIO    if p in texto)
    if pts_ref > pts_anun:
        return "referencia"
    return "anuncio"


def _imap_server(user: str) -> str:
    """Devuelve el servidor IMAP correcto según el dominio del email."""
    dominio = user.split("@")[-1].lower()
    if dominio in ("outlook.com", "hotmail.com", "live.com", "msn.com"):
        return "outlook.office365.com"
    return "imap.gmail.com"   # default Gmail


def _fetch_emails(user: str, pwd: str) -> list[dict]:
    """Conecta vía IMAP y retorna los correos no leídos con su contenido."""
    resultados = []
    servidor = _imap_server(user)
    try:
        mail = imaplib.IMAP4_SSL(servidor, 993)
        mail.login(user, pwd)
        for carpeta in ("inbox", "[Gmail]/Spam", "Junk", "Spam"):
            try:
                res, _ = mail.select(carpeta)
                if res != "OK":
                    continue
            except Exception:
                continue
            _, data = mail.search(None, "UNSEEN")
            ids = data[0].split()
            for eid in ids:
                _, raw_data = mail.fetch(eid, "(RFC822)")
                msg = email_lib.message_from_bytes(raw_data[0][1])
                mail.store(eid, "+FLAGS", "\\Seen")

                subject  = _decodificar_header(msg.get("Subject", ""))
                remitente = _decodificar_header(msg.get("From", ""))
                body = ""
                imagenes: list[tuple[str, bytes]] = []

                html_body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        ct = part.get_content_type()
                        if ct == "text/plain" and not body:
                            raw = part.get_payload(decode=True)
                            body = raw.decode(part.get_content_charset() or "utf-8", errors="ignore")
                        elif ct == "text/html" and not html_body:
                            raw = part.get_payload(decode=True)
                            html_body = raw.decode(part.get_content_charset() or "utf-8", errors="ignore")
                        elif ct.startswith("image/"):
                            ext = ct.split("/")[-1]
                            nombre = part.get_filename() or f"imagen.{ext}"
                            imagenes.append((nombre, part.get_payload(decode=True)))
                else:
                    raw = msg.get_payload(decode=True)
                    if raw:
                        ct = msg.get_content_type()
                        decoded = raw.decode(msg.get_content_charset() or "utf-8", errors="ignore")
                        if ct == "text/html":
                            html_body = decoded
                        else:
                            body = decoded
                # si no hay texto plano, extraer desde HTML
                if not body and html_body:
                    import html as html_lib
                    import re as _re
                    tmp = _re.sub(r'<br\s*/?>', '\n', html_body, flags=_re.IGNORECASE)
                    tmp = _re.sub(r'<p[^>]*>', '\n', tmp, flags=_re.IGNORECASE)
                    tmp = _re.sub(r'<[^>]+>', '', tmp)
                    body = html_lib.unescape(tmp)
                log.info("EMAIL RAW BODY (primeros 300 chars): %r", body[:300])

                if "molinamarkitos420@gmail.com" not in remitente.lower():
                    log.info("Email ignorado (remitente no autorizado): %s", remitente)
                    continue
                resultados.append({
                    "subject":  subject,
                    "from":     remitente,
                    "body":     body.strip(),
                    "imagenes": imagenes,
                })
        mail.logout()
    except imaplib.IMAP4.error as exc:
        log.warning("Error IMAP login: %s", exc)
    except Exception:
        log.exception("Error al revisar Gmail")
    return resultados


def _limpiar_texto(texto: str) -> tuple[str, bool]:
    """
    Sanitiza el cuerpo del email recibido desde WhatsApp/Gmail.
    Devuelve (texto_limpio, tiene_everyone).
    """
    import re

    # 1. Eliminar caracteres invisibles/direccionales que WhatsApp inyecta
    #    alrededor de menciones (@todos → @\u2068\u200etodos\u2069)
    INVISIBLES = (
        "\u2068\u2069\u200e\u200f\u200d\u200b\u200c"
        "\u200a\u2007\u2006\u2005\u2004\u2003\u2002\u2001\u2000"
        "\u202a\u202b\u202c\u202d\u202e\ufeff\u034f\u00ad"
    )
    for ch in INVISIBLES:
        texto = texto.replace(ch, "")

    # 2. Normalizar saltos de línea
    texto = texto.replace("\r\n", "\n").replace("\r", "\n")

    # 3. Detectar @todos DESPUÉS de limpiar invisibles
    tiene_everyone = bool(re.search(r"@todos", texto, re.IGNORECASE))

    # 4. Reemplazar @todos → @everyone
    texto = re.sub(r"@todos", "@everyone", texto, flags=re.IGNORECASE)

    # 5. Eliminar secuencias de guiones / separadores (3+ caracteres)
    texto = re.sub(r"[-—–_=]{3,}", "", texto)

    # 6. Quitar marcadores *negrita* de WhatsApp pero conservar el contenido
    texto = re.sub(r"\*([^*\n]+)\*", r"\1", texto)

    # 7. Cada emoji de viñeta que aparece en medio de una línea arranca nueva línea
    BULLETS = r"(🥷|💎|🧸|🇧🇷|📢|✅|⚠️|🔥|💰|🎁)"
    texto = re.sub(r"(?<!\n)\s+" + BULLETS, r"\n\1", texto)

    # 8. Limpiar línea a línea
    lineas_ok = []
    for linea in texto.splitlines():
        linea = linea.strip()
        if not linea:
            continue
        # descartar cabeceras residuales de email
        if re.match(
            r"^(de:|para:|from:|to:|date:|subject:|enviado|sent from|get outlook)",
            linea, re.IGNORECASE
        ):
            continue
        lineas_ok.append(linea)

    return "\n".join(lineas_ok).strip(), tiene_everyone


async def _publicar_email(info: dict) -> None:
    """Clasifica un email y lo publica en el canal de Discord correspondiente."""
    tipo = _clasificar(info["subject"], info["body"])
    canal_id = CANAL_REFERENCIAS_ID if tipo == "referencia" else CANAL_ANUNCIOS_ID

    canal = client.get_channel(canal_id)
    if canal is None:
        log.warning("Canal %s no encontrado", canal_id)
        return

    if tipo == "anuncio":
        color  = 0xF1C40F   # dorado
        titulo = "📢 NOVEDADES SENSI MARKE"
    else:
        color  = 0x2ECC71   # verde
        titulo = "✅ NUEVA REFERENCIA"

    desc, tiene_everyone = _limpiar_texto(info["body"]) if info["body"] else ("*(sin texto)*", False)

    embed = discord.Embed(title=titulo, description=desc[:3900], color=color)

    archivos = [
        discord.File(io.BytesIO(datos), filename=nombre)
        for nombre, datos in info["imagenes"][:4]
    ]
    if archivos:
        embed.set_image(url=f"attachment://{info['imagenes'][0][0]}")

    try:
        # si hay @todos: primero enviamos @everyone como texto plano (hace ping real),
        # luego el embed por separado
        if tiene_everyone:
            await canal.send(
                "@everyone",
                allowed_mentions=discord.AllowedMentions(everyone=True)
            )
        await canal.send(
            embed=embed,
            files=archivos or discord.utils.MISSING,
            allowed_mentions=discord.AllowedMentions(everyone=True)
        )
        log.info("Email publicado como %s en canal %s", tipo, canal_id)
    except Exception:
        log.exception("No pude publicar el email en Discord")


async def _loop_gmail_sync() -> None:
    """Tarea en segundo plano: revisa Gmail cada 60 s."""
    await client.wait_until_ready()
    user = os.environ.get("EMAIL_USER")
    pwd  = os.environ.get("EMAIL_PASS")
    if not user or not pwd:
        log.warning("EMAIL_USER / EMAIL_PASS no configurados — sincronización Gmail desactivada.")
        return
    log.info("Monitor email iniciado (%s → %s)", user, _imap_server(user))
    while not client.is_closed():
        try:
            emails = await asyncio.to_thread(_fetch_emails, user, pwd)
            for info in emails:
                await _publicar_email(info)
        except Exception:
            log.exception("Error en loop Gmail")
        await asyncio.sleep(60)


# ---------------------------------------------------------------------------
# Sistema de verificación / bienvenida
# ---------------------------------------------------------------------------
CANAL_VERIFICACION_ID = 1498949218973257848
CANAL_BIENVENIDA_ID   = 1499132924380053506
ROL_VERIFICADO_ID     = 1498949610792681502
ROL_USUARIO_ID        = None   # rol eliminado del servidor


async def _enviar_bienvenida(member: discord.Member) -> None:
    """Envía el embed de bienvenida al canal #bienvenida."""
    canal = client.get_channel(CANAL_BIENVENIDA_ID)
    if canal is None:
        return
    embed = discord.Embed(
        title="👋 ¡Bienvenido a Sensi Marke!",
        description=(
            f"Hola {member.mention}, ya sos parte de **Sensi Marke**.\n\n"
            "Aquí puedes obtener Diamantes y Archivos de iOS, PC y Android.\n\n"
            "Si tienes alguna duda, no dudes en abrir ticket que te estaremos "
            "contestando a la brevedad."
        ),
        color=0x1F3A6B,   # azul oscuro
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    await canal.send(embed=embed)


class VerificacionView(_SafeViewMixin, discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="✅  Verificarme",
        style=discord.ButtonStyle.success,
        custom_id="verificacion_btn",
        emoji="🔓",
    )
    async def verificar(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("Botón verificar presionado por %s (%s)", interaction.user, interaction.user.id)
        await _safe_defer(interaction, ephemeral=True, thinking=True)

        async def _reply(content: str):
            await interaction.followup.send(content, ephemeral=True)

        guild = interaction.guild
        if guild is None:
            await _reply("❌ Esta verificación solo funciona dentro del servidor.")
            return

        # Refrescar el member desde la API para tener roles actuales
        member = guild.get_member(interaction.user.id)
        if member is None:
            try:
                member = await guild.fetch_member(interaction.user.id)
            except discord.NotFound:
                await _reply("❌ No te encontré como miembro del servidor. Salí y volvé a entrar.")
                return
            except Exception:
                log.exception("Error haciendo fetch_member para %s", interaction.user.id)
                await _reply("❌ Error al obtener tu información. Intentá de nuevo.")
                return

        rol_verificado = guild.get_role(ROL_VERIFICADO_ID)
        if rol_verificado is None:
            log.error("ROL_VERIFICADO_ID=%s no existe en el guild %s",
                      ROL_VERIFICADO_ID, guild.id)
            await _reply(
                "⚠️ El rol de verificación no existe en el servidor. "
                "Avisale a un admin para que lo recree."
            )
            return

        if rol_verificado in getattr(member, "roles", []):
            await _reply("✅ Ya estás verificado. ¡Bienvenido al servidor!")
            return

        # Verificar jerarquía: el rol del bot tiene que estar POR ENCIMA del
        # rol que va a asignar.
        bot_member = guild.me
        if bot_member is None or bot_member.top_role <= rol_verificado:
            log.error(
                "Jerarquía inválida: top_role del bot=%s, rol verificado=%s",
                getattr(bot_member, "top_role", None), rol_verificado,
            )
            await _reply(
                "⚠️ El bot no puede asignar este rol porque está más abajo en "
                "la jerarquía. Pedile a un admin que mueva el rol del bot "
                "**por encima** de **@Verificado** en Ajustes → Roles."
            )
            return

        if not bot_member.guild_permissions.manage_roles:
            log.error("El bot no tiene permiso Manage Roles en %s", guild.id)
            await _reply(
                "⚠️ El bot no tiene el permiso **Gestionar Roles**. "
                "Pedile a un admin que se lo dé."
            )
            return

        try:
            await member.add_roles(rol_verificado, reason="Verificación por botón")
            await _reply("✅ ¡Verificado correctamente! Ya podés acceder a todos los canales. 🎉")
            log.info("Usuario verificado: %s (%s)", member, member.id)
            try:
                await _enviar_bienvenida(member)
            except Exception:
                log.exception("Error mandando bienvenida a %s", member)
        except discord.Forbidden as e:
            log.warning("Forbidden al verificar a %s: %s", member, e)
            await _reply(
                "❌ No tengo permiso para asignarte el rol. "
                "Avisale a un admin que revise la jerarquía y los permisos del bot."
            )
        except discord.HTTPException as e:
            log.warning("HTTPException al verificar a %s: %s (status=%s)",
                        member, e, getattr(e, "status", "?"))
            await _reply("❌ Discord rechazó la operación. Probá de nuevo en unos segundos.")
        except Exception:
            log.exception("Error inesperado al verificar a %s", member)
            await _reply("❌ Ocurrió un error inesperado. Intentá de nuevo o contactá a un admin.")


# ---------------------------------------------------------------------------
# Sistema de Tickets
# ---------------------------------------------------------------------------
_ticket_counter = 0
_active_tickets: dict[int, dict] = {}   # channel_id → info del ticket
_tickets_lock = threading.Lock()

# Pagos Binance pendientes de aprobación
# op_id → {discord_id, user_id, pack_id, pack, channel_id, username}
_pending_binance: dict[str, dict] = {}
_pending_binance_lock = threading.Lock()
# Usuarios esperando enviar captura: user_id → op_id
_waiting_comprobante: dict[int, str] = {}
# Usuarios esperando comprobante de transferencia de diamantes: user_id → {diamonds, id_freefire, precio}
_pending_diam_transferencia: dict[int, dict] = {}
# Guard anti-duplicados: op_ids ya acreditados (evita doble crédito si la IA
# y el admin aprueban casi simultáneamente)
_ops_ya_procesadas: set[str] = set()


def _ticket_mas_reciente() -> dict | None:
    """Retorna el ticket abierto más recientemente, o None si no hay ninguno."""
    with _tickets_lock:
        if not _active_tickets:
            return None
        return max(_active_tickets.values(), key=lambda t: t["created_at"])


async def _asignar_rol_verificado(user_id: int, motivo: str = "") -> bool:
    """Asigna el rol Verificado al miembro indicado.

    Devuelve True si se asignó (o si ya lo tenía), False si falló.
    Se usa al acreditar un pago: si pagó, está verificado.
    """
    if not GUILD_ID:
        return False
    try:
        guild = client.get_guild(int(GUILD_ID))
    except (TypeError, ValueError):
        return False
    if guild is None:
        return False

    member = guild.get_member(int(user_id))
    if member is None:
        try:
            member = await guild.fetch_member(int(user_id))
        except discord.NotFound:
            log.warning(
                "No pude asignar rol Verificado: %s no es miembro del guild",
                user_id,
            )
            return False
        except Exception:
            log.exception("Error fetcheando miembro %s", user_id)
            return False

    rol = guild.get_role(ROL_VERIFICADO_ID)
    if rol is None:
        log.error("ROL_VERIFICADO_ID=%s no existe", ROL_VERIFICADO_ID)
        return False

    if rol in member.roles:
        return True  # ya verificado

    bot_member = guild.me
    if bot_member is None or bot_member.top_role <= rol:
        log.error(
            "No puedo asignar rol Verificado a %s: jerarquía mala "
            "(bot top=%s, rol=%s)",
            member, getattr(bot_member, "top_role", None), rol,
        )
        return False

    try:
        await member.add_roles(rol, reason=motivo or "Pago aprobado")
        log.info("Rol Verificado asignado a %s (%s) — %s",
                 member, member.id, motivo or "pago aprobado")
        return True
    except discord.Forbidden:
        log.warning("Forbidden al asignar rol Verificado a %s", member)
        return False
    except Exception:
        log.exception("Error asignando rol Verificado a %s", member)
        return False


async def _acreditar_pago_aprobado(
    op_id: str, op: dict, fuente: str = "whatsapp"
) -> None:
    """Acredita los créditos del pack aprobado (o entrega el archivo), notifica
    al usuario por DM, publica en #ventas y borra el pago de la cola pendiente.
    """
    # ── Guard anti-duplicados ─────────────────────────────────────────────────
    with _pending_binance_lock:
        if op_id in _ops_ya_procesadas:
            log.warning(
                "Doble acreditación bloqueada para %s (fuente=%s) — ya fue procesado.",
                op_id, fuente,
            )
            return
        _ops_ya_procesadas.add(op_id)

    pack = op["pack"]
    discord_id = op["discord_id"]
    user_id = op["user_id"]
    es_nx = op_id.startswith("NX-")

    # ── Créditos / entrega según categoría del pack ─────────────────────────
    proxy_key: str | None = None
    if pack.categoria == "sensi":
        nuevo_total = database.add_sensi_credits(discord_id, pack.creditos)
        tipo_credito = "de sensi"
    elif pack.categoria in ("android", "ios"):
        nuevo_total = 0
        tipo_credito = pack.categoria
    else:
        # Proxy → generar key y enviar por DM (sin créditos)
        nuevo_total = 0
        tipo_credito = "de proxy"
        proxy_key = _generar_key_proxy()
        dias_proxy = int(pack.creditos)
        _key_registrada = False
        try:
            await telegram_client.cmd_gen(proxy_key, dias_proxy)
            _key_registrada = True
            log.info(
                "🔑 KEY PAGO — key=%s dias=%d op=%s pack=%s usuario=%s",
                proxy_key, dias_proxy, op_id, pack.id, discord_id,
            )
            asyncio.create_task(_log_key(
                "gen_ok", proxy_key, discord_id,
                dias=dias_proxy,
                metodo="Naranja X/Binance" if not op_id.startswith("NX-") else "Naranja X",
            ))
        except Exception as _exc_gen:
            log.exception(
                "Error generando key proxy via Telegram para %s pack=%s — key=%s",
                discord_id, pack.id, proxy_key,
            )
            asyncio.create_task(_log_key(
                "gen_error", proxy_key, discord_id,
                dias=dias_proxy,
                metodo="Naranja X/Binance",
                error=str(_exc_gen),
            ))
            # Avisar al canal de ventas para entrega manual
            try:
                canal_ventas = await _obtener_canal_ventas()
                if canal_ventas:
                    await canal_ventas.send(
                        f"⚠️ **KEY NO REGISTRADA — entrega manual requerida**\n"
                        f"Op: `{op_id}` | Usuario: <@{discord_id}>\n"
                        f"🔑 Key generada: ||`{proxy_key}`|| ({dias_proxy}d)\n"
                        f"Error Telegram: `{_exc_gen}`\n"
                        f"Usar `/enviar-key` cuando el sistema Telegram esté disponible."
                    )
            except Exception:
                log.exception("No pude avisar en canal ventas del error de gen para %s", op_id)

    status_str = "approved_naranjax" if es_nx else "approved_binance"
    if fuente == "ia":
        status_str += "_ia"
    database.record_payment(
        payment_id=op_id,
        discord_id=discord_id,
        pack=pack.id,
        credits_added=pack.creditos,
        amount=0.0,
        status=status_str,
    )

    metodo_str = "Naranja X" if es_nx else "Binance"
    log.info(
        "Pago %s %s aprobado (%s) → %s [%s]",
        metodo_str, op_id, fuente, discord_id, pack.id,
    )

    # Auto-verificar si pagó
    try:
        await _asignar_rol_verificado(
            user_id, motivo=f"Pago aprobado #{op_id}"
        )
    except Exception:
        log.exception("Error asignando rol Verificado tras aprobar %s", op_id)

    # ── Notificar + entregar por DM ──────────────────────────────────────────
    try:
        user = await client.fetch_user(user_id)
        if pack.categoria == "android":
            await user.send(
                f"✅ **¡Pago aprobado!** — operación `#{op_id}`\n"
                f"Tu **{pack.nombre}** está listo. Enviando descarga ahora... 👇"
            )
            await _enviar_entrega_android(user, pack)
        elif pack.categoria == "ios":
            await user.send(
                f"✅ **¡Pago aprobado!** — operación `#{op_id}`\n"
                f"Tu archivo iOS **{pack.nombre}** está listo. Enviando enlace ahora... 👇"
            )
            await _enviar_entrega_ios(user, pack)
        else:
            # Proxy → enviar key por DM (solo si fue registrada en Telegram)
            dias_proxy = int(pack.creditos)
            if not _key_registrada:
                log.error(
                    "KEY NO ENTREGADA — no se registró en Telegram. Op=%s user=%s key=%s",
                    op_id, discord_id, proxy_key,
                )
                return
            embed_key = discord.Embed(
                title="✅ ¡Pago aprobado! Tu key de proxy está lista",
                description=(
                    f"Tu operación `#{op_id}` fue confirmada.\n\n"
                    f"🔑 **Tu key ({dias_proxy} día{'s' if dias_proxy != 1 else ''}):**\n"
                    f"```\n{proxy_key}\n```\n"
                    f"Usá el comando `/key` en el servidor para activarla con tu IP.\n\n"
                    f"🌐 **Servidor:** `108.181.215.247`\n"
                    f"👔 **Puerto Cuello:** `10065`\n"
                    f"👕 **Puerto Pecho:** `10066`\n"
                    f"👤 **Login:** ||DGZADAXFF||\n"
                    f"🔒 **Contraseña:** ||DGZADAXFF||\n\n"
                    f"📋 El tutorial de configuración te lo enviamos por este mismo chat.\n\n"
                    f"¡Muchísimas gracias por comprar en **Sensi Marke**! 🖤\n"
                    f"Recordá estar atento al grupo:\n"
                    f"https://chat.whatsapp.com/DQxndyWBG860vpaVcxam3s"
                ),
                color=0x2ECC71,
            )
            await user.send(embed=embed_key)
    except Exception:
        log.exception("No pude notificar al usuario %s por DM", user_id)
        # Si es una key de proxy y el DM falló, logear la key en texto claro
        # para que el admin pueda entregarla a mano.
        if proxy_key:
            log.error(
                "⚠️ KEY NO ENTREGADA — op=%s user=%s key=%s dias=%s — "
                "DM bloqueado, entregar manualmente con /enviar-key",
                op_id, discord_id, proxy_key, int(pack.creditos),
            )
            # Intentar avisar en el canal de ventas
            try:
                canal_ventas = await _obtener_canal_ventas()
                if canal_ventas:
                    await canal_ventas.send(
                        f"⚠️ **KEY NO ENTREGADA por DM bloqueado**\n"
                        f"Op: `{op_id}` | Usuario: <@{user_id}>\n"
                        f"🔑 Key: ||`{proxy_key}`|| ({int(pack.creditos)}d)\n"
                        f"Entregarla manualmente con `/enviar-key`."
                    )
            except Exception:
                log.exception("Tampoco pude avisar en canal ventas para %s", op_id)

    # ── Comisión de afiliado (30%) ───────────────────────────────────────────
    try:
        referrer_id = database.get_referrer(discord_id)
        if referrer_id:
            commission = pack.precio * 0.30
            sales_count, _ = database.increment_referral_sales(referrer_id)
            nuevo_bal = database.add_referral_commission(referrer_id, commission)
            log.info(
                "Comisión %.2f ARS (30%%) acreditada a %s por compra de %s",
                commission, referrer_id, discord_id,
            )
            try:
                referrer_user = await client.fetch_user(int(referrer_id))
                await referrer_user.send(
                    f"💰 **¡Comisión de afiliado!**\n"
                    f"Tu referido acaba de comprar **{pack.nombre}** (${pack.precio:,.0f} ARS).\n"
                    f"Recibiste **${commission:,.0f} ARS** de comisión (30%). 🎉\n"
                    f"Saldo total acumulado: **${nuevo_bal:,.0f} ARS**\n\n"
                    f"Usá `/perfil` para ver todo tu historial."
                )
            except Exception:
                log.warning("No pude notificar comisión a %s por DM", referrer_id)
    except Exception:
        log.exception("Error procesando comisión de afiliado para %s", discord_id)

    await _publicar_en_ventas(discord_id, pack, pack.precio, metodo_str)
    try:
        database.delete_pending_payment(op_id)
    except Exception:
        log.exception("No pude borrar pending_payments %s", op_id)
    with _pending_binance_lock:
        _pending_binance.pop(op_id, None)


class AprobarPagoView(_SafeViewMixin, discord.ui.View):
    """Botones de Aprobar / Rechazar para revisión manual de comprobantes.

    Es una Vista PERSISTENTE (custom_ids fijos + registrada en on_ready).
    Cuando se restaura tras un reinicio, el op_id y op se reconstruyen
    desde el mensaje o la DB en lugar de quedar guardados en self.
    """

    def __init__(self, op_id: str = "", op: dict | None = None):
        super().__init__(timeout=None)
        self.op_id = op_id
        self.op    = op or {}

    # ── Helpers de recuperación post-reinicio ──────────────────────────────
    def _resolver_op_id(self, interaction: discord.Interaction) -> str:
        """Extrae el op_id del mensaje si no está en self.op_id."""
        if self.op_id:
            return self.op_id
        msg = interaction.message
        if msg:
            import re as _re
            # Buscar en contenido, description y también en field values del embed
            textos = [msg.content or ""]
            for e in (msg.embeds or []):
                textos.append(e.description or "")
                textos.append(e.title or "")
                for f in (e.fields or []):
                    textos.append(f.value or "")
            for texto in textos:
                m = _re.search(r"`#((?:NX|BN)-\d+)`", texto)
                if m:
                    return m.group(1)
        return ""

    def _resolver_op(self, op_id: str) -> dict:
        """Recupera el dict de la operación desde memoria o DB."""
        if self.op:
            return self.op
        with _pending_binance_lock:
            cached = _pending_binance.get(op_id)
        if cached:
            return cached
        # Fallback: reconstruir desde la DB
        row = database.get_pending_payment(op_id)
        if row:
            pack_obj = payments.PACKS.get(row["pack_id"])
            return {
                "discord_id": row["discord_id"],
                "user_id":    row["user_id"],
                "pack":       pack_obj,
                "channel_id": row["channel_id"],
                "username":   row["username"],
                "metodo":     row["metodo"],
            }
        return {}

    # ── Botón Aprobar ──────────────────────────────────────────────────────
    @discord.ui.button(label="✅ Aprobar", style=discord.ButtonStyle.success, custom_id="aprobar_pago")
    async def aprobar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _puede_registrar(interaction):
            try:
                await interaction.response.send_message("❌ Solo administradores.", ephemeral=True)
            except Exception:
                pass
            return

        # Recuperar op_id y op (funciona tanto en sesión viva como tras reinicio)
        op_id = self._resolver_op_id(interaction)
        op    = self._resolver_op(op_id)

        if not op_id or not op:
            await _safe_defer(interaction, ephemeral=True)
            await interaction.followup.send(
                "❌ No se pudo encontrar la operación. Usá `/aprobar-manual` para acreditar.",
                ephemeral=True,
            )
            return

        await _safe_defer(interaction, ephemeral=False)
        # Desactivar botones para evitar doble clic
        for child in self.children:
            child.disabled = True  # type: ignore[union-attr]
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass

        await _acreditar_pago_aprobado(op_id, op, fuente="discord_manual")
        try:
            await interaction.message.edit(
                content=f"✅ **Aprobado** por {interaction.user.mention} — `#{op_id}`",
                view=self,
            )
        except Exception:
            pass
        log.info("Comprobante %s aprobado desde botón Discord por %s", op_id, interaction.user)

    # ── Botón Rechazar ─────────────────────────────────────────────────────
    @discord.ui.button(label="❌ Rechazar", style=discord.ButtonStyle.danger, custom_id="rechazar_pago")
    async def rechazar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _puede_registrar(interaction):
            try:
                await interaction.response.send_message("❌ Solo administradores.", ephemeral=True)
            except Exception:
                pass
            return

        op_id = self._resolver_op_id(interaction)
        op    = self._resolver_op(op_id)

        if not op_id:
            await _safe_defer(interaction, ephemeral=True)
            await interaction.followup.send(
                "❌ No se pudo identificar la operación.", ephemeral=True
            )
            return

        await _safe_defer(interaction, ephemeral=False)
        for child in self.children:
            child.disabled = True  # type: ignore[union-attr]
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass

        # Notificar al usuario del rechazo
        try:
            user_id_val = op.get("user_id") if op else None
            if user_id_val:
                user = await client.fetch_user(int(user_id_val))
                await user.send(
                    f"❌ **Comprobante rechazado** — operación `#{op_id}`.\n"
                    "Si creés que es un error, abrí un ticket en el servidor para revisión manual."
                )
        except Exception:
            log.warning("No pude DM rechazar para op %s", op_id)

        try:
            database.delete_pending_payment(op_id)
        except Exception:
            pass
        with _pending_binance_lock:
            _pending_binance.pop(op_id, None)

        try:
            await interaction.message.edit(
                content=f"❌ **Rechazado** por {interaction.user.mention} — `#{op_id}`",
                view=self,
            )
        except Exception:
            pass
        log.info("Comprobante %s rechazado desde botón Discord por %s", op_id, interaction.user)


_MEDIA_DIR = Path(__file__).parent.parent / "tmp_media"
_MEDIA_DIR.mkdir(exist_ok=True)


async def _descargar_imagen_estable(discord_url: str, op_id: str) -> str:
    """Descarga la imagen de Discord CDN y la sirve desde nuestra URL pública estable.

    Las URLs de Discord CDN expiran rápidamente (parámetros ex=/is=/hm=).
    Twilio intenta descargar la imagen cuando procesa el mensaje, y si la URL
    ya expiró el mensaje no se entrega. Solución: la descargamos nosotros y la
    servimos desde nuestro servidor Flask que no expira.

    Retorna la URL estable, o la URL original si falla la descarga.
    """
    if not _REPLIT_DOMAIN:
        return discord_url
    try:
        safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", op_id)
        filename = f"comprobante_{safe_id}.jpg"
        dest = _MEDIA_DIR / filename
        async with aiohttp.ClientSession() as session:
            async with session.get(discord_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    log.warning("No pude descargar imagen para %s (status %s)", op_id, resp.status)
                    return discord_url
                data = await resp.read()
        dest.write_bytes(data)
        stable_url = f"https://{_REPLIT_DOMAIN}/media/{filename}"
        log.info("Imagen %s descargada → %s", op_id, stable_url)
        return stable_url
    except Exception:
        log.exception("Error descargando imagen para %s, usando URL original", op_id)
        return discord_url


async def _procesar_comprobante_con_ia(
    *,
    op_id: str,
    op: dict,
    image_url: str,
    user_message: discord.Message,
) -> None:
    """Analiza el comprobante con IA y decide:
       - approved → acredita automáticamente y avisa al user
       - manual   → manda a WhatsApp para que el admin confirme
       - rejected → DM al user con el motivo, no consume nada
    """
    pack = op["pack"]
    username = op["username"]
    discord_id = op["discord_id"]
    metodo_label = "NaranjaX" if op_id.startswith("NX-") else "Binance"
    metodo_friendly = "Naranja X" if metodo_label == "NaranjaX" else "Binance"

    # Aviso inmediato al usuario de que estamos analizando
    try:
        thinking = await user_message.reply(
            "🧾 Comprobante recibido, confirmando pago…"
        )
    except Exception:
        thinking = None

    # ── Notificar a #ventas apenas llega el comprobante (antes del análisis IA)
    try:
        canal_ventas = await _obtener_canal_ventas()
        if canal_ventas:
            embed_recibido = discord.Embed(
                title=f"📥 Comprobante recibido — {metodo_friendly}",
                color=0x3498DB,
            )
            embed_recibido.add_field(name="👤 Usuario", value=f"{username} (`{discord_id}`)", inline=True)
            embed_recibido.add_field(name="📦 Pack", value=f"{pack.nombre} — ${pack.precio:,.0f} ARS", inline=True)
            embed_recibido.add_field(name="🆔 Operación", value=f"`#{op_id}`", inline=False)
            embed_recibido.set_image(url=image_url)
            embed_recibido.set_footer(text="Podés aprobar o rechazar con los botones. La IA también analiza automáticamente.")
            await canal_ventas.send(embed=embed_recibido, view=AprobarPagoView(op_id=op_id, op=op))
    except Exception:
        log.exception("No pude postear comprobante recibido en #ventas para %s", op_id)

    # Llamar al analizador IA
    try:
        resultado = await comprobante_ai.analizar(
            client=_openai_client,
            image_url=image_url,
            metodo=metodo_label,
            monto_esperado_ars=float(pack.precio),
            op_id=op_id,
            op_id_existe_fn=database.numero_op_existe,
        )
    except Exception:
        log.exception("Error en analizador IA para %s", op_id)
        resultado = comprobante_ai.ResultadoComprobante(
            decision="manual",
            motivos=["Error inesperado al analizar la imagen."],
        )

    datos = resultado.datos or {}

    # ── Auto-aprobación blanda ────────────────────────────────────────────
    # Si la IA dejó el comprobante en "manual" pero NO detectó ninguna señal
    # de alarma, lo aprobamos solos en vez de molestar al admin por WhatsApp.
    # Solo aplica a NaranjaX (en NX se valida monto, destinatario y fecha
    # automáticamente). En Binance el monto va en USDT y depende de la
    # cotización del día → auto-aprobar sería peligroso, sigue manual.
    if (
        resultado.decision == "manual"
        and metodo_label == "NaranjaX"
        and not (datos.get("señales_alarma") or [])
        and datos.get("es_comprobante")
    ):
        log.info(
            "Comprobante %s → manual sin señales de alarma, AUTO-APROBADO "
            "(motivos originales: %s)",
            op_id, " | ".join(resultado.motivos) if resultado.motivos else "—",
        )
        resultado.decision = "approved"
        resultado.motivos.append("Auto-aprobado: la IA no reportó señales de alarma.")

    motivos_str = " | ".join(resultado.motivos) if resultado.motivos else ""

    # Persistir el análisis en DB (auditoría + duplicados)
    try:
        database.save_comprobante(
            payment_id=op_id,
            discord_id=str(discord_id),
            metodo=metodo_label,
            image_url=image_url,
            numero_op=(datos.get("numero_operacion") or None),
            monto=datos.get("monto"),
            moneda=datos.get("moneda"),
            titular=datos.get("destinatario_nombre"),
            alias=datos.get("destinatario_alias"),
            fecha_op=(datos.get("fecha_iso") or None),
            confianza=datos.get("confianza_real"),
            decision=resultado.decision,
            motivos=motivos_str,
            raw_json=resultado.raw[:4000] if resultado.raw else "",
        )
    except Exception:
        log.exception("No pude guardar análisis del comprobante %s", op_id)

    # 🟢 AUTO-APROBADO
    if resultado.decision == "approved":
        if thinking:
            try:
                await thinking.edit(
                    content="✅ Pago confirmado. Acreditando tu pack…"
                )
            except Exception:
                pass
        await _acreditar_pago_aprobado(op_id, op, fuente="ia")
        log.info(
            "Comprobante %s %s AUTO-APROBADO por IA (%s)",
            metodo_friendly, op_id, resultado.resumen(),
        )
        return

    # 🔴 RECHAZADO
    if resultado.decision == "rejected":
        motivo_user = resultado.motivo_user or (
            "El comprobante no pudo ser confirmado. Si creés que es un error, "
            "abrí un ticket para revisión manual."
        )
        # Devolver el usuario al estado de espera para que pueda reintentar
        with _pending_binance_lock:
            _waiting_comprobante[user_message.author.id] = op_id
        msg_user = (
            f"❌ **Comprobante rechazado** — operación `#{op_id}`.\n"
            f"**Motivo:** {motivo_user}\n\n"
            f"Si creés que es un error, podés:\n"
            f"• Volver a mandar el comprobante correcto aquí mismo, **o**\n"
            f"• Abrir un ticket en el servidor para revisión manual."
        )
        if thinking:
            try:
                await thinking.edit(content=msg_user)
            except Exception:
                await user_message.reply(msg_user)
        else:
            await user_message.reply(msg_user)

        # Avisar al admin igualmente para auditoría (sin pedir aprobación)
        try:
            wa_body = (
                f"🚨 *Comprobante RECHAZADO por IA* ({metodo_friendly})\n"
                f"👤 {username}\n"
                f"📦 {pack.nombre} — ${pack.precio:,.0f} ARS\n"
                f"🆔 #{op_id}\n"
                f"❌ {motivos_str}\n"
                f"📊 {resultado.resumen()}\n\n"
                f"Si querés aprobarlo igual: *#aprobar {op_id}*"
            )
            sent = await asyncio.to_thread(
                twilio_helper.send_whatsapp_media, wa_body, image_url
            )
            if not sent:
                await asyncio.to_thread(twilio_helper.send_whatsapp, wa_body)
        except Exception:
            log.exception("No pude notificar rechazo por WhatsApp para %s", op_id)
        log.warning(
            "Comprobante %s %s RECHAZADO por IA: %s",
            metodo_friendly, op_id, motivos_str,
        )
        return

    # 🟡 MANUAL — mandar al admin con datos extraídos para confirmar
    if thinking:
        try:
            await thinking.edit(
                content=(
                    f"✅ Comprobante recibido. Está en revisión manual "
                    f"(`#{op_id}`). Te aviso por aquí cuando se apruebe. 🕐"
                )
            )
        except Exception:
            pass
    else:
        try:
            await user_message.reply(
                f"✅ Comprobante recibido. La operación `#{op_id}` está siendo "
                f"revisada. Te aviso cuando sea aprobada. 🕐"
            )
        except Exception:
            pass

    # ── Embed con botones en #ventas (canal de admins) ───────────────────────
    try:
        canal_admin = await _obtener_canal_ventas()
        if canal_admin:
            embed_rev = discord.Embed(
                title=f"🧾 Comprobante en REVISIÓN — {metodo_friendly}",
                color=0xF39C12,
            )
            embed_rev.add_field(name="👤 Usuario", value=f"{username} (`{discord_id}`)", inline=True)
            embed_rev.add_field(name="📦 Pack", value=f"{pack.nombre} — ${pack.precio:,.0f} ARS", inline=True)
            embed_rev.add_field(name="🆔 Operación", value=f"`#{op_id}`", inline=True)
            embed_rev.add_field(name="📊 IA", value=resultado.resumen() or "Sin análisis", inline=False)
            embed_rev.add_field(name="⚠️ Alerta", value=motivos_str or "Sin alertas", inline=False)
            embed_rev.set_footer(text="Usá los botones para aprobar o rechazar")
            view = AprobarPagoView(op_id=op_id, op=op)
            await canal_admin.send(embed=embed_rev, view=view)
            log.info("Embed de revisión con botones posteado en #ventas para %s", op_id)
    except Exception:
        log.exception("No pude postear embed de revisión para %s", op_id)

    # ── WhatsApp ────────────────────────────────────────────────────────────
    wa_body = (
        f"🧾 *Comprobante {metodo_friendly}* — REVISIÓN\n"
        f"👤 {username}\n"
        f"📦 {pack.nombre} — ${pack.precio:,.0f} ARS\n"
        f"🆔 #{op_id}\n"
        f"📊 IA: {resultado.resumen()}\n"
        f"⚠️ {motivos_str or 'Sin alertas'}\n\n"
        f"Para aprobar: *#aprobar {op_id}*"
    )
    try:
        sent = await asyncio.to_thread(
            twilio_helper.send_whatsapp_media, wa_body, image_url
        )
        if not sent:
            await asyncio.to_thread(twilio_helper.send_whatsapp, wa_body)
    except Exception:
        log.exception("No pude enviar comprobante a WhatsApp para %s", op_id)
        try:
            await asyncio.to_thread(twilio_helper.send_whatsapp, wa_body)
        except Exception:
            pass
    log.info(
        "Comprobante %s %s a REVISIÓN manual: %s",
        metodo_friendly, op_id, motivos_str,
    )


def _whatsapp_a_discord(body: str, from_number: str) -> None:
    """Callback llamado desde Flask cuando llega un mensaje de WhatsApp."""
    if not client.is_ready():
        log.warning("Cliente Discord no listo para recibir respuesta WhatsApp")
        return

    # ── Comando #aprobar BN-XXXX o NX-XXXX ────────────────────────────────
    aprobar_match = re.match(r"#aprobar\s+((BN|NX)-\d+)", body, re.IGNORECASE)
    if aprobar_match:
        op_id = aprobar_match.group(1).upper()
        with _pending_binance_lock:
            op = _pending_binance.pop(op_id, None)

        # Si no está en memoria, intentar recuperar de DB (sobrevive a reinicios)
        if op is None:
            try:
                row = database.get_pending_payment(op_id)
            except Exception:
                row = None
                log.exception("Error consultando pending_payments para %s", op_id)
            if row:
                pack_obj = payments.PACKS.get(row["pack_id"])
                if pack_obj is None:
                    log.warning("Pack %s no encontrado para %s", row["pack_id"], op_id)
                    return
                op = {
                    "discord_id": row["discord_id"],
                    "user_id":    row["user_id"],
                    "pack":       pack_obj,
                    "channel_id": row["channel_id"],
                    "username":   row["username"],
                    "metodo":     row["metodo"],
                }
                log.info("Operación %s recuperada de DB tras reinicio", op_id)

        if op is None:
            log.warning("Operación %s no encontrada para aprobar", op_id)
            return

        asyncio.run_coroutine_threadsafe(
            _acreditar_pago_aprobado(op_id, op, fuente="whatsapp"),
            client.loop,
        )
        return

    # ── Routing a ticket (comportamiento existente) ────────────────────────
    target_ticket = None
    match = re.match(r"^#(\d+)\s*", body)
    if match:
        num = int(match.group(1))
        body = body[match.end():]
        with _tickets_lock:
            for t in _active_tickets.values():
                if t["ticket_num"] == num:
                    target_ticket = t
                    break

    if target_ticket is None:
        target_ticket = _ticket_mas_reciente()

    if target_ticket is None:
        log.info("Respuesta WhatsApp recibida pero no hay tickets abiertos: %r", body)
        return

    canal_id = target_ticket["canal_id"]

    async def _post():
        canal = client.get_channel(canal_id)
        if canal is None:
            log.warning("Canal ticket %s no encontrado para respuesta WhatsApp", canal_id)
            return
        await canal.send(f"🥷 **Marke Reseller:** {body}")
        log.info("Respuesta WhatsApp posteada en ticket #%04d", target_ticket["ticket_num"])

    asyncio.run_coroutine_threadsafe(_post(), client.loop)


async def _abrir_ticket(guild: discord.Guild, member: discord.Member, motivo: str) -> discord.TextChannel:
    """Lógica compartida para crear un canal de ticket."""
    global _ticket_counter
    with _tickets_lock:
        _ticket_counter += 1
        num = _ticket_counter

    category = discord.utils.find(
        lambda c: c.name.upper() in ("TICKETS", "SOPORTE", "SUPPORT", "TICKET"),
        guild.categories,
    )

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
    }
    if ADMIN_ROLE_ID:
        try:
            admin_role = guild.get_role(int(ADMIN_ROLE_ID))
            if admin_role:
                overwrites[admin_role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True
                )
        except ValueError:
            pass

    nombre_canal = f"ticket-{num:04d}-{member.name[:12].lower().replace(' ', '-')}"
    canal_ticket = await guild.create_text_channel(
        nombre_canal,
        category=category,
        overwrites=overwrites,
        topic=f"Ticket #{num:04d} | {member} | {motivo[:100]}",
        reason=f"Ticket #{num:04d} abierto por {member}",
    )

    now = _ahora_arg()
    with _tickets_lock:
        _active_tickets[canal_ticket.id] = {
            "ticket_num": num,
            "user": member,
            "user_id": member.id,
            "user_name": str(member),
            "motivo": motivo,
            "canal_id": canal_ticket.id,
            "created_at": now,
        }
    try:
        database.save_ticket(
            channel_id=canal_ticket.id,
            ticket_num=num,
            user_id=member.id,
            user_name=str(member),
            motivo=motivo,
            created_at=now.isoformat(),
        )
    except Exception:
        log.exception("No pude persistir ticket #%04d en DB", num)

    embed = discord.Embed(
        title=f"🎫 Ticket #{num:04d}",
        description=(
            f"Hola {member.mention}, tu ticket fue abierto.\n\n"
            f"**Motivo:** {motivo}\n\n"
            "Un administrador te responderá a la brevedad.\n"
            "Usá el botón **Cerrar ticket** cuando se resuelva tu consulta."
        ),
        color=0x3498DB,
    )
    embed.set_footer(text=f"Marke Panel • Ticket #{num:04d}")

    view_cerrar = CerrarTicketView()
    await canal_ticket.send(member.mention, embed=embed, view=view_cerrar)

    await asyncio.to_thread(
        twilio_helper.send_whatsapp,
        f"🎫 Ticket #{num:04d} abierto\n"
        f"Usuario: {member} ({member.id})\n"
        f"Motivo: {motivo}\n"
        f"Canal: #{nombre_canal}\n\n"
        f"Respondé acá y tu mensaje irá directo al ticket.\n"
        f"(Para elegir uno específico escribí #NNNN al inicio)"
    )

    log.info("Ticket #%04d abierto por %s en canal %s", num, member, canal_ticket.id)
    return canal_ticket


class TicketModal(discord.ui.Modal, title="Abrir ticket de soporte"):
    motivo = discord.ui.TextInput(
        label="¿Cuál es tu consulta o problema?",
        placeholder="Describí brevemente tu situación...",
        style=discord.TextStyle.paragraph,
        max_length=300,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await _safe_defer(interaction, ephemeral=True, thinking=True)

        # Un solo ticket por usuario
        with _tickets_lock:
            ticket_existente = next(
                (t for t in _active_tickets.values() if t["user_id"] == interaction.user.id),
                None,
            )
        if ticket_existente is not None:
            canal_existente = interaction.guild.get_channel(ticket_existente["canal_id"])
            mencion = canal_existente.mention if canal_existente else f"ticket-{ticket_existente['ticket_num']:04d}"
            await interaction.followup.send(
                f"Ya tenés un ticket abierto: {mencion}\n"
                "Cerralo antes de abrir uno nuevo.",
                ephemeral=True,
            )
            return

        canal_ticket = await _abrir_ticket(
            guild=interaction.guild,
            member=interaction.user,
            motivo=self.motivo.value,
        )
        await interaction.followup.send(
            f"✅ Tu ticket fue abierto en {canal_ticket.mention}. Un admin te responderá pronto.",
            ephemeral=True,
        )


class TicketPanelView(_SafeViewMixin, discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="🎫  Abrir ticket",
        style=discord.ButtonStyle.primary,
        custom_id="ticket_panel_abrir",
    )
    async def abrir_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.send_modal(TicketModal())
        except discord.InteractionResponded:
            pass
        except Exception as e:
            log.warning("No pude abrir modal de ticket para %s: %s", interaction.user, e)


class FlouriteCanalView(_SafeViewMixin, discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="🎫  Quiero Flourite",
        style=discord.ButtonStyle.success,
        custom_id="flourite_abrir_ticket",
    )
    async def abrir_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.send_modal(TicketModal())
        except discord.InteractionResponded:
            pass
        except Exception as e:
            log.warning("No pude abrir modal flourite para %s: %s", interaction.user, e)


class DiamantesCanalView(_SafeViewMixin, discord.ui.View):
    """Vista persistente del canal #diamantes con 6 botones de paquetes."""

    def __init__(self):
        super().__init__(timeout=None)

    async def _handle_pack(
        self,
        interaction: discord.Interaction,
        diamonds: int,
        precio: str,
    ) -> None:
        ch_name = getattr(interaction.channel, "name", "")
        if "diamante" not in ch_name.lower():
            await interaction.response.send_message(
                "❌ Este botón solo funciona en el canal **#diamantes**.",
                ephemeral=True,
            )
            return
        embed = discord.Embed(
            title=f"💎 {diamonds:,} Diamantes — {precio}",
            description=(
                f"Seleccionaste el paquete de **{diamonds:,} diamantes**.\n\n"
                "**¿Cómo querés pagar?**\n\n"
                "🏦 **Transferencia Bancaria** — Te mostramos el CBU/Alias y un admin "
                "procesa tu pedido tras verificar el comprobante.\n\n"
                "🔶 **Binance Pay** — Proceso 100% automático. Recibís los diamantes "
                "al instante sin intervención humana."
            ),
            color=0x1ABC9C,
        )
        embed.set_footer(text="Marke Panel • @markee.4 — Diamantes Argentina")
        await interaction.response.send_message(
            embed=embed,
            view=DiamantesMetodoPagoView(diamonds=diamonds),
            ephemeral=True,
        )

    @discord.ui.button(label="💎 110 Diams — $1.450", style=discord.ButtonStyle.primary, custom_id="diam_pack_110", row=0)
    async def pack_110(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_pack(interaction, 110, "$1.450 ARS")

    @discord.ui.button(label="💎 341 Diams — $4.250", style=discord.ButtonStyle.primary, custom_id="diam_pack_341", row=0)
    async def pack_341(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_pack(interaction, 341, "$4.250 ARS")

    @discord.ui.button(label="💎 572 Diams — $7.100", style=discord.ButtonStyle.primary, custom_id="diam_pack_572", row=0)
    async def pack_572(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_pack(interaction, 572, "$7.100 ARS")

    @discord.ui.button(label="💎 1.166 Diams — $14.000", style=discord.ButtonStyle.success, custom_id="diam_pack_1166", row=1)
    async def pack_1166(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_pack(interaction, 1166, "$14.000 ARS")

    @discord.ui.button(label="💎 2.398 Diams — $25.700", style=discord.ButtonStyle.success, custom_id="diam_pack_2398", row=1)
    async def pack_2398(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_pack(interaction, 2398, "$25.700 ARS")

    @discord.ui.button(label="💎 6.160 Diams — $64.300", style=discord.ButtonStyle.danger, custom_id="diam_pack_6160", row=1)
    async def pack_6160(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_pack(interaction, 6160, "$64.300 ARS")


class DiamantesMetodoPagoView(discord.ui.View):
    """Vista efímera para elegir método de pago de diamantes."""

    def __init__(self, diamonds: int):
        super().__init__(timeout=120)
        self.diamonds = diamonds

    @discord.ui.button(label="🏦 Transferencia Bancaria", style=discord.ButtonStyle.secondary, custom_id="diam_metodo_transferencia")
    async def transferencia(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(DiamantesFFIDModal(diamonds=self.diamonds, metodo="transferencia"))

    @discord.ui.button(label="🔶 Binance Pay (automático)", style=discord.ButtonStyle.primary, custom_id="diam_metodo_binance")
    async def binance(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(DiamantesFFIDModal(diamonds=self.diamonds, metodo="binance"))


class DiamantesFFIDModal(discord.ui.Modal):
    """Modal que solicita el ID de Free Fire para completar la compra de diamantes."""

    ff_id: discord.ui.TextInput = discord.ui.TextInput(
        label="Tu ID de Free Fire",
        placeholder="Ej: 1234567890",
        min_length=5,
        max_length=20,
        required=True,
    )

    def __init__(self, diamonds: int, metodo: str):
        super().__init__(title=f"💎 Comprar {diamonds:,} Diamantes")
        self.diamonds = diamonds
        self.metodo = metodo

    async def on_submit(self, interaction: discord.Interaction) -> None:
        id_freefire = self.ff_id.value.strip()
        if self.metodo == "transferencia":
            await _handle_diamantes_transferencia(interaction, self.diamonds, id_freefire)
        else:
            await _handle_diamantes_binance(interaction, self.diamonds, id_freefire)


_DIAM_PRECIOS: dict[int, str] = {
    110:  "$1.450",
    341:  "$4.250",
    572:  "$7.100",
    1166: "$14.000",
    2398: "$25.700",
    6160: "$64.300",
}


async def _handle_diamantes_transferencia(
    interaction: discord.Interaction,
    diamonds: int,
    id_freefire: str,
) -> None:
    """Muestra datos de CBU/Alias, envía DM al usuario y notifica al admin en #ventas."""
    await _safe_defer(interaction, ephemeral=True, thinking=False)

    precio = _DIAM_PRECIOS.get(diamonds, "")

    # ── Respuesta efímera en el canal ────────────────────────────────────────
    embed_canal = discord.Embed(
        title="📨 Revisá tus mensajes privados",
        description=(
            f"Te mandé los datos de pago por **DM** 💬\n\n"
            f"**Paquete:** {diamonds:,} 💎 — **{precio} ARS**\n"
            f"**ID Free Fire:** `{id_freefire}`\n\n"
            "Una vez que hagas la transferencia, enviá el comprobante "
            "directamente en el chat privado con el bot. ✅"
        ),
        color=0xF39C12,
    )
    embed_canal.set_footer(text="Marke Panel • @markee.4")
    await interaction.followup.send(embed=embed_canal, ephemeral=True)

    # ── DM al usuario con todos los datos ────────────────────────────────────
    embed_dm = discord.Embed(
        title="🏦 Datos para tu Transferencia Bancaria",
        description=(
            f"**Paquete:** {diamonds:,} 💎 Diamantes\n"
            f"**ID Free Fire:** `{id_freefire}`\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 **Monto a transferir:** `{precio} ARS`\n"
            f"👤 **Titular:** `Agustín Marquesini`\n"
            f"🏦 **CBU:** `{NARANJA_X_CBU}`\n"
            f"📌 **Alias:** `{NARANJA_X_ALIAS}`\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "✅ Una vez realizada la transferencia, enviá el **comprobante como imagen "
            "aquí mismo** en este chat privado.\n"
            "Un administrador verificará el pago y te acreditará los diamantes a la brevedad."
        ),
        color=0xF39C12,
    )
    embed_dm.set_footer(text="Marke Panel • @markee.4 — Guardá el comprobante")

    dm_enviado = False
    try:
        await interaction.user.send(embed=embed_dm)
        dm_enviado = True
        _pending_diam_transferencia[interaction.user.id] = {
            "diamonds":   diamonds,
            "id_freefire": id_freefire,
            "precio":     precio,
            "username":   str(interaction.user),
        }
        log.info(
            "DM de transferencia diamantes enviado a %s — %d 💎",
            interaction.user, diamonds,
        )
    except discord.Forbidden:
        log.warning("No pude enviar DM a %s (DMs cerrados)", interaction.user)
        # Si no puede recibir DMs, mostrar los datos directamente (efímero)
        embed_fallback = discord.Embed(
            title="🏦 Datos para tu Transferencia Bancaria",
            description=(
                f"⚠️ No pude mandarte un DM. Aquí están los datos:\n\n"
                f"💰 **Monto:** `{precio} ARS`\n"
                f"👤 **Titular:** `Agustín Marquesini`\n"
                f"🏦 **CBU:** `{NARANJA_X_CBU}`\n"
                f"📌 **Alias:** `{NARANJA_X_ALIAS}`\n\n"
                "Una vez transferido, abrí los ajustes de privacidad de Discord, "
                "activá los DMs de este servidor y escribile al bot para enviar el comprobante."
            ),
            color=0xE67E22,
        )
        await interaction.followup.send(embed=embed_fallback, ephemeral=True)
    except Exception:
        log.exception("Error enviando DM de transferencia a %s", interaction.user)

    # ── Notificar admin en #ventas ────────────────────────────────────────────
    try:
        canal_ventas = await _obtener_canal_ventas()
        if canal_ventas:
            embed_admin = discord.Embed(
                title="🏦 Pedido — Transferencia Bancaria — Diamantes",
                description=(
                    f"👤 **Usuario:** {interaction.user.mention} (`{interaction.user}`)\n"
                    f"💎 **Paquete:** {diamonds:,} Diamantes\n"
                    f"💰 **Monto:** {precio} ARS\n"
                    f"🎮 **ID Free Fire:** `{id_freefire}`\n\n"
                    f"{'📨 DM enviado al usuario.' if dm_enviado else '⚠️ No se pudo enviar DM (DMs cerrados).'}\n"
                    "Esperando comprobante por DM. Cuando llegue se reenviará aquí automáticamente."
                ),
                color=0xF39C12,
            )
            await canal_ventas.send(embed=embed_admin)
    except Exception:
        log.exception("Error notificando transferencia diamantes en #ventas")


async def _handle_diamantes_binance(
    interaction: discord.Interaction,
    diamonds: int,
    id_freefire: str,
) -> None:
    """Inicia el proceso automático Binance Pay en background."""
    await _safe_defer(interaction, ephemeral=True, thinking=False)
    await interaction.followup.send(
        "⏳ **Proceso iniciado.**\n"
        "Estoy abriendo la tienda y configurando tu pago Binance Pay...\n"
        "Te avisaré por **DM** cuando los diamantes estén acreditados. 🔶",
        ephemeral=True,
    )
    asyncio.create_task(_tarea_diamantes_binance(interaction.user, diamonds, id_freefire))


def _parsear_pendiente_manual(resultado: str) -> dict | None:
    """Extrae pin/id_ff/diamonds de un mensaje PENDIENTE_MANUAL del scraper.
    Devuelve None si el resultado no es un caso de canje manual pendiente."""
    if latingm_scraper._PENDIENTE_MANUAL_TAG not in resultado:
        return None
    pin_m  = re.search(r"PIN:([^\n]+)", resultado)
    id_m   = re.search(r"ID:([^\n]+)", resultado)
    diam_m = re.search(r"DIAM:(\d+)", resultado)
    return {
        "pin":      pin_m.group(1).strip()  if pin_m  else "N/A",
        "id_ff":    id_m.group(1).strip()   if id_m   else "N/A",
        "diamonds": int(diam_m.group(1))    if diam_m else 0,
    }


async def _dm_alerta_manual_admin(
    pin: str,
    id_freefire: str,
    diamonds: int,
    comprador: str = "",
) -> None:
    """
    DM limpio y accionable a todos los admins cuando el reCAPTCHA bloqueó
    el canje automático. El comprador NO ve ningún mensaje de error.
    """
    desc = (
        f"**ID Free Fire:** `{id_freefire}`\n"
        f"**PIN a canjear:**\n```\n{pin}\n```\n"
        f"**Link directo:** https://redeempins.com/\n"
        f"**Diamantes:** {diamonds:,} 💎"
    )
    if comprador:
        desc += f"\n**Comprador:** {comprador}"

    embed = discord.Embed(
        title="⚠️ Canje manual pendiente",
        description=desc,
        color=0xFFA500,
    )
    embed.set_footer(text="Marke Panel — el comprador espera la entrega")

    # DM a todos los admins con ese rol
    guild = _resolver_guild()
    if guild and ADMIN_ROLE_ID:
        try:
            admin_role = guild.get_role(int(ADMIN_ROLE_ID))
            if admin_role:
                for member in admin_role.members:
                    try:
                        await member.send(embed=embed)
                        log.info("_dm_alerta_manual_admin: DM enviado a %s", member)
                    except Exception:
                        pass
        except Exception:
            log.exception("_dm_alerta_manual_admin: error enviando DMs")


async def _tarea_diamantes_binance(
    user: discord.User | discord.Member,
    diamonds: int,
    id_freefire: str,
) -> None:
    """Background task: ejecuta el scraping completo latingm → redeempins."""

    async def notificar_pago(pay_url: str, msg: str) -> None:
        try:
            canal_ventas = await _obtener_canal_ventas()
            if canal_ventas:
                embed_binance = discord.Embed(
                    title="🔶 Pago Binance Pay — Acción requerida",
                    description=(
                        f"👤 **Usuario:** {user.mention} (`{user}`)\n"
                        f"💎 **Paquete:** {diamonds:,} Diamantes\n"
                        f"🎮 **ID Free Fire:** `{id_freefire}`\n\n"
                        f"{msg}\n\n"
                        f"🔗 **URL de pago:** {pay_url}\n\n"
                        "⚠️ Confirmá el pago en tu cuenta de Binance para completar el proceso."
                    ),
                    color=0xF0B90B,
                )
                embed_binance.set_footer(text="El bot completará el canje automáticamente tras la confirmación.")
                await canal_ventas.send(embed=embed_binance)
        except Exception:
            log.exception("Error notificando pago Binance en #ventas")

    try:
        def _guardar_pin_cb(p: str, oid: str) -> None:
            try:
                database.save_diamond_pin(diamonds, p, oid)
            except Exception as _exc:
                log.warning("_guardar_pin_cb: %s", _exc)

        screenshot_bytes, resultado = await latingm_scraper.comprar_diamantes(
            diamonds=diamonds,
            id_freefire=id_freefire,
            notificar_pago=notificar_pago,
            guardar_pin=_guardar_pin_cb,
        )

        # Fallback manual si el reCAPTCHA bloqueó el canje
        pm = _parsear_pendiente_manual(resultado)
        if pm:
            asyncio.create_task(_dm_alerta_manual_admin(
                pin=pm["pin"], id_freefire=pm["id_ff"],
                diamonds=pm["diamonds"], comprador=user.mention,
            ))

        exito = resultado.startswith("✅")

        # ── Mensaje al COMPRADOR: genérico, sin capturas ni datos internos ───
        if exito:
            msg_comprador = (
                f"✅ ¡Listo! Recibiste **{diamonds:,} 💎** en tu cuenta de Free Fire.\n"
                "Revisá tu cuenta — puede demorar unos minutos en aparecer."
            )
        else:
            msg_comprador = (
                f"💎 Tu recarga de **{diamonds:,} diamantes** está siendo procesada.\n"
                "En breve te llegará. Si tenés dudas escribí a un admin."
            )
        embed_comprador = discord.Embed(
            description=msg_comprador,
            color=0x2ECC71 if exito else 0xF1C40F,
        )
        embed_comprador.set_footer(text="Marke Panel • @markee.4")
        try:
            await user.send(embed=embed_comprador)
        except Exception:
            pass

        # ── Reporte completo en #ventas (solo admins lo ven) ─────────────────
        try:
            canal_ventas = await _obtener_canal_ventas()
            if canal_ventas:
                status = "✅ Completado" if exito else ("⏳ PENDIENTE MANUAL" if pm else "❌ Error")
                resumen = resultado[:400]
                msg_ventas = (
                    f"**{status}** — {diamonds:,}💎 · {user.mention} · ID FF: `{id_freefire}`\n"
                    f"```\n{resumen}\n```"
                )
                if screenshot_bytes:
                    await canal_ventas.send(
                        content=msg_ventas,
                        file=discord.File(io.BytesIO(screenshot_bytes), filename="resultado.png"),
                    )
                else:
                    await canal_ventas.send(msg_ventas)
        except Exception:
            pass

    except Exception as exc:
        log.exception("Error en _tarea_diamantes_binance para %s", user)
        try:
            embed_err = discord.Embed(
                description=(
                    f"💎 Tu recarga de **{diamonds:,} diamantes** está siendo procesada.\n"
                    "En breve te llegará. Si tenés dudas escribí a un admin."
                ),
                color=0xF1C40F,
            )
            embed_err.set_footer(text="Marke Panel • @markee.4")
            await user.send(embed=embed_err)
        except Exception:
            pass
        try:
            canal_ventas = await _obtener_canal_ventas()
            if canal_ventas:
                await canal_ventas.send(
                    f"❌ **Error interno** — {diamonds:,}💎 · {user.mention} · ID FF: `{id_freefire}`\n"
                    f"```\n{str(exc)[:400]}\n```"
                )
        except Exception:
            pass


class DiamantesAceptarPagoView(discord.ui.View):
    """Vista PERSISTENTE en #ventas: botón para que el admin acepte y lance el pago Binance.

    custom_ids fijos ('diam_accept' / 'diam_reject') → sobrevive reinicios del bot.
    Los datos (user_id, diamonds, id_freefire) se recuperan del embed del mensaje.
    """

    def __init__(self, user_id: int = 0, diamonds: int = 0, id_freefire: str = "", user_mention: str = ""):
        super().__init__(timeout=None)  # persistente
        self._user_id     = user_id
        self._diamonds    = diamonds
        self._id_freefire = id_freefire
        self._mention     = user_mention

    # ── Recuperación de datos desde el embed tras reinicio ─────────────────
    def _parse_embed(self, interaction: discord.Interaction) -> tuple[int, int, str, str]:
        """Extrae (user_id, diamonds, id_freefire, mention) del embed del mensaje."""
        user_id     = self._user_id
        diamonds    = self._diamonds
        id_freefire = self._id_freefire
        mention     = self._mention

        msg = interaction.message
        if not msg or (user_id and diamonds and id_freefire):
            return user_id, diamonds, id_freefire, mention

        import re as _re
        desc = ""
        for emb in (msg.embeds or []):
            desc += (emb.description or "") + "\n"
            for f in (emb.fields or []):
                desc += (f.value or "") + "\n"

        if not user_id:
            m = _re.search(r"<@(\d+)>", desc)
            if m:
                user_id = int(m.group(1))
                mention = f"<@{user_id}>"

        if not diamonds:
            m = _re.search(r"💎\s*\*\*Paquete:\*\*\s*([\d,.]+)\s*Diamantes", desc)
            if m:
                diamonds = int(m.group(1).replace(",", "").replace(".", ""))

        if not id_freefire:
            m = _re.search(r"🎮\s*\*\*ID Free Fire:\*\*\s*`([^`]+)`", desc)
            if m:
                id_freefire = m.group(1)

        return user_id, diamonds, id_freefire, mention

    # ── Botón Aceptar ──────────────────────────────────────────────────────
    @discord.ui.button(
        label="💎 ✅ Aceptar — Pagar en Binance",
        style=discord.ButtonStyle.success,
        custom_id="diam_accept",
    )
    async def btn_aceptar(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not _puede_registrar(interaction):
            try:
                await interaction.response.send_message("❌ Solo administradores.", ephemeral=True)
            except Exception:
                pass
            return

        user_id, diamonds, id_freefire, mention = self._parse_embed(interaction)

        # Deshabilitar botones para evitar doble-click
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)

        if not user_id or not diamonds or not id_freefire:
            await interaction.followup.send(
                "❌ No pude recuperar los datos del comprobante. Procesá el pago manualmente.",
                ephemeral=True,
            )
            return

        # Avisar al comprador
        buyer: discord.User | None = client.get_user(user_id)
        if buyer is None:
            try:
                buyer = await client.fetch_user(user_id)
            except Exception:
                buyer = None

        if buyer:
            try:
                await buyer.send(
                    f"✅ **Tu comprobante fue aprobado.**\n"
                    f"Iniciando el proceso automático para tus **{diamonds:,} 💎**. "
                    "Te avisaré por acá cuando estén listos. 🔶"
                )
            except Exception:
                pass

        await interaction.followup.send(
            f"⏳ Proceso Binance iniciado para **{diamonds:,} 💎** de {mention}.\n"
            "El resultado llegará al comprador por DM y se notificará aquí.",
            ephemeral=True,
        )

        if buyer:
            asyncio.create_task(
                _tarea_diamantes_binance(buyer, diamonds, id_freefire)
            )
        else:
            await interaction.followup.send(
                f"⚠️ No pude encontrar al usuario (id=`{user_id}`). "
                "Procesá el pago manualmente.",
                ephemeral=True,
            )

    # ── Botón Rechazar ─────────────────────────────────────────────────────
    @discord.ui.button(
        label="❌ Rechazar",
        style=discord.ButtonStyle.danger,
        custom_id="diam_reject",
    )
    async def btn_rechazar(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not _puede_registrar(interaction):
            try:
                await interaction.response.send_message("❌ Solo administradores.", ephemeral=True)
            except Exception:
                pass
            return

        user_id, diamonds, id_freefire, mention = self._parse_embed(interaction)

        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)

        buyer: discord.User | None = client.get_user(user_id) if user_id else None
        if buyer is None and user_id:
            try:
                buyer = await client.fetch_user(user_id)
            except Exception:
                buyer = None

        if buyer:
            try:
                await buyer.send(
                    "❌ **Tu comprobante no fue aprobado.**\n"
                    "Si creés que es un error, comunicate con un administrador del servidor."
                )
            except Exception:
                pass

        await interaction.followup.send(
            f"Comprobante de **{diamonds:,} 💎** de {mention} rechazado.",
            ephemeral=True,
        )


async def _ejecutar_cierre_ticket(canal: discord.TextChannel, user: discord.User | discord.Member, ticket_info: dict) -> None:
    """Lógica real de cierre: borra de DB, envía embed, elimina el canal."""
    num = ticket_info["ticket_num"]

    with _tickets_lock:
        _active_tickets.pop(canal.id, None)
    try:
        database.delete_ticket(canal.id)
    except Exception:
        log.exception("No pude borrar ticket #%04d de DB", num)

    embed = discord.Embed(
        title=f"🔒 Ticket #{num:04d} cerrado",
        description=f"Cerrado por {user.mention}. El canal se eliminará en 5 segundos.",
        color=0xE74C3C,
    )
    await canal.send(embed=embed)

    await asyncio.to_thread(
        twilio_helper.send_whatsapp,
        f"🔒 Ticket #{num:04d} cerrado por {user}"
    )
    log.info("Ticket #%04d cerrado por %s", num, user)

    await asyncio.sleep(5)
    try:
        await canal.delete(reason=f"Ticket #{num:04d} cerrado")
    except Exception:
        log.exception("No pude eliminar el canal del ticket #%04d", num)


class ConfirmarCierreView(_SafeViewMixin, discord.ui.View):
    """Vista efímera de confirmación antes de cerrar un ticket."""

    def __init__(self, ticket_info: dict):
        super().__init__(timeout=60)
        self._ticket_info = ticket_info

    @discord.ui.button(label="✅  Sí, cerrar", style=discord.ButtonStyle.danger)
    async def confirmar(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _safe_defer(interaction, ephemeral=True)
        self.stop()
        await interaction.followup.send("Cerrando ticket…", ephemeral=True)
        await _ejecutar_cierre_ticket(interaction.channel, interaction.user, self._ticket_info)

    @discord.ui.button(label="❌  Cancelar", style=discord.ButtonStyle.secondary)
    async def cancelar(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Cierre cancelado.", ephemeral=True)
        self.stop()


class CerrarTicketView(_SafeViewMixin, discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="🔒  Cerrar ticket",
        style=discord.ButtonStyle.danger,
        custom_id="ticket_cerrar_btn",
    )
    async def cerrar(self, interaction: discord.Interaction, button: discord.ui.Button):
        canal = interaction.channel
        with _tickets_lock:
            ticket_info = _active_tickets.get(canal.id)

        if ticket_info is None:
            await interaction.response.send_message(
                "Este canal no es un ticket activo.", ephemeral=True
            )
            return

        es_creador = interaction.user.id == ticket_info["user_id"]
        es_admin = _puede_registrar(interaction)
        if not (es_creador or es_admin):
            await interaction.response.send_message(
                "Solo el creador del ticket o un admin puede cerrarlo.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            "⚠️ **¿Seguro que querés cerrar este ticket?** Esta acción eliminará el canal permanentemente.",
            view=ConfirmarCierreView(ticket_info),
            ephemeral=True,
        )


class ForzarCierreView(_SafeViewMixin, discord.ui.View):
    """Vista de confirmación para forzar el cierre de un canal que no está en el registro."""

    def __init__(self, canal: discord.TextChannel):
        super().__init__(timeout=60)
        self._canal = canal

    @discord.ui.button(label="✅  Sí, eliminar canal", style=discord.ButtonStyle.danger)
    async def confirmar(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _safe_defer(interaction, ephemeral=True)
        self.stop()
        embed = discord.Embed(
            title="🔒 Canal cerrado por administrador",
            description=f"Cerrado forzosamente por {interaction.user.mention}. El canal se eliminará en 5 segundos.",
            color=0xE74C3C,
        )
        await self._canal.send(embed=embed)
        await asyncio.sleep(5)
        try:
            await self._canal.delete(reason=f"Cierre forzado por {interaction.user}")
        except Exception:
            log.exception("No pude eliminar el canal %s en cierre forzado", self._canal.id)

    @discord.ui.button(label="❌  Cancelar", style=discord.ButtonStyle.secondary)
    async def cancelar(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Cierre cancelado.", ephemeral=True)
        self.stop()


@tree.command(name="cerrar-ticket", description="Cerrar el ticket de soporte de este canal")
async def cerrar_ticket_cmd(interaction: discord.Interaction):
    canal = interaction.channel

    with _tickets_lock:
        ticket_info = _active_tickets.get(canal.id)

    es_admin = _puede_registrar(interaction)

    if ticket_info is None:
        # Canal no está en el registro activo — permitir cierre forzado a admins
        if es_admin:
            await interaction.response.send_message(
                "⚠️ Este canal **no figura en el registro de tickets activos** "
                "(puede ser un ticket viejo o un canal de postulación).\n\n"
                "¿Querés eliminarlo de todas formas?",
                view=ForzarCierreView(canal),
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "Este canal no es un ticket activo.", ephemeral=True
            )
        return

    es_creador = interaction.user.id == ticket_info["user_id"]

    if not (es_creador or es_admin):
        await interaction.response.send_message(
            "Solo el creador del ticket o un administrador puede cerrarlo.", ephemeral=True
        )
        return

    await interaction.response.send_message(
        "⚠️ **¿Seguro que querés cerrar este ticket?** Esta acción eliminará el canal permanentemente.",
        view=ConfirmarCierreView(ticket_info),
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# Sistema de postulaciones — Influencer / Creadores de contenido iOS
# ---------------------------------------------------------------------------

async def _abrir_ticket_postulacion(guild: discord.Guild, member: discord.Member) -> discord.TextChannel:
    """Crea un canal privado de postulación para el usuario."""
    global _ticket_counter
    with _tickets_lock:
        _ticket_counter += 1
        num = _ticket_counter

    category = discord.utils.find(
        lambda c: c.name.upper() in ("TICKETS", "SOPORTE", "SUPPORT", "TICKET", "POSTULACIONES"),
        guild.categories,
    )

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
    }
    if ADMIN_ROLE_ID:
        try:
            admin_role = guild.get_role(int(ADMIN_ROLE_ID))
            if admin_role:
                overwrites[admin_role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True
                )
        except ValueError:
            pass

    nombre_canal = f"postulacion-{member.name[:15].lower().replace(' ', '-')}"
    canal = await guild.create_text_channel(
        nombre_canal,
        category=category,
        overwrites=overwrites,
        topic=f"Postulación de influencer | {member} | #{num:04d}",
        reason=f"Postulación de influencer de {member}",
    )

    now = _ahora_arg()
    with _tickets_lock:
        _active_tickets[canal.id] = {
            "ticket_num": num,
            "user": member,
            "user_id": member.id,
            "user_name": str(member),
            "motivo": "Postulación de influencer",
            "canal_id": canal.id,
            "created_at": now,
        }
    try:
        database.save_ticket(
            channel_id=canal.id,
            ticket_num=num,
            user_id=member.id,
            user_name=str(member),
            motivo="Postulación de influencer",
            created_at=now.isoformat(),
        )
    except Exception:
        log.exception("No pude persistir postulación #%04d en DB", num)

    embed = discord.Embed(
        title="🎙️ ¡Gracias por postularte como Influencer!",
        description=(
            f"¡Hola {member.mention}! 👋\n\n"
            "Recibimos tu postulación para ser **Creador de Contenido de Sensi Marke**.\n\n"
            "📋 **Próximos pasos:**\n"
            "1. Enviá el **link de tu perfil de TikTok** en este canal.\n"
            "2. Un administrador te contactará para coordinar una **entrevista previa por llamada**.\n\n"
            "⏳ Respondemos a la brevedad. ¡Mucha suerte! 🖤"
        ),
        color=0x2ECC71,
    )
    embed.set_footer(text="Sensi Marke • Reclutamiento de Influencers")

    view_cerrar = CerrarTicketView()
    await canal.send(member.mention, embed=embed, view=view_cerrar)
    log.info("Postulación #%04d abierta por %s en canal %s", num, member, canal.id)
    return canal


class PostulacionView(_SafeViewMixin, discord.ui.View):
    """Vista persistente del embed de postulaciones."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="📩  Postularte",
        style=discord.ButtonStyle.success,
        custom_id="postulacion_btn",
    )
    async def postularse(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _safe_defer(interaction, ephemeral=True, thinking=True)
        guild = interaction.guild
        member = interaction.user

        # Verificar si ya tiene un ticket / postulación abierta
        with _tickets_lock:
            existente = next(
                (t for t in _active_tickets.values() if t["user_id"] == member.id),
                None,
            )
        if existente is not None:
            canal_existente = guild.get_channel(existente["canal_id"])
            mencion = canal_existente.mention if canal_existente else f"#{existente['ticket_num']:04d}"
            await interaction.followup.send(
                f"Ya tenés un ticket o postulación abierta: {mencion}\n"
                "Pasate por ahí para continuar.",
                ephemeral=True,
            )
            return

        try:
            canal_post = await _abrir_ticket_postulacion(guild, member)
            await interaction.followup.send(
                f"✅ Tu postulación fue creada en {canal_post.mention}. ¡Te esperamos ahí!",
                ephemeral=True,
            )
        except Exception as exc:
            log.exception("Error creando postulación para %s", member.id)
            await interaction.followup.send(
                f"❌ No se pudo crear tu canal de postulación. Contactá a un admin.\n```{exc}```",
                ephemeral=True,
            )


def _build_embed_postulaciones() -> discord.Embed:
    """Construye el embed de reclutamiento de influencers (reutilizable)."""
    embed = discord.Embed(
        title="🎮 Reclutamiento de Influencers — Sensi Marke",
        description=(
            "¿Sos creador de contenido de **Free Fire** en **iPhone (iOS)**?\n"
            "¡Esta es tu oportunidad de unirte al equipo de **Sensi Marke**! 🖤\n\n"

            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📋 **REQUISITOS**\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "• Ser mayor de **16 años** y tener madurez\n"
            "• Saber **stremear** y crear **lives dinámicos y divertidos**\n"
            "• Subir **clips** de Free Fire regularmente\n"
            "• Promocionar el **link del Discord** de Sensi Marke\n"
            "• Jugar en **iPhone (iOS)** — requisito excluyente\n\n"

            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "🎁 **BENEFICIOS**\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "• 🔑 **Key de proxy gratis** de 1 semana\n"
            "• 🛒 **Reventa** de todos los productos de la tienda\n"
            "• 💵 **Salario semanal de $30.000 ARS** por cumplir **5 lives de 3hs** cada uno\n\n"

            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📝 **PROCESO DE SELECCIÓN**\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "1️⃣ Hacé click en **Postularte** acá abajo\n"
            "2️⃣ Enviá el link de tu **perfil de TikTok**\n"
            "3️⃣ Coordinamos una **entrevista previa por llamada**\n\n"

            "*Solo se aceptan postulantes que cumplan todos los requisitos.*"
        ),
        color=0x1ABC9C,
    )
    embed.set_footer(text="Sensi Marke • Reclutamiento exclusivo para creadores iOS")
    return embed


async def _postear_panel_postulaciones() -> None:
    """Busca o crea el canal #postulaciones y postea el embed con el botón."""
    await client.wait_until_ready()

    guild = _resolver_guild()
    if guild is None:
        log.warning("_postear_panel_postulaciones: no encontré el guild")
        return

    # Buscar canal existente llamado postulaciones (o similar)
    canal = discord.utils.find(
        lambda c: isinstance(c, discord.TextChannel) and
                  any(n in c.name.lower() for n in ("postulacion", "postulaciones", "influencer", "reclutamiento")),
        guild.channels,
    )

    # Si no existe, crearlo
    if canal is None:
        category = await _obtener_o_crear_categoria(guild, _CAT_INFORMACION)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=True, send_messages=False, add_reactions=False
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_channels=True
            ),
        }
        if ADMIN_ROLE_ID:
            try:
                admin_role = guild.get_role(int(ADMIN_ROLE_ID))
                if admin_role:
                    overwrites[admin_role] = discord.PermissionOverwrite(
                        view_channel=True, send_messages=True
                    )
            except ValueError:
                pass
        canal = await guild.create_text_channel(
            "📩・postulaciones",
            category=category,
            overwrites=overwrites,
            topic="¿Querés ser influencer de Sensi Marke? Postulate acá tocando el botón.",
            reason="Canal de postulaciones creado automáticamente por Marke Panel",
        )
        log.info("Canal #postulaciones creado (id=%s)", canal.id)

    # Verificar si el embed ya está posteado
    async for msg in canal.history(limit=20):
        if msg.author == client.user and msg.embeds:
            for e in msg.embeds:
                if e.title and "Reclutamiento" in e.title:
                    log.info("Panel de postulaciones ya existe en #%s, no lo repito.", canal.name)
                    return

    embed = _build_embed_postulaciones()
    view = PostulacionView()
    await canal.send(embed=embed, view=view)
    log.info("Panel de postulaciones posteado en #%s", canal.name)


@tree.command(
    name="panel-postulaciones",
    description="(Admin) Republicar el embed de reclutamiento de influencers en este canal",
)
async def panel_postulaciones_cmd(interaction: discord.Interaction):
    await _safe_defer(interaction, ephemeral=True, thinking=True)
    if not _puede_registrar(interaction):
        await interaction.followup.send("❌ Solo administradores.", ephemeral=True)
        return

    embed = _build_embed_postulaciones()
    view = PostulacionView()
    await interaction.channel.send(embed=embed, view=view)
    await interaction.followup.send("✅ Panel de postulaciones publicado.", ephemeral=True)


# ---------------------------------------------------------------------------
# /sensibilidad — Genera configuración Free Fire OB53 para cualquier dispositivo
# ---------------------------------------------------------------------------
_SENSI_SYSTEM = (
    "Sos el sistema de IA de SENSI XITADAS de Marke Panel: experto profesional en "
    "sensibilidades de Free Fire OB53. Tu misión es dar sensibilidades REALES y ÚNICAS "
    "para cada celular — nunca valores genéricos ni calcados.\n\n"

    "════ VALORES ANCLA REALES (memorizalos, úsalos como base) ════\n"
    "Estos son valores ALTA comprobados para dispositivos reales. Usalos exactamente "
    "si el usuario pide ese modelo, o interpolá para modelos parecidos:\n"
    "ULTRA PREMIUM (120Hz+, SoC top, ≥8GB RAM):\n"
    "  Samsung S24 Ultra   → Gen 192 · ML 198 · 2x 174 · 4x 151 · 8x 122 · D 122\n"
    "  Samsung S23+        → Gen 188 · ML 195 · 2x 170 · 4x 148 · 8x 118 · D 118\n"
    "  iPhone 15 Pro Max   → Gen 190 · ML 197 · 2x 175 · 4x 153 · 8x 124 · D 124\n"
    "  iPhone 14 Pro       → Gen 186 · ML 193 · 2x 169 · 4x 146 · 8x 117 · D 117\n"
    "  Poco F5 Pro         → Gen 183 · ML 191 · 2x 166 · 4x 144 · 8x 115 · D 115\n"
    "  OnePlus 12          → Gen 185 · ML 193 · 2x 168 · 4x 146 · 8x 117 · D 117\n"
    "  Asus ROG Phone 7    → Gen 198 · ML 200 · 2x 182 · 4x 158 · 8x 128 · D 128\n"
    "  Xiaomi 14 Pro       → Gen 187 · ML 194 · 2x 171 · 4x 149 · 8x 119 · D 119\n"
    "GAMA ALTA (90-120Hz, SoC bueno, 6-8GB RAM):\n"
    "  Samsung S22 base    → Gen 172 · ML 180 · 2x 157 · 4x 133 · 8x 104 · D 104\n"
    "  iPhone 13 base      → Gen 162 · ML 170 · 2x 149 · 4x 126 · 8x 99 · D 99\n"
    "  iPhone 15 base      → Gen 167 · ML 175 · 2x 153 · 4x 129 · 8x 102 · D 102\n"
    "  Poco X5 Pro         → Gen 168 · ML 176 · 2x 153 · 4x 130 · 8x 102 · D 102\n"
    "  Poco F4             → Gen 174 · ML 182 · 2x 159 · 4x 136 · 8x 107 · D 107\n"
    "  Realme GT 2         → Gen 176 · ML 184 · 2x 160 · 4x 137 · 8x 108 · D 108\n"
    "  Moto G84            → Gen 155 · ML 164 · 2x 141 · 4x 118 · 8x 91 · D 91\n"
    "GAMA MEDIA (60-90Hz, SoC medio, 4-6GB RAM):\n"
    "  Samsung A55 5G      → Gen 148 · ML 157 · 2x 134 · 4x 112 · 8x 85 · D 85\n"
    "  Samsung A35 5G      → Gen 143 · ML 152 · 2x 129 · 4x 107 · 8x 81 · D 81\n"
    "  Redmi Note 13 Pro   → Gen 156 · ML 165 · 2x 142 · 4x 119 · 8x 92 · D 92\n"
    "  Redmi Note 12       → Gen 149 · ML 158 · 2x 135 · 4x 113 · 8x 86 · D 86\n"
    "  Redmi Note 11       → Gen 141 · ML 150 · 2x 127 · 4x 106 · 8x 80 · D 80\n"
    "  Moto G54            → Gen 138 · ML 147 · 2x 124 · 4x 103 · 8x 78 · D 78\n"
    "  Moto G34            → Gen 133 · ML 142 · 2x 119 · 4x 99 · 8x 75 · D 75\n"
    "  Samsung A53 5G      → Gen 145 · ML 154 · 2x 131 · 4x 109 · 8x 83 · D 83\n"
    "  Xiaomi Redmi 12     → Gen 136 · ML 145 · 2x 122 · 4x 101 · 8x 76 · D 76\n"
    "  iPhone 11           → Gen 158 · ML 166 · 2x 143 · 4x 121 · 8x 94 · D 94\n"
    "  iPhone XR           → Gen 151 · ML 159 · 2x 136 · 4x 115 · 8x 88 · D 88\n"
    "GAMA BAJA (60Hz, SoC básico, 3-4GB RAM):\n"
    "  Samsung A15         → Gen 112 · ML 122 · 2x 101 · 4x 83 · 8x 64 · D 64\n"
    "  Samsung A14         → Gen 108 · ML 118 · 2x 98 · 4x 80 · 8x 62 · D 62\n"
    "  Samsung A05s        → Gen 103 · ML 113 · 2x 94 · 4x 77 · 8x 59 · D 59\n"
    "  Redmi 10            → Gen 110 · ML 120 · 2x 100 · 4x 82 · 8x 63 · D 63\n"
    "  Redmi 9A/9C         → Gen 98 · ML 108 · 2x 90 · 4x 74 · 8x 57 · D 57\n"
    "  Moto E13/E14        → Gen 96 · ML 106 · 2x 88 · 4x 72 · 8x 56 · D 56\n"
    "  iPhone SE 2020      → Gen 144 · ML 152 · 2x 130 · 4x 109 · 8x 83 · D 83\n"
    "  iPhone 8            → Gen 138 · ML 146 · 2x 124 · 4x 104 · 8x 79 · D 79\n"
    "  Tecno Spark 20      → Gen 105 · ML 115 · 2x 96 · 4x 79 · 8x 61 · D 61\n"
    "  Infinix Hot 30      → Gen 109 · ML 119 · 2x 99 · 4x 81 · 8x 62 · D 62\n\n"

    "════ LEY DE VARIACIÓN OBLIGATORIA ════\n"
    "ESTO ES LO MÁS IMPORTANTE. La diferencia entre ALTA y BAJA DEBE ser real y notable:\n"
    "• General/Mira libre: ALTA − BAJA ≥ 45 puntos SIEMPRE. Sin excepciones.\n"
    "• Mira (4x): ALTA − BAJA ≥ 35 puntos.\n"
    "• Mira AWM (8x): ALTA − BAJA ≥ 28 puntos.\n"
    "• MEDIA siempre es el punto medio matemático entre ALTA y BAJA (±3 pts de variación "
    "natural para que no parezca calculado).\n"
    "• Dentro de cada perfil, cada slider DEBE bajar al aumentar el zoom:\n"
    "  General > Mira libre ≥ 2x > 4x > 8x ≈ AWM doble (±3pts).\n"
    "  EXCEPCIÓN aceptada: Mira libre puede superar a General en 3–8 pts (estilo pro).\n\n"

    "════ REGLA DE SINGULARIDAD POR DISPOSITIVO ════\n"
    "Cada celular tiene características únicas que DEBEN reflejarse en los valores:\n"
    "• Pantalla AMOLED grande (≥ 6.7\") + 120Hz: General puede llegar a 185–200.\n"
    "• Snapdragon 8 Gen 2 / Gen 3: aprovechar al máximo, valores premium totales.\n"
    "• Helio G99/G96 (Redmi Note 12/13 base): cap real ≈ 160 General ALTA.\n"
    "• Dimensity 1080 (Redmi Note 12 Pro): cap real ≈ 165 General ALTA.\n"
    "• Snapdragon 695 (A53 5G, Note 12 5G): cap real ≈ 150 General ALTA.\n"
    "• Snapdragon 680/662 (A53 4G, Note 11): cap real ≈ 145 General ALTA.\n"
    "• Helio G88/G85 (Redmi 10, Moto G31): cap real ≈ 118 General ALTA.\n"
    "• Helio G85/G70 (Samsung A14, A03s): cap real ≈ 112 General ALTA.\n"
    "• Pantalla LCD 60Hz de 6.0\" o menos: bajá 12pts extra en General ALTA.\n"
    "• iPhone Pro/Pro Max: diferencia entre modelos debe ser ≥ 8 pts en General.\n\n"

    "════ DPI (SOLO ANDROID) ════\n"
    "El campo 'DPI dispositivo' es el DPI del SO Android, NO un slider de Free Fire.\n"
    "• iOS/iPhone/iPad: OMITÍ POR COMPLETO la línea 'DPI dispositivo'. Empezá en "
    "'Botón disparo'.\n"
    "• Pantalla ≤ 6.0\" → nativo 320–390, recomendado +40: mostrar 360–430.\n"
    "• Pantalla 6.1\"–6.5\" → nativo 395–445, recomendado +45: mostrar 440–490.\n"
    "• Pantalla 6.6\"–6.8\" → nativo 440–510, recomendado +50: mostrar 490–560.\n"
    "• Tablet ≥ 7\" → nativo 280–340, recomendado +30: mostrar 310–370.\n"
    "Formato: 'DPI dispositivo : RRR  (nativo NNN)'. Usar valor recomendado (no nativo).\n\n"

    "════ BOTÓN DE DISPARO ════\n"
    "NUNCA superar 65% (70% solo en tablets). Valores 80/85/90% PROHIBIDOS.\n"
    "• ALTA → 62–65%  · MEDIA → 57–60%  · BAJA → 52–55%.\n"
    "• Celular ≤ 6.0\": restar 4% a cada valor (más espacio para ver la pantalla).\n"
    "• Posición SIEMPRE: media-baja.\n\n"

    "════ iOS/iPhone — REGLAS ESPECÍFICAS ════\n"
    "• iPhone 16 Pro/Max, 15 Pro/Max → A18/A17 · 120Hz ProMotion → ULTRA PREMIUM.\n"
    "• iPhone 14 Pro/Max → A16 · 120Hz ProMotion → ULTRA PREMIUM.\n"
    "• iPhone 13 Pro/Max → A15 · 120Hz ProMotion → PREMIUM (−8 pts respecto a 14 Pro).\n"
    "• iPhone 15/14/13 base, 12, 11 → 60Hz → GAMA ALTA (usar ancla de tabla).\n"
    "• iPhone XR, XS, X → 60Hz, más lentos → GAMA MEDIA-ALTA (−10 vs iPhone 11).\n"
    "• iPhone 8, SE → 60Hz, A11/A13 → GAMA MEDIA (usar anclas de tabla).\n"
    "• PROHIBIDO escribir 'DPI dispositivo' en cuadros iOS. Empezar en 'Botón disparo'.\n\n"

    "════ OPTIMIZACIÓN DEL SO (rutas exactas) ════\n"
    "• Samsung One UI: Ajustes → Funciones avanzadas → Game Booster → Rendimiento máximo. "
    "Ajustes → Pantalla → Suavidad de movimiento → Adaptable (o 120Hz).\n"
    "• Xiaomi / Redmi / Poco (HyperOS/MIUI): Ajustes → Apps especiales → Game Turbo → "
    "agregar FF → Modo Rendimiento + Mejora de toque. Ajustes → Pantalla → "
    "Frecuencia de actualización → máxima disponible.\n"
    "• Motorola: Ajustes → Apps → Moto Gametime → activar para FF. "
    "Ajustes → Pantalla → Frecuencia: Alta.\n"
    "• Realme / OPPO: Ajustes → Game Space → Pro Gamer + bloquear notificaciones. "
    "Ajustes → Pantalla → Frecuencia → 90Hz o 120Hz.\n"
    "• Vivo / iQOO: Ajustes → Ultra Game Mode → Modo Monster. "
    "Ajustes → Pantalla → Alta frecuencia.\n"
    "• Tecno / Infinix: Panel Juegos → Modo Rendimiento + No molestar.\n"
    "• Huawei / Honor: Ajustes → Asistente de juegos → Modo rendimiento.\n"
    "• Android genérico: Modo desarrollador → Velocidad animación 0.5x. "
    "Batería → Modo de alto rendimiento.\n"
    "• iPhone / iPad: Ajustes → Modo Concentración → Juegos. "
    "Ajustes → Batería → Modo bajo consumo: OFF. "
    "iPhone Pro: Ajustes → Pantalla → ProMotion ON. "
    "Cerrar todas las apps en background antes de jugar.\n\n"

    "════ FORMATO DE SALIDA ════\n"
    "Sin preámbulo. Sin saludos. Sin explicar nada. PROHIBIDO mencionar gráficos "
    "(Calidad, FPS, Sombras, Brillo, Anti-aliasing, Smooth, Ultra). Solo esto:\n\n"
    "**📱 Hardware detectado:** [modelo exacto] — [SoC real o 'gama X'] · [RAM]GB · "
    "[tamaño]\" · [Hz]Hz\n"
    "**🎯 Perfil recomendado:** [Alta/Media/Baja] _(1 línea de razón)_\n\n"
    "ANDROID — 3 perfiles con este cuadro EXACTO:\n"
    "**🟢 ALTA — agresivo / corto-medio**\n"
    "```\n"
    "DPI dispositivo : XXX  (nativo XXX)\n"
    "Botón disparo   : derecho · tamaño XX% · posición media-baja\n"
    "General         : XXX\n"
    "Mira libre      : XXX\n"
    "Mira roja (2x)  : XXX\n"
    "Mira (4x)       : XXX\n"
    "Mira AWM (8x)   : XXX\n"
    "Mira AWM doble  : XXX\n"
    "```\n"
    "**🟡 MEDIA — balanceado / todo terreno**\n"
    "```\n"
    "DPI dispositivo : XXX  (nativo XXX)\n"
    "Botón disparo   : derecho · tamaño XX% · posición media-baja\n"
    "General         : XXX\n"
    "Mira libre      : XXX\n"
    "Mira roja (2x)  : XXX\n"
    "Mira (4x)       : XXX\n"
    "Mira AWM (8x)   : XXX\n"
    "Mira AWM doble  : XXX\n"
    "```\n"
    "**🔴 BAJA — sniper / largo alcance / estable**\n"
    "```\n"
    "DPI dispositivo : XXX  (nativo XXX)\n"
    "Botón disparo   : derecho · tamaño XX% · posición media-baja\n"
    "General         : XXX\n"
    "Mira libre      : XXX\n"
    "Mira roja (2x)  : XXX\n"
    "Mira (4x)       : XXX\n"
    "Mira AWM (8x)   : XXX\n"
    "Mira AWM doble  : XXX\n"
    "```\n\n"
    "iOS — igual pero SIN línea 'DPI dispositivo' en NINGÚN cuadro. "
    "El primer renglón de cada cuadro es 'Botón disparo'.\n\n"
    "**⚙️ Optimización [marca del celular del usuario]**\n"
    "4–6 pasos con rutas exactas 'Ajustes → ... → ...' de la marca correcta.\n\n"
    "**💡 Tip pro:** [máx 18 palabras · si conocés el creador que popularizó esos "
    "valores para ese modelo, mencionalo]\n\n"

    "REGLAS ABSOLUTAS:\n"
    "R1. Diferencia ALTA−BAJA en General ≥ 45 puntos. Si no se cumple, ajustá.\n"
    "R2. Cada slider baja con el zoom: General ≥ Mira libre ≥ 2x > 4x > 8x ≈ AWMd.\n"
    "R3. Los valores DEBEN reflejar las características únicas del SoC/pantalla del "
    "modelo pedido. Si dos celulares diferentes dan exactamente los mismos números, "
    "algo está mal — revisá.\n"
    "R4. Si el modelo es desconocido, interpolá por nomenclatura y aclaralo con "
    "'_(estimado por nomenclatura)_' al final de Hardware detectado.\n"
    "R5. NUNCA mencionar gráficos del juego (Calidad/FPS/Sombras/Brillo/Anti-aliasing).\n"
    "R6. NUNCA línea 'DPI dispositivo' en cuadros iOS.\n"
    "R7. Botón disparo NUNCA > 65% (70% solo tablets). 80/85/90% = ERROR GRAVE.\n"
    "R8. Optimización SIEMPRE de la marca exacta del usuario, no genérica.\n"
    "R9. NO inventar nombres de SoC — si no estás seguro, poné 'gama [baja/media/alta]'."
)

_SENSI_COOLDOWN: dict[int, float] = {}
_SENSI_COOLDOWN_SEG = 20

# ---------------------------------------------------------------------------
# Motor de sensibilidad local — sin API, sin créditos, sin dependencias
# ---------------------------------------------------------------------------

# Tabla de dispositivos: (gen_alta, ml_alta, zoom2_alta, zoom4_alta, zoom8_alta)
_SENSI_DB: dict[str, tuple[int, int, int, int, int]] = {
    # ── ULTRA PREMIUM ────────────────────────────────────────────────────────
    "asus rog phone 8 pro": (200, 200, 186, 161, 131),
    "asus rog phone 8":     (200, 200, 185, 160, 130),
    "asus rog phone 7 ultimate": (199, 200, 183, 159, 129),
    "asus rog phone 7":     (198, 200, 182, 158, 128),
    "asus rog phone 6":     (194, 200, 178, 155, 125),
    "asus rog phone 5":     (190, 198, 174, 152, 123),
    "iphone 16 pro max":    (193, 200, 178, 155, 126),
    "iphone 16 pro":        (191, 198, 176, 153, 124),
    "iphone 15 pro max":    (190, 197, 175, 153, 124),
    "iphone 15 pro":        (188, 196, 173, 151, 122),
    "iphone 14 pro max":    (187, 194, 172, 150, 121),
    "iphone 14 pro":        (186, 193, 169, 146, 117),
    "iphone 13 pro max":    (182, 190, 166, 144, 115),
    "iphone 13 pro":        (180, 188, 164, 142, 113),
    "samsung s24 ultra":    (192, 198, 174, 151, 122),
    "samsung s24+":         (189, 196, 172, 150, 121),
    "samsung s24":          (186, 194, 170, 148, 119),
    "samsung s23 ultra":    (190, 197, 173, 151, 122),
    "samsung s23+":         (188, 195, 170, 148, 118),
    "samsung s23":          (184, 192, 168, 146, 117),
    "samsung s22 ultra":    (183, 191, 167, 145, 116),
    "samsung s22+":         (176, 184, 161, 138, 109),
    "samsung s22":          (172, 180, 157, 133, 104),
    "xiaomi 14 ultra":      (191, 198, 175, 153, 124),
    "xiaomi 14 pro":        (187, 194, 171, 149, 119),
    "xiaomi 14":            (184, 192, 168, 146, 117),
    "xiaomi 13 pro":        (185, 193, 169, 147, 118),
    "xiaomi 13":            (182, 190, 166, 144, 115),
    "oneplus 12":           (185, 193, 168, 146, 117),
    "oneplus 11":           (183, 191, 167, 145, 116),
    "oneplus 10 pro":       (179, 187, 163, 141, 112),
    "poco f6 pro":          (186, 193, 170, 148, 119),
    "poco f5 pro":          (183, 191, 166, 144, 115),
    "poco f4 gt":           (180, 188, 164, 142, 113),
    # ── GAMA ALTA ────────────────────────────────────────────────────────────
    "poco f5":              (178, 186, 162, 140, 111),
    "poco f4":              (174, 182, 159, 136, 107),
    "poco f3":              (172, 180, 157, 134, 105),
    "poco x6 pro":          (172, 180, 157, 135, 106),
    "poco x5 pro":          (168, 176, 153, 130, 102),
    "poco x4 pro":          (160, 168, 146, 123,  96),
    "realme gt 2 pro":      (180, 188, 164, 142, 113),
    "realme gt 2":          (176, 184, 160, 137, 108),
    "realme gt neo 3":      (170, 178, 155, 132, 104),
    "realme gt neo 2":      (166, 174, 151, 128, 101),
    "samsung s21 ultra":    (177, 185, 162, 139, 110),
    "samsung s21+":         (174, 182, 159, 136, 107),
    "samsung s21":          (169, 177, 154, 131, 103),
    "samsung s20 ultra":    (170, 178, 155, 132, 104),
    "samsung s20+":         (167, 175, 152, 129, 102),
    "samsung s20":          (164, 172, 149, 127, 100),
    "iphone 16":            (169, 177, 155, 131, 103),
    "iphone 15":            (167, 175, 153, 129, 102),
    "iphone 14":            (164, 172, 150, 127, 100),
    "iphone 13":            (162, 170, 149, 126,  99),
    "iphone 12 pro max":    (166, 174, 152, 128, 101),
    "iphone 12 pro":        (164, 172, 150, 127, 100),
    "iphone 12":            (161, 169, 147, 124,  97),
    "moto edge 50 pro":     (175, 183, 160, 137, 108),
    "moto edge 50":         (170, 178, 155, 132, 104),
    "moto edge 40 pro":     (174, 182, 159, 136, 107),
    "moto edge 40":         (170, 178, 155, 132, 104),
    "moto edge 30 ultra":   (174, 182, 159, 136, 107),
    "moto edge 30 pro":     (170, 178, 155, 132, 104),
    "moto edge 30":         (165, 173, 150, 128, 101),
    "moto g84":             (155, 164, 141, 118,  91),
    "moto g73":             (152, 161, 138, 115,  88),
    # ── GAMA MEDIA ───────────────────────────────────────────────────────────
    "samsung a55":          (148, 157, 134, 112,  85),
    "samsung a55 5g":       (148, 157, 134, 112,  85),
    "samsung a54":          (145, 154, 131, 109,  83),
    "samsung a53":          (145, 154, 131, 109,  83),
    "samsung a53 5g":       (145, 154, 131, 109,  83),
    "samsung a52":          (142, 151, 128, 107,  81),
    "samsung a35":          (143, 152, 129, 107,  81),
    "samsung a35 5g":       (143, 152, 129, 107,  81),
    "samsung a34":          (141, 150, 127, 106,  80),
    "samsung a33":          (140, 149, 126, 105,  79),
    "redmi note 13 pro+":   (163, 171, 148, 126,  99),
    "redmi note 13 pro":    (156, 165, 142, 119,  92),
    "redmi note 13":        (150, 159, 136, 114,  87),
    "redmi note 12 pro+":   (160, 169, 146, 123,  96),
    "redmi note 12 pro":    (158, 167, 144, 121,  94),
    "redmi note 12":        (149, 158, 135, 113,  86),
    "redmi note 11 pro+":   (155, 164, 141, 118,  91),
    "redmi note 11 pro":    (153, 162, 139, 116,  89),
    "redmi note 11":        (141, 150, 127, 106,  80),
    "redmi note 10 pro":    (152, 161, 138, 115,  88),
    "redmi note 10":        (144, 153, 130, 109,  83),
    "redmi note 9 pro":     (148, 157, 134, 112,  85),
    "redmi note 9":         (138, 147, 124, 103,  78),
    "poco x4":              (148, 157, 134, 112,  85),
    "poco m6 pro":          (152, 161, 138, 115,  88),
    "poco m5s":             (140, 149, 126, 105,  79),
    "poco m5":              (135, 144, 121, 101,  76),
    "poco m4 pro":          (150, 159, 136, 114,  87),
    "poco m4":              (140, 149, 126, 105,  79),
    "moto g54":             (138, 147, 124, 103,  78),
    "moto g54 5g":          (138, 147, 124, 103,  78),
    "moto g53":             (136, 145, 122, 101,  76),
    "moto g52":             (134, 143, 120,  99,  75),
    "moto g34":             (133, 142, 119,  99,  75),
    "moto g32":             (130, 139, 116,  97,  73),
    "moto g31":             (126, 135, 113,  94,  71),
    "redmi 12":             (136, 145, 122, 101,  76),
    "redmi 12 5g":          (139, 148, 125, 104,  79),
    "redmi 12c":            (120, 130, 109,  90,  69),
    "iphone 11 pro max":    (163, 171, 148, 126,  99),
    "iphone 11 pro":        (161, 169, 147, 124,  97),
    "iphone 11":            (158, 166, 143, 121,  94),
    "iphone xr":            (151, 159, 136, 115,  88),
    "iphone xs max":        (153, 161, 138, 116,  89),
    "iphone xs":            (151, 159, 136, 115,  88),
    "iphone x":             (148, 156, 133, 112,  85),
    "iphone se 2022":       (148, 156, 134, 112,  85),
    "iphone se 2020":       (144, 152, 130, 109,  83),
    "oppo a78":             (132, 141, 120,  99,  76),
    "oppo a58":             (122, 132, 111,  92,  71),
    "oppo a57":             (118, 128, 109,  90,  69),
    "vivo y36":             (130, 139, 118,  98,  75),
    "vivo y27":             (118, 128, 108,  89,  68),
    # ── GAMA BAJA ────────────────────────────────────────────────────────────
    "samsung a16":          (115, 125, 104,  86,  66),
    "samsung a15":          (112, 122, 101,  83,  64),
    "samsung a14":          (108, 118,  98,  80,  62),
    "samsung a13":          (105, 115,  96,  79,  61),
    "samsung a05s":         (103, 113,  94,  77,  59),
    "samsung a05":          (100, 110,  91,  75,  58),
    "samsung a04s":         ( 97, 107,  89,  73,  57),
    "samsung a03s":         ( 96, 106,  88,  72,  56),
    "redmi 10":             (110, 120, 100,  82,  63),
    "redmi 10c":            (108, 118,  98,  80,  62),
    "redmi 10a":            (104, 114,  95,  78,  60),
    "redmi 9a":             ( 98, 108,  90,  74,  57),
    "redmi 9c":             ( 98, 108,  90,  74,  57),
    "redmi 9":              (102, 112,  93,  76,  59),
    "moto g14":             (118, 128, 107,  88,  68),
    "moto g24":             (120, 130, 109,  90,  69),
    "moto g22":             (116, 126, 105,  86,  66),
    "moto e13":             ( 96, 106,  88,  72,  56),
    "moto e14":             ( 96, 106,  88,  72,  56),
    "moto e30":             ( 94, 104,  86,  71,  55),
    "moto e20":             ( 92, 102,  84,  69,  53),
    "iphone 8":             (138, 146, 124, 104,  79),
    "iphone 7":             (128, 136, 116,  97,  74),
    "iphone 6s":            (118, 126, 108,  90,  69),
    "tecno spark 20 pro":   (110, 120, 100,  82,  63),
    "tecno spark 20":       (105, 115,  96,  79,  61),
    "tecno spark 10 pro":   (104, 114,  95,  78,  60),
    "tecno spark 10":       (100, 110,  92,  76,  58),
    "infinix hot 40 pro":   (116, 126, 105,  86,  66),
    "infinix hot 40":       (113, 123, 102,  84,  65),
    "infinix hot 30":       (109, 119,  99,  81,  62),
    "infinix hot 20":       (104, 114,  95,  78,  60),
    "infinix smart 8":      ( 93, 103,  86,  70,  54),
}

# Optimización por marca
_SENSI_OPT: dict[str, str] = {
    "samsung": (
        "1. Ajustes → Funciones avanzadas → **Game Booster** → Rendimiento máximo\n"
        "2. Ajustes → Pantalla → **Suavidad de movimiento** → Adaptable (o 120Hz)\n"
        "3. Game Booster → Bloquear durante partida → activar\n"
        "4. Batería → **Modo de rendimiento** (no ahorro)\n"
        "5. Ajustes → Pantalla → **Brillo adaptable** → OFF (fijarlo al 80%)"
    ),
    "xiaomi": (
        "1. Ajustes → Apps especiales → **Game Turbo** → agregar FF → Modo Rendimiento\n"
        "2. Game Turbo → **Mejora de toque** → activar\n"
        "3. Ajustes → Pantalla → **Frecuencia de actualización** → máxima disponible\n"
        "4. Ajustes → Batería → **Modo rendimiento**\n"
        "5. MIUI/HyperOS → Modo desarrollador → **Velocidad de animación** → 0.5x"
    ),
    "redmi": (
        "1. Ajustes → Apps especiales → **Game Turbo** → agregar FF → Modo Rendimiento\n"
        "2. Game Turbo → **Mejora de toque** → activar\n"
        "3. Ajustes → Pantalla → **Frecuencia de actualización** → máxima disponible\n"
        "4. Ajustes → Batería → **Modo rendimiento**\n"
        "5. Modo desarrollador → **Velocidad de animación** → 0.5x"
    ),
    "poco": (
        "1. Ajustes → Apps especiales → **Game Turbo** → agregar FF → Modo Rendimiento\n"
        "2. Game Turbo → **Mejora de toque** → activar\n"
        "3. Ajustes → Pantalla → **Frecuencia de actualización** → máxima\n"
        "4. Ajustes → Batería → **Modo rendimiento**\n"
        "5. Modo desarrollador → **Velocidad de animación** → 0.5x"
    ),
    "motorola": (
        "1. Ajustes → Apps → **Moto Gametime** → activar para FF\n"
        "2. Gametime → **Modo inmersivo** + bloquear notificaciones\n"
        "3. Ajustes → Pantalla → **Frecuencia de actualización** → Alta\n"
        "4. Ajustes → Batería → **Modo de batería** → Rendimiento\n"
        "5. Modo desarrollador → **Velocidad de animación** → 0.5x"
    ),
    "realme": (
        "1. Ajustes → **Game Space** → agregar FF → Pro Gamer Mode\n"
        "2. Game Space → **Bloquear notificaciones** durante partida\n"
        "3. Ajustes → Pantalla → **Frecuencia de actualización** → 90Hz o 120Hz\n"
        "4. Ajustes → Batería → **Modo alto rendimiento**\n"
        "5. Modo desarrollador → **Velocidad de animación** → 0.5x"
    ),
    "oppo": (
        "1. Ajustes → **Game Space** → agregar FF → Pro Gamer Mode\n"
        "2. Game Space → bloquear notificaciones\n"
        "3. Ajustes → Pantalla → **Frecuencia de actualización** → máxima\n"
        "4. Ajustes → Batería → **Modo alto rendimiento**\n"
        "5. Modo desarrollador → **Velocidad de animación** → 0.5x"
    ),
    "vivo": (
        "1. Ajustes → **Ultra Game Mode** → Modo Monster → activar para FF\n"
        "2. Ultra Game Mode → **Touch de juego** → activar\n"
        "3. Ajustes → Pantalla → **Alta frecuencia de actualización**\n"
        "4. Ajustes → Batería → **Modo rendimiento**\n"
        "5. Modo desarrollador → **Velocidad de animación** → 0.5x"
    ),
    "iphone": (
        "1. Ajustes → **Modo Concentración** → Juegos → activar al jugar\n"
        "2. Ajustes → Batería → **Modo bajo consumo** → OFF\n"
        "3. iPhone Pro: Ajustes → Pantalla y brillo → **ProMotion** → ON\n"
        "4. Cerrar **todas las apps** en background antes de jugar\n"
        "5. Ajustes → General → **VPN y gestión** → desactivar VPN si hay activa"
    ),
    "tecno": (
        "1. Panel de Juegos → **Modo Rendimiento** + No molestar\n"
        "2. Ajustes → Pantalla → **Frecuencia** → máxima disponible\n"
        "3. Ajustes → Batería → **Modo rendimiento**\n"
        "4. Modo desarrollador → **Velocidad de animación** → 0.5x"
    ),
    "infinix": (
        "1. Panel de Juegos → **Modo Rendimiento** + No molestar\n"
        "2. Ajustes → Pantalla → **Frecuencia** → máxima disponible\n"
        "3. Ajustes → Batería → **Modo rendimiento**\n"
        "4. Modo desarrollador → **Velocidad de animación** → 0.5x"
    ),
    "generico": (
        "1. Modo desarrollador → **Velocidad de animación de ventana/transición** → 0.5x\n"
        "2. Batería → **Modo de alto rendimiento** (no ahorro)\n"
        "3. Pantalla → **Frecuencia de actualización** → máxima disponible\n"
        "4. Cerrar todas las apps en segundo plano antes de jugar"
    ),
}

_SENSI_TIPS: dict[str, str] = {
    "samsung":   "En Game Booster activá 'Prioridad de CPU/GPU' para FF exclusivamente.",
    "xiaomi":    "En Game Turbo forzá 60 FPS mínimo aunque la pantalla vaya a más.",
    "redmi":     "Desactivá 'Ahorro de recursos en segundo plano' en Game Turbo para FF.",
    "poco":      "El modo 'Rendimiento Pro' de Game Turbo da 8–10% más de FPS estables.",
    "motorola":  "Gametime → 'Rendimiento gráfico' → Estable elimina drops en Moto G.",
    "realme":    "Game Space Pro Gamer bloquea RAM para otras apps — hacé espacio antes.",
    "iphone":    "ProMotion ON + Batería OFF da la experiencia más fluida en iPhone Pro.",
    "tecno":     "Activá DND (No molestar) del Panel de Juegos, reduce latencia táctil.",
    "infinix":   "Modo Rendimiento + pantalla a máximo Hz = mejor registro de disparos.",
    "generico":  "Ajustá General de a 5 puntos hasta encontrar tu punto exacto de control.",
}


_MARCA_DISPLAY: dict[str, str] = {
    "samsung": "Samsung", "redmi": "Redmi", "xiaomi": "Xiaomi", "poco": "Poco",
    "motorola": "Motorola", "realme": "Realme", "oppo": "Oppo", "vivo": "Vivo",
    "iphone": "iPhone", "tecno": "Tecno", "infinix": "Infinix",
    "oneplus": "OnePlus", "asus": "Asus", "generico": "Android",
}


def _detectar_marca(d: str) -> str:
    d = d.lower()
    for marca in ("samsung", "redmi", "xiaomi", "poco", "motorola", "moto",
                  "realme", "oppo", "vivo", "iphone", "ipad", "apple",
                  "tecno", "infinix", "oneplus", "asus", "rog"):
        if marca in d:
            if marca in ("iphone", "ipad", "apple"):
                return "iphone"
            if marca in ("moto",):
                return "motorola"
            if marca in ("rog", "asus"):
                return "asus"
            return marca
    return "generico"


def _estimar_specs(d: str, plataforma: str) -> str:
    """Devuelve descripción aproximada de hardware para el header."""
    d = d.lower()
    # ── iPhone ──────────────────────────────────────────────────────────────
    if plataforma == "ios":
        if any(x in d for x in ("16 pro max", "16 pro", "15 pro max", "15 pro",
                                 "14 pro max", "14 pro")):
            return "A-series Pro · 6GB · 6.1\"–6.7\" · 120Hz ProMotion"
        if "13 pro" in d:
            return "A15 Pro · 6GB · 6.1\"–6.7\" · 120Hz ProMotion"
        if any(x in d for x in ("iphone 16", "iphone 15", "iphone 14")):
            return "A-series · 6GB · 6.1\" · 60Hz"
        if "iphone 13" in d:
            return "A15 · 4GB · 6.1\" · 60Hz"
        if "iphone 12" in d:
            return "A14 · 4GB · 6.1\" · 60Hz"
        if "iphone 11" in d:
            return "A13 · 4GB · 6.1\" · 60Hz"
        if any(x in d for x in ("iphone xr", "iphone xs")):
            return "A12 · 3–4GB · 6.1\" · 60Hz"
        if "iphone se" in d:
            return "A13/A15 · 3–4GB · 4.7\" · 60Hz"
        if "iphone 8" in d:
            return "A11 · 2GB · 4.7\" · 60Hz"
        return "Apple SoC · gama media-alta · 60Hz"
    # ── Android — orden de más específico a más genérico ─────────────────────
    if any(x in d for x in ("ultra", "rog", "zenfone")):
        return "SoC flagship · 12–16GB · 6.7\"+ · 120–165Hz"
    if any(x in d for x in ("samsung s24", "samsung s23", "oneplus 12", "oneplus 11",
                             "xiaomi 14", "xiaomi 13")):
        return "Snapdragon 8 Gen 2/3 · 8–12GB · 6.7\" · 120Hz"
    if "samsung s22" in d:
        return "Snapdragon 8 Gen 1 · 8GB · 6.6\" · 120Hz"
    if "samsung s21" in d:
        return "Exynos 2100/Snapdragon 888 · 8GB · 6.7\" · 120Hz"
    if any(x in d for x in ("poco f5 pro", "poco f4 gt", "poco f6 pro")):
        return "Snapdragon 8xx · 8–12GB · 6.6\" · 144Hz"
    if any(x in d for x in ("poco f5", "poco f4", "poco f3")):
        return "Snapdragon 870/888 · 8GB · 6.6\" · 120Hz"
    if any(x in d for x in ("poco x6 pro", "poco x5 pro", "gt 2 pro", "gt neo 3")):
        return "Dimensity 8100/9000 · 8GB · 6.6\" · 120Hz"
    if any(x in d for x in ("poco x4 pro", "poco x5", "realme gt 2")):
        return "Snapdragon 695/870 · 6–8GB · 6.6\" · 90–120Hz"
    if any(x in d for x in ("note 13 pro+", "note 12 pro+")):
        return "Dimensity 1080 · 8GB · 6.6\" · 120Hz"
    if any(x in d for x in ("note 13 pro", "note 12 pro")):
        return "Dimensity 1080 · 6–8GB · 6.6\" · 120Hz"
    if any(x in d for x in ("note 13", "note 12")):
        return "Helio G99/Snapdragon 685 · 4–6GB · 6.5\" · 90Hz"
    if any(x in d for x in ("note 11 pro", "note 10 pro")):
        return "Helio G96 · 6–8GB · 6.6\" · 90Hz"
    if any(x in d for x in ("note 11", "note 10", "note 9")):
        return "Snapdragon 680/Helio G88 · 4–6GB · 6.5\" · 60–90Hz"
    if any(x in d for x in ("samsung a55", "samsung a54", "samsung a53")):
        return "Exynos 1380/Snapdragon 695 · 6–8GB · 6.4\" · 120Hz"
    if any(x in d for x in ("samsung a35", "samsung a34", "samsung a33")):
        return "Exynos 1280/Dimensity 1080 · 6GB · 6.4\" · 90–120Hz"
    if any(x in d for x in ("samsung a16", "samsung a15", "samsung a14", "samsung a13")):
        return "Helio G85/Exynos 850 · 4–6GB · 6.5\" · 60–90Hz"
    if any(x in d for x in ("samsung a05", "samsung a04", "samsung a03")):
        return "Helio G85 · 3–4GB · 6.5\" · 60Hz"
    if any(x in d for x in ("moto edge 40", "moto edge 30")):
        return "Snapdragon 695/6 Gen 1 · 8GB · 6.5\" · 90–120Hz"
    if any(x in d for x in ("moto g84", "moto g73")):
        return "Snapdragon 695 · 8GB · 6.5\" · 90Hz"
    if any(x in d for x in ("moto g54", "moto g53", "moto g52")):
        return "Dimensity 700/7020 · 4–8GB · 6.5\" · 90Hz"
    if any(x in d for x in ("moto g34", "moto g32", "moto g31", "moto g22", "moto g14")):
        return "Snapdragon 480/695 · 4–6GB · 6.5\" · 60–90Hz"
    if any(x in d for x in ("moto e13", "moto e14", "moto e30")):
        return "Helio G37 · 2–3GB · 6.5\" · 60Hz"
    if any(x in d for x in ("redmi note",)):
        return "Helio G99/Dimensity 1080 · 4–8GB · 6.5\" · 90–120Hz"
    if any(x in d for x in ("redmi 12c", "redmi 10a")):
        return "Helio G85 · 3–4GB · 6.5\" · 60Hz"
    if any(x in d for x in ("redmi 10", "redmi 12")):
        return "Helio G88/Snapdragon 685 · 4–6GB · 6.5\" · 60–90Hz"
    if any(x in d for x in ("redmi 9a", "redmi 9c", "redmi 9")):
        return "Helio G25/G35 · 2–3GB · 6.5\" · 60Hz"
    if any(x in d for x in ("tecno spark", "infinix hot", "infinix smart")):
        return "Helio G37/G85 · 3–4GB · 6.5\" · 60Hz"
    return "gama media · 4–6GB · 6.5\" · 60–90Hz"


def _calcular_dpi(d: str) -> tuple[int, int]:
    """Devuelve (dpi_recomendado, dpi_nativo) para Android."""
    d = d.lower()
    # Tablets o pantallas grandes
    if any(x in d for x in ("tab", "pad", "fold", "flip")):
        return 330, 300
    # Pantallas pequeñas (≤ 6.0")
    if any(x in d for x in ("se", "mini", "compact", "iphone 8", "iphone 7",
                             "iphone 6", "iphone se")):
        return 390, 350
    # Ultra premium / grandes (6.6"–6.8")
    if any(x in d for x in ("ultra", "pro max", "plus", "s24", "s23", "note 13",
                             "note 12", "note 11", "edge", "oneplus", "rog")):
        return 520, 470
    # Gama alta 6.4"–6.6"
    if any(x in d for x in ("pro", "f5", "f4", "f3", "x5", "x6", "gt", "a55",
                             "a54", "a53", "a35", "g84", "g73")):
        return 470, 425
    # Por defecto: pantalla media (~6.5")
    return 450, 405


def _estimar_valores_por_tier(d: str, plataforma: str) -> tuple[int, int, int, int, int]:
    """Estima valores ALTA cuando el dispositivo no está en la tabla."""
    d = d.lower()

    # ── Extractar número de modelo para scoring ──
    import re as _re
    nums = [int(n) for n in _re.findall(r'\d+', d)]
    num_max = max(nums) if nums else 0

    # ── iPhone tiers ──
    if plataforma == "ios":
        if any(x in d for x in ("16 pro", "15 pro", "14 pro")):
            return (189, 196, 173, 151, 122)
        if any(x in d for x in ("13 pro",)):
            return (180, 188, 164, 142, 113)
        if any(x in d for x in ("16", "15", "14")):
            return (165, 173, 151, 128, 101)
        if "13" in d:
            return (162, 170, 149, 126,  99)
        if "12" in d:
            return (161, 169, 147, 124,  97)
        if "11" in d:
            return (158, 166, 143, 121,  94)
        if any(x in d for x in ("xr", "xs")):
            return (151, 159, 136, 115,  88)
        if "se" in d:
            return (144, 152, 130, 109,  83)
        if "8" in d:
            return (138, 146, 124, 104,  79)
        return (148, 156, 133, 112,  85)

    # ── Android — palabras clave de tier alto ──
    if any(x in d for x in ("ultra", "rog", "pro max", "pro+", "find x")):
        return (190, 197, 174, 152, 123)
    if any(x in d for x in ("pro", "plus", "gt", "f5", "f4", "f3",
                             "edge 40", "edge 30", "oneplus")):
        return (172, 180, 157, 134, 105)

    # ── Samsung A-series por número ──
    if "samsung" in d or d.startswith("a") or "galaxy" in d:
        if num_max >= 50:
            return (146, 155, 132, 110,  83)
        if num_max >= 30:
            return (141, 150, 127, 106,  80)
        if num_max >= 14:
            return (110, 120, 100,  83,  64)
        return (100, 110,  92,  76,  58)

    # ── Redmi Note / base ──
    if "note" in d:
        if num_max >= 12:
            return (150, 159, 136, 114,  87)
        if num_max >= 10:
            return (144, 153, 130, 109,  83)
        return (138, 147, 124, 103,  78)

    # ── Moto G por número ──
    if any(x in d for x in ("moto", "motorola")):
        if num_max >= 80:
            return (155, 164, 141, 118,  91)
        if num_max >= 50:
            return (136, 145, 122, 101,  76)
        if num_max >= 30:
            return (130, 139, 116,  97,  73)
        if num_max >= 13:
            return (118, 128, 107,  88,  68)
        return ( 96, 106,  88,  72,  56)

    # ── Presupuesto / desconocido ──
    if any(x in d for x in ("spark", "hot", "smart", "9a", "9c", "e13", "e14")):
        return (100, 110,  92,  76,  58)

    # ── Fallback genérico ──
    return (135, 144, 121, 101,  76)


def _generar_sensibilidad_local(dispositivo: str) -> str:
    import random
    d = dispositivo.lower().strip()
    plataforma = _detectar_plataforma(d)

    # 1 — Buscar en tabla (exact → partial)
    alta: tuple[int, int, int, int, int] | None = None
    estimado = False

    if d in _SENSI_DB:
        alta = _SENSI_DB[d]
    else:
        best_len = 0
        for key, vals in _SENSI_DB.items():
            if key in d or d in key:
                if len(key) > best_len:
                    alta = vals
                    best_len = len(key)

    if alta is None:
        alta = _estimar_valores_por_tier(d, plataforma)
        estimado = True

    g_a, ml_a, z2_a, z4_a, z8_a = alta

    # 2 — Generar BAJA (ALTA − 50/48/42/35/28)
    g_b  = g_a  - 50
    ml_b = ml_a - 48
    z2_b = z2_a - 42
    z4_b = z4_a - 35
    z8_b = z8_a - 28

    # 3 — MEDIA: punto medio ± variación natural
    rng = random.Random(hash(d) % (2**32))  # determinista por dispositivo

    def mid(a: int, b: int, spread: int = 2) -> int:
        return round((a + b) / 2) + rng.randint(-spread, spread)

    g_m  = mid(g_a, g_b)
    ml_m = mid(ml_a, ml_b)
    z2_m = mid(z2_a, z2_b, 1)
    z4_m = mid(z4_a, z4_b, 1)
    z8_m = mid(z8_a, z8_b, 1)

    # AWM doble ≈ 8x ± pequeña variación
    awd_a = z8_a + rng.randint(-2, 2)
    awd_m = z8_m + rng.randint(-2, 2)
    awd_b = z8_b + rng.randint(-2, 2)

    # 4 — DPI y botón
    dpi_rec, dpi_nat = _calcular_dpi(d)
    btn_a, btn_m, btn_b = 64, 59, 53
    if any(x in d for x in ("se", "mini", "compact", "iphone 8", "iphone 7")):
        btn_a, btn_m, btn_b = 60, 55, 49

    # 5 — Texto auxiliar
    marca = _detectar_marca(d)
    opt_texto = _SENSI_OPT.get(marca, _SENSI_OPT["generico"])
    tip = _SENSI_TIPS.get(marca, _SENSI_TIPS["generico"])
    specs = _estimar_specs(d, plataforma)
    est_tag = " _(estimado por nomenclatura)_" if estimado else ""

    if g_a >= 175:
        perfil_rec, razon_rec = "Alta", "alto rendimiento y pantalla de alta frecuencia"
    elif g_a >= 140:
        perfil_rec, razon_rec = "Media", "balance óptimo entre precisión y estabilidad"
    else:
        perfil_rec, razon_rec = "Baja", "máxima estabilidad en dispositivo de gama baja"

    # 6 — Formatear bloques
    def bloque(emoji: str, label: str,
               g: int, ml: int, z2: int, z4: int, z8: int, awd: int, btn: int) -> str:
        if plataforma == "ios":
            return (
                f"**{emoji} {label}**\n"
                f"```\n"
                f"Botón disparo   : derecho · tamaño {btn}% · posición media-baja\n"
                f"General         : {g}\n"
                f"Mira libre      : {ml}\n"
                f"Mira roja (2x)  : {z2}\n"
                f"Mira (4x)       : {z4}\n"
                f"Mira AWM (8x)   : {z8}\n"
                f"Mira AWM doble  : {awd}\n"
                "```"
            )
        return (
            f"**{emoji} {label}**\n"
            f"```\n"
            f"DPI dispositivo : {dpi_rec}  (nativo {dpi_nat})\n"
            f"Botón disparo   : derecho · tamaño {btn}% · posición media-baja\n"
            f"General         : {g}\n"
            f"Mira libre      : {ml}\n"
            f"Mira roja (2x)  : {z2}\n"
            f"Mira (4x)       : {z4}\n"
            f"Mira AWM (8x)   : {z8}\n"
            f"Mira AWM doble  : {awd}\n"
            "```"
        )

    b_alta  = bloque("🟢", "ALTA — agresivo / corto-medio",       g_a, ml_a, z2_a, z4_a, z8_a, awd_a, btn_a)
    b_media = bloque("🟡", "MEDIA — balanceado / todo terreno",   g_m, ml_m, z2_m, z4_m, z8_m, awd_m, btn_m)
    b_baja  = bloque("🔴", "BAJA — sniper / largo alcance",       g_b, ml_b, z2_b, z4_b, z8_b, awd_b, btn_b)

    marca_display = _MARCA_DISPLAY.get(marca, marca.title())
    return (
        f"**📱 Hardware detectado:** {dispositivo.strip()} — {specs}{est_tag}\n"
        f"**🎯 Perfil recomendado:** {perfil_rec} _({razon_rec})_\n\n"
        f"{b_alta}\n"
        f"{b_media}\n"
        f"{b_baja}\n\n"
        f"**⚙️ Optimización {marca_display}**\n"
        f"{opt_texto}\n\n"
        f"**💡 Tip pro:** {tip}"
    )


def _detectar_plataforma(dispositivo: str) -> str:
    """Detecta si el dispositivo es iPhone/iPad o Android."""
    d = dispositivo.lower()
    if any(k in d for k in ("iphone", "ipad", "ios", "apple")):
        return "ios"
    return "android"


@tree.command(
    name="sensibilidad",
    description="Sensi profesional Free Fire OB53 — Android e iPhone (0.1 crédito)",
)
@app_commands.describe(dispositivo="Modelo exacto, ej: iPhone 13 Pro, Samsung A16, Redmi Note 13, Moto G54")
async def sensibilidad(interaction: discord.Interaction, dispositivo: str):
    await _safe_defer(interaction, ephemeral=True, thinking=True)

    # Solo se puede usar en el canal sensis-xitadas
    if CANAL_SENSI_ID and interaction.channel_id != CANAL_SENSI_ID:
        await interaction.followup.send(
            f"🎯 Este comando solo funciona en <#{CANAL_SENSI_ID}>.",
            ephemeral=True,
        )
        return

    # Cooldown por usuario (evita spam)
    ahora = _dt.datetime.utcnow().timestamp()
    ultimo = _SENSI_COOLDOWN.get(interaction.user.id, 0)
    if ahora - ultimo < _SENSI_COOLDOWN_SEG:
        espera = int(_SENSI_COOLDOWN_SEG - (ahora - ultimo))
        await interaction.followup.send(
            f"⏳ Esperá **{espera}s** antes de pedir otra sensibilidad.", ephemeral=True
        )
        return

    # Verificar y descontar 0.1 créditos DE SENSI (billetera separada del proxy)
    discord_id = str(interaction.user.id)
    COSTO = 0.1
    saldo_actual = database.get_sensi_credits(discord_id)
    if saldo_actual < COSTO:
        await interaction.followup.send(
            f"❌ No tenés suficientes créditos de sensi.\n"
            f"🎯 Tu saldo: **{_fmt_creditos(saldo_actual)}** crédito(s) de sensi\n"
            f"📋 Costo: **{_fmt_creditos(COSTO)}** crédito (= $1.000 ARS)\n\n"
            f"⚠️ Los créditos de proxy **no sirven** aquí.\n"
            f"Comprá créditos con `/comprar` → pack **🎯 Sensi Xitada**.",
            ephemeral=True,
        )
        return

    ok = database.consume_sensi_credits(discord_id, COSTO)
    if not ok:
        await interaction.followup.send(
            "❌ No se pudo descontar el crédito. Intentá de nuevo.", ephemeral=True
        )
        return

    _SENSI_COOLDOWN[interaction.user.id] = ahora
    saldo_nuevo = database.get_sensi_credits(discord_id)

    dispositivo_clean = dispositivo.strip()[:80]
    plataforma = _detectar_plataforma(dispositivo_clean)

    # Motor local — sin API, sin créditos externos, instantáneo
    try:
        texto = _generar_sensibilidad_local(dispositivo_clean)
    except Exception:
        log.exception("Error en motor local de sensibilidad para %s", dispositivo_clean)
        database.add_sensi_credits(discord_id, COSTO)
        await interaction.followup.send(
            "❌ Hubo un error generando la sensibilidad. Tu crédito fue reembolsado. Intentá de nuevo.",
            ephemeral=True,
        )
        return

    # Entrega por DM (más privado y permite respuestas largas con varios embeds)
    icono_plat = "🍎" if plataforma == "ios" else "🤖"
    MAX = 4000
    fragmentos = [texto[i:i+MAX] for i in range(0, len(texto), MAX)]

    try:
        for idx, frag in enumerate(fragmentos):
            embed = discord.Embed(
                title=f"{icono_plat} Sensi Xitada — {dispositivo_clean}" if idx == 0 else None,
                description=frag,
                color=0xFF4500,
            )
            if idx == len(fragmentos) - 1:
                embed.set_footer(
                    text=f"Marke Panel • OB53 • Saldo restante: {_fmt_creditos(saldo_nuevo)} crédito(s)"
                )
            await interaction.user.send(embed=embed)
    except discord.Forbidden:
        # DMs cerrados → reembolsar y avisar en el canal
        database.add_sensi_credits(discord_id, COSTO)
        await interaction.followup.send(
            "❌ No pude enviarte un mensaje privado.\n"
            "Activá los DMs del servidor: **tocá el nombre del servidor → Privacidad → Permitir mensajes directos**.\n"
            "Tu crédito fue reembolsado. Volvé a usar `/sensibilidad` cuando lo tengas activado.",
            ephemeral=True,
        )
        log.warning("DM bloqueado para %s — reembolso aplicado", interaction.user)
        return
    except Exception:
        log.exception("Error enviando DM de sensi a %s", interaction.user)
        database.add_sensi_credits(discord_id, COSTO)
        await interaction.followup.send(
            "❌ Hubo un problema enviándote el DM. Tu crédito fue reembolsado.",
            ephemeral=True,
        )
        return

    # Recién ahora registramos en la DB (después de la entrega exitosa)
    try:
        log_id = database.record_sensi_request(
            discord_id=discord_id,
            username=str(interaction.user),
            dispositivo=dispositivo_clean,
            plataforma=plataforma,
            respuesta=texto,
        )
    except Exception:
        log.exception("No pude guardar la sensi en la DB (sigo igual)")
        log_id = 0

    # Aviso público breve en el canal
    await interaction.followup.send(
        f"✅ {icono_plat} Te envié tu **Sensi Xitada para {dispositivo_clean}** por mensaje privado.\n"
        f"📩 Revisá tus DMs con Marke Panel.\n"
        f"💰 Saldo restante: **{_fmt_creditos(saldo_nuevo)}** crédito(s) de sensi.",
        ephemeral=True,
    )

    log.info(
        "Sensi #%s entregada por DM [%s] dispositivo=%s usuario=%s saldo=%s",
        log_id, plataforma, dispositivo_clean, interaction.user, saldo_nuevo,
    )


@tree.command(
    name="sensi-stats",
    description="(Admin) Ver estadísticas de sensibilidades entregadas",
)
async def sensi_stats(interaction: discord.Interaction):
    await _safe_defer(interaction, ephemeral=True, thinking=True)
    if not _puede_registrar(interaction):
        await interaction.followup.send("❌ Solo administradores.", ephemeral=True)
        return

    total = database.count_sensi_logs()
    top = database.top_sensi_devices(limit=10)
    recientes = database.get_sensi_logs(limit=10)

    embed = discord.Embed(
        title="📊 Sensi Xitadas — estadísticas",
        description=f"**Total entregadas:** {total}",
        color=0xFF4500,
    )

    if top:
        top_str = "\n".join(
            f"`{i+1:2d}.` {('🍎' if r['plataforma']=='ios' else '🤖')} **{r['dispositivo']}** — {r['pedidos']} pedido(s)"
            for i, r in enumerate(top)
        )
        embed.add_field(name="🏆 Top dispositivos", value=top_str[:1024], inline=False)

    if recientes:
        rec_str = "\n".join(
            f"`#{r['id']}` {('🍎' if r['plataforma']=='ios' else '🤖')} **{r['dispositivo']}** "
            f"— {r['username']} · {r['created_at'][:16]}"
            for r in recientes
        )
        embed.add_field(name="🕒 Últimas 10", value=rec_str[:1024], inline=False)

    embed.set_footer(text="Marke Panel • Datos en credits.db → tabla sensi_logs")
    await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(
    name="aprobar-manual",
    description="(Admin) Aprobar a mano una operación NX/BN perdida y avisar al comprador",
)
@app_commands.describe(
    op_id="ID de la operación, ej: NX-3343 o BN-2862",
    usuario="Comprador a acreditar",
    pack="ID del pack a acreditar (1d, 7d, 30d, sx, etc.)",
)
async def aprobar_manual(
    interaction: discord.Interaction,
    op_id: str,
    usuario: discord.Member,
    pack: str,
):
    await _safe_defer(interaction, ephemeral=True, thinking=True)
    if not _puede_registrar(interaction):
        await interaction.followup.send("❌ Solo administradores.", ephemeral=True)
        return

    op_id_clean = op_id.strip().upper()
    if not re.match(r"^(NX|BN)-\d+$", op_id_clean):
        await interaction.followup.send(
            "❌ El ID debe tener formato `NX-1234` o `BN-1234`.", ephemeral=True,
        )
        return

    pack_obj = payments.PACKS.get(pack.strip().lower())
    if pack_obj is None:
        disponibles = ", ".join(f"`{k}`" for k in payments.PACKS.keys())
        await interaction.followup.send(
            f"❌ Pack `{pack}` no existe. Disponibles: {disponibles}", ephemeral=True,
        )
        return

    # Evitar acreditar dos veces
    ya = database.get_payment(op_id_clean) if hasattr(database, "get_payment") else None
    if ya:
        await interaction.followup.send(
            f"⚠️ La operación `{op_id_clean}` ya está registrada como **{ya.get('status')}**. "
            "No la voy a procesar de nuevo.",
            ephemeral=True,
        )
        return

    discord_id = str(usuario.id)
    metodo_str = "Naranja X" if op_id_clean.startswith("NX-") else "Binance"
    log.info(
        "Aprobación MANUAL %s %s por admin %s → usuario %s",
        metodo_str, op_id_clean, interaction.user, discord_id,
    )

    # Construir el op-dict igual al que usa el flujo automático y delegar
    op = {
        "pack": pack_obj,
        "discord_id": discord_id,
        "user_id": usuario.id,
        "username": str(usuario),
    }
    await _acreditar_pago_aprobado(op_id_clean, op, fuente="manual")

    # Notificar al admin por WhatsApp
    try:
        wa_texto = (
            f"✅ Pago aprobado MANUAL\n"
            f"Op: #{op_id_clean} | {metodo_str}\n"
            f"Pack: {pack_obj.nombre} (${pack_obj.precio:,.0f} ARS)\n"
            f"Usuario: {usuario} (id={discord_id})"
        )
        await asyncio.to_thread(twilio_helper.send_whatsapp, wa_texto)
    except Exception:
        log.warning("No pude enviar WhatsApp de aprobación manual %s", op_id_clean)

    await interaction.followup.send(
        f"✅ Operación `{op_id_clean}` procesada para {usuario.mention} — "
        f"pack **{pack_obj.nombre}** entregado. WhatsApp y #ventas notificados.",
        ephemeral=True,
    )


@tree.command(
    name="diamantes-manual",
    description="(Admin) Relanzar entrega de diamantes para un usuario que pagó pero no recibió",
)
@app_commands.describe(
    usuario="Usuario que debe recibir los diamantes",
    cantidad="Cantidad de diamantes (110, 341, 572, 1166, 2398 o 6160)",
    id_freefire="ID de Free Fire del comprador",
)
async def diamantes_manual(
    interaction: discord.Interaction,
    usuario: discord.Member,
    cantidad: int,
    id_freefire: str,
):
    await _safe_defer(interaction, ephemeral=True, thinking=True)
    if not _puede_registrar(interaction):
        await interaction.followup.send("❌ Solo administradores.", ephemeral=True)
        return

    validos = list(_DIAM_PRECIOS.keys())
    if cantidad not in validos:
        await interaction.followup.send(
            f"❌ Cantidad inválida. Packs válidos: {', '.join(str(v) for v in validos)}",
            ephemeral=True,
        )
        return

    precio = _DIAM_PRECIOS.get(cantidad, "?")
    log.info(
        "DIAMANTES MANUAL — admin=%s usuario=%s diamonds=%d id_ff=%s",
        interaction.user, usuario, cantidad, id_freefire,
    )

    canal_ventas = await _obtener_canal_ventas()
    if canal_ventas:
        embed_log = discord.Embed(
            title="💎 Entrega Manual de Diamantes",
            description=(
                f"👤 **Admin:** {interaction.user.mention}\n"
                f"🎯 **Comprador:** {usuario.mention} (`{usuario}`)\n"
                f"💎 **Paquete:** {cantidad:,} Diamantes — {precio} ARS\n"
                f"🎮 **ID Free Fire:** `{id_freefire}`\n\n"
                "⏳ Iniciando proceso automático..."
            ),
            color=0xF0B90B,
        )
        await canal_ventas.send(embed=embed_log)

    asyncio.create_task(_tarea_diamantes_binance(usuario, cantidad, id_freefire))

    await interaction.followup.send(
        f"✅ Proceso iniciado para **{cantidad:,} 💎** → {usuario.mention} (ID FF: `{id_freefire}`).\n"
        "El resultado llegará al usuario por DM y se notificará en #ventas.",
        ephemeral=True,
    )


@tree.command(
    name="reenviar-diamantes",
    description="(Admin) Canjear diamantes para un usuario; PIN opcional si la búsqueda automática falla",
)
@app_commands.describe(
    usuario="Usuario que debe recibir los diamantes",
    id_freefire="ID de Free Fire del comprador",
    cantidad="Cantidad de diamantes (110, 341, 572, 1166, 2398 o 6160)",
    pin="PIN del pedido (opcional — pegalo directo si la búsqueda automática falló)",
)
async def reenviar_diamantes_cmd(
    interaction: discord.Interaction,
    usuario: discord.Member,
    id_freefire: str,
    cantidad: int,
    pin: str | None = None,
):
    await _safe_defer(interaction, ephemeral=True, thinking=True)
    if not _puede_registrar(interaction):
        await interaction.followup.send("❌ Solo administradores.", ephemeral=True)
        return

    validos = list(_DIAM_PRECIOS.keys())
    if cantidad not in validos:
        await interaction.followup.send(
            f"❌ Cantidad inválida. Packs válidos: {', '.join(str(v) for v in validos)}",
            ephemeral=True,
        )
        return

    log.info(
        "REENVIAR DIAMANTES — admin=%s usuario=%s diamonds=%d id_ff=%s",
        interaction.user, usuario, cantidad, id_freefire,
    )

    canal_ventas = await _obtener_canal_ventas()
    if canal_ventas:
        modo = f"🔑 PIN manual: `{pin}`" if pin else "🔍 Buscando PIN del último pedido completado..."
        embed_log = discord.Embed(
            title="🔁 Reenvío de Diamantes",
            description=(
                f"👤 **Admin:** {interaction.user.mention}\n"
                f"🎯 **Comprador:** {usuario.mention} (`{usuario}`)\n"
                f"💎 **Paquete:** {cantidad:,} Diamantes\n"
                f"🎮 **ID Free Fire:** `{id_freefire}`\n\n"
                f"{modo}"
            ),
            color=0x3498DB,
        )
        await canal_ventas.send(embed=embed_log)

    if pin:
        msg_followup = f"⏳ Usando PIN manual para **{cantidad:,} 💎** → {usuario.mention}..."
    else:
        msg_followup = (
            f"⏳ Buscando el PIN del pedido de **{cantidad:,} 💎** y canjeando para {usuario.mention}...\n"
            "El resultado llegará al usuario por DM y se notificará en #ventas."
        )
    await interaction.followup.send(msg_followup, ephemeral=True)

    async def _tarea_reenvio():
        import io as _io
        pin_encontrado = pin.strip() if pin else ""
        order_id_encontrado = ""
        try:
            # ── 1. PIN: manual > DB > latingm.com (en ese orden de prioridad) ──
            if not pin_encontrado:
                # Primero buscar en la base de datos local (guardado durante la compra)
                pin_encontrado, order_id_encontrado = await asyncio.to_thread(
                    database.pop_diamond_pin, cantidad
                )
                if pin_encontrado:
                    log.info("reenviar_diamantes: PIN obtenido de DB local para %d💎 → %s", cantidad, pin_encontrado)

            if not pin_encontrado:
                # Fallback: scraping en latingm.com
                log.info("reenviar_diamantes: PIN no en DB, buscando en latingm.com...")
                pin_encontrado, order_id_encontrado = await latingm_scraper.obtener_pin_de_ultimo_pedido(cantidad)

            if not pin_encontrado:
                msg_fallo = (
                    f"❌ No se pudo procesar tu pedido de {cantidad:,} 💎.\n"
                    "El pedido puede que aún no esté confirmado. Un admin fue notificado."
                )
                try:
                    dm = await usuario.create_dm()
                    await dm.send(msg_fallo)
                except discord.Forbidden:
                    pass
                try:
                    canal = await _obtener_canal_ventas()
                    if canal:
                        await canal.send(embed=discord.Embed(
                            title="❌ Reenvío fallido — PIN no encontrado",
                            description=(
                                f"👤 **Admin:** {interaction.user.mention}\n"
                                f"🎯 **Comprador:** {usuario.mention}\n"
                                f"💎 **Diamantes:** {cantidad:,}\n"
                                f"🎮 **ID FF:** `{id_freefire}`\n\n"
                                "No se encontró el PIN del pedido en el proveedor."
                            ),
                            color=0xE74C3C,
                        ))
                except Exception:
                    pass
                log.warning("reenviar_diamantes: PIN no encontrado para %d diamantes", cantidad)
                return

            log.info("reenviar_diamantes: PIN encontrado=%s order=%s", pin_encontrado, order_id_encontrado)

            # ── 2. Canjear el PIN en redeempins.com ───────────────────────────
            screenshot, resultado = await latingm_scraper.canjear_pin_directo(
                pin=pin_encontrado,
                id_freefire=id_freefire,
                diamonds=cantidad,
            )
            exito = resultado.startswith("✅")

            # Fallback manual si el reCAPTCHA bloqueó el canje
            pm = _parsear_pendiente_manual(resultado)
            if pm:
                asyncio.create_task(_dm_alerta_manual_admin(
                    pin=pm["pin"], id_freefire=pm["id_ff"],
                    diamonds=pm["diamonds"], comprador=usuario.mention,
                ))

            # ── DM al COMPRADOR: genérico, sin capturas ni datos internos ────
            if not pm:
                try:
                    dm = await usuario.create_dm()
                    if exito:
                        msg_buyer = (
                            f"✅ ¡Listo! Recibiste **{cantidad:,} 💎** en tu cuenta de Free Fire.\n"
                            "Revisá tu cuenta — puede demorar unos minutos en aparecer."
                        )
                    else:
                        msg_buyer = (
                            f"💎 Tu recarga de **{cantidad:,} diamantes** está siendo procesada.\n"
                            "En breve te llegará. Si tenés dudas escribí a un admin."
                        )
                    embed_buyer = discord.Embed(
                        description=msg_buyer,
                        color=0x2ECC71 if exito else 0xF1C40F,
                    )
                    embed_buyer.set_footer(text="Marke Panel • @markee.4")
                    await dm.send(embed=embed_buyer)
                except discord.Forbidden:
                    log.warning("reenviar_diamantes: DM bloqueado para %s", usuario)

            # ── Reporte completo en #ventas (solo admins lo ven) ─────────────
            try:
                canal = await _obtener_canal_ventas()
                if canal:
                    color = 0x2ECC71 if exito else (0xFFA500 if pm else 0xE74C3C)
                    titulo = "✅ Reenvío completado" if exito else ("⏳ Pendiente manual" if pm else "❌ Reenvío fallido")
                    embed_res = discord.Embed(
                        title=titulo,
                        description=(
                            f"👤 **Admin:** {interaction.user.mention}\n"
                            f"🎯 **Comprador:** {usuario.mention}\n"
                            f"💎 **Diamantes:** {cantidad:,}\n"
                            f"🎮 **ID FF:** `{id_freefire}`\n"
                            f"🔑 **PIN:** `{pin_encontrado}`\n"
                            + (f"📋 **Pedido:** `#{order_id_encontrado}`\n" if order_id_encontrado else "")
                            + f"\n```\n{resultado[:300]}\n```"
                        ),
                        color=color,
                    )
                    if screenshot:
                        await canal.send(
                            embed=embed_res,
                            file=discord.File(
                                fp=_io.BytesIO(screenshot),
                                filename="reenvio_resultado.png",
                            ),
                        )
                    else:
                        await canal.send(embed=embed_res)
            except Exception:
                log.exception("reenviar_diamantes: error posteando en #ventas")

        except Exception:
            log.exception("reenviar_diamantes: error en tarea de reenvío")

    asyncio.create_task(_tarea_reenvio())


@tree.command(
    name="enviar-key",
    description="(Admin) Enviar una key de proxy por DM a un usuario",
)
@app_commands.describe(
    usuario="Usuario que debe recibir la key",
    key="La key a enviar (ej: MARKEXXX123)",
    dias="Días de acceso de la key",
)
async def enviar_key_cmd(
    interaction: discord.Interaction,
    usuario: discord.Member,
    key: str,
    dias: int,
):
    await _safe_defer(interaction, ephemeral=True, thinking=True)
    if not _puede_registrar(interaction):
        await interaction.followup.send("❌ Solo administradores.", ephemeral=True)
        return

    key = key.strip()

    # ── Registrar la key en el bot de Telegram ANTES de entregarla ───────────
    if not telegram_client.is_ready():
        await interaction.followup.send(
            "❌ El sistema Telegram no está disponible. Intentá en unos minutos.",
            ephemeral=True,
        )
        return

    try:
        await telegram_client.cmd_gen(key, dias)
        log.info("Key registrada en Telegram: %s (%dd) para %s", key, dias, usuario.id)
    except Exception as exc_gen:
        log.exception("Error registrando key en Telegram para %s — key=%s", usuario.id, key)
        await interaction.followup.send(
            f"⚠️ No se pudo registrar la key `{key}` en el sistema de Telegram:\n"
            f"```{exc_gen}```\n"
            f"La key **no fue enviada** al usuario para evitar entregar una key inválida.\n"
            f"Revisá el bot de Telegram y volvé a intentarlo.",
            ephemeral=True,
        )
        asyncio.create_task(_log_key(
            "gen_error", key, usuario.id,
            dias=dias, metodo="Admin /enviar-key", error=str(exc_gen),
            admin_id=interaction.user.id,
        ))
        return

    # ── Entregar por DM ──────────────────────────────────────────────────────
    embed_key = discord.Embed(
        title="✅ ¡Tu key de proxy está lista!",
        description=(
            f"🔑 **Tu key ({dias} día{'s' if dias != 1 else ''}):**\n"
            f"```\n{key}\n```\n"
            f"Usá el comando `/key` en el servidor para activarla con tu IP.\n\n"
            f"🌐 **Servidor:** `108.181.215.247`\n"
            f"👔 **Puerto Cuello:** `10065`\n"
            f"👕 **Puerto Pecho:** `10066`\n"
            f"👤 **Login:** ||DGZADAXFF||\n"
            f"🔒 **Contraseña:** ||DGZADAXFF||\n\n"
            f"¡Muchísimas gracias por comprar en **Sensi Marke**! 🖤\n"
            f"Recordá estar atento al grupo:\n"
            f"https://chat.whatsapp.com/DQxndyWBG860vpaVcxam3s"
        ),
        color=0x2ECC71,
    )
    try:
        await usuario.send(embed=embed_key)
        log.info("Key enviada manualmente a %s (%s): %s (%dd)", usuario, usuario.id, key, dias)
        await interaction.followup.send(
            f"✅ Key `{key}` registrada en Telegram y enviada por DM a {usuario.mention} ({dias}d).",
            ephemeral=True,
        )
        asyncio.create_task(_log_key(
            "enviada_ok", key, usuario.id,
            dias=dias, metodo="Admin /enviar-key",
            admin_id=interaction.user.id,
        ))
    except discord.Forbidden:
        await interaction.followup.send(
            f"⚠️ Key `{key}` registrada en Telegram OK, pero no pude enviar DM a {usuario.mention} — tiene los DMs cerrados.\n"
            f"Entregala por otro medio ({dias}d).",
            ephemeral=True,
        )
        asyncio.create_task(_log_key(
            "dm_bloqueado", key, usuario.id,
            dias=dias, metodo="Admin /enviar-key",
            error="DM bloqueado — el usuario no acepta mensajes directos",
            admin_id=interaction.user.id,
        ))
    except Exception as exc:
        log.exception("Error enviando key manual a %s", usuario.id)
        await interaction.followup.send(f"❌ Error enviando DM: {exc}", ephemeral=True)
        asyncio.create_task(_log_key(
            "gen_error", key, usuario.id,
            dias=dias, metodo="Admin /enviar-key", error=str(exc),
            admin_id=interaction.user.id,
        ))


CANAL_SENSI_ID: int | None = None   # se rellena en on_ready al crear/encontrar el canal
CANAL_PROXY_ID   = 1486438612990951646

# Nombres exactos de las categorías gestionadas por el bot
_CAT_STORE       = "🛒 STORE"
_CAT_COMUNIDAD   = "💬 COMUNIDAD"
_CAT_INFORMACION = "ℹ️ INFORMACIÓN"


async def _obtener_o_crear_categoria(
    guild: discord.Guild,
    nombre: str,
) -> discord.CategoryChannel | None:
    """Devuelve la categoría con ese nombre; la crea si no existe."""
    cat = discord.utils.find(lambda c: c.name == nombre, guild.categories)
    if cat:
        return cat
    try:
        cat = await guild.create_category(nombre, reason="Setup automático — Marke Panel")
        log.info("Categoría '%s' creada (id=%s)", nombre, cat.id)
        return cat
    except Exception:
        log.exception("No pude crear categoría '%s'", nombre)
        return None


async def _reorganizar_categorias() -> None:
    """
    Crea las categorías STORE, COMUNIDAD e INFORMACIÓN si no existen,
    mueve los canales correspondientes, sube STORE al inicio y borra
    cualquier canal suelto llamado 'regedits'.
    """
    await client.wait_until_ready()
    guild = _resolver_guild()
    if guild is None:
        return

    cat_store     = await _obtener_o_crear_categoria(guild, _CAT_STORE)
    cat_comunidad = await _obtener_o_crear_categoria(guild, _CAT_COMUNIDAD)
    cat_info      = await _obtener_o_crear_categoria(guild, _CAT_INFORMACION)

    # ── Fragmentos de nombre por categoría ────────────────────────────────
    nombres_store = [
        "proxy-marke", "android-regedit", "ios-archivos",
        "flourite", "diamantes-ff", "sensis-xitadas",
    ]
    nombres_comunidad  = ["chat"]
    nombres_informacion = ["info-referido", "referido", "postulacion", "postulaciones"]

    async def _mover(ch: discord.TextChannel, cat: discord.CategoryChannel, tag: str):
        if ch.category_id == cat.id:
            return
        try:
            await ch.edit(category=cat, reason="Reorganización de categorías")
            log.info("_reorganizar_categorias: #%s → %s", ch.name, tag)
        except Exception as exc:
            log.warning("_reorganizar_categorias: no pude mover #%s: %s", ch.name, exc)

    # ── Borrar canal llamado exactamente "regedits" (no android-regedits) ──
    for ch in list(guild.text_channels):
        ch_plain = ch.name.lower().replace("・", "").replace("🎮", "").replace("📱", "").strip()
        if ch_plain == "regedits" and "android" not in ch.name.lower():
            try:
                await ch.delete(reason="Limpieza automática — canal regedits obsoleto")
                log.info("_reorganizar_categorias: canal #%s eliminado", ch.name)
            except Exception as exc:
                log.warning("_reorganizar_categorias: no pude borrar #%s: %s", ch.name, exc)

    # ── Subir categoría CONTADOR al inicio absoluto ───────────────────────
    cat_contador = discord.utils.find(
        lambda c: isinstance(c, discord.CategoryChannel) and "CONTADOR" in c.name.upper(),
        guild.channels,
    )
    if cat_contador:
        try:
            await cat_contador.edit(position=0, reason="Contador al inicio")
            log.info("_reorganizar_categorias: categoría '%s' movida al inicio", cat_contador.name)
        except Exception as exc:
            log.warning("_reorganizar_categorias: no pude reposicionar CONTADOR: %s", exc)

    if cat_store:
        for ch in guild.text_channels:
            if any(frag in ch.name.lower() for frag in nombres_store):
                await _mover(ch, cat_store, _CAT_STORE)
        # Subir la categoría STORE justo después del contador
        try:
            await cat_store.edit(position=1, reason="STORE al inicio")
            log.info("_reorganizar_categorias: categoría '%s' movida al inicio", _CAT_STORE)
        except Exception as exc:
            log.warning("_reorganizar_categorias: no pude reposicionar STORE: %s", exc)

    if cat_comunidad:
        for ch in guild.text_channels:
            ch_plain = ch.name.lower().replace("💬", "").replace("・", "").strip()
            if ch_plain in nombres_comunidad:
                await _mover(ch, cat_comunidad, _CAT_COMUNIDAD)

    if cat_info:
        for ch in guild.text_channels:
            if any(frag in ch.name.lower() for frag in nombres_informacion):
                await _mover(ch, cat_info, _CAT_INFORMACION)

    log.info("_reorganizar_categorias: listo")


PROXY_INFO_GIF   = Path("attached_assets/marke_proxy_banner.jpg")
PROXY_VIDEO      = Path("attached_assets/demostracion_proxy.mp4")
PROXY_SERVER     = "31.97.100.157"
PROXY_PORTS = """
```
Puerto  │ Modo
────────┼──────────────────
7071    │ Cuello
7072    │ Pecho
7073    │ Holo Armas

── Con Holo Avatar ─────────
7081    │ Cuello Holo
7082    │ Pecho Holo
```"""

async def _configurar_permisos_proxy() -> None:
    """Configura los permisos del canal #proxy-marke al iniciar.

    Reglas:
     - @everyone      → no puede ver el canal
     - Rol Verificado → puede ver, usar slash commands, leer historial; NO puede escribir texto
     - El bot         → puede hacer todo (enviar respuestas, borrar, etc.)
    """
    await client.wait_until_ready()
    channel = client.get_channel(CANAL_PROXY_ID)
    if not isinstance(channel, discord.TextChannel):
        log.warning("No encontré #proxy-marke para configurar permisos (id=%s)", CANAL_PROXY_ID)
        return

    guild = channel.guild
    rol_verificado = guild.get_role(ROL_VERIFICADO_ID)
    bot_member     = guild.get_member(client.user.id)

    try:
        # @everyone: no ve el canal
        await channel.set_permissions(
            guild.default_role,
            view_channel=False,
            send_messages=False,
        )
        # Verificados: ven el canal y usan slash commands.
        # send_messages=True es NECESARIO para que Discord muestre la barra
        # de mensajes en móvil (sin ella los slash commands no aparecen).
        # El texto libre se borra automáticamente vía on_message.
        if rol_verificado:
            await channel.set_permissions(
                rol_verificado,
                view_channel=True,
                send_messages=True,
                use_application_commands=True,
                read_message_history=True,
                create_public_threads=False,
                create_private_threads=False,
                send_messages_in_threads=False,
                attach_files=False,
                embed_links=False,
                add_reactions=False,
            )
        # El bot puede todo
        if bot_member:
            await channel.set_permissions(
                bot_member,
                view_channel=True,
                send_messages=True,
                manage_messages=True,
                read_message_history=True,
                use_application_commands=True,
                create_public_threads=True,
                send_messages_in_threads=True,
                attach_files=True,
                embed_links=True,
            )
        # Renombrar canal si no tiene el emoji 🛜
        if "🛜" not in channel.name:
            try:
                await channel.edit(name="🛜・proxy-marke")
            except Exception:
                log.warning("No pude renombrar #proxy-marke con emoji.")
        log.info("Permisos de #proxy-marke configurados (escritura OFF para todos, slash commands ON).")
    except discord.Forbidden:
        log.warning("Sin permisos para modificar #proxy-marke (necesito Manage Channels).")
    except Exception:
        log.exception("Error configurando permisos de #proxy-marke.")


# ---------------------------------------------------------------------------
# Canal INFO-REFERIDOS — embed informativo del sistema de afiliados
# ---------------------------------------------------------------------------
async def _setup_canal_info_referidos() -> None:
    """Crea/encuentra el canal #info-referidos y postea el embed del sistema de afiliados."""
    await client.wait_until_ready()

    guild = _resolver_guild()
    if guild is None:
        log.warning("No encontré el guild para setup de #info-referidos.")
        return

    # Buscar canal existente
    canal: discord.TextChannel | None = None
    for ch in guild.text_channels:
        if "info-referidos" in ch.name.lower() or "referidos" in ch.name.lower():
            canal = ch
            break

    # Crear si no existe
    if canal is None:
        categoria = await _obtener_o_crear_categoria(guild, _CAT_INFORMACION)
        try:
            canal = await guild.create_text_channel(
                "📢・info-referidos",
                category=categoria,
                topic="Sistema de Afiliados Sensi Marke — Ganá comisiones invitando amigos 💸",
                reason="Setup automático — Marke Panel",
            )
            log.info("Canal #info-referidos creado (id=%s)", canal.id)
        except Exception:
            log.exception("No pude crear #info-referidos.")
            return

    # Configurar permisos: todos pueden ver pero no escribir
    try:
        rol_verificado = guild.get_role(ROL_VERIFICADO_ID)
        bot_member = guild.get_member(client.user.id)
        await canal.set_permissions(guild.default_role, view_channel=True, send_messages=False)
        if rol_verificado:
            await canal.set_permissions(
                rol_verificado,
                view_channel=True,
                send_messages=False,
                read_message_history=True,
                add_reactions=False,
            )
        if bot_member:
            await canal.set_permissions(
                bot_member,
                view_channel=True,
                send_messages=True,
                manage_messages=True,
                read_message_history=True,
                embed_links=True,
            )
        log.info("Permisos de #info-referidos configurados.")
    except Exception:
        log.exception("Error configurando permisos de #info-referidos.")

    # Borrar embeds anteriores para refrescar con el nuevo texto
    async for msg in canal.history(limit=20):
        if msg.author == client.user and msg.embeds:
            try:
                await msg.delete()
            except Exception:
                pass

    embed = discord.Embed(
        title="💸 Sistema de Afiliados — Sensi Marke",
        description=(
            "**¡Solo invitá y ganás el 30%!** No necesitás hacer nada más.\n"
            "Cuando alguien entra al servidor usando **tu link de invitación de Discord**, "
            "queda vinculado a vos automáticamente.\n"
            "Cada vez que compre cualquier producto, **recibís comisión directo a tu saldo**. 🤖\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━"
        ),
        color=0x9B59B6,
    )
    embed.add_field(
        name="🚀 ¿Cómo funciona?",
        value=(
            "1️⃣ Creá un **link de invitación** de este servidor:\n"
            "   → Clic derecho en cualquier canal → **Invitar gente**\n"
            "   → O desde ⚙️ Configuración del servidor → **Invitaciones**\n\n"
            "2️⃣ Compartí ese link (redes, grupos, amigos, donde quieras).\n\n"
            "3️⃣ Cuando alguien entra usando **tu link**, el bot lo detecta solo y lo registra "
            "como tu referido. ✅ **No hay código ni comando que usar.**\n\n"
            "4️⃣ Cada compra que haga ese usuario te genera **30% de comisión** automática. 💰"
        ),
        inline=False,
    )
    embed.add_field(
        name="📊 Comisión",
        value=(
            "💰 **30% fijo** por cada venta concretada con tu link.\n"
            "Aplica a **todos los productos** del servidor.\n"
            "Sin límite de referidos — cuanto más invitás, más ganás."
        ),
        inline=False,
    )
    embed.add_field(
        name="💵 Formas de cobro",
        value=(
            "• **Transferencia bancaria** (CBU/Alias)\n"
            "• **Diamantes** del bot\n"
            "• **Archivos del bot** (regedits, sensis, etc.)\n\n"
            "_Cuando quieras cobrar, contactá a un admin con tu saldo. Usá `/perfil` para verlo._"
        ),
        inline=False,
    )
    embed.add_field(
        name="📋 Comandos útiles",
        value=(
            "`/invitar` — Cómo crear tu link y cómo funciona el sistema.\n"
            "`/perfil` — Ver tus referidos, ventas generadas y saldo de comisiones.\n"
            "`/referido` — Registrar manualmente a quien te invitó "
            "(solo si entraste sin usar el link de alguien)."
        ),
        inline=False,
    )
    embed.set_footer(text="Marke Panel • Sistema de Afiliados Automático — Sensi Marke")
    await canal.send(embed=embed)
    log.info("Embed de #info-referidos posteado.")


# ---------------------------------------------------------------------------
# Canal SENSIS XITADAS — setup y embed de ventas con IA
# ---------------------------------------------------------------------------
async def _setup_canal_sensis() -> None:
    """Crea (o encuentra) el canal '🎯・sensis-xitadas' y configura permisos."""
    global CANAL_SENSI_ID
    await client.wait_until_ready()

    if not GUILD_ID:
        return
    guild = client.get_guild(int(GUILD_ID))
    if guild is None:
        return

    # Buscar canal existente por nombre (sin emojis para mayor tolerancia)
    canal_obj: discord.TextChannel | None = None
    for ch in guild.text_channels:
        if "sensis-xitadas" in ch.name:
            canal_obj = ch
            break

    if canal_obj:
        CANAL_SENSI_ID = canal_obj.id
        log.info("Canal sensis-xitadas encontrado (id=%s)", CANAL_SENSI_ID)
    else:
        # Crear el canal en la categoría STORE
        categoria = await _obtener_o_crear_categoria(guild, _CAT_STORE)
        try:
            canal_obj = await guild.create_text_channel(
                "🎯・sensis-xitadas",
                category=categoria,
                topic="Obtén tu sensibilidad personalizada para Free Fire OB53 🔥 Costo: 0.1 crédito por consulta",
                reason="Setup automático — Marke Panel",
            )
            CANAL_SENSI_ID = canal_obj.id
            log.info("Canal sensis-xitadas creado (id=%s)", CANAL_SENSI_ID)
        except Exception:
            log.exception("No pude crear el canal sensis-xitadas.")
            return

    canal = canal_obj

    # Configurar permisos
    try:
        if not isinstance(canal, discord.TextChannel):
            return
        rol_verificado = guild.get_role(ROL_VERIFICADO_ID)
        bot_member     = guild.get_member(client.user.id)
        # @everyone: no ve el canal Y hilos bloqueados (importante para que
        # Discord no muestre "este es un canal solo para hilos").
        await canal.set_permissions(
            guild.default_role,
            view_channel=False,
            send_messages=False,
            create_public_threads=False,
            create_private_threads=False,
            send_messages_in_threads=False,
        )
        if rol_verificado:
            # send_messages=True es NECESARIO para que Discord muestre la barra
            # en móvil. El texto libre se borra vía on_message.
            await canal.set_permissions(
                rol_verificado,
                view_channel=True,
                send_messages=True,
                use_application_commands=True,
                read_message_history=True,
                create_public_threads=False,
                create_private_threads=False,
                send_messages_in_threads=False,
                attach_files=False,
                embed_links=False,
                add_reactions=False,
            )
        if bot_member:
            await canal.set_permissions(
                bot_member,
                view_channel=True,
                send_messages=True,
                manage_messages=True,
                manage_threads=True,
                read_message_history=True,
                use_application_commands=True,
                create_public_threads=True,
                send_messages_in_threads=True,
                attach_files=True,
                embed_links=True,
            )
        log.info("Permisos de sensis-xitadas configurados (slash ON, texto borrado por bot).")
    except Exception:
        log.exception("Error configurando permisos de sensis-xitadas.")

    # Borrar hilos existentes que se hayan creado por error
    if isinstance(canal, discord.TextChannel):
        borrados = 0
        try:
            for thread in list(canal.threads):
                try:
                    await thread.delete()
                    borrados += 1
                except Exception as e:
                    log.warning("No pude borrar hilo activo %s: %s", thread.id, e)
            try:
                async for thread in canal.archived_threads(limit=100):
                    try:
                        await thread.delete()
                        borrados += 1
                    except Exception as e:
                        log.warning("No pude borrar hilo archivado %s: %s",
                                    thread.id, e)
            except discord.Forbidden:
                pass
            if borrados:
                log.info("Borré %s hilo(s) en sensis-xitadas.", borrados)
        except Exception:
            log.exception("Error limpiando hilos de sensis-xitadas.")

    # Postear embed de ventas si no existe (pasamos el objeto para evitar problemas de caché)
    await _postear_embed_sensis(canal)


_SENSI_PROMO_PROMPT = (
    "Escribí un texto de ventas corto y atractivo en español argentino para el canal de Discord "
    "'SENSIS XITADAS' de Marke. Este canal ofrece sensibilidades personalizadas de Free Fire "
    "para la actualización OB53, generadas por IA según el modelo del celular del usuario. "
    "El costo es de $1.000 ARS por consulta (0.1 crédito). El nombre del servicio es 'Sensi Xitada'.\n\n"
    "El texto debe:\n"
    "- Tener máximo 600 caracteres total\n"
    "- Explicar brevemente qué es y por qué es útil\n"
    "- Mencionar que incluye 3 perfiles: Alta, Media y Baja sensibilidad\n"
    "- Mencionar que sirve para OB53 y que es personalizada para tu celular\n"
    "- Mencionar EXPLÍCITAMENTE que funciona para Android Y iPhone (iOS)\n"
    "- Mencionar que ahora INCLUYE ajustes in-game de Free Fire + optimización del sistema operativo "
    "(Game Booster Samsung, Game Turbo Xiaomi, Modo Concentración iOS, etc.) específicos para tu celular\n"
    "- Mencionar que la sensi se entrega por mensaje privado (DM)\n"
    "- Tener emojis relevantes (🎯🔥📱💥🤖🍎📩⚙️)\n"
    "- Terminar con un call to action: usá /sensibilidad con el modelo de tu celu\n"
    "- NO incluir precios, solo describir el servicio"
)


async def _postear_embed_sensis(canal: discord.TextChannel | None = None) -> None:
    """Postea (o actualiza) el embed de presentación del canal Sensis Xitadas."""
    if canal is None:
        if not CANAL_SENSI_ID:
            return
        canal = client.get_channel(CANAL_SENSI_ID)  # type: ignore[assignment]
    if not isinstance(canal, discord.TextChannel):
        log.warning("_postear_embed_sensis: canal no es TextChannel (%r)", canal)
        return

    # Buscar todos los embeds de sensis del bot y limpiar duplicados
    sensi_msgs: list[discord.Message] = []
    async for msg in canal.history(limit=50):
        if msg.author == client.user and msg.embeds:
            for e in msg.embeds:
                if e.title and "sensi" in e.title.lower():
                    sensi_msgs.append(msg)
                    break

    if sensi_msgs:
        # Verificar si el embed más reciente está actualizado (menciona iPhone/iOS)
        ultimo = sensi_msgs[0]
        desc_actual = " ".join(
            (e.description or "") for e in ultimo.embeds
        ).lower()
        # Tiene que mencionar iPhone Y la nueva entrega por DM Y la optimización del SO
        menciona_iphone = ("iphone" in desc_actual) or ("ios" in desc_actual) or ("apple" in desc_actual)
        menciona_dm = ("dm" in desc_actual) or ("privado" in desc_actual) or ("mensaje" in desc_actual and "directo" in desc_actual)
        menciona_optim = ("optimiz" in desc_actual) or ("game booster" in desc_actual) or ("game turbo" in desc_actual) or ("ajuste" in desc_actual)
        esta_actualizado = menciona_iphone and menciona_dm and menciona_optim

        if esta_actualizado:
            # Limpieza estándar: borrar duplicados, conservar el más reciente
            for dup in sensi_msgs[1:]:
                try:
                    await dup.delete()
                    log.info("Embed duplicado de sensis borrado (id=%s).", dup.id)
                except Exception:
                    log.exception("No pude borrar embed duplicado de sensis.")
            log.info("Embed sensis-xitadas ya existe y está actualizado, no lo repito.")
            return
        else:
            # Embed obsoleto → borrar TODOS y postear uno nuevo abajo
            for old in sensi_msgs:
                try:
                    await old.delete()
                    log.info("Embed obsoleto de sensis borrado (id=%s).", old.id)
                except Exception:
                    log.exception("No pude borrar embed obsoleto de sensis.")
            log.info("Embed sensis-xitadas estaba desactualizado, posteando uno nuevo con iPhone.")

    # Generar descripción con IA
    try:
        resp = await _openai_client.chat.completions.create(
            model="gpt-5-mini",
            max_completion_tokens=400,
            messages=[
                {"role": "system", "content": "Sos un redactor de marketing para un servidor de Discord gamer argentino. Escribís textos cortos, directos y con energía."},
                {"role": "user",   "content": _SENSI_PROMO_PROMPT},
            ],
        )
        descripcion_ia = resp.choices[0].message.content or ""
    except Exception:
        log.exception("No pude generar el embed de sensis con IA, uso texto de respaldo.")
        descripcion_ia = (
            "🎯 **¿Querés jugar con la sensi perfecta para tu celu?**\n\n"
            "La **Sensi Xitada** es una configuración personalizada de Free Fire OB53 "
            "generada por IA según las características reales de tu dispositivo.\n\n"
            "📱 Funciona para cualquier celular del mundo.\n"
            "🔥 Incluye 3 perfiles: **Alta**, **Media** y **Baja** sensibilidad.\n"
            "💥 DPI, botón de disparo y todos los valores de sensibilidad optimizados.\n\n"
            "👉 Usá `/sensibilidad` con el modelo de tu celular y recibís tu config al instante."
        )

    embed = discord.Embed(
        title="🎯 SENSIS XITADAS — Free Fire OB53",
        description=descripcion_ia,
        color=0xFF4500,
    )
    embed.add_field(
        name="💰 Costo por consulta",
        value="**0.1 crédito** = $1.000 ARS\n_(comprá créditos con `/comprar`)_",
        inline=True,
    )
    embed.add_field(
        name="📋 Qué incluye",
        value="• 3 perfiles (Alta / Media / Baja)\n• DPI + botón de disparo\n• Todos los valores de sensibilidad",
        inline=True,
    )
    embed.set_footer(text="Marke Panel • Sensibilidades actualizadas para la ultima OB53")
    await canal.send(embed=embed)
    log.info("Embed de sensis-xitadas posteado.")


def _resolver_guild() -> discord.Guild | None:
    """Resuelve el guild usando GUILD_ID o fallback por canales conocidos."""
    guild: discord.Guild | None = None
    if GUILD_ID:
        guild = client.get_guild(int(GUILD_ID))
    if guild is None:
        for known_id in (CANAL_PROXY_ID, CANAL_VERIFICACION_ID):
            ch = client.get_channel(known_id)
            if ch and hasattr(ch, "guild"):
                guild = ch.guild  # type: ignore[assignment]
                break
    if guild is None and client.guilds:
        guild = client.guilds[0]
    return guild


async def _setup_canal_con_catalogo(
    nombre_buscar: str,
    nombre_crear: str,
    topic: str,
    categoria_pack: str,
    titulo_embed: str,
    descripcion_embed: str,
    color_embed: int,
    titulo_campo: str,
    emoji_pack: str,
    cta_extra: str,
    log_tag: str,
) -> None:
    """Función genérica: crea/encuentra un canal y postea su catálogo."""
    await client.wait_until_ready()

    guild = _resolver_guild()
    if guild is None:
        log.warning("No encontré el guild para setup de canal %s", log_tag)
        return

    canal_obj: discord.TextChannel | None = None
    for ch in guild.text_channels:
        if nombre_buscar in ch.name.lower():
            canal_obj = ch
            break

    if canal_obj:
        log.info("Canal %s encontrado: %s (id=%s)", log_tag, canal_obj.name, canal_obj.id)
    else:
        categoria = await _obtener_o_crear_categoria(guild, _CAT_STORE)
        try:
            canal_obj = await guild.create_text_channel(
                nombre_crear,
                category=categoria,
                topic=topic,
                reason="Setup automático — Marke Panel",
            )
            log.info("Canal %s creado (id=%s)", log_tag, canal_obj.id)
        except Exception:
            log.exception("No pude crear el canal %s.", log_tag)
            return

    canal = canal_obj

    # Configurar permisos
    try:
        rol_verificado = guild.get_role(ROL_VERIFICADO_ID)
        bot_member = guild.get_member(client.user.id)
        await canal.set_permissions(guild.default_role, view_channel=False, send_messages=False)
        if rol_verificado:
            # send_messages=True es NECESARIO para que Discord muestre la barra
            # en móvil. El texto libre se borra vía on_message.
            await canal.set_permissions(
                rol_verificado,
                view_channel=True,
                send_messages=True,
                use_application_commands=True,
                read_message_history=True,
                create_public_threads=False,
                create_private_threads=False,
                send_messages_in_threads=False,
                attach_files=False,
                embed_links=False,
                add_reactions=False,
            )
        if bot_member:
            await canal.set_permissions(
                bot_member,
                view_channel=True,
                send_messages=True,
                manage_messages=True,
                read_message_history=True,
                use_application_commands=True,
                embed_links=True,
                attach_files=True,
            )
        log.info("Permisos de #%s configurados (slash ON, texto borrado por bot).", canal.name)
    except Exception:
        log.exception("Error configurando permisos de #%s.", canal.name)

    # Verificar si ya hay catálogo actualizado
    async for msg in canal.history(limit=20):
        if msg.author == client.user and msg.embeds:
            for e in msg.embeds:
                if e.title and titulo_embed in e.title:
                    log.info("Catálogo %s ya existe, no lo repito.", log_tag)
                    return

    packs = [p for p in payments.PACKS.values() if p.categoria == categoria_pack]
    pack_lines = "\n".join(f"{emoji_pack} **{p.nombre}** — ${p.precio:,.0f} ARS" for p in packs)

    embed = discord.Embed(title=titulo_embed, description=descripcion_embed, color=color_embed)
    embed.add_field(name=titulo_campo, value=pack_lines or "_(próximamente)_", inline=False)
    embed.add_field(
        name="━━━━━━━━━━━━━━━━━━━━━━━━",
        value=f"🛒 Usá `/comprar` para adquirir cualquier pack.\n{cta_extra}",
        inline=False,
    )
    embed.set_footer(text="Marke Panel • Entrega automática al confirmar el pago")
    await canal.send(embed=embed)
    log.info("Catálogo %s posteado en #%s.", log_tag, canal.name)


async def _setup_canal_android() -> None:
    """Crea/encuentra el canal Android y postea su catálogo."""
    await _setup_canal_con_catalogo(
        nombre_buscar="android",
        nombre_crear="🤖・android-regedits",
        topic="Regedits para Android — Descarga + Tutorial automático por DM 🤖",
        categoria_pack="android",
        titulo_embed="🤖 Regedits Android — Sensi Marke",
        descripcion_embed=(
            "Los mejores regedits para **Free Fire Android**.\n"
            "Al pagar recibís el link de descarga y el tutorial **automáticamente por DM**. 📥\n\n"
            f"{NOTA_SOPORTE}\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━"
        ),
        color_embed=0x2ECC71,
        titulo_campo="🤖 Packs disponibles — Android",
        emoji_pack="🤖",
        cta_extra="💬 ¿Dudas? Abrí un ticket con `/ticket`.",
        log_tag="android",
    )


async def _setup_canal_ios() -> None:
    """Crea/encuentra el canal iOS y postea su catálogo."""
    await _setup_canal_con_catalogo(
        nombre_buscar="ios-",
        nombre_crear="🍎・ios-archivos",
        topic="Archivos para iOS — Link de WhatsApp automático por DM 🍎",
        categoria_pack="ios",
        titulo_embed="🍎 Archivos iOS — Sensi Marke",
        descripcion_embed=(
            "Los mejores archivos para **Free Fire iOS**.\n"
            "Al pagar recibís el link de WhatsApp con Markee **automáticamente por DM**. 📱\n\n"
            f"{NOTA_SOPORTE}\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━"
        ),
        color_embed=0x3498DB,
        titulo_campo="🍎 Packs disponibles — iOS",
        emoji_pack="🍎",
        cta_extra="💬 ¿Dudas? Abrí un ticket con `/ticket`.",
        log_tag="ios",
    )


async def _setup_canal_flourite() -> None:
    """Crea/encuentra el canal Flourite (iOS) y postea su info con botón de ticket."""
    await client.wait_until_ready()
    guild = _resolver_guild()
    if guild is None:
        log.warning("No encontré el guild para setup de canal flourite")
        return

    canal: discord.TextChannel | None = None
    for ch in guild.text_channels:
        if "flourite" in ch.name.lower():
            canal = ch
            break

    if canal is None:
        categoria = await _obtener_o_crear_categoria(guild, _CAT_STORE)
        try:
            canal = await guild.create_text_channel(
                "🔮・flourite-ios",
                category=categoria,
                topic="Flourite iOS — El hack definitivo para Free Fire iOS 🍎🔮",
                reason="Setup automático — Marke Panel",
            )
            log.info("Canal flourite creado (id=%s)", canal.id)
        except Exception:
            log.exception("No pude crear el canal flourite.")
            return
    else:
        log.info("Canal flourite encontrado: %s (id=%s)", canal.name, canal.id)

    try:
        rol_verificado = guild.get_role(ROL_VERIFICADO_ID)
        bot_member = guild.get_member(client.user.id)
        await canal.set_permissions(guild.default_role, view_channel=False, send_messages=False)
        if rol_verificado:
            await canal.set_permissions(
                rol_verificado,
                view_channel=True,
                send_messages=True,
                use_application_commands=True,
                read_message_history=True,
                create_public_threads=False,
                create_private_threads=False,
                send_messages_in_threads=False,
                attach_files=False,
                embed_links=False,
                add_reactions=False,
            )
        if bot_member:
            await canal.set_permissions(
                bot_member,
                view_channel=True,
                send_messages=True,
                manage_messages=True,
                read_message_history=True,
                embed_links=True,
                attach_files=True,
            )
        log.info("Permisos de #%s configurados.", canal.name)
    except Exception:
        log.exception("Error configurando permisos de #flourite.")

    _FLOURITE_VERSION = "flourite-v3-sinJB"
    async for msg in canal.history(limit=20):
        if msg.author == client.user and msg.embeds:
            for e in msg.embeds:
                if e.title and "Flourite" in e.title:
                    footer_text = e.footer.text if e.footer else ""
                    if _FLOURITE_VERSION in (footer_text or ""):
                        log.info("Catálogo Flourite ya está actualizado, no lo repito.")
                        return
                    try:
                        await msg.delete()
                        log.info("Embed Flourite antiguo eliminado para repostear.")
                    except Exception:
                        pass

    embed = discord.Embed(
        title="🔮 Flourite iOS — Panel Free Fire",
        description=(
            "**Flourite** es el panel más completo para **Free Fire en iOS**.\n"
            "Todo rojo con **aimbot** incluido y funciones premium que le dan una ventaja "
            "total en el juego. **No necesita Jailbreak.** 📱✅\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "**🎯 Funciones de Aim**\n"
            "• **Aim** — Apuntado automático\n"
            "• **Silent Aim** — Dispara sin mover la mira\n"
            "• **Vectored** — Dirección de proyectil controlada\n"
            "• **HEAD** — Prioridad cabeza\n"
            "• **NECK** — Prioridad cuello\n\n"
            "**👁️ Visuales / ESP**\n"
            "• **Chams** — Enemigos iluminados a través de paredes\n"
            "• **Líneas** — Línea de dirección a enemigos\n"
            "• **ESP** — Info de enemigos en pantalla\n"
            "• **Box** — Caja alrededor de los jugadores\n"
            "• **HP** — Vida de los enemigos visible\n\n"
            "**⚡ Extras**\n"
            "• **Pared invertida** — Invertí la colisión de muros\n"
            "• **Unlock 120 FPS** — Máximo rendimiento\n"
            "• **Fast Shoot** — Disparo ultra rápido\n"
            "• Y muchas opciones más dentro del panel\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━"
        ),
        color=0x9B59B6,
    )
    embed.add_field(
        name="🗝️ Precio — Key mensual",
        value="**$36,000 ARS** / mes",
        inline=False,
    )
    embed.add_field(
        name="📦 ¿Qué incluye?",
        value=(
            "**Solo la KEY** del panel.\n"
            "El certificado para instalar la IPA **no está incluido** — "
            "es necesario para poder instalar la aplicación en tu dispositivo.\n"
            "Consultá disponibilidad de certificado por ticket."
        ),
        inline=False,
    )
    embed.add_field(
        name="━━━━━━━━━━━━━━━━━━━━━━━━",
        value="📩 Tocá el botón para abrir un ticket y coordinar tu compra.",
        inline=False,
    )
    embed.set_footer(text=f"Marke Panel • @markee.4 • {_FLOURITE_VERSION}")
    await canal.send(embed=embed, view=FlouriteCanalView())
    log.info("Catálogo Flourite posteado en #%s.", canal.name)


async def _setup_canal_diamantes() -> None:
    """Crea/encuentra el canal Diamantes y postea su catálogo con botón de ticket."""
    await client.wait_until_ready()
    guild = _resolver_guild()
    if guild is None:
        log.warning("No encontré el guild para setup de canal diamantes")
        return

    canal: discord.TextChannel | None = None
    for ch in guild.text_channels:
        if "diamante" in ch.name.lower():
            canal = ch
            break

    if canal is None:
        categoria = await _obtener_o_crear_categoria(guild, _CAT_STORE)
        try:
            canal = await guild.create_text_channel(
                "💎・diamantes-ff",
                category=categoria,
                topic="Diamantes Free Fire — Precios imbatibles 💎",
                reason="Setup automático — Marke Panel",
            )
            log.info("Canal diamantes creado (id=%s)", canal.id)
        except Exception:
            log.exception("No pude crear el canal diamantes.")
            return
    else:
        log.info("Canal diamantes encontrado: %s (id=%s)", canal.name, canal.id)

    try:
        rol_verificado = guild.get_role(ROL_VERIFICADO_ID)
        bot_member = guild.get_member(client.user.id)
        await canal.set_permissions(guild.default_role, view_channel=False, send_messages=False)
        if rol_verificado:
            await canal.set_permissions(
                rol_verificado,
                view_channel=True,
                send_messages=True,
                use_application_commands=True,
                read_message_history=True,
                create_public_threads=False,
                create_private_threads=False,
                send_messages_in_threads=False,
                attach_files=False,
                embed_links=False,
                add_reactions=False,
            )
        if bot_member:
            await canal.set_permissions(
                bot_member,
                view_channel=True,
                send_messages=True,
                manage_messages=True,
                read_message_history=True,
                embed_links=True,
                attach_files=True,
            )
        log.info("Permisos de #%s configurados.", canal.name)
    except Exception:
        log.exception("Error configurando permisos de #diamantes.")

    _VERSION_DIAMANTES = "v_binance_v1"   # marcador de versión — cambiar para forzar reposteo
    async for msg in canal.history(limit=20):
        if msg.author == client.user and msg.embeds:
            for e in msg.embeds:
                if e.title and "Diamante" in e.title:
                    footer_text = e.footer.text if e.footer else ""
                    if _VERSION_DIAMANTES in footer_text:
                        log.info("Catálogo Diamantes ya está actualizado, no lo repito.")
                        return
                    try:
                        await msg.delete()
                        log.info("Catálogo Diamantes viejo eliminado para repostear.")
                    except Exception:
                        pass

    embed = discord.Embed(
        title="💎 Diamantes Argentina Ilimitados — Free Fire",
        description=(
            "Cargá diamantes en tu cuenta de **Free Fire** al precio más bajo.\n\n"
            "🥷 **Sin límite de recargas y sin restricciones**\n"
            "Podés cargar todas las veces que quieras. 🚀\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "**💎 Paquetes disponibles — Elegí el tuyo:**"
        ),
        color=0x1ABC9C,
    )
    embed.add_field(name="💎 110 Diamantes",   value="**$1.450 ARS**\n*(100 + 10 bono)*",   inline=True)
    embed.add_field(name="💎 341 Diamantes",   value="**$4.250 ARS**\n*(310 + 31 bono)*",   inline=True)
    embed.add_field(name="💎 572 Diamantes",   value="**$7.100 ARS**\n*(520 + 52 bono)*",   inline=True)
    embed.add_field(name="💎 1.166 Diamantes", value="**$14.000 ARS**\n*(1060 + 106 bono)*", inline=True)
    embed.add_field(name="💎 2.398 Diamantes", value="**$25.700 ARS**\n*(2180 + 218 bono)*", inline=True)
    embed.add_field(name="💎 6.160 Diamantes", value="**$64.300 ARS**\n*(5600 + 560 bono)*", inline=True)
    embed.add_field(
        name="━━━━━━━━━━━━━━━━━━━━━━━━",
        value=(
            "✅ Entrega rápida y segura\n"
            "🏦 Pagá por **Transferencia Bancaria** o **Binance Pay**.\n"
            "👇 Tocá el botón de tu paquete para empezar."
        ),
        inline=False,
    )
    embed.set_footer(text=f"Marke Panel • @markee.4 — Diamantes Argentina | {_VERSION_DIAMANTES}")
    await canal.send(embed=embed, view=DiamantesCanalView())
    log.info("Catálogo Diamantes (ARS) posteado en #%s.", canal.name)


# ---------------------------------------------------------------------------
# View persistente del canal proxy-marke: botones Comprar y FREE
# ---------------------------------------------------------------------------
class ProxyInfoView(_SafeViewMixin, discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="🛒 Comprar",
        style=discord.ButtonStyle.blurple,
        custom_id="proxy_info_comprar",
    )
    async def btn_comprar(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _safe_defer(interaction, ephemeral=True, thinking=True)
        desc = (
            "Elegí el plan de proxy que querés comprar.\n"
            "Tu key llegará por DM al instante una vez confirmado el pago. 🔑"
            + (f"\n\n[TUTORIAL AQUÍ]({PROXY_TUTORIAL_URL})" if PROXY_TUTORIAL_URL else "")
        )
        embed = discord.Embed(title="Tienda de keys Sensi Marke", description=desc, color=0xF1C40F)
        await interaction.followup.send(embed=embed, view=PackView(modo="proxy"), ephemeral=True)

    @discord.ui.button(
        label="🆓 FREE",
        style=discord.ButtonStyle.green,
        custom_id="proxy_info_free",
    )
    async def btn_free(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _safe_defer(interaction, ephemeral=True, thinking=True)

        es_admin = _puede_registrar(interaction)

        if not es_admin:
            expiry_str = database.get_config("gratismarke_expiry")
            if not expiry_str or time.time() > float(expiry_str):
                await interaction.followup.send(
                    "⏰ La promo gratuita no está activa en este momento.\n"
                    "Cuando se active una nueva promo te avisamos por el servidor.",
                    ephemeral=True,
                )
                return

        discord_id = str(interaction.user.id)
        if not es_admin and database.has_used_free_trial(discord_id):
            await interaction.followup.send(
                "Ya usaste tu key gratuita. Para seguir usando el proxy, usá `/comprar`.",
                ephemeral=True,
            )
            return

        if not telegram_client.is_ready():
            await interaction.followup.send(
                "❌ El sistema de generación de keys no está disponible ahora. Intentá en unos minutos.",
                ephemeral=True,
            )
            return

        key = _generar_key_proxy()
        try:
            await telegram_client.cmd_gen(key, 3)
            log.info("🔑 KEY GRATIS — key=%s dias=3 usuario=%s (%s)", key, interaction.user, interaction.user.id)
            asyncio.create_task(_log_key(
                "gen_ok", key, interaction.user.id,
                dias=3, metodo="Key gratis (prueba)",
            ))
        except Exception as exc:
            log.exception("Error generando key gratuita via Telegram para %s", discord_id)
            asyncio.create_task(_log_key(
                "gen_error", key, interaction.user.id,
                dias=3, metodo="Key gratis (prueba)", error=str(exc),
            ))
            await interaction.followup.send(
                f"❌ No se pudo generar tu key en este momento. Intentá de nuevo.\n```{exc}```",
                ephemeral=True,
            )
            return

        database.mark_free_trial_used(discord_id)

        embed_key = discord.Embed(
            title="🎁 Tu key gratuita de 3 días",
            description=(
                f"¡Listo! Tu key de prueba por **3 días** está activa.\n\n"
                f"🔑 **Tu key:**\n```\n{key}\n```\n"
                f"Usá el comando `/key` en el servidor para activarla con tu IP.\n\n"
                f"🌐 **Servidor:** `108.181.215.247`\n"
                f"👔 **Puerto Cuello:** `10065`\n"
                f"👕 **Puerto Pecho:** `10066`\n"
                f"👤 **Login:** ||DGZADAXFF||\n"
                f"🔒 **Contraseña:** ||DGZADAXFF||\n\n"
                + (f"▶️ **Tutorial de configuración:**\n{PROXY_TUTORIAL_URL}\n\n" if PROXY_TUTORIAL_URL else "")
                + "📲 **Grupo de WhatsApp:**\nhttps://chat.whatsapp.com/DQxndyWBG860vpaVcxam3s"
            ),
            color=0xF1C40F,
        )
        embed_key.set_footer(text="Esta prueba solo se puede usar una vez por cuenta.")
        try:
            await interaction.user.send(embed=embed_key)
            await interaction.followup.send(
                "✅ Te mandé tu key por mensaje privado. Usá `/key` para activarla.",
                ephemeral=True,
            )
        except discord.Forbidden:
            await interaction.followup.send(embed=embed_key, ephemeral=True)


class ProxyInfoViewSinFree(_SafeViewMixin, discord.ui.View):
    """View del canal proxy-marke SIN el botón FREE (post-promo)."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="🛒 Comprar",
        style=discord.ButtonStyle.blurple,
        custom_id="proxy_info_comprar",
    )
    async def btn_comprar(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _safe_defer(interaction, ephemeral=True, thinking=True)
        desc = (
            "Elegí el plan de proxy que querés comprar.\n"
            "Tu key llegará por DM al instante una vez confirmado el pago. 🔑"
            + (f"\n\n[TUTORIAL AQUÍ]({PROXY_TUTORIAL_URL})" if PROXY_TUTORIAL_URL else "")
        )
        embed = discord.Embed(title="Tienda de keys Sensi Marke", description=desc, color=0xF1C40F)
        await interaction.followup.send(embed=embed, view=PackView(modo="proxy"), ephemeral=True)


async def _deshabilitar_gratis_en_1h():
    """Espera 1 hora y luego: expira la promo, quita botón FREE del embed y borra /gratismarke."""
    await client.wait_until_ready()
    # Si la promo ya fue expirada manualmente (expiry=0), ejecutar sin esperar
    expiry_actual = database.get_config("gratismarke_expiry")
    if expiry_actual != "0":
        await asyncio.sleep(3600)
    # Si ya se ejecutó antes (flag), no repetir
    if database.get_config("gratismarke_disabled") == "1":
        return

    # 1. Expirar la promo en la DB
    database.set_config("gratismarke_expiry", "0")
    log.info("⏰ Promo /gratismarke expirada por temporizador de 1h.")

    # 2. Editar el mensaje del canal proxy-marke: quitar botón FREE y mención del comando
    channel = client.get_channel(CANAL_PROXY_ID)
    if channel:
        async for msg in channel.history(limit=30):
            if msg.author == client.user and msg.embeds:
                for e in msg.embeds:
                    if e.title and "Conexión" in e.title:
                        nueva_desc = (e.description or "").replace(
                            "🆓 `/gratismarke` — Key de prueba 3 días gratis (una sola vez, cuando hay promo activa)\n",
                            "",
                        )
                        nuevo_embed = discord.Embed(
                            title=e.title,
                            description=nueva_desc,
                            color=e.color,
                        )
                        if e.footer:
                            nuevo_embed.set_footer(text=e.footer.text)
                        if e.image:
                            nuevo_embed.set_image(url=e.image.url)
                        try:
                            await msg.edit(embed=nuevo_embed, view=ProxyInfoViewSinFree())
                            log.info("✅ Botón FREE eliminado del embed de #proxy-marke.")
                        except Exception:
                            log.exception("No pude editar el embed de proxy-marke para quitar FREE.")
                        break

    # 3. Eliminar /gratismarke del árbol de comandos y re-sincronizar con Discord
    try:
        tree.remove_command("gratismarke")
        guild = _resolver_guild()
        if guild:
            await tree.sync(guild=guild)
        log.info("✅ Comando /gratismarke eliminado y árbol re-sincronizado.")
    except Exception:
        log.exception("No pude eliminar /gratismarke del árbol de comandos.")

    # Marcar como ejecutado para no repetir en futuros reinicios
    database.set_config("gratismarke_disabled", "1")


async def _postear_info_proxy():
    """Postea (una vez) el mensaje de info de conexión en #proxy-marke."""
    await client.wait_until_ready()
    channel = client.get_channel(CANAL_PROXY_ID)
    if channel is None:
        log.warning("No encontré el canal proxy-marke (id=%s)", CANAL_PROXY_ID)
        return

    # Si ya existe un mensaje del bot con el título correcto y el banner nuevo, no lo repite
    async for msg in channel.history(limit=30):
        if msg.author == client.user and msg.embeds:
            for e in msg.embeds:
                if e.title and "Conexión" in e.title:
                    desc = e.description or ""
                    footer_txt = (e.footer.text or "") if e.footer else ""
                    # Repostear si tiene formato viejo O si aún no tiene los botones
                    es_viejo = (
                        "Servidor" in desc
                        or "Puertos" in desc
                        or "Conectate al proxy" in desc
                        or "btn_v3" not in footer_txt
                    )
                    if es_viejo:
                        try:
                            await msg.delete()
                            log.info("Embed de info proxy borrado para repostear (con botones).")
                        except Exception:
                            pass
                        break
                    # Ya está actualizado con botones → no repostear
                    log.info("Mensaje de info proxy ya existe con botones, no lo repito.")
                    return

    embed = discord.Embed(
        title="🌐  Información de Conexión — Marke Panel",
        description=(
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            + (
                f"**▶️  Tutorial de configuración**\n"
                f"{PROXY_TUTORIAL_URL}\n\n"
                if PROXY_TUTORIAL_URL else ""
            )
            + f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"**📋  Comandos disponibles**\n\n"
            f"🆓 `/gratismarke` — Key de prueba 3 días gratis (una sola vez, cuando hay promo activa)\n"
            f"🛒 `/comprar` — Comprá tu plan de proxy\n"
            f"🔑 `/key` — Activá tu key con tu IP\n"
            f"📜 `/historial` — Ver tus últimas compras\n"
            f"🌍 `/mi-ip` — Ver tu IP actual (enlace a ipleak.net)\n"
            f"📄 `/certificado` — Descargar el certificado HTTPS\n"
            f"\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📋 El tutorial de configuración se envía por DM al comprar."
            f"\n━━━━━━━━━━━━━━━━━━━━━━━━"
        ),
        color=0x9B59B6,
    )
    embed.set_footer(text="Marke Panel • Soporte disponible en canales de voz [btn_v3]")

    files = []
    if PROXY_INFO_GIF.exists():
        files.append(discord.File(str(PROXY_INFO_GIF), filename="marke_proxy_banner.jpg"))
        embed.set_image(url="attachment://marke_proxy_banner.jpg")
    await channel.send(embed=embed, view=ProxyInfoView(), files=files if files else discord.utils.MISSING)

    log.info("Mensaje de info proxy posteado en #proxy-marke.")


async def _purgar_canal_proxy() -> None:
    """Borra todos los mensajes de #proxy-marke excepto el embed de info del bot."""
    await client.wait_until_ready()
    # Esperar a que el embed ya esté posteado
    await asyncio.sleep(5)
    channel = client.get_channel(CANAL_PROXY_ID)
    if not isinstance(channel, discord.TextChannel):
        return

    def _es_embed_bot(msg: discord.Message) -> bool:
        """Retorna True si el mensaje es el embed de info del bot (conservar)."""
        if msg.author != client.user:
            return False
        for e in msg.embeds:
            if e.title and "Conexión" in e.title:
                return True
        return False

    borrados = 0
    try:
        # channel.purge() solo funciona con mensajes de los últimos 14 días
        deleted = await channel.purge(
            limit=500,
            check=lambda m: not _es_embed_bot(m),
            bulk=True,
        )
        borrados = len(deleted)
    except discord.Forbidden:
        log.warning("Sin permiso para purgar #proxy-marke (necesito Manage Messages).")
        return
    except Exception:
        log.exception("Error purgando #proxy-marke.")
        return

    # Para mensajes > 14 días que purge() no puede borrar en bulk, borrarlos uno a uno
    try:
        async for msg in channel.history(limit=200):
            if not _es_embed_bot(msg):
                try:
                    await msg.delete()
                    borrados += 1
                    await asyncio.sleep(0.5)  # evitar rate limit
                except Exception:
                    pass
    except Exception:
        pass

    if borrados:
        log.info("Purga de #proxy-marke: %d mensaje(s) eliminado(s).", borrados)
    else:
        log.info("Purga de #proxy-marke: canal ya estaba limpio.")


async def _configurar_canal_verificacion() -> None:
    """Configura el canal de verificación:
    - @everyone puede VER el canal y el botón pero NO puede escribir ni crear hilos.
    - El bot puede gestionar todo.
    - Borra todos los hilos (activos y archivados) que existan.
    """
    await client.wait_until_ready()
    channel = client.get_channel(CANAL_VERIFICACION_ID)
    if not isinstance(channel, discord.TextChannel):
        log.warning("No encontré el canal de verificación (id=%s)",
                    CANAL_VERIFICACION_ID)
        return

    guild = channel.guild
    bot_member = guild.me

    # 1) Configurar permisos
    try:
        await channel.set_permissions(
            guild.default_role,
            view_channel=True,
            send_messages=False,
            create_public_threads=False,
            create_private_threads=False,
            send_messages_in_threads=False,
            add_reactions=False,
            attach_files=False,
            embed_links=False,
        )
        if bot_member:
            await channel.set_permissions(
                bot_member,
                view_channel=True,
                send_messages=True,
                manage_messages=True,
                manage_threads=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
            )
        log.info("Permisos de #verificacion configurados (sin hilos, sin texto).")
    except discord.Forbidden:
        log.warning("Sin permisos para modificar #verificacion (necesito Manage Channels).")
    except Exception:
        log.exception("Error configurando permisos de #verificacion.")

    # 2) Borrar todos los hilos existentes
    borrados = 0
    try:
        # Hilos activos
        for thread in list(channel.threads):
            try:
                await thread.delete()
                borrados += 1
            except Exception as e:
                log.warning("No pude borrar hilo activo %s: %s", thread.id, e)
        # Hilos archivados (públicos)
        try:
            async for thread in channel.archived_threads(limit=100):
                try:
                    await thread.delete()
                    borrados += 1
                except Exception as e:
                    log.warning("No pude borrar hilo archivado %s: %s",
                                thread.id, e)
        except discord.Forbidden:
            pass
        # Hilos archivados privados
        try:
            async for thread in channel.archived_threads(
                limit=100, private=True
            ):
                try:
                    await thread.delete()
                    borrados += 1
                except Exception as e:
                    log.warning("No pude borrar hilo privado %s: %s",
                                thread.id, e)
        except discord.Forbidden:
            pass
        if borrados:
            log.info("Borré %s hilo(s) en #verificacion.", borrados)
    except Exception:
        log.exception("Error limpiando hilos de #verificacion.")


async def _configurar_canal_ventas() -> None:
    """Canal #💰・ventas: SOLO owners (Owner del servidor + rol Admin) pueden verlo.
    @everyone bloqueado. El bot necesita acceso para publicar las ventas.
    """
    await client.wait_until_ready()
    canal = await _obtener_canal_ventas()
    if canal is None:
        log.info("No encontré canal #ventas — nada que configurar.")
        return

    guild = canal.guild
    bot_member = guild.me
    try:
        # @everyone: NO ve el canal
        await canal.set_permissions(
            guild.default_role,
            view_channel=False,
            send_messages=False,
            read_message_history=False,
        )
        # Rol Admin (Owner): VE el canal y puede leer historial.
        # No le doy send_messages porque el canal es para registros automáticos
        # del bot, pero si querés escribir igual podés (al ser Owner del server,
        # los overrides no te limitan a vos).
        if ADMIN_ROLE_ID:
            try:
                admin_role = guild.get_role(int(ADMIN_ROLE_ID))
            except (TypeError, ValueError):
                admin_role = None
            if admin_role:
                await canal.set_permissions(
                    admin_role,
                    view_channel=True,
                    read_message_history=True,
                    send_messages=True,
                    add_reactions=True,
                    embed_links=True,
                    attach_files=True,
                )
        # Bot: full acceso (publica las ventas automáticas)
        if bot_member:
            await canal.set_permissions(
                bot_member,
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_messages=True,
                embed_links=True,
                attach_files=True,
            )
        log.info(
            "Permisos de #%s configurados: solo Owners (rol admin + dueño) pueden ver.",
            canal.name,
        )
    except discord.Forbidden:
        log.warning("Sin permisos para modificar #%s.", canal.name)
    except Exception:
        log.exception("Error configurando permisos de #%s.", canal.name)


async def _configurar_canal_general() -> None:
    """Canal #general: SOLO el dueño del servidor puede verlo.
    @everyone queda bloqueado. El Owner siempre puede ver todo (los overrides
    no se aplican al owner) así que con bloquear @everyone alcanza.
    El bot también necesita acceso explícito para poder gestionar el canal.
    """
    await client.wait_until_ready()
    if not GUILD_ID:
        return
    guild = client.get_guild(int(GUILD_ID))
    if guild is None:
        return

    # Buscar el canal #general por nombre (tolerante a emojis/decoración)
    canal: discord.TextChannel | None = None
    for ch in guild.text_channels:
        nombre = ch.name.lower()
        # Match exacto "general" o que contenga "general" sin ser otra cosa
        # Evitamos canales como "general-anuncios" o similares
        if nombre == "general" or nombre.endswith("・general") or nombre.endswith("-general"):
            canal = ch
            break
    # Si no lo encontré con nombre exacto, busco el más parecido
    if canal is None:
        for ch in guild.text_channels:
            if "general" in ch.name.lower():
                canal = ch
                break

    if canal is None:
        log.info("No encontré canal #general — nada que configurar.")
        return

    bot_member = guild.me
    try:
        # @everyone: NO ve el canal
        await canal.set_permissions(
            guild.default_role,
            view_channel=False,
            send_messages=False,
            read_message_history=False,
        )
        # Bot: full acceso (para poder seguir gestionando el canal)
        if bot_member:
            await canal.set_permissions(
                bot_member,
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_messages=True,
                embed_links=True,
                attach_files=True,
            )
        log.info(
            "Permisos de #%s configurados: solo Owner del servidor puede ver.",
            canal.name,
        )
    except discord.Forbidden:
        log.warning("Sin permisos para modificar #%s.", canal.name)
    except Exception:
        log.exception("Error configurando permisos de #%s.", canal.name)


async def _configurar_canal_anuncios() -> None:
    """Canal #anuncios:
    - @everyone: VE el canal y lee historial, pero NO escribe ni reacciona ni crea hilos.
    - Rol admin: puede escribir / publicar normalmente.
    - El bot: full access (publica los embeds de NOVEDADES SENSI MARKE).
    """
    await client.wait_until_ready()
    channel = client.get_channel(CANAL_ANUNCIOS_ID)
    if not isinstance(channel, discord.TextChannel):
        log.warning("No encontré el canal #anuncios (id=%s)", CANAL_ANUNCIOS_ID)
        return

    guild = channel.guild
    bot_member = guild.me

    try:
        # @everyone: solo lectura
        await channel.set_permissions(
            guild.default_role,
            view_channel=True,
            read_message_history=True,
            send_messages=False,
            send_messages_in_threads=False,
            create_public_threads=False,
            create_private_threads=False,
            add_reactions=False,
            attach_files=False,
            embed_links=False,
            use_application_commands=False,
        )

        # Rol admin: puede escribir
        if ADMIN_ROLE_ID:
            try:
                admin_role = guild.get_role(int(ADMIN_ROLE_ID))
            except (TypeError, ValueError):
                admin_role = None
            if admin_role:
                await channel.set_permissions(
                    admin_role,
                    view_channel=True,
                    read_message_history=True,
                    send_messages=True,
                    send_messages_in_threads=True,
                    create_public_threads=True,
                    add_reactions=True,
                    attach_files=True,
                    embed_links=True,
                    mention_everyone=True,
                    manage_messages=True,
                    use_application_commands=True,
                )

        # Bot: full
        if bot_member:
            await channel.set_permissions(
                bot_member,
                view_channel=True,
                read_message_history=True,
                send_messages=True,
                send_messages_in_threads=True,
                manage_messages=True,
                manage_threads=True,
                attach_files=True,
                embed_links=True,
                mention_everyone=True,
                use_application_commands=True,
            )
        log.info("Permisos de #anuncios configurados (lectura para todos, escritura solo bot+admin).")
    except discord.Forbidden:
        log.warning("Sin permisos para modificar #anuncios (necesito Manage Channels).")
    except Exception:
        log.exception("Error configurando permisos de #anuncios.")


async def _postear_verificacion():
    """Postea el mensaje de verificación si el canal no tiene uno del bot con botón."""
    await client.wait_until_ready()
    channel = client.get_channel(CANAL_VERIFICACION_ID)
    if channel is None:
        log.warning("No encontré el canal de verificación (id=%s)", CANAL_VERIFICACION_ID)
        return

    # Buscar mensaje del bot que tenga el botón de verificación
    msg_existente: discord.Message | None = None
    async for msg in channel.history(limit=30):
        if msg.author == client.user:
            if msg.components:
                log.info("Mensaje de verificación con botón ya existe, no lo repito.")
                return
            else:
                # Mensaje del bot sin botón — lo borramos y reposteamos
                log.warning(
                    "Mensaje del bot en #verificacion sin botón (id=%s) — borrando y reposteando.",
                    msg.id,
                )
                msg_existente = msg
                break

    if msg_existente:
        try:
            await msg_existente.delete()
        except Exception:
            log.exception("No pude borrar el mensaje viejo de verificación.")

    img_path = Path("attached_assets/IMG_0733_1777449451295.png")
    embed = discord.Embed(
        title="Verificación del servidor",
        description=(
            "Bienvenido a **Marke Panel** 👋\n\n"
            "Para acceder a todos los canales del servidor, "
            "hacé click en el botón de abajo.\n\n"
            "⚠️ Leé las reglas en `📋・reglas` antes de interactuar."
        ),
        color=0x9B59B6,
    )
    embed.set_footer(text="Marke Panel • Verificación automática")

    if img_path.exists():
        embed.set_image(url="attachment://IMG_0733_1777449451295.png")
        await channel.send(
            embed=embed,
            file=discord.File(str(img_path), filename="IMG_0733_1777449451295.png"),
            view=VerificacionView(),
        )
    else:
        await channel.send(embed=embed, view=VerificacionView())

    log.info("Mensaje de verificación posteado en #verificacion.")


# ---------------------------------------------------------------------------
# Eventos
# ---------------------------------------------------------------------------
@client.event
async def on_thread_create(thread: discord.Thread) -> None:
    """Si alguien crea un hilo en canales protegidos, borrarlo al toque."""
    parent_id = thread.parent_id
    canales_sin_hilos = {CANAL_VERIFICACION_ID, CANAL_PROXY_ID}
    if CANAL_SENSI_ID:
        canales_sin_hilos.add(CANAL_SENSI_ID)
    if parent_id in canales_sin_hilos:
        try:
            await thread.delete()
            log.info(
                "Borré hilo no permitido '%s' (id=%s) en canal %s, creado por %s",
                thread.name, thread.id, parent_id, thread.owner_id,
            )
        except discord.Forbidden:
            log.warning(
                "Sin permiso Manage Threads para borrar hilo en canal %s",
                parent_id,
            )
        except Exception:
            log.exception("Error borrando hilo %s", thread.id)


# ---------------------------------------------------------------------------
# Caché de invites para detección automática de referidos
# ---------------------------------------------------------------------------
# guild_id → {invite_code → uses_count}
_invite_cache: dict[int, dict[str, int]] = {}


async def _refresh_invite_cache(guild: discord.Guild) -> None:
    """Actualiza el caché de invites del guild."""
    try:
        invites = await guild.invites()
        _invite_cache[guild.id] = {inv.code: (inv.uses or 0) for inv in invites}
        log.info("Caché de invites actualizado para %s (%d invites)", guild.name, len(invites))
    except discord.Forbidden:
        log.warning("Sin permiso para listar invites del guild %s (necesito Manage Guild)", guild.id)
    except Exception:
        log.exception("Error actualizando caché de invites de %s", guild.id)


@client.event
async def on_invite_create(invite: discord.Invite) -> None:
    """Actualiza el caché cuando se crea una nueva invite."""
    if invite.guild:
        cache = _invite_cache.setdefault(invite.guild.id, {})
        cache[invite.code] = invite.uses or 0
        log.info("Nueva invite creada: %s por %s", invite.code, invite.inviter)


@client.event
async def on_invite_delete(invite: discord.Invite) -> None:
    """Actualiza el caché cuando se elimina una invite."""
    if invite.guild:
        _invite_cache.get(invite.guild.id, {}).pop(invite.code, None)


# ---------------------------------------------------------------------------
# Anti-raid — detección de joins masivos y lockdown automático
# ---------------------------------------------------------------------------
_RAID_VENTANA_SEG        = 10    # ventana para contar joins
_RAID_MAX_JOINS          = 5     # joins en esa ventana → activar lockdown
_RAID_LOCKDOWN_MIN       = 15    # minutos que dura el lockdown automático
_EDAD_MINIMA_DIAS        = 7     # cuentas más nuevas que esto → kick inmediato

_raid_join_timestamps: collections.deque = collections.deque()
_raid_lockdown_activo: bool = False
_raid_lockdown_nivel_previo: discord.VerificationLevel | None = None

_INVITE_RE = re.compile(
    r"(discord\.gg/|discord\.com/invite/|discordapp\.com/invite/)\S+",
    re.IGNORECASE,
)


async def _log_antiraid(titulo: str, color: int, descripcion: str) -> None:
    """Publica un embed de alerta anti-raid en #logs-registros."""
    try:
        canal = await _obtener_canal_logs()
        if canal is None:
            return
        embed = discord.Embed(title=titulo, description=descripcion, color=color)
        embed.set_footer(text="Sistema Anti-Raid — Marke Panel")
        embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
        await canal.send(embed=embed)
    except Exception:
        log.exception("Error posteando log anti-raid")


async def _activar_lockdown(guild: discord.Guild, motivo: str = "Raid detectado") -> None:
    """Eleva el nivel de verificación al máximo y notifica."""
    global _raid_lockdown_activo, _raid_lockdown_nivel_previo
    if _raid_lockdown_activo:
        return
    _raid_lockdown_activo = True
    _raid_lockdown_nivel_previo = guild.verification_level
    try:
        await guild.edit(
            verification_level=discord.VerificationLevel.highest,
            reason=f"[Anti-raid] {motivo}",
        )
        log.warning("LOCKDOWN activado en %s — %s", guild.name, motivo)
    except Exception:
        log.exception("No pude elevar el nivel de verificación")

    await _log_antiraid(
        "🚨 LOCKDOWN ACTIVADO — Posible raid",
        0xFF0000,
        f"**Motivo:** {motivo}\n"
        f"**Verificación:** elevada al máximo\n"
        f"**Duración automática:** {_RAID_LOCKDOWN_MIN} minutos\n\n"
        "Usá `/desbloquear-servidor` para levantar el lockdown manualmente.",
    )
    # Auto-desbloqueo después de N minutos
    await asyncio.sleep(_RAID_LOCKDOWN_MIN * 60)
    await _desactivar_lockdown(guild, auto=True)


async def _desactivar_lockdown(guild: discord.Guild, auto: bool = False) -> None:
    """Restaura el nivel de verificación anterior."""
    global _raid_lockdown_activo, _raid_lockdown_nivel_previo
    if not _raid_lockdown_activo:
        return
    _raid_lockdown_activo = False
    nivel = _raid_lockdown_nivel_previo or discord.VerificationLevel.medium
    _raid_lockdown_nivel_previo = None
    try:
        await guild.edit(verification_level=nivel, reason="[Anti-raid] Lockdown levantado")
        log.info("LOCKDOWN desactivado en %s (auto=%s)", guild.name, auto)
    except Exception:
        log.exception("No pude restaurar el nivel de verificación")

    await _log_antiraid(
        "✅ Lockdown levantado",
        0x2ECC71,
        f"El servidor volvió al nivel de verificación normal.\n"
        f"({'Automático' if auto else 'Manual por admin'})",
    )


@tree.command(
    name="desbloquear-servidor",
    description="(Admin) Levanta el lockdown anti-raid manualmente",
)
async def desbloquear_servidor_cmd(interaction: discord.Interaction):
    if not _puede_registrar(interaction):
        try:
            await interaction.response.send_message("❌ No tenés permiso.", ephemeral=True)
        except Exception:
            pass
        return
    if not _raid_lockdown_activo:
        try:
            await interaction.response.send_message("ℹ️ El servidor no está en lockdown.", ephemeral=True)
        except Exception:
            pass
        return
    await _safe_defer(interaction, ephemeral=True, thinking=True)
    await _desactivar_lockdown(interaction.guild, auto=False)
    await interaction.followup.send("✅ Lockdown levantado correctamente.", ephemeral=True)


@client.event
async def on_member_join(member: discord.Member) -> None:
    """Nuevo miembro: anti-raid + cuenta nueva + detección de referido."""
    log.info("Nuevo miembro se unió: %s (%s)", member, member.id)

    guild = member.guild

    # ── Protección por edad de cuenta ────────────────────────────────────
    ahora_utc = datetime.datetime.now(datetime.timezone.utc)
    edad_dias  = (ahora_utc - member.created_at).days
    if edad_dias < _EDAD_MINIMA_DIAS:
        try:
            await member.send(
                f"⚠️ Tu cuenta de Discord es muy nueva ({edad_dias} días).\n"
                "Por seguridad no podés unirte a este servidor hasta que tu cuenta tenga "
                f"al menos {_EDAD_MINIMA_DIAS} días. ¡Volvé pronto!"
            )
        except Exception:
            pass
        try:
            await member.kick(reason=f"[Anti-raid] Cuenta nueva ({edad_dias}d < {_EDAD_MINIMA_DIAS}d)")
            log.warning("KICK cuenta nueva: %s (%sd) id=%s", member, edad_dias, member.id)
            await _log_antiraid(
                "👢 Kick — cuenta nueva",
                0xFFA500,
                f"**Usuario:** {member} (`{member.id}`)\n"
                f"**Edad de cuenta:** {edad_dias} días\n"
                f"**Límite mínimo:** {_EDAD_MINIMA_DIAS} días",
            )
        except Exception:
            log.exception("No pude kickear cuenta nueva %s", member)
        return

    # ── Detección de join masivo (raid) ───────────────────────────────────
    ts_ahora = ahora_utc.timestamp()
    _raid_join_timestamps.append(ts_ahora)
    while _raid_join_timestamps and ts_ahora - _raid_join_timestamps[0] > _RAID_VENTANA_SEG:
        _raid_join_timestamps.popleft()

    if len(_raid_join_timestamps) >= _RAID_MAX_JOINS and not _raid_lockdown_activo:
        motivo_raid = (
            f"{len(_raid_join_timestamps)} usuarios se unieron en menos de "
            f"{_RAID_VENTANA_SEG} segundos"
        )
        log.warning("RAID detectado: %s", motivo_raid)
        asyncio.create_task(_activar_lockdown(guild, motivo=motivo_raid))

    old_cache = _invite_cache.get(guild.id, {})

    # Obtener invites actuales para comparar
    try:
        new_invites = await guild.invites()
    except Exception:
        log.warning("No pude obtener invites al unirse %s — salteo detección de referido", member)
        new_invites = []

    # Actualizar caché inmediatamente
    _invite_cache[guild.id] = {inv.code: (inv.uses or 0) for inv in new_invites}

    # Encontrar la invite cuyo contador subió
    inviter_id: str | None = None
    for inv in new_invites:
        if inv.inviter and (inv.uses or 0) > old_cache.get(inv.code, 0):
            inviter_id = str(inv.inviter.id)
            log.info(
                "Referido automático detectado: %s (%s) llegó via invite %s de %s (%s)",
                member, member.id, inv.code, inv.inviter, inv.inviter.id,
            )
            break

    if not inviter_id or inviter_id == str(member.id):
        return  # No se detectó invite o se auto-invitó

    # Registrar solo si aún no tiene referidor
    if database.get_referrer(str(member.id)):
        return

    ok = database.register_referral(inviter_id, str(member.id))
    if not ok:
        return

    # Notificar al invitador por DM
    try:
        inviter_user = await client.fetch_user(int(inviter_id))
        rate = database.get_commission_rate(inviter_id)
        pct = int(rate * 100)
        await inviter_user.send(
            f"🎉 **¡Nuevo referido!**\n"
            f"**{member.display_name}** entró al servidor usando tu link de invitación y quedó registrado como tu referido.\n"
            f"Cuando realice cualquier compra, vas a ganar automáticamente el **{pct}%** de comisión. 💰\n\n"
            f"Usá `/perfil` para ver tus referidos y saldo acumulado."
        )
    except Exception:
        log.warning("No pude notificar al invitador %s por DM", inviter_id)


# ---------------------------------------------------------------------------
# Canal #chat — moderación y control
# ---------------------------------------------------------------------------
_CANAL_CHAT_ID: int | None = None   # se rellena en _setup_canal_chat()
_CANAL_CHAT_NOMBRE = "💬・chat"

# ¿Está habilitado para que la gente escriba? Persiste en DB.
_chat_habilitado: bool = False

# Regex para links de grupos externos que se banean en #chat
_CHAT_LINKS_EXTERNOS_RE = re.compile(
    r"(t\.me/|telegram\.me/|wa\.me/|chat\.whatsapp\.com/|"
    r"discord\.gg/|discord\.com/invite/|discordapp\.com/invite/)",
    re.IGNORECASE,
)

# Regex para contenido de estafas / casinos / cripto / sorteos falsos
_CHAT_SCAM_RE = re.compile(
    r"(wesobit|stake\.com|roobet|bc\.game|duelbits|rollbit|"
    r"jackbit|betfury|crashino|luckyblock|bspin|"
    r"promo\s*code|promo\s*cod|codigo\s*promo|bonus\s*code|"
    r"withdrawal\s+success|withdraw\s+success|retiro\s+exitoso|"
    r"gana(?:r)?\s+\$|te\s+regalo\s+\$|(?:doy|dando|regalando)\s+\$|"
    r"crypto\s*casino|casino\s*cripto|"
    r"free\s*usdt|gratis\s*usdt|free\s*btc|"
    r"invest(?:ment)?\s*platform|plataforma\s*de\s*inversion)",
    re.IGNORECASE,
)


async def _ban_chat_infraccion(
    message: discord.Message,
    motivo_corto: str,
    motivo_razon: str,
) -> None:
    """Borra el mensaje, banea al usuario y logea en #logs-registros."""
    try:
        await message.delete()
    except Exception:
        pass
    try:
        await message.guild.ban(
            message.author,
            reason=f"[#chat] {motivo_razon}",
            delete_message_seconds=86400,
        )
        log.warning(
            "BAN #chat — %s: %s (%s)",
            motivo_corto, message.author, message.author.id,
        )
    except discord.Forbidden:
        log.warning("Sin permiso para banear a %s en #chat", message.author)
    except Exception:
        log.exception("Error baneando a %s en #chat", message.author)
    await _log_antiraid(
        f"🔨 Ban automático — #chat — {motivo_corto}",
        0xFF0000,
        f"**Usuario:** {message.author} (`{message.author.id}`)\n"
        f"**Motivo:** {motivo_razon}\n"
        f"**Contenido:** {(message.content or '')[:300]}",
    )


# ---------------------------------------------------------------------------
# Anti-spam — detección y ban automático
# ---------------------------------------------------------------------------
# Parámetros de detección
_SPAM_VENTANA_SEG     = 6     # ventana de tiempo para contar mensajes
_SPAM_MAX_MSGS        = 5     # máx mensajes en la ventana antes de banear
_SPAM_VENTANA_REPEAT  = 10    # ventana para detectar contenido repetido
_SPAM_MAX_REPEAT      = 3     # misma frase N veces → ban

# user_id → deque de timestamps (float)
_spam_timestamps: dict[int, "collections.deque[float]"] = {}
# user_id → deque de (content_hash, timestamp)
_spam_content: dict[int, "collections.deque[tuple[int, float]]"] = {}


async def _log_spam_ban(
    *,
    user: discord.abc.User,
    guild: discord.Guild,
    motivo: str,
    canal_nombre: str,
) -> None:
    """Publica un embed de ban por spam en #logs-registros."""
    try:
        canal = await _obtener_canal_logs()
        if canal is None:
            return
        embed = discord.Embed(
            title="🔨 Ban automático — spam detectado",
            color=0xFF0000,
        )
        embed.set_author(
            name=f"{user} ({user.id})",
            icon_url=getattr(user.display_avatar, "url", None),
        )
        embed.add_field(name="Canal", value=f"#{canal_nombre}", inline=True)
        embed.add_field(name="Motivo", value=motivo, inline=False)
        embed.set_footer(text="Ban automático por sistema anti-spam")
        import datetime
        embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
        await canal.send(embed=embed)
    except Exception:
        log.exception("Error posteando log de ban por spam")


@client.event
async def on_message(message: discord.Message):
    # ── Moderación del canal #chat ────────────────────────────────────────
    if (
        message.guild is not None
        and not message.author.bot
        and _CANAL_CHAT_ID
        and message.channel.id == _CANAL_CHAT_ID
        and _chat_habilitado
    ):
        # Excluir admins de las restricciones de contenido en #chat
        es_admin_chat = False
        if ADMIN_ROLE_ID:
            try:
                rid = int(ADMIN_ROLE_ID)
                if isinstance(message.author, discord.Member):
                    es_admin_chat = (
                        any(r.id == rid for r in message.author.roles)
                        or message.author.guild_permissions.administrator
                    )
            except (TypeError, ValueError):
                pass

        if not es_admin_chat:
            contenido = message.content or ""
            # 1. Links a grupos externos (Telegram, WhatsApp, Discord invite)
            if _CHAT_LINKS_EXTERNOS_RE.search(contenido):
                await _ban_chat_infraccion(
                    message,
                    "link externo",
                    "Envío de link a grupo/canal externo en #chat",
                )
                return
            # 2. Contenido de estafa / casino / cripto falso
            if _CHAT_SCAM_RE.search(contenido):
                await _ban_chat_infraccion(
                    message,
                    "contenido scam",
                    "Contenido de estafa o casino enviado en #chat",
                )
                return
            # 3. Imágenes adjuntas con posible scam (capturas de casino/cripto)
            #    No se puede analizar el contenido visual, pero sí el texto
            #    de los embeds que Discord genera automáticamente de URLs
            for emb in message.embeds:
                emb_text = " ".join(filter(None, [
                    emb.title, emb.description,
                    getattr(emb.author, "name", None),
                    getattr(emb.footer, "text", None),
                ]))
                if _CHAT_SCAM_RE.search(emb_text) or _CHAT_LINKS_EXTERNOS_RE.search(emb_text):
                    await _ban_chat_infraccion(
                        message,
                        "embed scam",
                        "Embed con contenido de estafa en #chat",
                    )
                    return
        return  # mensaje legítimo en #chat → no procesar más abajo

    # ── Detección de invite links (incluye bots/apps) ─────────────────────
    if message.guild is not None and _INVITE_RE.search(message.content or ""):
        try:
            await message.delete()
        except Exception:
            pass
        if not message.author.bot:
            # Ban al usuario que mandó el link de invite
            try:
                await message.guild.ban(
                    message.author,
                    reason="[Anti-raid] Envío de invite link de Discord",
                    delete_message_seconds=86400,
                )
                log.warning(
                    "BAN por invite link: %s (%s) en #%s",
                    message.author, message.author.id,
                    getattr(message.channel, "name", message.channel.id),
                )
                await _log_antiraid(
                    "🔨 Ban — invite link enviado",
                    0xFF0000,
                    f"**Usuario:** {message.author} (`{message.author.id}`)\n"
                    f"**Canal:** #{getattr(message.channel, 'name', message.channel.id)}\n"
                    f"**Contenido:** {(message.content or '')[:200]}",
                )
            except discord.Forbidden:
                log.warning("Sin permiso para banear a %s por invite link", message.author)
            except Exception:
                log.exception("Error baneando por invite link a %s", message.author)
        return

    if message.author.bot:
        return

    # ── Anti-spam (canales de guild, excluye admins) ───────────────────────
    if message.guild is not None:
        es_admin_spam = False
        if ADMIN_ROLE_ID:
            try:
                rid = int(ADMIN_ROLE_ID)
                if isinstance(message.author, discord.Member):
                    es_admin_spam = (
                        any(r.id == rid for r in message.author.roles)
                        or message.author.guild_permissions.administrator
                    )
            except (TypeError, ValueError):
                pass

        if not es_admin_spam:
            uid   = message.author.id
            ahora = message.created_at.timestamp()

            # — Detección por velocidad (N mensajes en ventana) —
            if uid not in _spam_timestamps:
                _spam_timestamps[uid] = collections.deque()
            dq = _spam_timestamps[uid]
            dq.append(ahora)
            while dq and ahora - dq[0] > _SPAM_VENTANA_SEG:
                dq.popleft()

            # — Detección por contenido repetido —
            contenido = (message.content or "").strip().lower()
            repetido  = False
            if contenido:
                chash = hash(contenido)
                if uid not in _spam_content:
                    _spam_content[uid] = collections.deque()
                dqc = _spam_content[uid]
                dqc.append((chash, ahora))
                while dqc and ahora - dqc[0][1] > _SPAM_VENTANA_REPEAT:
                    dqc.popleft()
                repetido = sum(1 for h, _ in dqc if h == chash) >= _SPAM_MAX_REPEAT

            spam_velocidad = len(dq) >= _SPAM_MAX_MSGS

            if spam_velocidad or repetido:
                motivo_ban = (
                    f"{'Mensajes repetidos' if repetido else 'Flood'} — "
                    f"{len(dq)} msgs en {_SPAM_VENTANA_SEG}s"
                    if spam_velocidad
                    else f"Mismo mensaje enviado {_SPAM_MAX_REPEAT}+ veces"
                )
                canal_nombre = getattr(message.channel, "name", str(message.channel.id))
                guild        = message.guild

                # Limpiar trackers de este user
                _spam_timestamps.pop(uid, None)
                _spam_content.pop(uid, None)

                # Intentar banear
                ban_ok = False
                try:
                    await guild.ban(
                        message.author,
                        reason=f"[Anti-spam] {motivo_ban}",
                        delete_message_seconds=86400,
                    )
                    ban_ok = True
                    log.warning(
                        "BAN por spam: %s (%s) en #%s — %s",
                        message.author, uid, canal_nombre, motivo_ban,
                    )
                except discord.Forbidden:
                    log.warning("Sin permiso para banear a %s (%s)", message.author, uid)
                except Exception:
                    log.exception("Error baneando a %s", message.author)

                if ban_ok:
                    await _log_spam_ban(
                        user=message.author,
                        guild=guild,
                        motivo=motivo_ban,
                        canal_nombre=canal_nombre,
                    )
                return

    # ── Todos los canales de guild: solo slash commands permitidos ────────
    # Cualquier mensaje de texto en el servidor es borrado silenciosamente,
    # excepto si el autor es admin o el canal es un ticket activo.
    if message.guild is not None:
        es_admin = False
        if ADMIN_ROLE_ID:
            try:
                rid = int(ADMIN_ROLE_ID)
                if isinstance(message.author, discord.Member):
                    es_admin = (
                        any(r.id == rid for r in message.author.roles)
                        or message.author.guild_permissions.administrator
                    )
            except (TypeError, ValueError):
                pass
        with _tickets_lock:
            es_ticket = message.channel.id in _active_tickets
        # Fallback: si el canal se llama ticket-XXXX-... lo tratamos como ticket
        # (cubre tickets creados antes de la persistencia en DB o tras limpiar la tabla)
        if not es_ticket:
            canal_name = getattr(message.channel, "name", "") or ""
            if canal_name.startswith("ticket-"):
                es_ticket = True
        if not es_admin and not es_ticket:
            try:
                await message.delete()
            except discord.Forbidden:
                log.warning(
                    "Sin permiso para borrar mensaje de %s (%s) en #%s — "
                    "verificar que el bot tenga Manage Messages en el canal.",
                    message.author, message.author.id,
                    getattr(message.channel, "name", message.channel.id),
                )
            except discord.NotFound:
                pass  # ya fue borrado
            except Exception:
                log.exception(
                    "Error borrando mensaje de %s en #%s",
                    message.author,
                    getattr(message.channel, "name", message.channel.id),
                )
            return

    # ── Comprobante de Binance por DM ─────────────────────────────────────
    is_dm = message.guild is None

    # ── Comprobante de diamantes (transferencia) — prioridad sobre NX ─────
    # Se verifica PRIMERO para que no lo atrape el flujo de NX/Binance.
    if is_dm and message.attachments:
        pedido_diam = _pending_diam_transferencia.pop(message.author.id, None)
        if pedido_diam:
            diamonds   = pedido_diam["diamonds"]
            id_ff      = pedido_diam["id_freefire"]
            precio     = pedido_diam["precio"]
            username   = pedido_diam["username"]
            imagen_url = message.attachments[0].url

            try:
                await message.reply(
                    "✅ **Comprobante recibido.** Un administrador lo revisará y te acreditará "
                    f"los **{diamonds:,} 💎** a la brevedad. ¡Gracias!"
                )
            except Exception:
                pass

            try:
                canal_ventas = await _obtener_canal_ventas()
                if canal_ventas:
                    embed_comp = discord.Embed(
                        title="📎 Comprobante recibido — Transferencia Diamantes",
                        description=(
                            f"👤 **Usuario:** {message.author.mention} (`{username}`)\n"
                            f"💎 **Paquete:** {diamonds:,} Diamantes\n"
                            f"💰 **Monto:** {precio} ARS\n"
                            f"🎮 **ID Free Fire:** `{id_ff}`\n\n"
                            "Verificá el monto y el destino en el comprobante.\n"
                            "Al aceptar, el bot comprará los diamantes en Binance automáticamente."
                        ),
                        color=0x2ECC71,
                    )
                    embed_comp.set_image(url=imagen_url)
                    embed_comp.set_footer(text="Marke Panel • @markee.4")
                    view_aceptar = DiamantesAceptarPagoView(
                        user_id=message.author.id,
                        diamonds=diamonds,
                        id_freefire=id_ff,
                        user_mention=message.author.mention,
                    )
                    await canal_ventas.send(embed=embed_comp, view=view_aceptar)
                    log.info(
                        "Comprobante diamantes reenviado a #ventas — usuario=%s, diamonds=%d",
                        username, diamonds,
                    )
            except Exception:
                log.exception("Error reenviando comprobante diamantes a #ventas")
            return  # no continuar con el flujo NX

    with _pending_binance_lock:
        op_id = _waiting_comprobante.get(message.author.id)

    # Fallback: si el user mandó una imagen por DM y no está en memoria,
    # buscamos en la DB. Esto cubre el caso de "el bot se reinició entre que
    # le pidió pagar y mandó el comprobante" — antes el comprobante se
    # ignoraba silenciosamente.
    if is_dm and not op_id and message.attachments:
        try:
            row = database.get_pending_payment_by_user(message.author.id)
        except Exception:
            row = None
            log.exception("Error consultando pending_payments para %s",
                          message.author.id)
        if row:
            pack_obj = payments.PACKS.get(row["pack_id"])
            if pack_obj is not None:
                op_id = row["payment_id"]
                with _pending_binance_lock:
                    if op_id not in _pending_binance:
                        _pending_binance[op_id] = {
                            "discord_id": row["discord_id"],
                            "user_id":    row["user_id"],
                            "pack":       pack_obj,
                            "channel_id": row["channel_id"],
                            "username":   row["username"],
                            "metodo":     row["metodo"],
                        }
                    _waiting_comprobante[message.author.id] = op_id
                log.info(
                    "Recuperé operación %s para %s desde DB (no estaba en "
                    "memoria, posible reinicio del bot)",
                    op_id, message.author,
                )

    # Si el usuario en espera manda la foto en el canal → borrarla y redirigir al DM
    if not is_dm and op_id and message.attachments:
        try:
            await message.delete()
        except Exception:
            pass
        try:
            await message.author.send(
                "⚠️ Enviaste la captura en el canal. Por seguridad la borré.\n"
                f"Mandame el comprobante aquí en el chat privado (operación `#{op_id}`)."
            )
        except Exception:
            pass
        return

    if is_dm and op_id and message.attachments:
        image_url = message.attachments[0].url
        with _pending_binance_lock:
            op = _pending_binance.get(op_id)
            _waiting_comprobante.pop(message.author.id, None)

        if op:
            await _procesar_comprobante_con_ia(
                op_id=op_id,
                op=op,
                image_url=image_url,
                user_message=message,
            )
        return

    if is_dm:
        return

    # Reenviar mensajes del canal de ticket a WhatsApp
    with _tickets_lock:
        ticket_info = _active_tickets.get(message.channel.id)
    if ticket_info is not None and message.content:
        num = ticket_info["ticket_num"]
        texto = (
            f"💬 Ticket #{num:04d} — {message.author.display_name}:\n{message.content}"
        )
        await asyncio.to_thread(twilio_helper.send_whatsapp, texto)

    if "@todos" in message.content.lower():
        nuevo = message.content.replace("@todos", "@everyone").replace("@Todos", "@everyone")
        await message.delete()
        await message.channel.send(
            f"**{message.author.display_name}:** {nuevo}",
            allowed_mentions=discord.AllowedMentions(everyone=True)
        )


CANAL_TICKETS_PANEL_ID: int | None = None   # se setea en _postear_panel_tickets


async def _postear_panel_tickets() -> None:
    """Busca o crea el canal de tickets y postea el panel con botón."""
    global CANAL_TICKETS_PANEL_ID
    await client.wait_until_ready()

    guild = client.get_guild(int(GUILD_ID)) if GUILD_ID else None
    if guild is None:
        log.warning("No encontré el guild para crear el panel de tickets")
        return

    # Buscar canal existente llamado "crear-ticket" o similar
    canal = discord.utils.find(
        lambda c: isinstance(c, discord.TextChannel) and
                  any(n in c.name.lower() for n in ("crear-ticket", "tickets", "soporte-ticket", "abrir-ticket")),
        guild.channels,
    )

    # Si no existe, crearlo
    if canal is None:
        category = discord.utils.find(
            lambda c: c.name.upper() in ("TICKETS", "SOPORTE", "SUPPORT", "TICKET"),
            guild.categories,
        )
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=True, send_messages=False
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_channels=True
            ),
        }
        canal = await guild.create_text_channel(
            "📩・crear-ticket",
            category=category,
            overwrites=overwrites,
            topic="Abrí un ticket de soporte tocando el botón de abajo.",
            reason="Canal de tickets creado automáticamente por Marke Panel",
        )
        log.info("Canal de tickets creado: %s (id=%s)", canal.name, canal.id)

    CANAL_TICKETS_PANEL_ID = canal.id

    # Verificar si el panel ya existe
    async for msg in canal.history(limit=20):
        if msg.author == client.user and msg.components:
            log.info("Panel de tickets ya existe en #%s, no lo repito.", canal.name)
            return

    embed = discord.Embed(
        title="🎫  Soporte — Marke Panel",
        description=(
            "¿Tenés alguna duda, problema o consulta?\n\n"
            "Tocá el botón de abajo para abrir un ticket privado.\n"
            "Un administrador te responderá a la brevedad.\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📌 **Antes de abrir un ticket:**\n"
            "• Revisá los canales de información\n"
            "• Describí tu problema con el mayor detalle posible\n"
            "• Un solo ticket por consulta"
        ),
        color=0x3498DB,
    )
    embed.set_footer(text="Marke Panel • Sistema de tickets")
    await canal.send(embed=embed, view=TicketPanelView())
    log.info("Panel de tickets posteado en #%s", canal.name)


# ---------------------------------------------------------------------------
# Canales contadores
# ---------------------------------------------------------------------------
_id_canal_miembros:    int | None = None
_id_canal_proxies:     int | None = None
_id_canal_ventas:      int | None = None
_id_canal_registros:   int | None = None

# Caché del conteo real del dashboard (Chromium es pesado — se refresca cada 20 min)
_proxies_dashboard_cache: int = -1          # -1 = nunca actualizado
_proxies_last_refresh:    float = 0.0       # timestamp unix
_PROXIES_REFRESH_INTERVAL = 20 * 60        # 20 minutos


def _buscar_o_crear_voice(guild, categoria, cache_id, keyword, nombre_nuevo, overwrites):
    """Auxiliar síncrona: devuelve el canal de voz existente por cache_id o por keyword en la categoría."""
    if cache_id:
        ch = guild.get_channel(cache_id)
        if ch:
            return ch, cache_id
    ch = discord.utils.find(
        lambda c: isinstance(c, discord.VoiceChannel) and keyword.lower() in c.name.lower() and c.category_id == categoria.id,
        guild.channels,
    )
    return ch, (ch.id if ch else None)


async def _actualizar_contadores() -> None:
    """Crea o actualiza los canales contadores en la categoría CONTADOR 📈."""
    global _id_canal_miembros, _id_canal_proxies, _id_canal_ventas, _id_canal_registros
    await client.wait_until_ready()

    guild = _resolver_guild()
    if guild is None:
        log.warning("_actualizar_contadores: guild no disponible, skip")
        return

    log.info("_actualizar_contadores: inicio para guild %s", guild.id)

    overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=True, connect=False)}

    # ── Buscar o crear categoría (sin editar position para evitar rate limit) ─
    categoria = discord.utils.find(
        lambda c: isinstance(c, discord.CategoryChannel) and "CONTADOR" in c.name.upper(),
        guild.channels,
    )
    if categoria is None:
        categoria = await guild.create_category("CONTADOR 📈", overwrites=overwrites)
        log.info("Categoría CONTADOR creada")

    # ── Obtener valores en paralelo ───────────────────────────────────────────
    global _proxies_dashboard_cache, _proxies_last_refresh
    import time as _time
    miembros = guild.member_count or 0
    ventas, registros = await asyncio.gather(
        asyncio.to_thread(database.count_total_sales),
        asyncio.to_thread(database.count_total_registrations),
    )

    # Clientes proxy: scrape del dashboard real cada 20 min, cachea el resto del tiempo
    ahora = _time.monotonic()
    if ahora - _proxies_last_refresh >= _PROXIES_REFRESH_INTERVAL or _proxies_dashboard_cache < 0:
        log.info("Actualizando conteo de clientes proxy desde el dashboard...")
        try:
            resultado = await asyncio.wait_for(automation.contar_clientes_marke(), timeout=45)
            if resultado >= 0:
                _proxies_dashboard_cache = resultado
                _proxies_last_refresh = ahora
                log.info("Clientes proxy en dashboard: %d", _proxies_dashboard_cache)
            else:
                log.warning("contar_clientes_marke() devolvió -1 — usando caché anterior: %d", _proxies_dashboard_cache)
        except asyncio.TimeoutError:
            log.warning("Timeout al contar clientes proxy en dashboard — usando caché anterior: %d", _proxies_dashboard_cache)
    proxies = _proxies_dashboard_cache

    # ── Actualizar cada canal de forma independiente ──────────────────────────
    proxies_str = str(proxies) if proxies >= 0 else "?"
    contadores = [
        ("miembros",   "_id_canal_miembros",  "Miembros",  f"👥 Miembros: {miembros}"),
        ("proxies",    "_id_canal_proxies",   "Proxy",     f"🔌 Clientes Proxy: {proxies_str}"),
        ("ventas",     "_id_canal_ventas",    "Ventas",    f"💰 Ventas: {ventas}"),
        ("registros",  "_id_canal_registros", "Registros", f"📋 Registros: {registros}"),
    ]

    _cache = {
        "_id_canal_miembros":  _id_canal_miembros,
        "_id_canal_proxies":   _id_canal_proxies,
        "_id_canal_ventas":    _id_canal_ventas,
        "_id_canal_registros": _id_canal_registros,
    }

    for key, cache_attr, keyword, nombre in contadores:
        try:
            cache_id = _cache[cache_attr]
            ch, found_id = _buscar_o_crear_voice(guild, categoria, cache_id, keyword, nombre, overwrites)
            if ch:
                _cache[cache_attr] = found_id or ch.id
                if ch.name != nombre:
                    try:
                        await asyncio.wait_for(ch.edit(name=nombre), timeout=15)
                    except asyncio.TimeoutError:
                        log.warning("Contador %s: timeout al editar nombre (rate limit Discord) — se reintentará en 5 min", key)
            else:
                try:
                    ch = await asyncio.wait_for(
                        categoria.create_voice_channel(nombre, overwrites=overwrites),
                        timeout=15,
                    )
                    _cache[cache_attr] = ch.id
                    log.info("Canal contador %s creado (id=%s)", key, ch.id)
                except asyncio.TimeoutError:
                    log.warning("Contador %s: timeout al crear canal — se reintentará en 5 min", key)
        except Exception:
            log.exception("Error actualizando canal contador %s", key)

    _id_canal_miembros  = _cache["_id_canal_miembros"]
    _id_canal_proxies   = _cache["_id_canal_proxies"]
    _id_canal_ventas    = _cache["_id_canal_ventas"]
    _id_canal_registros = _cache["_id_canal_registros"]

    log.info(
        "Contadores actualizados — Miembros: %d | Proxies: %s | Ventas: %d | Registros: %d",
        miembros, proxies_str, ventas, registros,
    )


async def _loop_contadores() -> None:
    """Actualiza los contadores cada 5 minutos."""
    log.info("_loop_contadores: esperando ready...")
    await client.wait_until_ready()
    log.info("_loop_contadores: iniciando ciclo")
    while not client.is_closed():
        try:
            await _actualizar_contadores()
        except Exception:
            log.exception("Error actualizando contadores")
        await asyncio.sleep(300)   # 5 minutos


async def _purgar_mensajes_usuario(nombre_parcial: str) -> None:
    """Recorre todos los canales de texto y borra mensajes del usuario cuyo nombre contenga `nombre_parcial`."""
    await client.wait_until_ready()
    guild = _resolver_guild()
    if guild is None:
        return
    nombre_lower = nombre_parcial.lower()
    total = 0
    for canal in guild.text_channels:
        try:
            async for msg in canal.history(limit=500):
                autor = msg.author
                if (
                    nombre_lower in autor.name.lower()
                    or nombre_lower in getattr(autor, "display_name", "").lower()
                    or nombre_lower in (getattr(autor, "global_name", None) or "").lower()
                ):
                    try:
                        await msg.delete()
                        total += 1
                        await asyncio.sleep(0.3)
                    except Exception:
                        pass
        except discord.Forbidden:
            pass
        except Exception:
            log.exception("Error purgando mensajes en #%s", canal.name)
    log.info("Purga '%s' completada: %d mensaje(s) eliminado(s).", nombre_parcial, total)


async def _banear_usuarios_por_nombre(nombre_parcial: str) -> None:
    """Banea a todos los miembros cuyo nombre contenga `nombre_parcial`."""
    await client.wait_until_ready()
    guild = _resolver_guild()
    if guild is None:
        return
    nombre_lower = nombre_parcial.lower()
    total = 0
    async for member in guild.fetch_members(limit=None):
        if member.bot and not (
            nombre_lower in member.name.lower()
            or nombre_lower in (member.display_name or "").lower()
        ):
            continue
        if (
            nombre_lower in member.name.lower()
            or nombre_lower in (member.display_name or "").lower()
            or nombre_lower in (getattr(member, "global_name", None) or "").lower()
        ):
            try:
                await guild.ban(
                    member,
                    reason=f"[Auto-ban] nombre contiene '{nombre_parcial}'",
                    delete_message_seconds=86400,
                )
                total += 1
                log.warning("Auto-ban '%s': %s (%s)", nombre_parcial, member, member.id)
                await asyncio.sleep(0.5)
            except Exception:
                log.exception("No pude banear a %s", member)
    log.info("Auto-ban '%s' completado: %d usuario(s) baneado(s).", nombre_parcial, total)


async def _anunciar_nuevo_video_proxy() -> None:
    """Publica el anuncio del nuevo video del proxy en #anuncios con @everyone."""
    await asyncio.sleep(5)  # Esperar a que los canales estén configurados
    canal = client.get_channel(CANAL_ANUNCIOS_ID)
    if canal is None:
        log.warning("_anunciar_nuevo_video_proxy: no encontré #anuncios (id=%s)", CANAL_ANUNCIOS_ID)
        return
    try:
        embed = discord.Embed(
            title="🎬  NUEVO VIDEO — PROXY MARKE V3",
            description=(
                "¡Ya está disponible el **nuevo tutorial del Proxy Marke**! 🔥\n\n"
                "📹 En este video vas a aprender a configurar el proxy paso a paso "
                "con la versión más nueva, más estable y más rápida que lanzamos.\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "🎁  **PROMO EXCLUSIVA — 3 DÍAS GRATIS**\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "Comprá el proxy esta semana y te regalamos **3 días extra** sin costo.\n\n"
                f"▶️  **[MIRÁ EL TUTORIAL ACÁ]({PROXY_TUTORIAL_URL})**\n\n"
                "📲 Para comprarlo abrí un ticket o escribile a un admin.\n"
                "⬇️  No te lo pierdas, dale like y suscribite al canal 🙌"
            ),
            color=0xFF0000,
        )
        embed.set_thumbnail(url="https://img.youtube.com/vi/bNxxIBlbWWka/hqdefault.jpg")
        embed.set_footer(text="Sensi Marke • @markee.4  |  youtube.com/@shutupmarke")
        await canal.send(
            "@everyone",
            allowed_mentions=discord.AllowedMentions(everyone=True),
        )
        await canal.send(embed=embed)
        log.info("_anunciar_nuevo_video_proxy: anuncio publicado en #anuncios")
    except Exception:
        log.exception("_anunciar_nuevo_video_proxy: error publicando anuncio")


# ---------------------------------------------------------------------------
# Setup canal #chat
# ---------------------------------------------------------------------------
async def _setup_canal_chat() -> None:
    """Crea/encuentra el canal #chat y lo deja bloqueado hasta que se habilite."""
    global _CANAL_CHAT_ID, _chat_habilitado
    await client.wait_until_ready()

    guild = _resolver_guild()
    if guild is None:
        log.warning("_setup_canal_chat: guild no encontrado")
        return

    # Leer estado persistido
    _chat_habilitado = database.get_config("chat_habilitado", "0") == "1"

    # Buscar canal existente por nombre
    canal: discord.TextChannel | None = None
    for ch in guild.text_channels:
        if ch.name.lower().replace("💬", "").replace("・", "").strip() == "chat":
            canal = ch
            break

    categoria = await _obtener_o_crear_categoria(guild, _CAT_COMUNIDAD)

    if canal is None:
        try:
            canal = await guild.create_text_channel(
                _CANAL_CHAT_NOMBRE,
                category=categoria,
                topic="Chat general del servidor — Sensi Marke 🎮",
                reason="Setup automático — Marke Panel",
            )
            log.info("_setup_canal_chat: canal creado (id=%s)", canal.id)
        except Exception:
            log.exception("_setup_canal_chat: no pude crear el canal")
            return

    _CANAL_CHAT_ID = canal.id

    # Configurar permisos según el estado actual
    await _aplicar_permisos_chat(guild, canal, _chat_habilitado)
    log.info("_setup_canal_chat: listo — habilitado=%s canal=%s", _chat_habilitado, canal.id)


async def _aplicar_permisos_chat(
    guild: discord.Guild,
    canal: discord.TextChannel,
    habilitado: bool,
) -> None:
    """Aplica permisos al canal #chat (bloqueado o abierto para verificados)."""
    try:
        bot_member = guild.get_member(client.user.id)
        rol_verificado = guild.get_role(ROL_VERIFICADO_ID)
        admin_role = guild.get_role(int(ADMIN_ROLE_ID)) if ADMIN_ROLE_ID else None

        # Default (todos): solo ver, no escribir
        await canal.set_permissions(
            guild.default_role,
            view_channel=True,
            send_messages=False,
            add_reactions=False,
        )
        # Verificados: escribir solo cuando está habilitado
        if rol_verificado:
            await canal.set_permissions(
                rol_verificado,
                view_channel=True,
                send_messages=habilitado,
                read_message_history=True,
                add_reactions=habilitado,
                attach_files=habilitado,
            )
        # Admin: siempre puede escribir
        if admin_role:
            await canal.set_permissions(
                admin_role,
                view_channel=True,
                send_messages=True,
                manage_messages=True,
                read_message_history=True,
            )
        # Bot: siempre puede gestionar
        if bot_member:
            await canal.set_permissions(
                bot_member,
                view_channel=True,
                send_messages=True,
                manage_messages=True,
                read_message_history=True,
                embed_links=True,
            )
        log.info("_aplicar_permisos_chat: permisos aplicados (habilitado=%s)", habilitado)
    except Exception:
        log.exception("_aplicar_permisos_chat: error configurando permisos")


@tree.command(
    name="habilitar-chat",
    description="(Admin) Abre el canal #chat para que la gente pueda escribir",
)
async def habilitar_chat_cmd(interaction: discord.Interaction):
    global _chat_habilitado
    await _safe_defer(interaction, ephemeral=True, thinking=True)
    if not _puede_registrar(interaction):
        await interaction.followup.send("❌ Solo administradores.", ephemeral=True)
        return

    guild = interaction.guild
    canal: discord.TextChannel | None = None
    if _CANAL_CHAT_ID:
        canal = guild.get_channel(_CANAL_CHAT_ID)
    if canal is None:
        for ch in guild.text_channels:
            if ch.name.lower().replace("💬", "").replace("・", "").strip() == "chat":
                canal = ch
                break

    if canal is None:
        await interaction.followup.send("❌ No encontré el canal #chat.", ephemeral=True)
        return

    _chat_habilitado = True
    database.set_config("chat_habilitado", "1")
    await _aplicar_permisos_chat(guild, canal, True)

    await canal.send(
        "💬 **¡El chat está abierto!**\n"
        "Bienvenidos. Respetense, no spam, no links externos.\n"
        "El incumplimiento de las reglas resulta en **ban permanente** automático. 🚫"
    )

    await interaction.followup.send(
        f"✅ Canal {canal.mention} **habilitado**. La gente ya puede escribir.",
        ephemeral=True,
    )
    log.info("habilitar-chat: activado por %s", interaction.user)


@tree.command(
    name="deshabilitar-chat",
    description="(Admin) Cierra el canal #chat para que nadie pueda escribir",
)
async def deshabilitar_chat_cmd(interaction: discord.Interaction):
    global _chat_habilitado
    await _safe_defer(interaction, ephemeral=True, thinking=True)
    if not _puede_registrar(interaction):
        await interaction.followup.send("❌ Solo administradores.", ephemeral=True)
        return

    guild = interaction.guild
    canal: discord.TextChannel | None = None
    if _CANAL_CHAT_ID:
        canal = guild.get_channel(_CANAL_CHAT_ID)
    if canal is None:
        for ch in guild.text_channels:
            if ch.name.lower().replace("💬", "").replace("・", "").strip() == "chat":
                canal = ch
                break

    if canal is None:
        await interaction.followup.send("❌ No encontré el canal #chat.", ephemeral=True)
        return

    _chat_habilitado = False
    database.set_config("chat_habilitado", "0")
    await _aplicar_permisos_chat(guild, canal, False)

    await interaction.followup.send(
        f"🔒 Canal {canal.mention} **deshabilitado**. Nadie puede escribir hasta que lo habilites.",
        ephemeral=True,
    )
    log.info("deshabilitar-chat: desactivado por %s", interaction.user)


@client.event
async def on_ready():
    global MODO_AUTO_MP, _ARG_EMOJI_ID
    MODO_AUTO_MP = database.get_config("MODO_AUTO_MP", "0") == "1"
    log.info("Bot conectado como %s (id=%s)", client.user, client.user.id)
    log.info("MODO_AUTO_MP cargado desde DB: %s", MODO_AUTO_MP)

    # Conectar Telethon para /gen y /key (schedule_connect guarda referencia fuerte
    # para evitar que el GC destruya la task antes de que termine)
    telegram_client.schedule_connect()

    # Reset one-time de gratismarke (v2): si nunca se hizo, resetea todos los
    # free trials y abre una ventana de 42 h desde ahora.
    if database.get_config("gratismarke_reset_v2") != "done":
        nueva_exp = int(time.time()) + 42 * 3600
        database.set_config("gratismarke_expiry", str(nueva_exp))
        database.reset_free_trial_all()
        database.set_config("gratismarke_reset_v2", "done")
        log.info("gratismarke_reset_v2: ventana reseteada, expira en 42h")

    # Crear/encontrar emoji personalizado de bandera argentina para el botón Transferencia
    try:
        guild = _resolver_guild()
        if guild:
            flag_path = Path(__file__).parent.parent / "assets" / "argentina_flag.jpg"
            with open(flag_path, "rb") as _f:
                _img_bytes = _f.read()
            existing = next((e for e in guild.emojis if e.name == "argentina"), None)
            if existing:
                await existing.delete(reason="Actualizando imagen del emoji argentina")
                log.info("Emoji 'argentina' anterior eliminado (id=%s)", existing.id)
            new_emoji = await guild.create_custom_emoji(name="argentina", image=_img_bytes)
            _ARG_EMOJI_ID = new_emoji.id
            log.info("Emoji 'argentina' creado/actualizado (id=%s)", _ARG_EMOJI_ID)
    except Exception:
        log.exception("No pude crear el emoji 'argentina'; el botón usará 🇦🇷")
    client.add_view(VerificacionView())
    client.add_view(TicketPanelView())
    client.add_view(CerrarTicketView())
    client.add_view(PackView(modo="proxy"))
    client.add_view(PackView(modo="sensi"))
    client.add_view(PackView(modo="android"))
    client.add_view(PackView(modo="ios"))
    client.add_view(PackView(modo="regedit"))
    # Vista persistente de aprobación manual — registrarla sin datos;
    # los callbacks recuperan op_id/op desde el mensaje o la DB.
    client.add_view(AprobarPagoView())
    # Vista persistente de aceptación de diamantes — custom_ids fijos diam_accept/diam_reject
    client.add_view(DiamantesAceptarPagoView())
    client.add_view(FlouriteCanalView())
    client.add_view(DiamantesCanalView())
    client.add_view(ProxyInfoView())
    client.add_view(PostulacionView())
    asyncio.create_task(_actualizar_perfil())
    asyncio.create_task(_configurar_canal_verificacion())
    asyncio.create_task(_postear_verificacion())
    asyncio.create_task(_postear_info_proxy())
    asyncio.create_task(_purgar_canal_proxy())
    asyncio.create_task(_postear_panel_tickets())
    asyncio.create_task(_postear_panel_postulaciones())
    asyncio.create_task(_configurar_permisos_proxy())
    asyncio.create_task(_setup_canal_info_referidos())
    # Cargar caché de invites para detección automática de referidos
    for _g in client.guilds:
        asyncio.create_task(_refresh_invite_cache(_g))
    asyncio.create_task(_setup_canal_sensis())
    asyncio.create_task(_setup_canal_android())
    asyncio.create_task(_setup_canal_ios())
    asyncio.create_task(_setup_canal_flourite())
    asyncio.create_task(_setup_canal_diamantes())
    asyncio.create_task(_setup_canal_chat())
    asyncio.create_task(_reorganizar_categorias())
    asyncio.create_task(_configurar_canal_anuncios())
    asyncio.create_task(_configurar_canal_general())
    asyncio.create_task(_configurar_canal_ventas())
    asyncio.create_task(_configurar_canal_logs())
    asyncio.create_task(_loop_tiktok_live())
    asyncio.create_task(_loop_gmail_sync())
    asyncio.create_task(_loop_contadores())
    asyncio.create_task(_purgar_mensajes_usuario("poya"))
    asyncio.create_task(_banear_usuarios_por_nombre("poya"))
    asyncio.create_task(_deshabilitar_gratis_en_1h())
    # Recuperar operaciones pendientes que sobrevivieron al reinicio.
    # Restauramos TANTO _pending_binance como _waiting_comprobante (este último
    # se perdía antes en cada reinicio, dejando comprobantes de DM ignorados).
    try:
        restored = 0
        waiting_restored = 0
        for row in database.list_pending_payments():
            pack_obj = payments.PACKS.get(row["pack_id"])
            if pack_obj is None:
                continue
            with _pending_binance_lock:
                if row["payment_id"] not in _pending_binance:
                    _pending_binance[row["payment_id"]] = {
                        "discord_id": row["discord_id"],
                        "user_id":    row["user_id"],
                        "pack":       pack_obj,
                        "channel_id": row["channel_id"],
                        "username":   row["username"],
                        "metodo":     row["metodo"],
                    }
                    restored += 1
                # Si el user todavía no tiene una operación asignada en
                # _waiting_comprobante, le asignamos esta (la más reciente
                # gana porque iteramos en ORDER BY created_at DESC).
                if row["user_id"] not in _waiting_comprobante:
                    _waiting_comprobante[row["user_id"]] = row["payment_id"]
                    waiting_restored += 1
        if restored or waiting_restored:
            log.info(
                "Recuperé %d operación(es) pendiente(s) y %d espera(s) de "
                "comprobante de la DB",
                restored, waiting_restored,
            )
    except Exception:
        log.exception("No pude recuperar pending_payments al iniciar")

    # Recargar tickets activos desde DB (sobreviven a reinicios del bot)
    try:
        tickets_db = database.load_all_tickets()
        reloaded = 0
        for t in tickets_db:
            cid = t["channel_id"]
            # Verificar que el canal todavía exista en Discord
            ch = client.get_channel(cid)
            if ch is None:
                # El canal fue eliminado manualmente — limpiar de DB
                try:
                    database.delete_ticket(cid)
                except Exception:
                    pass
                continue
            with _tickets_lock:
                if cid not in _active_tickets:
                    _active_tickets[cid] = {
                        "ticket_num": t["ticket_num"],
                        "user": None,
                        "user_id": t["user_id"],
                        "user_name": t["user_name"],
                        "motivo": t["motivo"],
                        "canal_id": cid,
                        "created_at": t["created_at"],
                    }
                    reloaded += 1
        if reloaded:
            log.info("Recargué %d ticket(s) activo(s) desde DB", reloaded)
    except Exception:
        log.exception("No pude recargar tickets desde DB al iniciar")

    # Sync de roles: cualquier user con créditos > 0 que NO tenga el rol
    # Verificado, se lo asignamos. Esto rescata casos donde el pago se aprobó
    # pero el user nunca pasó por el flujo de verificación (problema histórico
    # antes del fix).
    async def _sync_roles_verificados():
        try:
            users = database.list_users_with_credits()
        except Exception:
            log.exception("No pude listar users con créditos para sync de rol")
            return
        asignados = 0
        for u in users:
            try:
                uid = int(u["discord_id"])
            except (ValueError, TypeError):
                continue
            try:
                ok = await _asignar_rol_verificado(
                    uid, motivo="Sync inicial: ya tenía créditos"
                )
                if ok:
                    asignados += 1
            except Exception:
                log.exception("Error en sync de rol Verificado para %s", uid)
        if asignados:
            log.info("Sync de rol Verificado: %d user(s) procesados (incluye "
                     "los que ya lo tenían)", asignados)
    asyncio.create_task(_sync_roles_verificados())
    invite_url = (
        f"https://discord.com/api/oauth2/authorize?client_id={client.user.id}"
        f"&permissions=2147485696&scope=bot%20applications.commands"
    )
    log.info("Link para invitar al bot: %s", invite_url)
    wa_webhook_url = f"{_public_base_url()}/api/whatsapp-webhook"
    log.info("URL webhook WhatsApp → configura en Twilio Sandbox: %s", wa_webhook_url)

    # Estrategia: intentar sync por guild (instantáneo). Si falla, caer a
    # sync global SIN borrar comandos previamente (evita ventana sin comandos).
    guild_obj: discord.Guild | None = _resolver_guild()
    guild_disc = discord.Object(id=int(GUILD_ID)) if GUILD_ID else None

    # 1) Intentar sync por guild usando el objeto resuelto del cache
    for g in ([guild_obj] if guild_obj else []) + ([guild_disc] if guild_disc and guild_disc != guild_obj else []):
        if g is None:
            continue
        try:
            tree.copy_global_to(guild=g)
            synced = await tree.sync(guild=g)
            log.info("Slash commands sync (guild): %d comandos", len(synced))
            # Éxito: ahora sí limpiamos globales para evitar duplicados
            try:
                await client.http.bulk_upsert_global_commands(client.user.id, [])
                log.info("Comandos globales limpiados (Discord-side).")
            except Exception:
                pass
            return
        except discord.Forbidden:
            log.warning("Forbidden en guild sync (scope applications.commands no otorgado). Intentando global sync.")
        except discord.HTTPException as e:
            if e.status == 429:
                log.warning("Rate limit en guild sync (%s). Los comandos existentes siguen activos.", e.retry_after)
                return
            log.exception("HTTPException en guild sync, intento global")
        except Exception:
            log.exception("Error en guild sync, intento global")

    # 2) Fallback: sync global (sin limpiar comandos antes)
    try:
        synced = await tree.sync()
        log.info(
            "Slash commands sync global: %d (puede tardar hasta 1h en aparecer)",
            len(synced),
        )
    except discord.HTTPException as e:
        if e.status == 429:
            log.warning("Rate limit en global sync. Los comandos existentes siguen activos hasta la próxima sincronización.")
        else:
            log.exception("HTTPException en global sync")
    except Exception:
        log.exception("Error sincronizando slash commands global")


# ---------------------------------------------------------------------------
# Canal de ventas públicas + notificación desde el webhook → DM al usuario
# ---------------------------------------------------------------------------
_id_canal_ventas: int | None = None


async def _obtener_canal_ventas() -> discord.TextChannel | None:
    """Devuelve (creando si no existe) el canal #💰・ventas."""
    global _id_canal_ventas
    guild = _resolver_guild()
    if guild is None:
        return None

    if _id_canal_ventas:
        ch = guild.get_channel(_id_canal_ventas)
        if ch:
            return ch

    # Buscar por nombre
    ch = discord.utils.find(
        lambda c: isinstance(c, discord.TextChannel) and "ventas" in c.name.lower(),
        guild.channels,
    )
    if ch:
        _id_canal_ventas = ch.id
        return ch

    # Crear si no existe — privado: solo Owner + rol Admin lo ven
    overwrites: dict = {
        guild.default_role: discord.PermissionOverwrite(
            view_channel=False, send_messages=False, read_message_history=False
        ),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, embed_links=True,
            read_message_history=True,
        ),
    }
    if ADMIN_ROLE_ID:
        try:
            admin_role = guild.get_role(int(ADMIN_ROLE_ID))
            if admin_role:
                overwrites[admin_role] = discord.PermissionOverwrite(
                    view_channel=True, read_message_history=True,
                    send_messages=True, embed_links=True, attach_files=True,
                )
        except (TypeError, ValueError):
            pass
    ch = await guild.create_text_channel("💰・ventas", overwrites=overwrites)
    _id_canal_ventas = ch.id
    log.info("Canal #ventas creado (id=%s)", ch.id)
    return ch


async def _publicar_en_ventas(
    discord_id: str,
    pack: payments.Pack,
    amount: float,
    metodo: str,
) -> None:
    """Publica el registro de venta aprobada en el canal público #ventas."""
    canal = await _obtener_canal_ventas()
    if canal is None:
        return

    try:
        user = await client.fetch_user(int(discord_id))
        nombre_display = f"{user.name} ({discord_id})"
    except Exception:
        nombre_display = f"ID {discord_id}"

    ahora = _ahora_arg().strftime("%-d/%-m/%Y, %-I:%M %p")

    embed = discord.Embed(
        title="🔵 Marke Panel | Venta aprobada",
        color=0x1ABC9C,
    )
    embed.add_field(
        name="👥 | CLIENTE:",
        value=nombre_display,
        inline=False,
    )
    embed.add_field(
        name="🛒 | PRODUCTO(S) COMPRADO(S):",
        value=f"• 1x {pack.nombre}",
        inline=False,
    )
    embed.add_field(
        name="🏷️ | DESCUENTOS:",
        value="Ningún descuento aplicado",
        inline=False,
    )
    embed.add_field(
        name="💰 | TOTAL PAGADO:",
        value=f"${amount:,.2f} ARS" if amount > 0 else f"${pack.precio:,.2f} ARS",
        inline=False,
    )
    embed.add_field(
        name="📅 | FECHA/HORA:",
        value=ahora,
        inline=False,
    )
    embed.add_field(
        name="⭐ | CALIFICACIÓN:",
        value="Ninguna calificación registrada.",
        inline=False,
    )
    embed.set_footer(text="Marke Panel - Todos los derechos reservados.")

    try:
        await canal.send(embed=embed)
    except Exception:
        log.exception("No pude postear en #ventas")


# ---------------------------------------------------------------------------
# Canal de logs de registros — solo admins ven cada /registrar y /cambiar-ip
# ---------------------------------------------------------------------------
_id_canal_logs: int | None = None


async def _obtener_canal_logs() -> discord.TextChannel | None:
    """Devuelve (creando si no existe) el canal #📋・logs-registros."""
    global _id_canal_logs
    guild = _resolver_guild()
    if guild is None:
        return None

    if _id_canal_logs:
        ch = guild.get_channel(_id_canal_logs)
        if isinstance(ch, discord.TextChannel):
            return ch

    # Buscar por nombre
    for ch in guild.text_channels:
        if "logs-registros" in ch.name.lower():
            _id_canal_logs = ch.id
            return ch

    # Crear si no existe — privado: solo bot + rol Admin
    overwrites: dict = {
        guild.default_role: discord.PermissionOverwrite(
            view_channel=False, send_messages=False, read_message_history=False,
        ),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, embed_links=True,
            read_message_history=True, manage_messages=True,
        ),
    }
    if ADMIN_ROLE_ID:
        try:
            admin_role = guild.get_role(int(ADMIN_ROLE_ID))
            if admin_role:
                overwrites[admin_role] = discord.PermissionOverwrite(
                    view_channel=True, read_message_history=True,
                    send_messages=True, embed_links=True, attach_files=True,
                )
        except (TypeError, ValueError):
            pass

    # Ubicar bajo la misma categoría que #ventas si existe
    categoria = None
    canal_ventas = await _obtener_canal_ventas()
    if canal_ventas is not None:
        categoria = canal_ventas.category

    try:
        ch = await guild.create_text_channel(
            "📋・logs-registros",
            overwrites=overwrites,
            category=categoria,
            topic="Auditoría de /registrar y /cambiar-ip — éxitos y fallos.",
            reason="Setup automático — canal de logs",
        )
        _id_canal_logs = ch.id
        log.info("Canal #logs-registros creado (id=%s)", ch.id)
        return ch
    except Exception:
        log.exception("No pude crear el canal #logs-registros")
        return None


async def _configurar_canal_logs() -> None:
    """Asegura los permisos correctos del canal de logs en cada arranque."""
    await client.wait_until_ready()
    canal = await _obtener_canal_logs()
    if canal is None:
        return
    guild = canal.guild
    bot_member = guild.me
    try:
        await canal.set_permissions(
            guild.default_role,
            view_channel=False, send_messages=False, read_message_history=False,
        )
        if ADMIN_ROLE_ID:
            try:
                admin_role = guild.get_role(int(ADMIN_ROLE_ID))
            except (TypeError, ValueError):
                admin_role = None
            if admin_role:
                await canal.set_permissions(
                    admin_role,
                    view_channel=True, read_message_history=True,
                    send_messages=True, embed_links=True, attach_files=True,
                )
        if bot_member:
            await canal.set_permissions(
                bot_member,
                view_channel=True, send_messages=True,
                read_message_history=True, manage_messages=True,
                embed_links=True, attach_files=True,
            )
        log.info("Permisos de #%s configurados (solo admins).", canal.name)
    except discord.Forbidden:
        log.warning("Sin permisos para configurar #%s.", canal.name)
    except Exception:
        log.exception("Error configurando #%s.", canal.name)


async def _log_registro(
    *,
    user: discord.abc.User,
    accion: str,                       # "registrar" | "cambiar-ip"
    ip: str,
    dias: int | None = None,
    horas_restantes: float | None = None,  # para cambiar-ip: horas exactas restantes
    servicio: str | None = None,
    usuario_panel: str | None = None,
    exito: bool,
    motivo: str = "",                  # mensaje de error/detalle
    creditos_antes: float | None = None,
    creditos_despues: float | None = None,
    costo: float | None = None,
) -> None:
    """Publica una entrada de auditoría en #logs-registros.

    Nunca tira excepciones hacia afuera — el log es best-effort.
    """
    try:
        canal = await _obtener_canal_logs()
        if canal is None:
            log.warning("_log_registro: canal de logs no disponible, saltando")
            return

        es_cambio_ip = accion == "cambiar-ip"

        if exito:
            color = 0x3498DB if es_cambio_ip else 0x2ECC71
            icono = "🔄" if es_cambio_ip else "✅"
            titulo = f"{icono} {accion} — éxito"
        else:
            color = 0xE74C3C
            icono = "❌"
            titulo = f"{icono} {accion} — falló"

        embed = discord.Embed(title=titulo, color=color)
        embed.set_author(
            name=f"{user} ({user.id})",
            icon_url=getattr(user.display_avatar, "url", None),
        )
        embed.add_field(name="IP", value=f"`{ip or '(vacía)'}`", inline=True)

        # Para cambiar-ip mostramos las horas exactas restantes
        if es_cambio_ip and horas_restantes is not None:
            h_total = int(horas_restantes)
            dias_disp = h_total // 24
            hs_disp   = h_total % 24
            tiempo_label = f"{dias_disp}d {hs_disp}h" if dias_disp else f"{hs_disp}h"
            embed.add_field(
                name="Tiempo restante aplicado",
                value=f"`{tiempo_label}` (~{horas_restantes:.1f}h exactas)",
                inline=True,
            )
        elif dias is not None:
            if dias == 0:
                duracion = "3 horas (prueba)"
            else:
                duracion = f"{dias} día(s)"
            embed.add_field(name="Duración", value=duracion, inline=True)

        if servicio:
            embed.add_field(name="Servicio", value=servicio, inline=True)
        if usuario_panel:
            embed.add_field(
                name="Usuario panel", value=f"`{usuario_panel}`", inline=True,
            )
        if costo is not None:
            embed.add_field(
                name="Costo",
                value=f"{_fmt_creditos(costo)} crédito(s)",
                inline=True,
            )
        if creditos_antes is not None and creditos_despues is not None:
            embed.add_field(
                name="Saldo",
                value=(
                    f"{_fmt_creditos(creditos_antes)} → "
                    f"**{_fmt_creditos(creditos_despues)}**"
                ),
                inline=True,
            )
        if motivo:
            texto = motivo if len(motivo) <= 1000 else motivo[:1000] + "…"
            embed.add_field(
                name="Detalle" if exito else "Motivo del fallo",
                value=f"```\n{texto}\n```",
                inline=False,
            )
        embed.timestamp = discord.utils.utcnow()
        embed.set_footer(text=f"Marke Panel • discord_id: {user.id}")

        await canal.send(embed=embed)
    except Exception:
        log.exception("No pude postear en #logs-registros")


# ---------------------------------------------------------------------------
async def _log_key(
    evento: str,
    key: str,
    discord_id: str | int,
    dias: int | None = None,
    metodo: str | None = None,
    ip: str | None = None,
    respuesta_tg: str | None = None,
    error: str | None = None,
    admin_id: str | int | None = None,
) -> None:
    """Publica un evento de key en #logs-registros. Best-effort, nunca tira."""
    try:
        canal = await _obtener_canal_logs()
        if canal is None:
            return

        EVENTOS = {
            "gen_ok":        (0x2ECC71, "🔑", "Key generada y registrada"),
            "gen_error":     (0xE74C3C, "❌", "Error al registrar key en Telegram"),
            "enviada_ok":    (0x3498DB, "📤", "Key enviada manualmente por DM"),
            "dm_bloqueado":  (0xE67E22, "⚠️", "Key generada — DM bloqueado"),
            "activada_ok":   (0x27AE60, "✅", "Key activada con /key"),
            "activada_error":(0xE74C3C, "❌", "Error al activar key con /key"),
        }
        color, icono, titulo = EVENTOS.get(evento, (0x95A5A6, "ℹ️", evento))

        embed = discord.Embed(title=f"{icono} {titulo}", color=color)
        embed.add_field(name="🔑 Key", value=f"`{key}`", inline=True)
        embed.add_field(name="👤 Usuario", value=f"<@{discord_id}>", inline=True)
        if dias is not None:
            embed.add_field(name="📅 Días", value=str(dias), inline=True)
        if metodo:
            embed.add_field(name="💳 Método", value=metodo, inline=True)
        if ip:
            embed.add_field(name="🌐 IP activada", value=f"`{ip}`", inline=True)
        if admin_id:
            embed.add_field(name="🛡️ Admin", value=f"<@{admin_id}>", inline=True)
        if respuesta_tg:
            txt = respuesta_tg if len(respuesta_tg) <= 500 else respuesta_tg[:500] + "…"
            embed.add_field(name="🤖 Respuesta Telegram", value=f"```\n{txt}\n```", inline=False)
        if error:
            txt = str(error) if len(str(error)) <= 500 else str(error)[:500] + "…"
            embed.add_field(name="⚠️ Error", value=f"```\n{txt}\n```", inline=False)
        embed.timestamp = discord.utils.utcnow()
        embed.set_footer(text=f"Marke Panel • discord_id: {discord_id}")
        await canal.send(embed=embed)
    except Exception:
        log.exception("_log_key: no pude postear en #logs-registros")


# ---------------------------------------------------------------------------
def _notificar_pago(
    discord_id: str,
    pack: payments.Pack,
    total_creditos: float,
    amount: float = 0.0,
) -> None:
    """Llamado desde Flask cuando se aprueba un pago. Envía DM al usuario y publica en #ventas."""
    if not client.is_ready():
        log.warning("Cliente Discord no listo, no puedo notificar a %s", discord_id)
        return

    async def _send():
        try:
            user = await client.fetch_user(int(discord_id))

            if pack.categoria == "sensi":
                embed_pago = discord.Embed(
                    title="✅  Pago aprobado — Sensi Xitada",
                    description=(
                        f"Recibimos tu pago del pack **{pack.nombre}**.\n"
                        f"Se sumaron **{_fmt_creditos(pack.creditos)}** crédito(s) de sensi.\n"
                        f"Tu nuevo saldo: **{_fmt_creditos(total_creditos)}** crédito(s) de sensi.\n\n"
                        f"🎯 Usá `/sensibilidad` en el canal <#{CANAL_SENSI_ID}> con el modelo de tu celular."
                    ),
                    color=0xFF4500,
                )
                embed_pago.set_footer(text="Marke Panel • Sensibilidades actualizadas para la ultima OB53")
                await user.send(embed=embed_pago)

            elif pack.categoria == "android":
                await user.send(
                    f"✅ **¡Pago aprobado!** (Mercado Pago)\n"
                    f"Tu **{pack.nombre}** está listo. Enviando descarga ahora... 👇"
                )
                await _enviar_entrega_android(user, pack)

            elif pack.categoria == "ios":
                await user.send(
                    f"✅ **¡Pago aprobado!** (Mercado Pago)\n"
                    f"Tu archivo iOS **{pack.nombre}** está listo. Enviando enlace ahora... 👇"
                )
                await _enviar_entrega_ios(user, pack)

            else:
                # Pack de proxy: generar key y entregar igual que pago manual
                dias_proxy = int(pack.creditos)
                proxy_key_mp = _generar_key_proxy()
                _mp_key_registrada = False
                try:
                    await telegram_client.cmd_gen(proxy_key_mp, dias_proxy)
                    _mp_key_registrada = True
                    log.info(
                        "🔑 KEY PAGO MP — key=%s dias=%d pack=%s usuario=%s",
                        proxy_key_mp, dias_proxy, pack.id, discord_id,
                    )
                    asyncio.create_task(_log_key(
                        "gen_ok", proxy_key_mp, discord_id,
                        dias=dias_proxy, metodo="Mercado Pago",
                    ))
                except Exception as _exc_mp:
                    log.exception(
                        "Error registrando key MP via Telegram para %s pack=%s — key=%s",
                        discord_id, pack.id, proxy_key_mp,
                    )
                    asyncio.create_task(_log_key(
                        "gen_error", proxy_key_mp, discord_id,
                        dias=dias_proxy, metodo="Mercado Pago", error=str(_exc_mp),
                    ))
                    try:
                        canal_ventas = await _obtener_canal_ventas()
                        if canal_ventas:
                            await canal_ventas.send(
                                f"⚠️ **KEY MP NO REGISTRADA — entrega manual requerida**\n"
                                f"Usuario: <@{discord_id}>\n"
                                f"🔑 Key generada: ||`{proxy_key_mp}`|| ({dias_proxy}d)\n"
                                f"Error Telegram: `{_exc_mp}`\n"
                                f"Usar `/enviar-key` cuando el sistema Telegram esté disponible."
                            )
                    except Exception:
                        log.exception("No pude avisar en canal ventas del error MP gen para %s", discord_id)

                if not _mp_key_registrada:
                    log.error(
                        "KEY MP NO ENTREGADA — no se registró en Telegram. user=%s key=%s",
                        discord_id, proxy_key_mp,
                    )
                    return

                embed_key_mp = discord.Embed(
                    title="✅ ¡Pago aprobado! Tu key de proxy está lista",
                    description=(
                        f"Recibimos tu pago del pack **{pack.nombre}**.\n\n"
                        f"🔑 **Tu key ({dias_proxy} día{'s' if dias_proxy != 1 else ''}):**\n"
                        f"```\n{proxy_key_mp}\n```\n"
                        f"Usá el comando `/key` en el servidor para activarla con tu IP.\n\n"
                        f"🌐 **Servidor:** `108.181.215.247`\n"
                        f"👔 **Puerto Cuello:** `10065`\n"
                        f"👕 **Puerto Pecho:** `10066`\n"
                        f"👤 **Login:** ||DGZADAXFF||\n"
                        f"🔒 **Contraseña:** ||DGZADAXFF||\n\n"
                        f"📋 El tutorial de configuración te lo enviamos por este mismo chat.\n\n"
                        f"¡Muchísimas gracias por comprar en **Sensi Marke**! 🖤\n"
                        f"Recordá estar atento al grupo:\n"
                        f"https://chat.whatsapp.com/DQxndyWBG860vpaVcxam3s"
                    ),
                    color=0x2ECC71,
                )
                try:
                    await user.send(embed=embed_key_mp)
                except Exception:
                    log.exception("No pude enviar key MP por DM a %s — key=%s", discord_id, proxy_key_mp)
                    try:
                        canal_ventas = await _obtener_canal_ventas()
                        if canal_ventas:
                            await canal_ventas.send(
                                f"⚠️ **KEY MP NO ENTREGADA por DM bloqueado**\n"
                                f"Usuario: <@{discord_id}>\n"
                                f"🔑 Key: ||`{proxy_key_mp}`|| ({dias_proxy}d)\n"
                                f"Entregarla manualmente con `/enviar-key`."
                            )
                    except Exception:
                        log.exception("Tampoco pude avisar en canal ventas para %s", discord_id)
                    return

        except Exception:  # noqa: BLE001
            log.exception("No pude mandar DM a %s", discord_id)

        # Publicar en el canal público #ventas
        await _publicar_en_ventas(discord_id, pack, amount, "Mercado Pago")

        # ── Comisión de afiliado (10%) ─────────────────────────────────────
        try:
            referrer_id = database.get_referrer(discord_id)
            if referrer_id:
                rate = database.get_commission_rate(referrer_id)
                commission = pack.precio * rate
                sales_count, new_rate = database.increment_referral_sales(referrer_id)
                nuevo_bal = database.add_referral_commission(referrer_id, commission)
                pct_actual = int(rate * 100)
                pct_nuevo = int(new_rate * 100)
                log.info(
                    "Comisión MP %.2f ARS (%d%%) acreditada a %s por compra de %s",
                    commission, pct_actual, referrer_id, discord_id,
                )
                try:
                    referrer_user = await client.fetch_user(int(referrer_id))
                    ventas_para_subir = 20 - (sales_count % 20) if pct_nuevo < 25 else 0
                    nivel_msg = ""
                    if pct_nuevo > pct_actual:
                        nivel_msg = f"\n\n🚀 **¡Subiste de nivel!** Tu comisión ahora es **{pct_nuevo}%**."
                    elif pct_nuevo < 25 and ventas_para_subir > 0:
                        nivel_msg = f"\n\n📈 Te faltan **{ventas_para_subir}** venta(s) más para subir al **{pct_nuevo + 2}%**."
                    elif pct_nuevo == 25:
                        nivel_msg = "\n\n👑 Estás en el **nivel máximo (25%)**. ¡Seguí así!"
                    await referrer_user.send(
                        f"💰 **¡Comisión de afiliado!**\n"
                        f"Tu referido acaba de comprar **{pack.nombre}** (${pack.precio:,.0f} ARS).\n"
                        f"Recibiste **${commission:,.0f} ARS** de comisión ({pct_actual}%). 🎉\n"
                        f"Saldo total acumulado: **${nuevo_bal:,.0f} ARS**"
                        f"{nivel_msg}\n\n"
                        f"Usá `/perfil` para ver todo tu historial."
                    )
                except Exception:
                    log.warning("No pude notificar comisión a %s por DM", referrer_id)
        except Exception:
            log.exception("Error procesando comisión MP de afiliado para %s", discord_id)

    asyncio.run_coroutine_threadsafe(_send(), client.loop)


# ---------------------------------------------------------------------------
# Boot
# ---------------------------------------------------------------------------
def main() -> None:
    if not DISCORD_TOKEN:
        raise SystemExit("Falta el secret DISCORD_TOKEN")
    if not os.environ.get("MP_ACCESS_TOKEN"):
        log.warning("Falta MP_ACCESS_TOKEN: /comprar fallará hasta cargarlo.")

    database.init_db()

    flask_app = webhook_server.create_app(_notificar_pago, _whatsapp_a_discord)
    t = threading.Thread(
        target=webhook_server.run_flask, args=(flask_app,), daemon=True, name="flask"
    )
    t.start()

    log.info("Iniciando bot de Discord...")
    try:
        client.run(DISCORD_TOKEN, log_handler=None)
    except (discord.errors.DiscordServerError, discord.errors.HTTPException) as e:
        # DiscordServerError = 503 upstream
        # HTTPException 429  = rate limit por demasiadas reconexiones rápidas
        _is_retryable = isinstance(e, discord.errors.DiscordServerError) or (
            isinstance(e, discord.errors.HTTPException) and e.status == 429
        )
        if _is_retryable:
            # Código 42 = señal a start.sh para que reintente con backoff
            log.warning("Discord temporalmente no disponible: %s — saliendo con código 42 para retry", e)
            os._exit(42)
        log.exception("Error HTTP fatal en client.run, abortando.")
        raise
    except Exception:
        log.exception("Error fatal en client.run, abortando.")
        raise


if __name__ == "__main__":
    main()
