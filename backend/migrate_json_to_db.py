"""
migrate_json_to_db.py — Importa o antigo backend/data/data.json (mono-usuário)
para o novo banco PostgreSQL, virando a conta "Admin" existente em um usuário
normal (role='admin') dentro da nova estrutura multi-usuário.

Uso:
    cd backend
    python migrate_json_to_db.py caminho/para/data.json admin@seudominio.com

Requer as mesmas variáveis de ambiente do main.py (.env): DB_*, SECRET_KEY,
ENCRYPTION_KEY. Rode o schema.sql (db/schema.sql) ANTES deste script.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import db  # noqa: E402
import data_repo  # noqa: E402
from crypto_utils import encrypt_json, encrypt_str  # noqa: E402


def _parse_dt(s, default=None):
    if not s:
        return default
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return default


def migrate(json_path: str, admin_email: str) -> None:
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    user = data["user"]
    profile = data.get("profile", {})
    config = data.get("config", {})
    portfolio = data.get("portfolio", {})

    print(f"Migrando usuário '{user['username']}' ({admin_email})...")

    existing = data_repo.email_or_username_taken(admin_email, user["username"])
    if existing:
        print(f"Já existe um usuário com esse {existing}. Abortando para não duplicar.")
        return

    with db.get_conn() as conn:
        with conn.cursor() as cur:
            # 1) usuário — mantém o hash bcrypt já existente (não é re-hasheado)
            cur.execute(
                "INSERT INTO users (email, username, password_hash, role, email_verified) "
                "VALUES (%s,%s,%s,'admin',1) RETURNING id",
                (admin_email.lower().strip(), user["username"], user["password_hash"]),
            )
            user_id = cur.fetchone()["id"]
            print(f"  -> users.id = {user_id}")

            # 2) perfil
            cur.execute(
                "INSERT INTO profiles (user_id, display_name, investor_type, note, interests, "
                "alerts_seen, ai_used, level, xp, experience_level, member_since) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    user_id,
                    profile.get("display_name", user["username"]),
                    profile.get("investor_type", "moderado"),
                    profile.get("note", ""),
                    json.dumps(profile.get("interests", [])),
                    profile.get("alerts_seen", 0),
                    int(bool(profile.get("ai_used"))),
                    profile.get("level", "iniciante"),
                    profile.get("xp", 0),
                    profile.get("experience_level", "beginner"),
                    profile.get("member_since") or datetime.now().date(),
                ),
            )

            # 3) config
            cfg_defaults = dict(
                autorefresh=1, interval_sec=60, currency="BRL", threshold=2.0,
                accent_color="#4f8dff", compact_mode=0, show_instab=1, animations=1,
                alert_strong=1, alert_interest=1, news_interest=1, cache_enabled=1,
            )
            cfg_defaults.update({k: (int(v) if isinstance(v, bool) else v) for k, v in config.items()})
            cur.execute(
                "INSERT INTO user_configs (user_id, autorefresh, interval_sec, currency, threshold, "
                "accent_color, compact_mode, show_instab, animations, alert_strong, alert_interest, "
                "news_interest, cache_enabled) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (user_id, *[cfg_defaults[k] for k in (
                    "autorefresh", "interval_sec", "currency", "threshold", "accent_color",
                    "compact_mode", "show_instab", "animations", "alert_strong",
                    "alert_interest", "news_interest", "cache_enabled")]),
            )

            # 4) assinatura — usuário migrado vira 'free' por padrão; ajuste manualmente se for pago
            cur.execute(
                "INSERT INTO subscriptions (user_id, plan, status) VALUES (%s,'free','active')",
                (user_id,),
            )

            # 5) portfólio
            cur.execute(
                "INSERT INTO portfolio_goals (user_id, goal_amount, goal_label, goal_deadline) "
                "VALUES (%s,%s,%s,%s)",
                (user_id, portfolio.get("goal_amount"), portfolio.get("goal_label"),
                 portfolio.get("goal_deadline")),
            )
            for a in portfolio.get("assets", []):
                cur.execute(
                    "INSERT INTO portfolio_assets (user_id, asset_id, name, pair, amount, buy_price, "
                    "buy_date, added_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (user_id, a["id"], a["name"], a["pair"], a["amount"], a["buy_price"],
                     a.get("buy_date"), _parse_dt(a.get("added_at"), datetime.now())),
                )
            print(f"  -> {len(portfolio.get('assets', []))} ativos migrados")

            # 6) posições B3 (cifradas)
            b3 = portfolio.get("b3_positions", [])
            for p in b3:
                enc = encrypt_json({"quantity": p.get("quantity", 0), "institution": p.get("institution", "")})
                cur.execute(
                    "INSERT INTO b3_positions (user_id, symbol, asset_type, enc_payload) VALUES (%s,%s,%s,%s)",
                    (user_id, p["symbol"], p.get("type", ""), enc),
                )
            if b3:
                cur.execute(
                    "INSERT INTO portfolio_b3_meta (user_id, imported_at) VALUES (%s,%s)",
                    (user_id, _parse_dt(portfolio.get("b3_imported_at"), datetime.now())),
                )
                print(f"  -> {len(b3)} posições B3 migradas (cifradas)")

            # 7) chaves de API (cifradas)
            for provider, key in data.get("api_keys", {}).items():
                if key:
                    cur.execute(
                        "INSERT INTO api_keys (user_id, provider, enc_key) VALUES (%s,%s,%s)",
                        (user_id, provider, encrypt_str(key)),
                    )
            print(f"  -> {len(data.get('api_keys', {}))} chaves de API migradas (cifradas)")

            # 8) sessões de chat
            for s in data.get("chat_sessions", []):
                cur.execute(
                    "INSERT INTO chat_sessions (user_id, title, created_at, updated_at) VALUES (%s,%s,%s,%s) RETURNING id",
                    (user_id, s.get("title", "Nova conversa"),
                     _parse_dt(s.get("created_at"), datetime.now()),
                     _parse_dt(s.get("updated_at"), datetime.now())),
                )
                session_id = cur.fetchone()["id"]
                for m in s.get("messages", []):
                    cur.execute(
                        "INSERT INTO chat_messages (session_id, role, content, created_at) VALUES (%s,%s,%s,%s)",
                        (session_id, m["role"], m["content"], _parse_dt(m.get("created_at"), datetime.now())),
                    )
            print(f"  -> {len(data.get('chat_sessions', []))} sessões de chat migradas")

            # 9) resumos noturnos / relatórios mensais
            for s in data.get("night_summaries", []):
                cur.execute(
                    "INSERT INTO night_summaries (user_id, summary_text, instab_score, type, period_label, "
                    "full_report, generated_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (user_id, s.get("summary_text", ""), s.get("instab_score", 0),
                     s.get("type", "daily"), s.get("period"),
                     json.dumps(s["full_report"]) if s.get("full_report") else None,
                     _parse_dt(s.get("generated_at"), datetime.now())),
                )
            print(f"  -> {len(data.get('night_summaries', []))} resumos migrados")

            # 10) insights do Jarvis
            for j in data.get("jarvis_insights", []):
                cur.execute(
                    "INSERT INTO jarvis_insights (user_id, insight, type, asset, urgency, generated_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s)",
                    (user_id, j.get("insight", ""), j.get("type", "dica"), j.get("asset"),
                     j.get("urgency"), _parse_dt(j.get("generated_at"), datetime.now())),
                )

        conn.commit()

    print(f"\nMigração concluída. Usuário '{user['username']}' agora é o id={user_id} (role=admin) no PostgreSQL.")
    print("A senha continua a mesma de antes (o hash bcrypt foi preservado).")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Uso: python {sys.argv[0]} <caminho_data.json> <email_do_admin>")
        sys.exit(1)
    migrate(sys.argv[1], sys.argv[2])
