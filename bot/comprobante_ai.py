"""Análisis de comprobantes de pago con IA visión (gpt-5-mini).

Devuelve una decisión: 'approved' (auto-aprobar y acreditar), 'manual'
(que el admin confirme a mano) o 'rejected' (rechazar y avisar al user).

La decisión se basa en:
  - Lo que la IA extrae del comprobante (monto, alias, titular, fecha, etc.)
  - Comparación con los datos esperados (alias/CBU/titular configurados como
    secrets, monto del pack, ventana de tiempo)
  - Detección de duplicados por número de operación

Si los secrets de validación no están configurados → SIEMPRE cae a 'manual'
(no se aprueba nada en automático sin datos verificados).
"""
from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from openai import AsyncOpenAI

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Datos esperados (vienen de secrets — si faltan, todo cae a manual)
# ---------------------------------------------------------------------------
def _expected_nx() -> dict[str, str]:
    return {
        "alias":   (os.environ.get("NX_ALIAS") or "").strip(),
        "cbu":     re.sub(r"\D+", "", os.environ.get("NX_CBU") or ""),
        "titular": (os.environ.get("NX_TITULAR") or "").strip(),
    }


def _expected_binance() -> dict[str, str]:
    return {
        "pay_id":  (os.environ.get("BINANCE_PAY_ID") or "").strip(),
        "titular": (os.environ.get("BINANCE_TITULAR") or "").strip(),
    }


# Tolerancia y ventana
TOLERANCIA_MONTO_ARS = 15.0   # ±$15 ARS
VENTANA_MINUTOS      = 40     # comprobantes de hasta 40 min de antigüedad
CONFIANZA_OK         = 0.82   # mínima para auto-aprobar
CONFIANZA_RECHAZO    = 0.40   # debajo de esto, rechazo directo


# ---------------------------------------------------------------------------
# Resultado
# ---------------------------------------------------------------------------
@dataclass
class ResultadoComprobante:
    decision: str                 # 'approved' | 'manual' | 'rejected'
    motivos: list[str] = field(default_factory=list)   # detallados (admin/log)
    motivo_user: str = ""         # genérico, lo que se le muestra al usuario
    datos:   dict[str, Any] = field(default_factory=dict)
    raw:     str = ""

    def resumen(self) -> str:
        d = self.datos
        return (
            f"Monto: {d.get('monto')} {d.get('moneda') or ''} · "
            f"Alias: {d.get('destinatario_alias') or '—'} · "
            f"Titular: {d.get('destinatario_nombre') or '—'} · "
            f"Op#: {d.get('numero_operacion') or '—'} · "
            f"Fecha: {d.get('fecha_iso') or '—'} {d.get('hora') or ''} · "
            f"Confianza: {d.get('confianza_real')}"
        )


# ---------------------------------------------------------------------------
# Helpers de normalización y match
# ---------------------------------------------------------------------------
def _norm(s: str | None) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _alias_match(extraido: str | None, esperado: str) -> bool:
    if not esperado:
        return False
    return _norm(extraido) == _norm(esperado)


def _cbu_match(extraido: str | None, esperado: str) -> bool:
    if not esperado:
        return False
    a = re.sub(r"\D+", "", extraido or "")
    return bool(a) and a == esperado


def _titular_match(extraido: str | None, esperado: str) -> bool:
    """Match laxo: comparten al menos 2 palabras de >=3 letras o >=70%
    de tokens en común."""
    if not esperado or not extraido:
        return False
    e = set(re.findall(r"[a-záéíóúñ]{3,}", esperado.lower()))
    x = set(re.findall(r"[a-záéíóúñ]{3,}", extraido.lower()))
    if not e or not x:
        return False
    comunes = len(e & x)
    return comunes >= 2 or comunes / max(len(e), 1) >= 0.7


def _parsear_fecha(fecha_iso: str | None, hora: str | None) -> datetime | None:
    if not fecha_iso:
        return None
    try:
        if hora and re.match(r"\d{1,2}:\d{2}", hora):
            return datetime.fromisoformat(f"{fecha_iso} {hora}")
        return datetime.fromisoformat(fecha_iso)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Llamada a la IA visión
# ---------------------------------------------------------------------------
_PROMPT_VISION = (
    "Sos un perito experto en detección de comprobantes de pago FALSIFICADOS "
    "(argentinos: Mercado Pago, Naranja X, Binance Pay, Brubank, Modo, Galicia, "
    "Santander, BBVA, Macro, Ualá, Personal Pay, etc.). Tu trabajo es decidir "
    "si esta captura es REAL o EDITADA/INVENTADA, prestando ATENCIÓN ESPECIAL "
    "A LA TIPOGRAFÍA.\n\n"
    "DEVOLVÉ EXCLUSIVAMENTE un JSON válido (sin texto antes ni después, sin "
    "markdown, sin ```). Estructura exacta:\n\n"
    "{\n"
    '  "es_comprobante": bool,            // true si parece comprobante\n'
    '  "tipo": "transferencia"|"binance"|"mercadopago"|"otro",\n'
    '  "monto": number|null,              // número (sin símbolos)\n'
    '  "moneda": "ARS"|"USDT"|"USD"|null,\n'
    '  "fecha_iso": "YYYY-MM-DD"|null,    // fecha de la operación\n'
    '  "hora": "HH:MM"|null,              // 24h\n'
    '  "destinatario_alias": string|null, // alias.tipo.cosa\n'
    '  "destinatario_cbu": string|null,   // 22 dígitos sin espacios\n'
    '  "destinatario_nombre": string|null,\n'
    '  "remitente_nombre": string|null,\n'
    '  "numero_operacion": string|null,   // ID/comprobante/referencia\n'
    '  "banco_origen": string|null,\n'
    '  "estado": "exitosa"|"pendiente"|"rechazada"|null,\n'
    '  "tipografia_consistente": bool,    // true si toda la captura usa una '
    'misma familia de fuente coherente con la app\n'
    '  "tipografia_coincide_app": bool,   // true si la fuente del texto '
    'coincide con la fuente OFICIAL de la app/banco que dice ser\n'
    '  "confianza_real": number,          // 0.0–1.0 — qué tan real parece\n'
    '  "señales_alarma": [string]         // lista de banderas rojas\n'
    "}\n\n"
    "TIPOGRAFÍAS OFICIALES DE REFERENCIA:\n"
    "- Mercado Pago: 'Proxima Nova' / sans-serif limpia, azul #009EE3, "
    "íconos redondeados.\n"
    "- Naranja X: sans-serif moderna tipo Inter/Helvetica, naranja #FF6B00, "
    "fondo blanco/gris claro, montos en negrita gruesa.\n"
    "- Binance: 'Binance Sans' / IBM Plex, fondo oscuro o claro, amarillo "
    "#F0B90B, números monoespaciados en operaciones.\n"
    "- Brubank: sans-serif geométrica, fondo blanco, violeta corporativo.\n"
    "- Modo: sans-serif limpia con verde/azul.\n"
    "- Galicia / Santander / BBVA / Macro / Ualá: sans-serif corporativas "
    "consistentes, cada una con su color.\n\n"
    "BANDERAS ROJAS QUE INDICAN COMPROBANTE FALSO (cargalas en señales_alarma "
    "y BAJÁ confianza_real a 0.3 o menos):\n"
    "- Tipografía inconsistente: el monto está en una fuente, el alias en "
    "otra, las fechas en otra. Las apps oficiales SIEMPRE usan una sola "
    "familia tipográfica.\n"
    "- Tipografía de la captura NO coincide con la oficial de la app que "
    "dice ser (ej. dice 'Naranja X' pero usa Times New Roman, Comic Sans, "
    "Arial básico, Calibri o cualquier fuente serif/decorativa).\n"
    "- Texto con bordes pixelados, halo, anti-aliasing distinto del resto, "
    "o que claramente fue pegado encima (típico de Paint/Photoshop).\n"
    "- Números del monto desalineados con la línea base del resto del texto, "
    "o con espaciado raro entre dígitos.\n"
    "- Color del texto del monto distinto al color usado por el resto de la "
    "interfaz (ej. negro puro #000 cuando la app usa gris oscuro).\n"
    "- Logos borrosos, deformados, en baja resolución, o con colores que no "
    "coinciden con los oficiales.\n"
    "- Layout/espaciado que no se parece al de la app oficial.\n"
    "- Fechas inconsistentes (dice 'hoy' pero la fecha es vieja, o el día "
    "de la semana no corresponde).\n"
    "- Números de operación con formato sospechoso (muy cortos, todos ceros, "
    "o muy distintos al patrón típico de la app).\n"
    "- Captura de captura (foto de pantalla a otra pantalla).\n\n"
    "REGLAS:\n"
    "- Si NO es un comprobante (foto random, meme, captura de chat sin datos "
    "de transferencia), poné es_comprobante=false y confianza_real=0.\n"
    "- Si la tipografía NO coincide con la app oficial → "
    "tipografia_coincide_app=false y confianza_real ≤ 0.25.\n"
    "- Si hay tipografía mezclada dentro de la misma captura → "
    "tipografia_consistente=false y confianza_real ≤ 0.30.\n"
    "- Una captura con tipografía perfecta y consistente con la app oficial → "
    "confianza_real ≥ 0.85.\n"
    "- Si dudás de un campo, ponelo en null. NUNCA inventes datos.\n"
)


async def _llamar_ia_vision(
    client: AsyncOpenAI, image_url: str
) -> tuple[dict[str, Any], str]:
    resp = await client.chat.completions.create(
        model="gpt-5-mini",
        max_completion_tokens=1500,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _PROMPT_VISION},
                    {
                        "type": "image_url",
                        "image_url": {"url": image_url, "detail": "high"},
                    },
                ],
            }
        ],
    )
    raw = (resp.choices[0].message.content or "").strip()
    # A veces el modelo igual pone bloque markdown; lo limpiamos.
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE)
    try:
        datos = json.loads(raw)
    except json.JSONDecodeError:
        # Intentar extraer el primer { ... } del texto
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                datos = json.loads(m.group(0))
            except Exception:
                datos = {"es_comprobante": False, "confianza_real": 0.0}
        else:
            datos = {"es_comprobante": False, "confianza_real": 0.0}
    return datos, raw


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------
async def analizar(
    *,
    client: AsyncOpenAI,
    image_url: str,
    metodo: str,                # "NaranjaX" | "Binance"
    monto_esperado_ars: float,  # precio del pack en ARS
    op_id: str,
    op_id_existe_fn=None,       # callable(numero_operacion: str) -> bool
) -> ResultadoComprobante:
    """Analiza una imagen de comprobante y decide si aprobar / manual /
    rechazar. Nunca auto-aprueba si faltan secrets de validación."""

    # 1) Llamar a IA
    try:
        datos, raw = await _llamar_ia_vision(client, image_url)
    except Exception as exc:
        log.exception("Error llamando IA visión para %s", op_id)
        return ResultadoComprobante(
            decision="manual",
            motivos=[f"No pude analizar la imagen ({type(exc).__name__})."],
            raw="",
        )

    res = ResultadoComprobante(decision="manual", datos=datos, raw=raw)

    # Mensaje genérico al user para todos los rechazos por sospecha
    # (no le damos pistas de qué chequeamos)
    MSG_GENERICO = (
        "El comprobante no pudo ser confirmado. "
        "Si creés que es un error, abrí un ticket para revisión manual."
    )

    # 2) Checks duros (rechazo)
    if not datos.get("es_comprobante"):
        res.decision = "rejected"
        res.motivos.append("La imagen no parece un comprobante de pago.")
        res.motivo_user = (
            "La imagen enviada no parece ser un comprobante de pago válido. "
            "Mandá la captura completa del comprobante."
        )
        return res

    confianza = float(datos.get("confianza_real") or 0)
    if confianza < CONFIANZA_RECHAZO:
        res.decision = "rejected"
        res.motivos.append(
            f"Confianza baja ({confianza:.0%}) — posible comprobante falso."
        )
        if datos.get("señales_alarma"):
            res.motivos.append("Señales: " + ", ".join(datos["señales_alarma"]))
        res.motivo_user = MSG_GENERICO
        return res

    if datos.get("estado") in ("rechazada", "pendiente"):
        res.decision = "rejected"
        res.motivos.append(
            f"La operación figura como '{datos['estado']}' en el comprobante."
        )
        res.motivo_user = (
            f"La operación del comprobante figura como **{datos['estado']}**. "
            f"Esperá a que se acredite y volvé a mandar el comprobante."
        )
        return res

    # 2.b) Tipografía: si no coincide con la app oficial o es inconsistente
    # dentro de la misma captura → rechazo directo (señal clarísima de edición).
    if datos.get("tipografia_coincide_app") is False:
        res.decision = "rejected"
        res.motivos.append(
            "Tipografía no coincide con la fuente oficial de la app."
        )
        if datos.get("señales_alarma"):
            res.motivos.append("Señales: " + ", ".join(datos["señales_alarma"]))
        res.motivo_user = MSG_GENERICO
        return res

    if datos.get("tipografia_consistente") is False:
        res.decision = "rejected"
        res.motivos.append(
            "Tipografías mezcladas dentro de la captura (edición Paint/PS)."
        )
        if datos.get("señales_alarma"):
            res.motivos.append("Señales: " + ", ".join(datos["señales_alarma"]))
        res.motivo_user = MSG_GENERICO
        return res

    # 2.c) Palabras clave críticas en señales de alarma
    señales = " ".join(datos.get("señales_alarma") or []).lower()
    palabras_criticas = (
        "tipograf", "fuente", "editad", "photoshop", "paint", "manipulac",
        "montaje", "retoqu", "pegad", "halo", "pixelad",
    )
    if any(p in señales for p in palabras_criticas):
        res.decision = "rejected"
        res.motivos.append(
            "Indicios de edición: " + ", ".join(datos["señales_alarma"])
        )
        res.motivo_user = MSG_GENERICO
        return res

    # 3) Duplicado por número de operación
    numero_op = (datos.get("numero_operacion") or "").strip()
    if numero_op and op_id_existe_fn and op_id_existe_fn(numero_op):
        res.decision = "rejected"
        res.motivos.append(
            f"El número de operación {numero_op} ya fue usado en otra compra."
        )
        res.motivo_user = (
            "Ese número de operación ya fue usado en otra compra anteriormente."
        )
        return res

    # 4) Fecha dentro de la ventana (hora Argentina, UTC-3)
    fecha = _parsear_fecha(datos.get("fecha_iso"), datos.get("hora"))
    if fecha:
        ahora_arg = datetime.utcnow() - timedelta(hours=3)
        if fecha > ahora_arg + timedelta(minutes=2):
            res.decision = "rejected"
            res.motivos.append("Fecha del comprobante es del futuro.")
            res.motivo_user = (
                "La fecha del comprobante no es válida."
            )
            return res
        if ahora_arg - fecha > timedelta(minutes=VENTANA_MINUTOS):
            antiguedad_min = int((ahora_arg - fecha).total_seconds() // 60)
            res.decision = "rejected"
            res.motivos.append(
                f"Comprobante con {antiguedad_min} min de antigüedad "
                f"(máx {VENTANA_MINUTOS} min)."
            )
            res.motivo_user = (
                f"El comprobante tiene **{antiguedad_min} minutos** de "
                f"antigüedad. Solo se aceptan transferencias hechas en los "
                f"últimos **{VENTANA_MINUTOS} minutos**.\n"
                f"Hacé el pago de nuevo y mandá el comprobante recién hecho."
            )
            return res

    # 5) Validación específica por método
    monto = datos.get("monto")
    moneda = (datos.get("moneda") or "").upper()

    if metodo == "NaranjaX":
        esperado = _expected_nx()
        # Si faltan secrets → manual
        if not (esperado["alias"] or esperado["cbu"]):
            res.decision = "manual"
            res.motivos.append(
                "No tengo configurado el alias/CBU de NX para validar "
                "automáticamente. Confirmalo a mano."
            )
            return res

        # Monto
        if monto is None or moneda not in ("", "ARS"):
            res.decision = "manual"
            res.motivos.append("No pude leer el monto en pesos con seguridad.")
        elif abs(float(monto) - monto_esperado_ars) > TOLERANCIA_MONTO_ARS:
            res.decision = "rejected"
            res.motivos.append(
                f"Monto del comprobante (${monto:,.0f} ARS) ≠ pack "
                f"(${monto_esperado_ars:,.0f} ARS)."
            )
            res.motivo_user = (
                f"El monto del comprobante (**${float(monto):,.0f} ARS**) no "
                f"coincide con el del pack (**${monto_esperado_ars:,.0f} ARS**)."
            )
            return res

        # Destinatario (alias O CBU O titular)
        alias_ok   = _alias_match(datos.get("destinatario_alias"), esperado["alias"])
        cbu_ok     = _cbu_match(datos.get("destinatario_cbu"), esperado["cbu"])
        titular_ok = _titular_match(datos.get("destinatario_nombre"), esperado["titular"])
        match_destinatario = alias_ok or cbu_ok or titular_ok

        if not match_destinatario:
            res.decision = "rejected"
            res.motivos.append(
                "Destinatario del comprobante no coincide con la cuenta de NX."
            )
            res.motivo_user = (
                "El destinatario del comprobante no coincide con la cuenta "
                "del bot. Asegurate de transferir al alias/CBU correcto."
            )
            return res
        # Si solo matcheó por titular y no por alias/cbu → manual (más seguro)
        if titular_ok and not (alias_ok or cbu_ok):
            res.decision = "manual"
            res.motivos.append(
                "El titular coincide pero no pude verificar el alias/CBU. "
                "Confirmalo a mano."
            )

    elif metodo == "Binance":
        esperado_bn = _expected_binance()
        if not (esperado_bn["pay_id"] or esperado_bn["titular"]):
            res.decision = "manual"
            res.motivos.append(
                "No tengo configurado el Pay ID/titular de Binance para validar "
                "automáticamente. Confirmalo a mano."
            )
            return res

        pay_id_extraido = _norm(datos.get("numero_operacion") or "")
        pay_id_esperado = _norm(esperado_bn["pay_id"])
        titular_ok_bn   = _titular_match(
            datos.get("remitente_nombre") or datos.get("destinatario_nombre"),
            esperado_bn["titular"],
        )
        pay_id_ok = bool(pay_id_esperado) and (
            pay_id_esperado in pay_id_extraido
            or pay_id_extraido in pay_id_esperado
        )

        if not pay_id_ok and not titular_ok_bn:
            res.decision = "rejected"
            res.motivos.append(
                "Pay ID y titular de Binance no coinciden con los datos configurados."
            )
            res.motivo_user = (
                "El Pay ID o titular del comprobante de Binance no coincide con "
                "la cuenta del bot. Asegurate de enviar al Pay ID correcto."
            )
            return res

        if pay_id_ok and confianza >= CONFIANZA_OK:
            res.decision = "approved"
            res.motivos.append(
                f"Binance auto-aprobado: Pay ID verificado y confianza {confianza:.0%}."
            )
            return res

        res.decision = "manual"
        res.motivos.append(
            "Binance: Pay ID detectado pero confianza insuficiente para auto-aprobar. "
            "Confirmalo a mano."
        )
        return res

    else:
        res.decision = "manual"
        res.motivos.append(f"Método desconocido: {metodo}.")
        return res

    # 6) Confianza final para aprobar
    if res.decision == "manual":
        # ya seteado arriba con motivo
        return res

    if confianza >= CONFIANZA_OK:
        res.decision = "approved"
        res.motivos.append(
            f"Auto-aprobado: monto, destinatario y fecha verificados "
            f"(confianza {confianza:.0%})."
        )
        if datos.get("señales_alarma"):
            # Aunque haya señales menores, si todo lo demás coincide
            # se aprueba pero se loguea.
            log.warning(
                "Auto-aprobado %s con señales: %s",
                op_id, datos["señales_alarma"],
            )
        return res

    # Confianza media → manual
    res.decision = "manual"
    res.motivos.append(
        f"Confianza media de la IA ({confianza:.0%}). Confirmalo a mano."
    )
    return res
