"""
data_repo.py — Repositório de dados multi-usuário (substitui o antigo
load_data()/save_data() em cima de um único data.json).

Toda função aqui recebe um user_id explícito e nunca devolve/mistura dados
de outro usuário — é a principal barreira de isolamento entre contas.

Campos sensíveis (chaves de API e posições B3) são cifrados/decifrados
aqui mesmo, então o restante do main.py nunca lida com bytes cifrados.

Nota (PostgreSQL): colunas JSONB voltam já decodificadas em Python
(list/dict) pelo psycopg2 — não é preciso chamar json.loads() nelas.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Optional

import db
from crypto_utils import decrypt_json, decrypt_str, encrypt_json, encrypt_str

# ── Registro de usuário ───────────────────────────────────────────────────────
def create_user(email: str, username: str, password_hash: str, display_name: str) -> int:
    """Cria usuário + perfil + config padrão + assinatura free. Tudo em uma transação."""
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (email, username, password_hash) VALUES (%s,%s,%s) RETURNING id",
                (email.lower().strip(), username.strip(), password_hash),
            )
            user_id = cur.fetchone()["id"]

            cur.execute(
                "INSERT INTO profiles (user_id, display_name, interests, member_since) "
                "VALUES (%s,%s,%s,%s)",
                (user_id, display_name or username, json.dumps([]), date.today()),
            )
            cur.execute("INSERT INTO user_configs (user_id) VALUES (%s)", (user_id,))
            cur.execute(
                "INSERT INTO subscriptions (user_id, plan, status) VALUES (%s,'free','active')",
                (user_id,),
            )
            cur.execute("INSERT INTO portfolio_goals (user_id) VALUES (%s)", (user_id,))
        conn.commit()
    return user_id


def find_user_by_login(identifier: str) -> Optional[dict]:
    """Login pode ser feito por username OU e-mail."""
    return db.fetchone(
        "SELECT id, email, username, password_hash, is_active, "
        "failed_login_attempts, locked_until FROM users "
        "WHERE username=%s OR email=%s",
        (identifier.strip(), identifier.strip().lower()),
    )


def register_failed_login(user_id: int, max_attempts: int = 5, lock_minutes: int = 15) -> None:
    row = db.fetchone("SELECT failed_login_attempts FROM users WHERE id=%s", (user_id,))
    attempts = (row["failed_login_attempts"] if row else 0) + 1
    if attempts >= max_attempts:
        db.execute(
            "UPDATE users SET failed_login_attempts=%s, "
            "locked_until = NOW() + make_interval(mins => %s) WHERE id=%s",
            (attempts, lock_minutes, user_id),
        )
    else:
        db.execute("UPDATE users SET failed_login_attempts=%s WHERE id=%s", (attempts, user_id))


def reset_failed_login(user_id: int) -> None:
    db.execute(
        "UPDATE users SET failed_login_attempts=0, locked_until=NULL WHERE id=%s",
        (user_id,),
    )


def email_or_username_taken(email: str, username: str) -> Optional[str]:
    row = db.fetchone("SELECT email, username FROM users WHERE email=%s OR username=%s",
                       (email.lower().strip(), username.strip()))
    if not row:
        return None
    if row["email"] == email.lower().strip():
        return "email"
    return "username"


# ── Perfil ─────────────────────────────────────────────────────────────────────
def get_profile(user_id: int) -> dict:
    row = db.fetchone("SELECT * FROM profiles WHERE user_id=%s", (user_id,))
    if not row:
        return {}
    row["interests"] = row.get("interests") or []
    row["ai_used"] = bool(row["ai_used"])
    return row


_PROFILE_COLS = {"display_name", "investor_type", "note", "interests", "experience_level"}


def update_profile(user_id: int, updates: dict) -> None:
    updates = {k: v for k, v in updates.items() if k in _PROFILE_COLS}
    if not updates:
        return
    sets, params = [], []
    for k, v in updates.items():
        sets.append(f"{k}=%s")
        params.append(json.dumps(v) if k == "interests" else v)
    params.append(user_id)
    db.execute(f"UPDATE profiles SET {', '.join(sets)} WHERE user_id=%s", params)


# ── Config ─────────────────────────────────────────────────────────────────────
_CONFIG_COLS = {
    "autorefresh", "interval_sec", "currency", "threshold", "accent_color",
    "compact_mode", "show_instab", "animations", "alert_strong",
    "alert_interest", "news_interest", "cache_enabled",
}


def get_config(user_id: int) -> dict:
    row = db.fetchone("SELECT * FROM user_configs WHERE user_id=%s", (user_id,))
    if not row:
        return {}
    for b in ("autorefresh", "compact_mode", "show_instab", "animations",
              "alert_strong", "alert_interest", "news_interest", "cache_enabled"):
        row[b] = bool(row[b])
    row.pop("user_id", None)
    return row


def update_config(user_id: int, updates: dict) -> None:
    updates = {k: v for k, v in updates.items() if k in _CONFIG_COLS}
    if not updates:
        return
    sets = [f"{k}=%s" for k in updates]
    params = list(updates.values()) + [user_id]
    db.execute(f"UPDATE user_configs SET {', '.join(sets)} WHERE user_id=%s", params)


# ── API Keys (cifradas) ────────────────────────────────────────────────────────
def upsert_api_key(user_id: int, provider: str, key: str) -> None:
    enc = encrypt_str(key)
    db.execute(
        "INSERT INTO api_keys (user_id, provider, enc_key) VALUES (%s,%s,%s) "
        "ON CONFLICT (user_id, provider) DO UPDATE SET enc_key = EXCLUDED.enc_key",
        (user_id, provider, enc),
    )


def get_api_key(user_id: int, provider: str) -> Optional[str]:
    row = db.fetchone(
        "SELECT enc_key FROM api_keys WHERE user_id=%s AND provider=%s",
        (user_id, provider),
    )
    if not row:
        return None
    return decrypt_str(row["enc_key"])


def api_key_exists(user_id: int, provider: str) -> bool:
    row = db.fetchone(
        "SELECT id FROM api_keys WHERE user_id=%s AND provider=%s",
        (user_id, provider),
    )
    return row is not None


# ── Portfólio ──────────────────────────────────────────────────────────────────
def get_portfolio_assets(user_id: int) -> list[dict]:
    rows = db.fetchall(
        "SELECT asset_id AS id, name, pair, amount, buy_price, buy_date, added_at "
        "FROM portfolio_assets WHERE user_id=%s ORDER BY added_at DESC",
        (user_id,),
    )
    for r in rows:
        r["amount"] = float(r["amount"])
        r["buy_price"] = float(r["buy_price"])
    return rows


def get_portfolio_goal(user_id: int) -> dict:
    row = db.fetchone(
        "SELECT goal_amount, goal_label, goal_deadline FROM portfolio_goals WHERE user_id=%s",
        (user_id,),
    )
    if not row:
        return {"goal_amount": None, "goal_label": None, "goal_deadline": None}
    if row["goal_amount"] is not None:
        row["goal_amount"] = float(row["goal_amount"])
    return row


def add_asset(user_id: int, asset: dict) -> None:
    db.execute(
        "INSERT INTO portfolio_assets (user_id, asset_id, name, pair, amount, buy_price, buy_date) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (user_id, asset_id) DO UPDATE SET "
        "name=EXCLUDED.name, pair=EXCLUDED.pair, amount=EXCLUDED.amount, "
        "buy_price=EXCLUDED.buy_price, buy_date=EXCLUDED.buy_date, added_at=CURRENT_TIMESTAMP",
        (user_id, asset["id"], asset["name"], asset["pair"], asset["amount"],
         asset["buy_price"], asset.get("buy_date") or date.today()),
    )


def remove_asset(user_id: int, asset_id: str) -> None:
    db.execute(
        "DELETE FROM portfolio_assets WHERE user_id=%s AND asset_id=%s",
        (user_id, asset_id),
    )


def update_goal(user_id: int, updates: dict) -> None:
    allowed = {"goal_amount", "goal_label", "goal_deadline"}
    updates = {k: v for k, v in updates.items() if k in allowed}
    if not updates:
        return
    sets = [f"{k}=%s" for k in updates]
    params = list(updates.values()) + [user_id]
    db.execute(
        "INSERT INTO portfolio_goals (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING",
        (user_id,),
    )
    db.execute(f"UPDATE portfolio_goals SET {', '.join(sets)} WHERE user_id=%s", params)


# ── Posições B3 (sensíveis, cifradas) ────────────────────────────────────────
def import_b3_positions(user_id: int, positions: list[dict]) -> None:
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM b3_positions WHERE user_id=%s", (user_id,))
            for p in positions:
                enc = encrypt_json({"quantity": p["quantity"], "institution": p.get("institution", "")})
                cur.execute(
                    "INSERT INTO b3_positions (user_id, symbol, asset_type, enc_payload) "
                    "VALUES (%s,%s,%s,%s)",
                    (user_id, p["symbol"], p.get("type", ""), enc),
                )
            cur.execute(
                "INSERT INTO portfolio_b3_meta (user_id, imported_at) VALUES (%s, NOW()) "
                "ON CONFLICT (user_id) DO UPDATE SET imported_at = EXCLUDED.imported_at",
                (user_id,),
            )
        conn.commit()


def get_b3_positions(user_id: int) -> dict:
    rows = db.fetchall(
        "SELECT symbol, asset_type, enc_payload FROM b3_positions WHERE user_id=%s",
        (user_id,),
    )
    positions = []
    for r in rows:
        payload = decrypt_json(r["enc_payload"])
        positions.append({
            "symbol": r["symbol"],
            "type": r["asset_type"],
            "quantity": payload["quantity"],
            "institution": payload.get("institution", ""),
        })
    meta = db.fetchone("SELECT imported_at FROM portfolio_b3_meta WHERE user_id=%s", (user_id,))
    return {"positions": positions, "imported_at": meta["imported_at"].isoformat() if meta and meta["imported_at"] else None}


def clear_b3_positions(user_id: int) -> None:
    db.execute("DELETE FROM b3_positions WHERE user_id=%s", (user_id,))
    db.execute("UPDATE portfolio_b3_meta SET imported_at=NULL WHERE user_id=%s", (user_id,))


# ── Chat ───────────────────────────────────────────────────────────────────────
def list_chat_sessions(user_id: int, limit: int = 50) -> list[dict]:
    return db.fetchall(
        "SELECT id, title, created_at, updated_at FROM chat_sessions "
        "WHERE user_id=%s ORDER BY updated_at DESC LIMIT %s",
        (user_id, limit),
    )


def create_chat_session(user_id: int, title: str = "Nova conversa") -> dict:
    sid = db.execute(
        "INSERT INTO chat_sessions (user_id, title) VALUES (%s,%s) RETURNING id",
        (user_id, title),
    )
    return {"id": sid, "title": title}


def get_owned_session(user_id: int, session_id: int) -> Optional[dict]:
    return db.fetchone(
        "SELECT id, title, created_at, updated_at FROM chat_sessions WHERE id=%s AND user_id=%s",
        (session_id, user_id),
    )


def delete_chat_session(user_id: int, session_id: int) -> None:
    db.execute("DELETE FROM chat_sessions WHERE id=%s AND user_id=%s", (session_id, user_id))


def get_chat_messages(session_id: int) -> list[dict]:
    return db.fetchall(
        "SELECT role, content, created_at FROM chat_messages WHERE session_id=%s ORDER BY id ASC",
        (session_id,),
    )


def append_messages(session_id: int, pairs: list[tuple[str, str]]) -> None:
    db.execute_many(
        "INSERT INTO chat_messages (session_id, role, content) VALUES (%s,%s,%s)",
        [(session_id, role, content) for role, content in pairs],
    )


def touch_session(session_id: int, title: Optional[str] = None) -> None:
    if title:
        db.execute(
            "UPDATE chat_sessions SET title=%s, updated_at=CURRENT_TIMESTAMP WHERE id=%s",
            (title, session_id),
        )
    else:
        db.execute(
            "UPDATE chat_sessions SET updated_at=CURRENT_TIMESTAMP WHERE id=%s",
            (session_id,),
        )


# ── Resumos noturnos / relatórios ────────────────────────────────────────────
def add_night_summary(user_id: int, summary_text: str, instab_score: int = 0,
                       type_: str = "daily", period_label: Optional[str] = None,
                       full_report: Optional[dict] = None) -> None:
    db.execute(
        "INSERT INTO night_summaries (user_id, summary_text, instab_score, type, period_label, full_report) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        (user_id, summary_text, instab_score, type_, period_label,
         json.dumps(full_report, default=str) if full_report else None),
    )
    # mantém só os últimos 30 'daily' por usuário (paridade com comportamento antigo)
    db.execute(
        "DELETE FROM night_summaries WHERE user_id=%s AND type='daily' AND id NOT IN ("
        "  SELECT id FROM (SELECT id FROM night_summaries WHERE user_id=%s AND type='daily' "
        "  ORDER BY generated_at DESC LIMIT 30) t)",
        (user_id, user_id),
    )


def get_night_summaries(user_id: int, type_: Optional[str] = None, limit: int = 30) -> list[dict]:
    if type_:
        rows = db.fetchall(
            "SELECT summary_text, instab_score, type, period_label, full_report, generated_at "
            "FROM night_summaries WHERE user_id=%s AND type=%s ORDER BY generated_at DESC LIMIT %s",
            (user_id, type_, limit),
        )
    else:
        rows = db.fetchall(
            "SELECT summary_text, instab_score, type, period_label, full_report, generated_at "
            "FROM night_summaries WHERE user_id=%s ORDER BY generated_at DESC LIMIT %s",
            (user_id, limit),
        )
    return rows


# ── Jarvis insights ────────────────────────────────────────────────────────────
def add_jarvis_insight(user_id: int, insight: str, type_: str, asset: Optional[str], urgency: Optional[str]) -> None:
    db.execute(
        "INSERT INTO jarvis_insights (user_id, insight, type, asset, urgency) VALUES (%s,%s,%s,%s,%s)",
        (user_id, insight, type_, asset, urgency),
    )
    db.execute(
        "DELETE FROM jarvis_insights WHERE user_id=%s AND id NOT IN ("
        "  SELECT id FROM (SELECT id FROM jarvis_insights WHERE user_id=%s "
        "  ORDER BY generated_at DESC LIMIT 50) t)",
        (user_id, user_id),
    )


def get_jarvis_insights(user_id: int, limit: int = 20) -> list[dict]:
    return db.fetchall(
        "SELECT insight, type, asset, urgency, generated_at FROM jarvis_insights "
        "WHERE user_id=%s ORDER BY generated_at DESC LIMIT %s",
        (user_id, limit),
    )


# ── XP / Nível ─────────────────────────────────────────────────────────────────
LEVEL_THRESHOLDS = {"iniciante": 0, "intermediario": 100, "avancado": 300}
LEVEL_LABELS = {"iniciante": "Iniciante", "intermediario": "Intermediário", "avancado": "Avançado"}
XP_EVENTS = {"login": 5, "chat_message": 8, "portfolio_add": 10, "alert_set": 5, "profile_complete": 15}


def grant_xp(user_id: int, event: str, pts: Optional[int] = None) -> dict:
    pts = pts if pts is not None else XP_EVENTS.get(event, 0)
    row = db.fetchone("SELECT xp, level FROM profiles WHERE user_id=%s", (user_id,))
    old_level = row["level"]
    new_xp = row["xp"] + pts

    new_level = old_level
    for lvl, threshold in sorted(LEVEL_THRESHOLDS.items(), key=lambda x: -x[1]):
        if new_xp >= threshold:
            new_level = lvl
            break

    db.execute("UPDATE profiles SET xp=%s, level=%s WHERE user_id=%s", (new_xp, new_level, user_id))
    return {
        "xp": new_xp, "level": new_level,
        "leveled_up": new_level != old_level,
        "level_label": LEVEL_LABELS[new_level],
    }


def get_level(user_id: int) -> dict:
    row = db.fetchone("SELECT xp, level FROM profiles WHERE user_id=%s", (user_id,))
    xp, level = row["xp"], row["level"]
    next_thresholds = {k: v for k, v in LEVEL_THRESHOLDS.items() if v > LEVEL_THRESHOLDS.get(level, 0)}
    next_xp = min(next_thresholds.values()) if next_thresholds else None
    return {
        "xp": xp, "level": level, "level_label": LEVEL_LABELS.get(level, "Iniciante"),
        "next_level_xp": next_xp,
        "progress_pct": int(min(100, (xp / next_xp * 100))) if next_xp else 100,
    }
