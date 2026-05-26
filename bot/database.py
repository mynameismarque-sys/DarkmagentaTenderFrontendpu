"""SQLite database para tracking de créditos por usuario de Discord."""
import sqlite3
import threading
import uuid as _uuid
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "credits.db"
_lock = threading.Lock()


def init_db() -> None:
    """Crea las tablas si no existen y aplica migraciones."""
    with _lock, _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                discord_id      TEXT PRIMARY KEY,
                credits         REAL NOT NULL DEFAULT 0,
                sensi_credits   REAL NOT NULL DEFAULT 0,
                referral_balance REAL NOT NULL DEFAULT 0
            )
            """
        )
        # Migraciones: agregar columnas si la tabla ya existía sin ellas
        cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
        if "sensi_credits" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN sensi_credits REAL NOT NULL DEFAULT 0")
        if "referral_balance" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN referral_balance REAL NOT NULL DEFAULT 0")
        if "referral_sales_count" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN referral_sales_count INTEGER NOT NULL DEFAULT 0")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                payment_id    TEXT PRIMARY KEY,
                discord_id    TEXT NOT NULL,
                pack          TEXT NOT NULL,
                credits_added REAL NOT NULL,
                amount        REAL NOT NULL,
                status        TEXT NOT NULL,
                created_at    DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS registrations (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id  TEXT NOT NULL,
                ip          TEXT NOT NULL,
                dias        INTEGER NOT NULL,
                usuario     TEXT NOT NULL,
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS free_trial_used (
                discord_id TEXT PRIMARY KEY,
                used_at    DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bypass_free_trial_used (
                discord_id TEXT PRIMARY KEY,
                used_at    DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sensi_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id  TEXT NOT NULL,
                username    TEXT NOT NULL,
                dispositivo TEXT NOT NULL,
                plataforma  TEXT NOT NULL,
                respuesta   TEXT NOT NULL,
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # Operaciones pendientes (NX/BN) — sobreviven a reinicios
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_payments (
                payment_id  TEXT PRIMARY KEY,
                discord_id  TEXT NOT NULL,
                user_id     INTEGER NOT NULL,
                pack_id     TEXT NOT NULL,
                channel_id  INTEGER,
                username    TEXT NOT NULL,
                metodo      TEXT NOT NULL,
                extra_data  TEXT,
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # Migración: agregar extra_data si la tabla ya existía sin ella
        pp_cols = [r[1] for r in conn.execute("PRAGMA table_info(pending_payments)").fetchall()]
        if "extra_data" not in pp_cols:
            conn.execute("ALTER TABLE pending_payments ADD COLUMN extra_data TEXT")
        # Análisis IA de comprobantes (para auditoría y detección de duplicados)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS comprobantes (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                payment_id      TEXT NOT NULL,
                discord_id      TEXT NOT NULL,
                metodo          TEXT NOT NULL,
                image_url       TEXT NOT NULL,
                numero_op       TEXT,
                monto           REAL,
                moneda          TEXT,
                titular         TEXT,
                alias           TEXT,
                fecha_op        TEXT,
                confianza       REAL,
                decision        TEXT NOT NULL,
                motivos         TEXT,
                raw_json        TEXT,
                created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_comprobantes_numero "
            "ON comprobantes(numero_op)"
        )
        # ── Sistema de afiliados ────────────────────────────────────────────
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS referral_codes (
                discord_id  TEXT PRIMARY KEY,
                code        TEXT UNIQUE NOT NULL,
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS referrals (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id  TEXT NOT NULL,
                referred_id  TEXT NOT NULL UNIQUE,
                created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # Tickets activos — persisten entre reinicios del bot
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS active_tickets (
                channel_id   INTEGER PRIMARY KEY,
                ticket_num   INTEGER NOT NULL,
                user_id      INTEGER NOT NULL,
                user_name    TEXT NOT NULL,
                motivo       TEXT NOT NULL,
                created_at   TEXT NOT NULL
            )
            """
        )
        # Configuración general del bot (clave-valor persistente)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS config (
                key    TEXT PRIMARY KEY,
                value  TEXT NOT NULL
            )
            """
        )
        # Keys baneadas — no pueden activarse nunca más
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS banned_keys (
                key         TEXT PRIMARY KEY,
                discord_id  TEXT,
                reason      TEXT,
                banned_by   TEXT,
                banned_at   DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # Activaciones de keys — vincula key → discord_id → IP → usuario proxy
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS keys_activations (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                key         TEXT NOT NULL,
                discord_id  TEXT NOT NULL,
                ip          TEXT NOT NULL,
                proxy_user  TEXT,
                activated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS ix_keys_activations_key ON keys_activations(key)")
        # PINs de diamantes — se guardan al comprarse para poder reenviarlos luego
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS diamond_pins (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                diamonds   INTEGER NOT NULL,
                pin        TEXT    NOT NULL,
                order_id   TEXT    NOT NULL DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # Órdenes de Bypass-UID — almacena el FF player ID por compra
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bypass_orders (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id TEXT NOT NULL,
                pack_id    TEXT NOT NULL,
                ff_id      TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_bypass_orders_user "
            "ON bypass_orders(discord_id, pack_id)"
        )
        # Stock de keys de Bypass-UID (1d / 7d / 30d)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bypass_keys (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                key_value  TEXT NOT NULL,
                duration   TEXT NOT NULL,
                used       INTEGER NOT NULL DEFAULT 0,
                discord_id TEXT,
                used_at    DATETIME,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()


def save_diamond_pin(diamonds: int, pin: str, order_id: str = "") -> None:
    """Persiste el PIN de un pedido de diamantes para uso posterior (reenvío manual)."""
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO diamond_pins (diamonds, pin, order_id) VALUES (?, ?, ?)",
            (diamonds, pin.strip(), order_id),
        )
        conn.commit()


def pop_diamond_pin(diamonds: int) -> tuple[str, str]:
    """
    Devuelve y elimina el PIN más reciente para esa cantidad de diamantes.
    Retorna (pin, order_id) o ("", "") si no hay ninguno guardado.
    """
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT id, pin, order_id FROM diamond_pins "
            "WHERE diamonds = ? ORDER BY created_at DESC LIMIT 1",
            (diamonds,),
        ).fetchone()
        if not row:
            return "", ""
        conn.execute("DELETE FROM diamond_pins WHERE id = ?", (row["id"],))
        conn.commit()
        return row["pin"], row["order_id"]


@contextmanager
def _connect():
    conn = sqlite3.connect(str(DB_PATH), timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Créditos de PROXY
# ---------------------------------------------------------------------------
def get_credits(discord_id: str) -> float:
    with _connect() as conn:
        row = conn.execute(
            "SELECT credits FROM users WHERE discord_id = ?", (str(discord_id),)
        ).fetchone()
        return float(row["credits"]) if row else 0.0


def add_credits(discord_id: str, amount: float) -> float:
    """Suma créditos de proxy al usuario y devuelve el total nuevo."""
    discord_id = str(discord_id)
    with _lock, _connect() as conn:
        conn.execute(
            """
            INSERT INTO users (discord_id, credits, sensi_credits, referral_balance) VALUES (?, ?, 0, 0)
            ON CONFLICT(discord_id) DO UPDATE SET credits = credits + excluded.credits
            """,
            (discord_id, float(amount)),
        )
        conn.commit()
        row = conn.execute(
            "SELECT credits FROM users WHERE discord_id = ?", (discord_id,)
        ).fetchone()
        return float(row["credits"])


def consume_credit(discord_id: str) -> bool:
    """Resta 1 crédito de proxy si hay disponible."""
    return consume_credits(discord_id, 1.0)


def consume_credits(discord_id: str, amount: float) -> bool:
    """Resta `amount` créditos de proxy de forma atómica. Devuelve True si pudo consumir."""
    discord_id = str(discord_id)
    amount = float(amount)
    if amount <= 0:
        return True
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT credits FROM users WHERE discord_id = ?", (discord_id,)
        ).fetchone()
        if not row or float(row["credits"]) < amount:
            return False
        conn.execute(
            "UPDATE users SET credits = credits - ? WHERE discord_id = ?",
            (amount, discord_id),
        )
        conn.commit()
        return True


# ---------------------------------------------------------------------------
# Créditos de SENSIS XITADAS (completamente separados de los de proxy)
# ---------------------------------------------------------------------------
def get_sensi_credits(discord_id: str) -> float:
    with _connect() as conn:
        row = conn.execute(
            "SELECT sensi_credits FROM users WHERE discord_id = ?", (str(discord_id),)
        ).fetchone()
        return float(row["sensi_credits"]) if row else 0.0


def ha_pagado_alguna_vez(discord_id: str) -> bool:
    """True si el usuario tiene al menos un pago aprobado o ya existe en users."""
    discord_id = str(discord_id)
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM users WHERE discord_id = ?", (discord_id,)
        ).fetchone()
        if row:
            return True
        row = conn.execute(
            "SELECT 1 FROM payments WHERE discord_id = ? AND status LIKE 'approved%' LIMIT 1",
            (discord_id,),
        ).fetchone()
        return bool(row)


def add_sensi_credits(discord_id: str, amount: float) -> float:
    """Suma créditos de sensi al usuario y devuelve el total nuevo."""
    discord_id = str(discord_id)
    with _lock, _connect() as conn:
        conn.execute(
            """
            INSERT INTO users (discord_id, credits, sensi_credits, referral_balance) VALUES (?, 0, ?, 0)
            ON CONFLICT(discord_id) DO UPDATE SET sensi_credits = sensi_credits + excluded.sensi_credits
            """,
            (discord_id, float(amount)),
        )
        conn.commit()
        row = conn.execute(
            "SELECT sensi_credits FROM users WHERE discord_id = ?", (discord_id,)
        ).fetchone()
        return float(row["sensi_credits"])


def consume_sensi_credits(discord_id: str, amount: float) -> bool:
    """Resta `amount` créditos de sensi de forma atómica. Devuelve True si pudo consumir."""
    discord_id = str(discord_id)
    amount = float(amount)
    if amount <= 0:
        return True
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT sensi_credits FROM users WHERE discord_id = ?", (discord_id,)
        ).fetchone()
        if not row or float(row["sensi_credits"]) < amount:
            return False
        conn.execute(
            "UPDATE users SET sensi_credits = sensi_credits - ? WHERE discord_id = ?",
            (amount, discord_id),
        )
        conn.commit()
        return True


# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------
def has_used_free_trial(discord_id: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM free_trial_used WHERE discord_id = ?", (str(discord_id),)
        ).fetchone()
        return row is not None


def mark_free_trial_used(discord_id: str) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO free_trial_used (discord_id) VALUES (?)",
            (str(discord_id),),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Bypass-UID free trial
# ---------------------------------------------------------------------------
def has_used_bypass_trial(discord_id: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM bypass_free_trial_used WHERE discord_id = ?", (str(discord_id),)
        ).fetchone()
        return row is not None


def mark_bypass_trial_used(discord_id: str) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO bypass_free_trial_used (discord_id) VALUES (?)",
            (str(discord_id),),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Bypass-UID orders — almacena el FF player ID asociado a cada compra
# ---------------------------------------------------------------------------
def save_bypass_order(discord_id: str, pack_id: str, ff_id: str) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO bypass_orders (discord_id, pack_id, ff_id) VALUES (?, ?, ?)",
            (str(discord_id), str(pack_id), str(ff_id).strip()),
        )
        conn.commit()


def get_bypass_order_ff_id(discord_id: str, pack_id: str) -> str:
    """Devuelve el FF player ID más reciente para (discord_id, pack_id), o '' si no existe."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT ff_id FROM bypass_orders WHERE discord_id = ? AND pack_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (str(discord_id), str(pack_id)),
        ).fetchone()
        return row["ff_id"] if row else ""


# ---------------------------------------------------------------------------
# Bypass keys stock
# ---------------------------------------------------------------------------

def add_bypass_keys(keys: list[str], duration: str) -> int:
    """Agrega una lista de keys al stock para la duración dada. Retorna cuántas se agregaron."""
    duration = duration.strip().lower()
    added = 0
    with _lock, _connect() as conn:
        for k in keys:
            k = k.strip()
            if not k:
                continue
            conn.execute(
                "INSERT INTO bypass_keys (key_value, duration) VALUES (?, ?)",
                (k, duration),
            )
            added += 1
        conn.commit()
    return added


def pop_bypass_key(duration: str, discord_id: str = "") -> str:
    """Obtiene y marca como usada la key disponible más antigua para esa duración.
    Retorna la key o '' si no hay stock."""
    duration = duration.strip().lower()
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT id, key_value FROM bypass_keys "
            "WHERE duration = ? AND used = 0 ORDER BY created_at ASC LIMIT 1",
            (duration,),
        ).fetchone()
        if not row:
            return ""
        conn.execute(
            "UPDATE bypass_keys SET used = 1, discord_id = ?, used_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (str(discord_id) or None, row["id"]),
        )
        conn.commit()
        return row["key_value"]


def count_bypass_keys() -> dict[str, int]:
    """Retorna el conteo de keys disponibles por duración: {'1d': N, '7d': N, '30d': N}."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT duration, COUNT(*) as cnt FROM bypass_keys "
            "WHERE used = 0 GROUP BY duration"
        ).fetchall()
    result = {"1d": 0, "7d": 0, "30d": 0}
    for r in rows:
        result[r["duration"]] = r["cnt"]
    return result


def payment_exists(payment_id: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM payments WHERE payment_id = ?", (str(payment_id),)
        ).fetchone()
        return row is not None


def record_payment(
    payment_id: str,
    discord_id: str,
    pack: str,
    credits_added: float,
    amount: float,
    status: str,
) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO payments
                (payment_id, discord_id, pack, credits_added, amount, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (str(payment_id), str(discord_id), pack, credits_added, amount, status),
        )
        conn.commit()


def record_registration(
    discord_id: str, ip: str, dias: int, usuario: str
) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            """
            INSERT INTO registrations (discord_id, ip, dias, usuario)
            VALUES (?, ?, ?, ?)
            """,
            (str(discord_id), ip, int(dias), usuario),
        )
        conn.commit()


def get_registrations(discord_id: str, limit: int = 10) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT ip, dias, usuario, created_at
            FROM registrations
            WHERE discord_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (str(discord_id), int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]


def count_active_proxies() -> int:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) as total
            FROM registrations
            WHERE usuario LIKE '%Marke%'
              AND (
                dias = 0 AND datetime(created_at, '+3 hours') > datetime('now')
                OR
                dias > 0 AND datetime(created_at, '+' || dias || ' days') > datetime('now')
              )
            """
        ).fetchone()
        return int(row["total"]) if row else 0


def record_sensi_request(
    discord_id: str,
    username: str,
    dispositivo: str,
    plataforma: str,
    respuesta: str,
) -> int:
    """Guarda cada sensibilidad entregada para análisis posterior."""
    with _lock, _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO sensi_logs (discord_id, username, dispositivo, plataforma, respuesta)
            VALUES (?, ?, ?, ?, ?)
            """,
            (str(discord_id), str(username), str(dispositivo), str(plataforma), str(respuesta)),
        )
        conn.commit()
        return int(cur.lastrowid or 0)


def get_sensi_logs(limit: int = 20, discord_id: str | None = None) -> list[dict]:
    """Lista las sensibilidades entregadas (filtrable por usuario)."""
    with _connect() as conn:
        if discord_id:
            rows = conn.execute(
                """
                SELECT id, discord_id, username, dispositivo, plataforma, created_at
                FROM sensi_logs
                WHERE discord_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (str(discord_id), int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, discord_id, username, dispositivo, plataforma, created_at
                FROM sensi_logs
                ORDER BY id DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]


def count_sensi_logs() -> int:
    with _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS total FROM sensi_logs").fetchone()
        return int(row["total"]) if row else 0


def count_total_sales() -> int:
    """Devuelve el total de pagos aprobados (ventas exitosas)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS total FROM payments WHERE status LIKE 'approved%'"
        ).fetchone()
        return int(row["total"]) if row else 0


def count_total_registrations() -> int:
    """Devuelve el total de registros de proxy realizados (histórico completo)."""
    with _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS total FROM registrations").fetchone()
        return int(row["total"]) if row else 0


def top_sensi_devices(limit: int = 10) -> list[dict]:
    """Devuelve los dispositivos más solicitados."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT dispositivo, plataforma, COUNT(*) AS pedidos
            FROM sensi_logs
            GROUP BY LOWER(dispositivo)
            ORDER BY pedidos DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Operaciones pendientes (NX/BN) — sobreviven a reinicios
# ---------------------------------------------------------------------------
def save_pending_payment(
    payment_id: str,
    discord_id: str,
    user_id: int,
    pack_id: str,
    channel_id: int | None,
    username: str,
    metodo: str,
    extra_data: str | None = None,
) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO pending_payments
            (payment_id, discord_id, user_id, pack_id, channel_id, username, metodo, extra_data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (str(payment_id), str(discord_id), int(user_id), str(pack_id),
             int(channel_id) if channel_id else None, str(username), str(metodo), extra_data),
        )
        conn.commit()


def get_pending_payment(payment_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM pending_payments WHERE payment_id = ?",
            (str(payment_id),),
        ).fetchone()
        return dict(row) if row else None


def delete_pending_payment(payment_id: str) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "DELETE FROM pending_payments WHERE payment_id = ?",
            (str(payment_id),),
        )
        conn.commit()


def list_pending_payments() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM pending_payments ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def list_users_with_credits() -> list[dict]:
    """Lista todos los discord_id que tienen créditos > 0 (de proxy o sensi)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT discord_id, credits, sensi_credits FROM users "
            "WHERE credits > 0 OR sensi_credits > 0"
        ).fetchall()
        return [dict(r) for r in rows]


def get_pending_payment_by_user(user_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM pending_payments WHERE user_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (int(user_id),),
        ).fetchone()
        return dict(row) if row else None


def get_payment(payment_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM payments WHERE payment_id = ?",
            (str(payment_id),),
        ).fetchone()
        return dict(row) if row else None


def get_payments(discord_id: str, limit: int = 10) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT payment_id, pack, credits_added, amount, status, created_at
            FROM payments
            WHERE discord_id = ?
            ORDER BY rowid DESC
            LIMIT ?
            """,
            (str(discord_id), int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Comprobantes analizados con IA
# ---------------------------------------------------------------------------
def save_comprobante(
    *,
    payment_id: str,
    discord_id: str,
    metodo: str,
    image_url: str,
    numero_op: str | None,
    monto: float | None,
    moneda: str | None,
    titular: str | None,
    alias: str | None,
    fecha_op: str | None,
    confianza: float | None,
    decision: str,
    motivos: str,
    raw_json: str,
) -> int:
    with _lock, _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO comprobantes
                (payment_id, discord_id, metodo, image_url, numero_op,
                 monto, moneda, titular, alias, fecha_op, confianza,
                 decision, motivos, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(payment_id), str(discord_id), metodo, image_url, numero_op,
                monto, moneda, titular, alias, fecha_op, confianza,
                decision, motivos, raw_json,
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)


def numero_op_existe(numero_op: str) -> bool:
    """True si ese número de operación ya fue usado en un comprobante aprobado."""
    if not numero_op:
        return False
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM comprobantes
            WHERE numero_op = ? AND decision IN ('approved', 'manual_approved')
            LIMIT 1
            """,
            (numero_op,),
        ).fetchone()
        return row is not None


# ---------------------------------------------------------------------------
# Sistema de afiliados
# ---------------------------------------------------------------------------
def get_or_create_referral_code(discord_id: str) -> str:
    """Devuelve el código de afiliado del usuario, creándolo si no existe."""
    discord_id = str(discord_id)
    with _connect() as conn:
        row = conn.execute(
            "SELECT code FROM referral_codes WHERE discord_id = ?", (discord_id,)
        ).fetchone()
        if row:
            return str(row["code"])
    # Crear código único
    while True:
        code = "MARKE" + _uuid.uuid4().hex[:6].upper()
        try:
            with _lock, _connect() as conn:
                conn.execute(
                    "INSERT INTO referral_codes (discord_id, code) VALUES (?, ?)",
                    (discord_id, code),
                )
                conn.commit()
            return code
        except sqlite3.IntegrityError:
            continue   # colisión, reintenta


def get_referral_code(discord_id: str) -> str | None:
    """Devuelve el código de afiliado del usuario, o None si no existe."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT code FROM referral_codes WHERE discord_id = ?", (str(discord_id),)
        ).fetchone()
        return str(row["code"]) if row else None


def get_referrer_by_code(code: str) -> str | None:
    """Devuelve el discord_id del dueño del código, o None si no existe."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT discord_id FROM referral_codes WHERE code = ?", (code.upper().strip(),)
        ).fetchone()
        return str(row["discord_id"]) if row else None


def get_referrer(discord_id: str) -> str | None:
    """Devuelve el discord_id de quien invitó a este usuario, o None."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT referrer_id FROM referrals WHERE referred_id = ?",
            (str(discord_id),),
        ).fetchone()
        return str(row["referrer_id"]) if row else None


def register_referral(referrer_id: str, referred_id: str) -> bool:
    """Registra que referred_id fue invitado por referrer_id. Devuelve True si fue exitoso."""
    try:
        with _lock, _connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO referrals (referrer_id, referred_id) VALUES (?, ?)",
                (str(referrer_id), str(referred_id)),
            )
            conn.commit()
        return True
    except Exception:
        return False


def get_referral_balance(discord_id: str) -> float:
    """Devuelve el saldo acumulado de comisiones de afiliado."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT referral_balance FROM users WHERE discord_id = ?", (str(discord_id),)
        ).fetchone()
        return float(row["referral_balance"]) if row else 0.0


def add_referral_commission(discord_id: str, amount: float) -> float:
    """Suma comisión al saldo de afiliado y devuelve el total nuevo."""
    discord_id = str(discord_id)
    with _lock, _connect() as conn:
        conn.execute(
            """
            INSERT INTO users (discord_id, credits, sensi_credits, referral_balance)
            VALUES (?, 0, 0, ?)
            ON CONFLICT(discord_id) DO UPDATE SET referral_balance = referral_balance + excluded.referral_balance
            """,
            (discord_id, float(amount)),
        )
        conn.commit()
        row = conn.execute(
            "SELECT referral_balance FROM users WHERE discord_id = ?", (discord_id,)
        ).fetchone()
        return float(row["referral_balance"]) if row else float(amount)


def get_referral_count(discord_id: str) -> int:
    """Cuenta cuántos usuarios invitó este afiliado."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS total FROM referrals WHERE referrer_id = ?",
            (str(discord_id),),
        ).fetchone()
        return int(row["total"]) if row else 0


def get_referral_sales_count(discord_id: str) -> int:
    """Devuelve la cantidad de ventas exitosas generadas por los referidos del afiliado."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT referral_sales_count FROM users WHERE discord_id = ?",
            (str(discord_id),),
        ).fetchone()
        return int(row["referral_sales_count"]) if row else 0


def get_commission_rate(discord_id: str) -> float:
    """Devuelve la tasa de comisión del afiliado (fija: 30%)."""
    return 0.30


def increment_referral_sales(discord_id: str) -> tuple[int, float]:
    """Incrementa el contador de ventas referidas del afiliado.

    Devuelve (nuevo_total_ventas, nueva_tasa_comision).
    """
    discord_id = str(discord_id)
    with _lock, _connect() as conn:
        conn.execute(
            """
            INSERT INTO users (discord_id, credits, sensi_credits, referral_balance, referral_sales_count)
            VALUES (?, 0, 0, 0, 1)
            ON CONFLICT(discord_id) DO UPDATE SET referral_sales_count = referral_sales_count + 1
            """,
            (discord_id,),
        )
        conn.commit()
        row = conn.execute(
            "SELECT referral_sales_count FROM users WHERE discord_id = ?", (discord_id,)
        ).fetchone()
    new_count = int(row["referral_sales_count"]) if row else 1
    return new_count, 0.30


# ── Persistencia de tickets activos ────────────────────────────────────────


def save_ticket(channel_id: int, ticket_num: int, user_id: int,
                user_name: str, motivo: str, created_at: str) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO active_tickets
                (channel_id, ticket_num, user_id, user_name, motivo, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (channel_id, ticket_num, user_id, user_name, motivo, created_at),
        )
        conn.commit()


def delete_ticket(channel_id: int) -> None:
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM active_tickets WHERE channel_id = ?", (channel_id,))
        conn.commit()


def get_config(key: str, default: str | None = None) -> str | None:
    """Lee un valor de configuración persistente por clave."""
    with _lock, _connect() as conn:
        row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_config(key: str, value: str) -> None:
    """Guarda o actualiza un valor de configuración persistente."""
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
            (key, value),
        )
        conn.commit()


def reset_free_trial_all() -> int:
    """Borra todos los registros de free_trial_used para que todos puedan usar /gratismarke de nuevo."""
    with _lock, _connect() as conn:
        cur = conn.execute("DELETE FROM free_trial_used")
        conn.commit()
        return cur.rowcount


def load_all_tickets() -> list[dict]:
    with _lock, _connect() as conn:
        rows = conn.execute("SELECT * FROM active_tickets").fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Keys baneadas
# ---------------------------------------------------------------------------
def ban_key(key: str, discord_id: str | None, reason: str | None, banned_by: str) -> None:
    """Banea una key para que nunca se pueda volver a activar."""
    with _lock, _connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO banned_keys (key, discord_id, reason, banned_by)
            VALUES (?, ?, ?, ?)
            """,
            (key.upper().strip(), discord_id, reason, banned_by),
        )
        conn.commit()


def unban_key(key: str) -> bool:
    """Desbanea una key. Devuelve True si existía."""
    with _lock, _connect() as conn:
        cur = conn.execute("DELETE FROM banned_keys WHERE key = ?", (key.upper().strip(),))
        conn.commit()
        return cur.rowcount > 0


def is_key_banned(key: str) -> dict | None:
    """Devuelve el registro de ban si la key está baneada, o None si no lo está."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM banned_keys WHERE key = ?", (key.upper().strip(),)
        ).fetchone()
        return dict(row) if row else None


def list_banned_keys(limit: int = 50) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM banned_keys ORDER BY banned_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Activaciones de keys — vincula key → IP → usuario proxy
# ---------------------------------------------------------------------------
def record_key_activation(key: str, discord_id: str, ip: str, proxy_user: str | None = None) -> None:
    """Guarda cada vez que una key se activa exitosamente con una IP."""
    with _lock, _connect() as conn:
        conn.execute(
            """
            INSERT INTO keys_activations (key, discord_id, ip, proxy_user)
            VALUES (?, ?, ?, ?)
            """,
            (key.upper().strip(), str(discord_id), ip, proxy_user),
        )
        conn.commit()


def get_key_activations(key: str) -> list[dict]:
    """Devuelve todas las activaciones de una key (más reciente primero)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM keys_activations WHERE key = ? ORDER BY id DESC",
            (key.upper().strip(),),
        ).fetchall()
        return [dict(r) for r in rows]
