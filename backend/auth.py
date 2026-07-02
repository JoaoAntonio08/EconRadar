"""
auth.py — Autenticação multi-usuário e controle de plano (free/pro/premium).

Mudanças em relação à v1.0 (mono-usuário):
  - JWT carrega o user_id real no campo "sub" (antes era fixo em "admin").
  - Toda rota autenticada recebe o usuário atual via Depends(get_current_user).
  - Cadastro é livre e gratuito: todo novo usuário nasce com plano "free".
  - Existe um decorator/dependency `require_plan(...)` pra travar features
    pagas sem espalhar lógica de plano pelas rotas.
"""
from __future__ import annotations

import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt as _bcrypt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

import db

SECRET_KEY = os.getenv("SECRET_KEY", "")
if not SECRET_KEY or len(SECRET_KEY) < 32:
    raise RuntimeError(
        "SECRET_KEY ausente ou fraca (mínimo 32 caracteres). Gere uma com "
        "`python -c \"import secrets; print(secrets.token_urlsafe(48))\"`."
    )

ALGORITHM = "HS256"
ACCESS_TOKEN_MIN = int(os.getenv("ACCESS_TOKEN_MIN", "30"))          # token de acesso, curto
REFRESH_TOKEN_DAYS = int(os.getenv("REFRESH_TOKEN_DAYS", "30"))      # refresh, mais longo

bearer = HTTPBearer()

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,30}$")

PLAN_RANK = {"free": 0, "pro": 1, "premium": 2}


# ── Senha ──────────────────────────────────────────────────────────────────────
def hash_password(plain: str) -> str:
    if len(plain) < 8:
        raise HTTPException(400, "A senha deve ter ao menos 8 caracteres.")
    return _bcrypt.hashpw(plain.encode("utf-8"), _bcrypt.gensalt(rounds=12)).decode("utf-8")


def check_password(plain: str, hashed: str) -> bool:
    try:
        return _bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


# ── JWT (access token) ───────────────────────────────────────────────────────
def create_access_token(user_id: int) -> str:
    exp = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_MIN)
    return jwt.encode({"sub": str(user_id), "type": "access", "exp": exp}, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="Token inválido ou expirado")


def get_current_user(creds: HTTPAuthorizationCredentials = Depends(bearer)) -> dict:
    """Dependency principal: valida o JWT e carrega o usuário + plano atual."""
    payload = decode_token(creds.credentials)
    if payload.get("type") != "access":
        raise HTTPException(401, "Tipo de token inválido")
    try:
        user_id = int(payload["sub"])
    except (KeyError, ValueError):
        raise HTTPException(401, "Token malformado")

    user = db.fetchone(
        "SELECT id, email, username, role, is_active FROM users WHERE id=%s",
        (user_id,),
    )
    if not user or not user["is_active"]:
        raise HTTPException(401, "Usuário não encontrado ou desativado")

    sub = db.fetchone(
        "SELECT plan, status, current_period_end FROM subscriptions WHERE user_id=%s",
        (user_id,),
    )
    user["plan"] = sub["plan"] if sub else "free"
    user["plan_status"] = sub["status"] if sub else "active"
    return user


def require_plan(min_plan: str):
    """Dependency factory: `Depends(require_plan('pro'))` bloqueia free."""

    def _check(user: dict = Depends(get_current_user)) -> dict:
        active_ok = user["plan_status"] in ("active", "trialing")
        if not active_ok or PLAN_RANK.get(user["plan"], 0) < PLAN_RANK.get(min_plan, 0):
            raise HTTPException(
                403,
                f"Recurso disponível apenas para assinantes do plano '{min_plan}' ou superior.",
            )
        return user

    return _check


# ── Refresh tokens (permitem logout / revogação real) ────────────────────────
def _hash_token(raw: str) -> str:
    import hashlib

    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def issue_refresh_token(user_id: int, request: Optional[Request] = None) -> str:
    raw = secrets.token_urlsafe(48)
    expires_at = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_DAYS)
    ua = request.headers.get("user-agent", "")[:255] if request else None
    ip = request.client.host if request and request.client else None
    db.execute(
        "INSERT INTO refresh_tokens (user_id, token_hash, expires_at, user_agent, ip_address) "
        "VALUES (%s,%s,%s,%s,%s)",
        (user_id, _hash_token(raw), expires_at, ua, ip),
    )
    return raw


def rotate_refresh_token(raw_token: str, request: Optional[Request] = None) -> tuple[str, int]:
    """Valida um refresh token, revoga-o e emite outro (rotação). Retorna (novo_token, user_id)."""
    h = _hash_token(raw_token)
    row = db.fetchone(
        "SELECT id, user_id, expires_at, revoked FROM refresh_tokens WHERE token_hash=%s",
        (h,),
    )
    if not row or row["revoked"] or row["expires_at"] < datetime.now(timezone.utc).replace(tzinfo=None):
        raise HTTPException(401, "Refresh token inválido ou expirado. Faça login novamente.")
    db.execute("UPDATE refresh_tokens SET revoked=1 WHERE id=%s", (row["id"],))
    new_token = issue_refresh_token(row["user_id"], request)
    return new_token, row["user_id"]


def revoke_refresh_token(raw_token: str) -> None:
    db.execute(
        "UPDATE refresh_tokens SET revoked=1 WHERE token_hash=%s",
        (_hash_token(raw_token),),
    )


def revoke_all_sessions(user_id: int) -> None:
    db.execute("UPDATE refresh_tokens SET revoked=1 WHERE user_id=%s", (user_id,))


# ── Validações de cadastro ────────────────────────────────────────────────────
def validate_registration(email: str, username: str, password: str) -> None:
    if not EMAIL_RE.match(email or ""):
        raise HTTPException(400, "E-mail inválido.")
    if not USERNAME_RE.match(username or ""):
        raise HTTPException(400, "Usuário deve ter 3-30 caracteres (letras, números, _ . -).")
    if len(password or "") < 8:
        raise HTTPException(400, "A senha deve ter ao menos 8 caracteres.")


# ── Auditoria leve ─────────────────────────────────────────────────────────────
def audit(event: str, user_id: Optional[int] = None, request: Optional[Request] = None, meta: Optional[dict] = None) -> None:
    import json as _json

    ip = request.client.host if request and request.client else None
    db.execute(
        "INSERT INTO audit_log (user_id, event, ip_address, meta) VALUES (%s,%s,%s,%s)",
        (user_id, event, ip, _json.dumps(meta or {}, default=str)),
    )
