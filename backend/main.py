"""
EconRadar Backend — Versão 2.0 (Multi-usuário / PostgreSQL)
- Cadastro gratuito e aberto, com controle de planos pagos (subscriptions)
- Persistência em PostgreSQL (sem ORM, SQL parametrizado) em vez de data.json
- Senhas com bcrypt, API keys e posições B3 cifradas em repouso (Fernet/AES)
- JWT por usuário (access curto + refresh revogável), rate limiting, lockout
- Indicadores técnicos (RSI, MACD, Bollinger)
- Importação de carteira B3 via CSV
"""

from dotenv import load_dotenv
from pathlib import Path

_env_paths = [
    Path(__file__).parent / ".env",
    Path(__file__).parent.parent / ".env",
]
for _p in _env_paths:
    if _p.exists():
        load_dotenv(_p)
        break
else:
    load_dotenv()

from fastapi import FastAPI, HTTPException, Depends, Request, UploadFile, File, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, EmailStr
from datetime import datetime
import httpx, json, os, re, xml.etree.ElementTree as _ET
from typing import Any, Optional
import time, math, io, csv

from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

import auth
import data_repo
from data_repo import LEVEL_THRESHOLDS, LEVEL_LABELS, XP_EVENTS  # noqa: F401 (reexport p/ paridade)

# ── Configuração ───────────────────────────────────────────────────────────────
OR_KEY        = os.getenv("OPENROUTER_API_KEY", "")
FH_KEY        = os.getenv("FINNHUB_API_KEY", "")
OR_MODEL      = os.getenv("OR_MODEL", "google/gemma-4-31b-it:free")
OR_MODEL_FALLBACK = os.getenv("OR_MODEL_FALLBACK", "nvidia/nemotron-3-ultra-550b-a55b:free")
ALLOWED_ENV    = os.getenv("ALLOWED_ORIGINS", "").strip()
_default_origins = [
    "http://localhost:8000", "http://127.0.0.1:8000",
    "http://localhost:5500", "http://127.0.0.1:5500",
    "http://localhost:3000", "http://127.0.0.1:3000",
]
ALLOWED = _default_origins + ([o.strip() for o in ALLOWED_ENV.split(",") if o.strip()] if ALLOWED_ENV else [])

# ── Rate Limiter ───────────────────────────────────────────────────────────────
limiter = Limiter(key_func=get_remote_address)

# ── Cache Inteligente em Memória (não guarda nada sensível) ──────────────────
class SimpleCache:
    def __init__(self):
        self._store: dict[str, dict] = {}

    def get(self, key: str):
        entry = self._store.get(key)
        if not entry:
            return None
        if time.time() > entry["expires_at"]:
            del self._store[key]
            return None
        return entry["value"]

    def set(self, key: str, value: Any, ttl_seconds: int = 60):
        self._store[key] = {"value": value, "expires_at": time.time() + ttl_seconds}

    def invalidate(self, key: str):
        self._store.pop(key, None)

    def clear(self):
        self._store.clear()

    def size(self) -> int:
        now = time.time()
        self._store = {k: v for k, v in self._store.items() if v["expires_at"] > now}
        return len(self._store)

cache = SimpleCache()

# ── OpenRouter helper com fallback ────────────────────────────────────────────
def _extract_or_content(resp_json: dict) -> str:
    try:
        choices = resp_json.get("choices") or []
        if not choices:
            return ""
        msg = choices[0].get("message") or {}
        content = msg.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
            return " ".join(parts).strip()
    except Exception:
        pass
    return ""

async def _or_call(or_key: str, messages: list, max_tokens: int = 1000,
                   temperature: float = 0.5, timeout: int = 45) -> str:
    models_to_try = [OR_MODEL]
    for fb in [OR_MODEL_FALLBACK, "meta-llama/llama-3.3-70b-instruct:free"]:
        if fb and fb not in models_to_try:
            models_to_try.append(fb)

    last_error = "IA não retornou resposta"
    async with httpx.AsyncClient(timeout=timeout) as client:
        for model in models_to_try:
            try:
                resp = await client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={"Authorization": f"Bearer {or_key}", "Content-Type": "application/json"},
                    json={"model": model, "messages": messages,
                          "max_tokens": max_tokens, "temperature": temperature}
                )
                if resp.status_code == 401:
                    raise HTTPException(401, "Chave OpenRouter inválida ou expirada.")
                if resp.status_code == 404:
                    last_error = f"Modelo não encontrado ({model})"
                    continue
                if resp.status_code != 200:
                    last_error = f"Erro OpenRouter ({model}): {resp.text[:200]}"
                    continue
                text = _extract_or_content(resp.json())
                if text:
                    return text
                last_error = f"IA ({model}) não retornou resposta"
            except (httpx.ReadTimeout, httpx.ConnectTimeout):
                last_error = f"Timeout ao chamar OpenRouter ({model})"
            except Exception as e:
                last_error = str(e)[:150]

    raise HTTPException(502, last_error)

def _user_or_key(user: dict) -> str:
    return OR_KEY or (data_repo.get_api_key(user["id"], "openrouter") or "")

def _user_fh_key(user: dict) -> str:
    return FH_KEY or (data_repo.get_api_key(user["id"], "finnhub") or "")

# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(title="EconRadar API", version="2.0.0")
app.state.limiter = limiter

@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    retry = getattr(exc, "retry_after", 60)
    return JSONResponse(
        status_code=429,
        content={"error": "rate_limit", "message": "Muitas requisições. Tente novamente em breve.", "retry_after": retry},
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED,
    allow_origin_regex=r"https://.*\.ngrok(-free)?\.app|https://.*\.ngrok\.io|https://.*\.ngrok-free\.dev",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

@app.get("/", include_in_schema=False)
def root():
    return FileResponse(str(FRONTEND_DIR / "landing.html"))

@app.get("/app", include_in_schema=False)
def serve_app():
    return FileResponse(str(FRONTEND_DIR / "index.html"))

@app.get("/api.js", include_in_schema=False)
def serve_apijs():
    return FileResponse(str(FRONTEND_DIR / "api.js"))

# ══════════════════════════════════════════════════════════════════════════════
#  AUTENTICAÇÃO — cadastro gratuito + login + refresh + planos
# ══════════════════════════════════════════════════════════════════════════════

class RegisterIn(BaseModel):
    email: EmailStr
    username: str
    password: str
    display_name: Optional[str] = None

@app.post("/api/auth/register")
@limiter.limit("5/minute")
async def register(request: Request, body: RegisterIn):
    auth.validate_registration(body.email, body.username, body.password)
    taken = data_repo.email_or_username_taken(body.email, body.username)
    if taken == "email":
        raise HTTPException(409, "Já existe uma conta com este e-mail.")
    if taken == "username":
        raise HTTPException(409, "Este nome de usuário já está em uso.")

    pw_hash = auth.hash_password(body.password)
    user_id = data_repo.create_user(body.email, body.username, pw_hash, body.display_name or body.username)
    auth.audit("register", user_id, request)

    access = auth.create_access_token(user_id)
    refresh = auth.issue_refresh_token(user_id, request)
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "bearer",
        "display_name": body.display_name or body.username,
        "plan": "free",
    }


class LoginIn(BaseModel):
    username: str   # aceita username OU e-mail
    password: str

@app.post("/api/auth/login")
@limiter.limit("10/minute")
async def login(request: Request, body: LoginIn):
    user = data_repo.find_user_by_login(body.username)
    if not user or not user["is_active"]:
        raise HTTPException(401, "Usuário ou senha incorretos")

    if user["locked_until"] and user["locked_until"] > datetime.now():
        raise HTTPException(429, "Conta temporariamente bloqueada por excesso de tentativas. Tente novamente mais tarde.")

    if not auth.check_password(body.password, user["password_hash"]):
        data_repo.register_failed_login(user["id"])
        auth.audit("login_failed", user["id"], request)
        raise HTTPException(401, "Usuário ou senha incorretos")

    data_repo.reset_failed_login(user["id"])
    profile = data_repo.get_profile(user["id"])
    xp_result = data_repo.grant_xp(user["id"], "login")
    auth.audit("login_success", user["id"], request)

    access = auth.create_access_token(user["id"])
    refresh = auth.issue_refresh_token(user["id"], request)
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "bearer",
        "display_name": profile.get("display_name"),
        "level": xp_result,
    }


class RefreshIn(BaseModel):
    refresh_token: str

@app.post("/api/auth/refresh")
@limiter.limit("20/minute")
async def refresh_token(request: Request, body: RefreshIn):
    new_refresh, user_id = auth.rotate_refresh_token(body.refresh_token, request)
    return {"access_token": auth.create_access_token(user_id), "refresh_token": new_refresh, "token_type": "bearer"}


@app.post("/api/auth/logout")
async def logout(body: RefreshIn):
    auth.revoke_refresh_token(body.refresh_token)
    return {"ok": True}


@app.get("/api/auth/me")
def me(user: dict = Depends(auth.get_current_user)):
    profile = data_repo.get_profile(user["id"])
    return {
        "username": user["username"],
        "email": user["email"],
        "display_name": profile.get("display_name"),
        "plan": user["plan"],
        "plan_status": user["plan_status"],
    }

# ── Plano / Assinatura ──────────────────────────────────────────────────────────
@app.get("/api/subscription/me", dependencies=[])
def my_subscription(user: dict = Depends(auth.get_current_user)):
    row = data_repo.db.fetchone(
        "SELECT plan, status, provider, current_period_start, current_period_end, cancel_at_period_end "
        "FROM subscriptions WHERE user_id=%s", (user["id"],)
    )
    return row or {"plan": "free", "status": "active"}


class AdminSetPlanIn(BaseModel):
    user_id: int
    plan: str          # 'free' | 'pro' | 'premium'
    status: str = "active"
    period_end: Optional[str] = None

def require_admin(user: dict = Depends(auth.get_current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "Apenas administradores podem fazer isso.")
    return user

@app.post("/api/subscription/admin/set-plan", dependencies=[Depends(require_admin)])
def admin_set_plan(body: AdminSetPlanIn):
    """
    Ponto único para refletir mudanças de plano vindas do gateway de pagamento
    escolhido no futuro (Stripe/Mercado Pago/etc). Hoje é manual; quando o
    gateway for definido, troque por um webhook que chama esta mesma lógica.
    """
    if body.plan not in ("free", "pro", "premium"):
        raise HTTPException(400, "plano inválido")
    data_repo.db.execute(
        "INSERT INTO subscriptions (user_id, plan, status, current_period_end) VALUES (%s,%s,%s,%s) "
        "ON CONFLICT (user_id) DO UPDATE SET plan=EXCLUDED.plan, status=EXCLUDED.status, "
        "current_period_end=EXCLUDED.current_period_end",
        (body.user_id, body.plan, body.status, body.period_end),
    )
    data_repo.db.execute(
        "INSERT INTO subscription_events (user_id, event_type, raw_payload) VALUES (%s,'manual_admin_change',%s)",
        (body.user_id, json.dumps(body.model_dump())),
    )
    return {"ok": True}

# ── Perfil ─────────────────────────────────────────────────────────────────────
class ProfileIn(BaseModel):
    display_name:     Optional[str] = None
    investor_type:    Optional[str] = None
    note:             Optional[str] = None
    interests:        list[str] | None = None
    experience_level: Optional[str] = None

@app.get("/api/users/profile")
def get_profile(user: dict = Depends(auth.get_current_user)):
    return data_repo.get_profile(user["id"])

@app.put("/api/users/profile")
def update_profile(body: ProfileIn, user: dict = Depends(auth.get_current_user)):
    allowed_exp = {"beginner", "intermediate", "advanced"}
    updates = body.model_dump(exclude_none=True)
    if "experience_level" in updates and updates["experience_level"] not in allowed_exp:
        raise HTTPException(400, "experience_level inválido. Use: beginner, intermediate, advanced")
    data_repo.update_profile(user["id"], updates)
    return {"ok": True}

# ── Config ─────────────────────────────────────────────────────────────────────
class ConfigIn(BaseModel):
    autorefresh:    bool  | None = None
    interval_sec:   int   | None = None
    currency:       str   | None = None
    threshold:      Optional[float] = None
    accent_color:   str   | None = None
    compact_mode:   bool  | None = None
    show_instab:    bool  | None = None
    animations:     bool  | None = None
    alert_strong:   bool  | None = None
    alert_interest: bool  | None = None
    news_interest:  bool  | None = None
    cache_enabled:  bool  | None = None

@app.get("/api/users/config")
def get_config(user: dict = Depends(auth.get_current_user)):
    return data_repo.get_config(user["id"])

@app.put("/api/users/config")
def update_config(body: ConfigIn, user: dict = Depends(auth.get_current_user)):
    data_repo.update_config(user["id"], body.model_dump(exclude_none=True))
    return {"ok": True}

# ── API Keys (cifradas) ────────────────────────────────────────────────────────
class ApiKeyIn(BaseModel):
    provider: str
    key:      str

@app.put("/api/users/apikeys")
def upsert_apikey(body: ApiKeyIn, user: dict = Depends(auth.get_current_user)):
    if body.provider not in ("finnhub", "openrouter"):
        raise HTTPException(400, "provider inválido")
    data_repo.upsert_api_key(user["id"], body.provider, body.key)
    return {"ok": True}

@app.get("/api/users/apikeys/{provider}/exists")
def apikey_exists(provider: str, user: dict = Depends(auth.get_current_user)):
    return {"exists": data_repo.api_key_exists(user["id"], provider)}

# ── Cache Management ───────────────────────────────────────────────────────────
@app.delete("/api/cache")
def clear_cache(user: dict = Depends(auth.get_current_user)):
    cache.clear()
    return {"ok": True, "message": "Cache limpo com sucesso."}

@app.get("/api/cache/stats")
def cache_stats(user: dict = Depends(auth.get_current_user)):
    return {"entries": cache.size()}

# ══════════════════════════════════════════════════════════════════════════════
#  INDICADORES TÉCNICOS
# ══════════════════════════════════════════════════════════════════════════════

async def _fh_candle(symbol: str, user: dict, resolution: str = "D", count: int = 60) -> list[float]:
    cache_key = f"candle:{symbol}:{resolution}:{count}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    now = int(time.time())
    from_ts = now - (count * 86400 * 2)
    fh_key_rt = _user_fh_key(user)
    params = {"symbol": symbol, "resolution": resolution, "from": from_ts, "to": now, "token": fh_key_rt}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get("https://finnhub.io/api/v1/stock/candle", params=params)
        if r.status_code != 200:
            raise HTTPException(502, f"Finnhub candle error {r.status_code}")
        result = r.json()
        closes = result.get("c", [])
        if not closes:
            raise HTTPException(404, f"Sem dados históricos para {symbol}")
        cfg = data_repo.get_config(user["id"])
        if cfg.get("cache_enabled", True):
            cache.set(cache_key, closes, ttl_seconds=60)
        return closes
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Erro ao buscar candles: {str(e)[:100]}")

def _calc_rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        raise HTTPException(422, f"Dados insuficientes para RSI({period}): precisa de ao menos {period+1} candles")
    changes = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [max(c, 0) for c in changes]
    losses = [abs(min(c, 0)) for c in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

def _calc_ema(closes: list[float], period: int) -> list[float]:
    if len(closes) < period:
        return []
    k = 2 / (period + 1)
    emas = [sum(closes[:period]) / period]
    for price in closes[period:]:
        emas.append(price * k + emas[-1] * (1 - k))
    return emas

def _calc_macd(closes: list[float]):
    ema12 = _calc_ema(closes, 12)
    ema26 = _calc_ema(closes, 26)
    if not ema12 or not ema26:
        raise HTTPException(422, "Dados insuficientes para MACD")
    diff = len(ema12) - len(ema26)
    ema12_aligned = ema12[diff:]
    macd_line = [e12 - e26 for e12, e26 in zip(ema12_aligned, ema26)]
    signal_line = _calc_ema(macd_line, 9)
    if not signal_line:
        raise HTTPException(422, "Dados insuficientes para linha de sinal MACD")
    macd_val   = round(macd_line[-1], 4)
    signal_val = round(signal_line[-1], 4)
    hist_val   = round(macd_val - signal_val, 4)
    return macd_val, signal_val, hist_val

def _calc_bollinger(closes: list[float], period: int = 20, std_dev: float = 2.0):
    if len(closes) < period:
        raise HTTPException(422, f"Dados insuficientes para Bollinger({period})")
    window = closes[-period:]
    middle = sum(window) / period
    variance = sum((x - middle) ** 2 for x in window) / period
    std = math.sqrt(variance)
    upper = round(middle + std_dev * std, 4)
    lower = round(middle - std_dev * std, 4)
    return round(upper, 4), round(middle, 4), round(lower, 4)

class IndicatorIn(BaseModel):
    symbol: str
    period: Optional[int] = None

@app.post("/api/indicators/rsi")
async def indicator_rsi(body: IndicatorIn, user: dict = Depends(auth.get_current_user)):
    period = body.period or 14
    closes = await _fh_candle(body.symbol, user, count=max(100, period * 3))
    rsi = _calc_rsi(closes, period)
    signal = "overbought" if rsi >= 70 else ("oversold" if rsi <= 30 else "neutral")
    return {"symbol": body.symbol, "rsi": rsi, "signal": signal, "period": period}

@app.post("/api/indicators/macd")
async def indicator_macd(body: IndicatorIn, user: dict = Depends(auth.get_current_user)):
    closes = await _fh_candle(body.symbol, user, count=200)
    macd, signal, histogram = _calc_macd(closes)
    trend = "bullish" if histogram > 0 else "bearish"
    return {"symbol": body.symbol, "macd": macd, "signal": signal, "histogram": histogram, "trend": trend}

@app.post("/api/indicators/bollinger")
async def indicator_bollinger(body: IndicatorIn, user: dict = Depends(auth.get_current_user)):
    period = body.period or 20
    closes = await _fh_candle(body.symbol, user, count=max(100, period * 3))
    upper, middle, lower = _calc_bollinger(closes, period)
    current = round(closes[-1], 4)
    band_range = upper - lower
    if band_range > 0:
        rel = (current - lower) / band_range
        position = "near_upper" if rel >= 0.9 else ("near_lower" if rel <= 0.1 else ("above_middle" if rel >= 0.5 else "below_middle"))
    else:
        position = "above_middle"
    return {"symbol": body.symbol, "upper": upper, "middle": middle, "lower": lower,
            "current_price": current, "position": position, "period": period}

@app.post("/api/indicators/all")
async def indicator_all(body: IndicatorIn, user: dict = Depends(auth.get_current_user)):
    closes = await _fh_candle(body.symbol, user, count=200)
    try:
        rsi_val = _calc_rsi(closes, 14)
        rsi_sig = "overbought" if rsi_val >= 70 else ("oversold" if rsi_val <= 30 else "neutral")
        rsi_result = {"rsi": rsi_val, "signal": rsi_sig, "period": 14}
    except Exception as e:
        rsi_result = {"error": str(e)}
    try:
        macd, sig, hist = _calc_macd(closes)
        macd_result = {"macd": macd, "signal": sig, "histogram": hist, "trend": "bullish" if hist > 0 else "bearish"}
    except Exception as e:
        macd_result = {"error": str(e)}
    try:
        upper, middle, lower = _calc_bollinger(closes, 20)
        current = round(closes[-1], 4)
        band_range = upper - lower
        if band_range > 0:
            rel = (current - lower) / band_range
            pos = "near_upper" if rel >= 0.9 else ("near_lower" if rel <= 0.1 else ("above_middle" if rel >= 0.5 else "below_middle"))
        else:
            pos = "above_middle"
        boll_result = {"upper": upper, "middle": middle, "lower": lower, "current_price": current, "position": pos, "period": 20}
    except Exception as e:
        boll_result = {"error": str(e)}
    return {"symbol": body.symbol, "rsi": rsi_result, "macd": macd_result, "bollinger": boll_result}

# ── Chat ───────────────────────────────────────────────────────────────────────
class SendIn(BaseModel):
    session_id: Optional[int] = None
    message:    str
    context:    Optional[str] = None
    history:    list[dict] | None = None

@app.get("/api/chat/sessions")
def list_sessions(user: dict = Depends(auth.get_current_user)):
    return data_repo.list_chat_sessions(user["id"])

@app.post("/api/chat/sessions")
def create_session(user: dict = Depends(auth.get_current_user)):
    return data_repo.create_chat_session(user["id"])

@app.delete("/api/chat/sessions/{session_id}")
def delete_session(session_id: int, user: dict = Depends(auth.get_current_user)):
    data_repo.delete_chat_session(user["id"], session_id)
    return {"ok": True}

@app.get("/api/chat/sessions/{session_id}/messages")
def get_messages(session_id: int, user: dict = Depends(auth.get_current_user)):
    session = data_repo.get_owned_session(user["id"], session_id)
    if not session:
        raise HTTPException(404, "Sessão não encontrada")
    return data_repo.get_chat_messages(session_id)

@app.post("/api/chat/send")
@limiter.limit("20/minute")
async def send_message(request: Request, body: SendIn, user: dict = Depends(auth.get_current_user)):
    profile = data_repo.get_profile(user["id"])

    if body.session_id:
        session = data_repo.get_owned_session(user["id"], body.session_id)
        if not session:
            raise HTTPException(404, "Sessão não encontrada")
    else:
        session = data_repo.create_chat_session(user["id"])

    name = profile.get("display_name") or "investidor"
    inv  = profile.get("investor_type", "moderado")
    note = profile.get("note", "")
    ints = ", ".join(profile.get("interests", [])).upper()
    exp  = profile.get("experience_level", "beginner")
    exp_map = {
        "beginner": "linguagem simples, sem jargões técnicos",
        "intermediate": "dados técnicos com contexto explicado",
        "advanced": "dados brutos e análise técnica direta, sem explicações adicionais"
    }
    exp_instruction = exp_map.get(exp, exp_map["beginner"])

    system_prompt = f"""Você é o EconRadar, consultor de mercados financeiros para {name}.
Perfil do investidor: {inv}. Foco: {ints or 'não definido'}.{f' Nota: "{note}"' if note else ''}
Nível de experiência: {exp} — use {exp_instruction}.
{f'Contexto de mercado: {body.context}' if body.context else ''}
Responda em português, seja direto e analítico. Máximo 4 parágrafos."""

    messages = (body.history or []) + [{"role": "user", "content": body.message}]

    or_key = _user_or_key(user)
    if not or_key:
        raise HTTPException(503, "Chave OpenRouter não configurada.")

    reply = await _or_call(
        or_key,
        messages=[{"role": "system", "content": system_prompt}] + messages,
        max_tokens=1000, temperature=0.7, timeout=20
    )

    data_repo.append_messages(session["id"], [("user", body.message), ("assistant", reply)])
    new_title = body.message[:60] + ("…" if len(body.message) > 60 else "") if session.get("title", "Nova conversa") == "Nova conversa" else None
    data_repo.touch_session(session["id"], title=new_title)

    if not profile.get("ai_used"):
        data_repo.update_profile(user["id"], {})  # no-op pra manter padrão; flag abaixo
        data_repo.db.execute("UPDATE profiles SET ai_used=1 WHERE user_id=%s", (user["id"],))

    data_repo.grant_xp(user["id"], "chat_message")
    return {"session_id": session["id"], "reply": reply}

# ── Resumo Noturno ─────────────────────────────────────────────────────────────
def _safe_json(raw: str, context: str = "") -> dict:
    raw = raw.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    match = re.search(r'\{[\s\S]*\}', raw)
    if match:
        candidate = match.group(0)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            open_braces   = candidate.count('{') - candidate.count('}')
            open_brackets = candidate.count('[') - candidate.count(']')
            fixed = candidate + (']' * max(0, open_brackets)) + ('}' * max(0, open_braces))
            try:
                return json.loads(fixed)
            except json.JSONDecodeError:
                pass
    raise HTTPException(502, f"IA retornou JSON inválido{(' em '+context) if context else ''}. Tente novamente.")


class SummaryIn(BaseModel):
    context:      str
    instab_score: int
    crit_count:   int
    assets_json:  Any = None

@app.post("/api/summary/generate")
@limiter.limit("5/minute")
async def generate_summary(request: Request, body: SummaryIn, user: dict = Depends(auth.get_current_user)):
    cfg = data_repo.get_config(user["id"])
    cache_key = f"summary:{user['id']}:{hash(body.context[:100])}"
    if cfg.get("cache_enabled", True):
        cached = cache.get(cache_key)
        if cached:
            return cached

    or_key = _user_or_key(user)
    if not or_key:
        raise HTTPException(503, "Chave OpenRouter não configurada.")

    prompt = (
        f"Dados de mercado do fechamento: {body.context} | "
        f"Instabilidade: {body.instab_score}/100 | Alertas críticos: {body.crit_count}\n\n"
        'Retorne SOMENTE este JSON preenchido, sem markdown:\n'
        '{"resumo":"2-3 frases sobre os principais movimentos do dia.",'
        '"observar":[{"ativo":"ATIVO1","motivo":"motivo curto","direcao":"alta"},'
        '{"ativo":"ATIVO2","motivo":"motivo curto","direcao":"baixa"},'
        '{"ativo":"ATIVO3","motivo":"motivo curto","direcao":"neutro"}]}'
    )

    raw = await _or_call(
        or_key,
        messages=[
            {"role": "system", "content": "Você é um analista financeiro. Responda APENAS com JSON válido."},
            {"role": "user", "content": prompt}
        ],
        max_tokens=600, temperature=0.3, timeout=20
    )
    parsed = _safe_json(raw, "resumo noturno")
    data_repo.add_night_summary(user["id"], parsed.get("resumo", ""), body.instab_score)

    if cfg.get("cache_enabled", True):
        cache.set(cache_key, parsed, ttl_seconds=300)
    return parsed

@app.get("/api/summary/history")
def summary_history(user: dict = Depends(auth.get_current_user)):
    return data_repo.get_night_summaries(user["id"], type_="daily")

# ── Market Proxy (rotas públicas, sem dado de usuário) ───────────────────────
async def _fh(path: str, params: dict, fh_key_rt: str):
    params["token"] = fh_key_rt
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(f"https://finnhub.io/api/v1{path}", params=params)
    except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.TimeoutException):
        raise HTTPException(504, "Finnhub timeout — tente novamente em instantes")
    except httpx.RequestError as e:
        raise HTTPException(502, f"Finnhub connection error: {str(e)[:100]}")
    if r.status_code != 200:
        raise HTTPException(502, f"Finnhub error {r.status_code}: {r.text[:200]}")
    return r.json()

@app.get("/api/market/quote")
@limiter.limit("60/minute")
async def market_quote(request: Request, symbol: str, user: dict = Depends(auth.get_current_user)):
    cfg = data_repo.get_config(user["id"])
    cache_key = f"quote:{symbol}"
    if cfg.get("cache_enabled", True):
        cached = cache.get(cache_key)
        if cached:
            return cached
    result = await _fh("/quote", {"symbol": symbol}, _user_fh_key(user))
    if cfg.get("cache_enabled", True):
        cache.set(cache_key, result, ttl_seconds=60)
    return result

@app.get("/api/market/forex")
async def market_forex(base: str = "USD"):
    return await _fh("/forex/rates", {"base": base}, FH_KEY)

@app.get("/api/market/news")
async def market_news(category: str = "general"):
    return await _fh("/news", {"category": category}, FH_KEY)

@app.get("/api/proxy/awesome")
async def proxy_awesome(pairs: str):
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(f"https://economia.awesomeapi.com.br/json/last/{pairs}")
    except (httpx.ReadTimeout, httpx.ConnectTimeout):
        raise HTTPException(504, "AwesomeAPI timeout")
    if r.status_code != 200:
        raise HTTPException(502, f"AwesomeAPI error: {r.status_code}")
    return r.json()

@app.get("/api/proxy/coingecko")
async def proxy_coingecko(ids: str, vs_currencies: str = "usd,brl", include_24hr_change: str = "true"):
    params = {"ids": ids, "vs_currencies": vs_currencies, "include_24hr_change": include_24hr_change}
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get("https://api.coingecko.com/api/v3/simple/price", params=params)
    if r.status_code != 200:
        raise HTTPException(502, f"CoinGecko error: {r.status_code}")
    return r.json()

@app.get("/api/proxy/yahoo")
async def proxy_yahoo(symbols: str):
    syms = symbols.replace(' ', '').split(',')[:10]
    yf_symbols = ','.join(syms)
    try:
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "Mozilla/5.0"}) as client:
            r = await client.get(
                "https://query1.finance.yahoo.com/v7/finance/quote",
                params={"symbols": yf_symbols}
            )
        if r.status_code != 200:
            raise HTTPException(502, f"Yahoo error: {r.status_code}")
        return r.json()
    except (httpx.ReadTimeout, httpx.ConnectTimeout):
        raise HTTPException(504, "Yahoo timeout")

GNEWS_TOPICS = {
    "general": "https://news.google.com/rss?hl=pt-BR&gl=BR&ceid=BR:pt-419",
    "forex": "https://news.google.com/rss/search?q=dólar+OR+câmbio+OR+forex&hl=pt-BR&gl=BR&ceid=BR:pt-419",
    "crypto": "https://news.google.com/rss/search?q=bitcoin+OR+criptomoeda&hl=pt-BR&gl=BR&ceid=BR:pt-419",
}

@app.get("/api/proxy/finnhub/quote")
async def proxy_finnhub_quote(symbol: str):
    try:
        result = await _fh("/quote", {"symbol": symbol}, FH_KEY)
        return result
    except HTTPException:
        raise

@app.get("/api/proxy/finnhub/news")
async def proxy_finnhub_news(category: str = "general"):
    try:
        result = await _fh("/news", {"category": category}, FH_KEY)
        return result
    except HTTPException:
        pass

    url = GNEWS_TOPICS.get(category, GNEWS_TOPICS["general"])
    try:
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "Mozilla/5.0"}) as client:
            r = await client.get(url)
        if r.status_code != 200:
            return []
        root = _ET.fromstring(r.text)
        items = []
        for item in root.findall(".//item")[:20]:
            title   = (item.findtext("title") or "").strip()
            link    = (item.findtext("link")  or "").strip()
            source  = (item.findtext("source") or "Google News").strip()
            pub     = item.findtext("pubDate") or ""
            try:
                import email.utils
                ts = int(email.utils.parsedate_to_datetime(pub).timestamp())
            except Exception:
                ts = int(time.time())
            items.append({
                "category": category, "datetime": ts, "headline": title,
                "id": abs(hash(link)), "image": "", "related": "",
                "source": source, "summary": title, "url": link,
            })
        return items
    except Exception:
        return []

# ══════════════════════════════════════════════════════════════════════════════
#  PORTFÓLIO PESSOAL
# ══════════════════════════════════════════════════════════════════════════════

class PortfolioAsset(BaseModel):
    id:        str
    name:      str
    pair:      str
    amount:    float
    buy_price: float
    buy_date:  Optional[str] = None

class PortfolioGoal(BaseModel):
    goal_amount:   Optional[float] = None
    goal_label:    str   | None = None
    goal_deadline: str   | None = None

@app.get("/api/portfolio")
def get_portfolio(user: dict = Depends(auth.get_current_user)):
    goal = data_repo.get_portfolio_goal(user["id"])
    b3 = data_repo.get_b3_positions(user["id"])
    return {
        "assets": data_repo.get_portfolio_assets(user["id"]),
        **goal,
        "b3_positions": b3["positions"],
        "b3_imported_at": b3["imported_at"],
    }

@app.post("/api/portfolio/assets")
def add_asset(body: PortfolioAsset, user: dict = Depends(auth.get_current_user)):
    data_repo.add_asset(user["id"], body.model_dump())
    data_repo.grant_xp(user["id"], "portfolio_add", 10)
    return {"ok": True}

@app.delete("/api/portfolio/assets/{asset_id}")
def remove_asset(asset_id: str, user: dict = Depends(auth.get_current_user)):
    data_repo.remove_asset(user["id"], asset_id)
    return {"ok": True}

@app.put("/api/portfolio/goal")
def update_goal(body: PortfolioGoal, user: dict = Depends(auth.get_current_user)):
    data_repo.update_goal(user["id"], body.model_dump(exclude_none=True))
    return {"ok": True}

# ══════════════════════════════════════════════════════════════════════════════
#  IMPORTAÇÃO DE CARTEIRA B3 VIA CSV (dados sensíveis — cifrados em repouso)
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/portfolio/import-csv")
async def import_portfolio_csv(file: UploadFile = File(...), user: dict = Depends(auth.get_current_user)):
    if not file.filename.lower().endswith(".csv"):
        raise HTTPException(400, "Envie um arquivo .csv")

    content = await file.read()
    for enc in ("utf-8-sig", "latin-1", "utf-8"):
        try:
            text = content.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise HTTPException(400, "Não foi possível decodificar o arquivo. Salve como UTF-8.")

    reader = csv.DictReader(io.StringIO(text), delimiter=";")

    def norm(s):
        return s.strip().lstrip("\ufeff").lower()

    positions = []
    errors = []
    row_num = 0
    for row in reader:
        row_num += 1
        normalized = {norm(k): v.strip() for k, v in row.items() if k}
        symbol_raw  = normalized.get("código de negociação") or normalized.get("codigo de negociacao") or normalized.get("codigo") or ""
        qty_raw     = normalized.get("quantidade disponível") or normalized.get("quantidade disponivel") or normalized.get("quantidade") or "0"
        institution = normalized.get("instituição") or normalized.get("instituicao") or normalized.get("corretora") or ""
        asset_type  = normalized.get("tipo") or ""

        symbol = re.sub(r'[^A-Z0-9]', '', symbol_raw.upper())
        if not symbol:
            errors.append(f"Linha {row_num}: símbolo vazio, ignorada.")
            continue
        try:
            qty_clean = re.sub(r'[^\d]', '', qty_raw)
            quantity = int(qty_clean) if qty_clean else 0
        except ValueError:
            quantity = 0
        if quantity <= 0:
            errors.append(f"Linha {row_num}: {symbol} com quantidade zero/inválida, ignorada.")
            continue

        positions.append({"symbol": symbol, "quantity": quantity, "institution": institution, "type": asset_type})

    if not positions:
        raise HTTPException(422, f"Nenhuma posição válida encontrada no CSV. Erros: {'; '.join(errors[:5])}")

    data_repo.import_b3_positions(user["id"], positions)

    return {"ok": True, "imported": len(positions), "skipped": len(errors),
            "positions": positions, "warnings": errors[:10]}

@app.get("/api/portfolio/b3")
def get_b3_positions(user: dict = Depends(auth.get_current_user)):
    return data_repo.get_b3_positions(user["id"])

@app.delete("/api/portfolio/b3")
def clear_b3_positions(user: dict = Depends(auth.get_current_user)):
    data_repo.clear_b3_positions(user["id"])
    return {"ok": True}

# ══════════════════════════════════════════════════════════════════════════════
#  SISTEMA DE NÍVEIS / XP
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/level")
def get_level(user: dict = Depends(auth.get_current_user)):
    return data_repo.get_level(user["id"])

@app.post("/api/level/event")
def register_event(event: str, user: dict = Depends(auth.get_current_user)):
    return data_repo.grant_xp(user["id"], event)

# ══════════════════════════════════════════════════════════════════════════════
#  JARVIS — IA PROATIVA
# ══════════════════════════════════════════════════════════════════════════════

class JarvisIn(BaseModel):
    market_context: str
    portfolio_value: Optional[float] = None
    last_insight_at: Optional[str] = None

@app.post("/api/jarvis/insight")
async def jarvis_insight(body: JarvisIn, user: dict = Depends(auth.get_current_user)):
    profile = data_repo.get_profile(user["id"])
    assets = data_repo.get_portfolio_assets(user["id"])
    goal = data_repo.get_portfolio_goal(user["id"])
    or_key = _user_or_key(user)
    if not or_key:
        raise HTTPException(503, "Chave OpenRouter não configurada.")

    name     = profile.get("display_name", "investidor")
    inv_type = profile.get("investor_type", "moderado")
    level    = profile.get("level", "iniciante")

    if assets:
        lines = [f"- {a['name']}: {a['amount']} unidades (comprado a R${a['buy_price']:.2f})" for a in assets]
        portfolio_str = "Portfólio do usuário:\n" + "\n".join(lines)
    else:
        portfolio_str = "Usuário ainda não cadastrou portfólio."

    prompt = f"""Você é o EconRadar, assessor financeiro pessoal de {name} (perfil {inv_type}, nível {level}).

{portfolio_str}
Meta financeira: {goal.get('goal_label') or 'não definida'}
Contexto de mercado agora: {body.market_context[:800]}

SUA TAREFA: analise SE há algo realmente relevante para avisar este investidor AGORA.
Seja criterioso — só gere um insight se houver algo genuinamente útil e acionável.
Se não houver nada relevante, retorne exatamente: {{"insight": "", "type": "none"}}

Se houver algo relevante, retorne JSON com:
- "insight": texto curto (máx 2 frases), direto, personalizado para {name}
- "type": um de "oportunidade" | "alerta" | "dica" | "patrimonio"
- "asset": ativo relacionado (ex: "btc") ou null
- "urgency": "alta" | "media" | "baixa"

Retorne APENAS o JSON, sem markdown, sem explicação."""

    try:
        raw = await _or_call(
            or_key,
            messages=[
                {"role": "system", "content": "Você é um assessor financeiro. Responda APENAS com JSON válido."},
                {"role": "user",   "content": prompt}
            ],
            max_tokens=300, temperature=0.4, timeout=15
        )
    except HTTPException:
        return {"insight": "", "type": "none"}

    raw = raw.replace("```json", "").replace("```", "").strip()
    try:
        result = _safe_json(raw, "jarvis insight")
    except HTTPException:
        return {"insight": "", "type": "none"}

    if result.get("insight"):
        data_repo.add_jarvis_insight(user["id"], result["insight"], result.get("type", "dica"),
                                      result.get("asset"), result.get("urgency"))
    return result

@app.get("/api/jarvis/history")
def jarvis_history(user: dict = Depends(auth.get_current_user)):
    return data_repo.get_jarvis_insights(user["id"], limit=20)

# ══════════════════════════════════════════════════════════════════════════════
#  SCORE DE MOMENTO
# ══════════════════════════════════════════════════════════════════════════════

class ScoreIn(BaseModel):
    asset_id:   str
    asset_name: str
    pair:       str
    price:      float
    chg_pct:    float
    high_hist:  Optional[str] = None
    low_hist:   Optional[str] = None
    var_30d:    Optional[float] = None

@app.post("/api/score/moment")
async def score_moment(body: ScoreIn, user: dict = Depends(auth.get_current_user)):
    profile = data_repo.get_profile(user["id"])
    or_key = _user_or_key(user)
    if not or_key:
        raise HTTPException(503, "Chave OpenRouter não configurada.")

    inv_type = profile.get("investor_type", "moderado")
    level    = profile.get("level", "iniciante")

    prompt = f"""Você é um analista financeiro quantitativo. Calcule o Score de Momento para o ativo abaixo.

Ativo: {body.asset_name} ({body.pair})
Preço atual: {body.price}
Variação hoje: {body.chg_pct:+.2f}%
Variação 30 dias: {body.var_30d:+.1f}% (estimada)
Máxima histórica: {body.high_hist or 'desconhecida'}
Mínima histórica: {body.low_hist or 'desconhecida'}
Perfil do investidor: {inv_type} (nível {level})

Analise:
1. Tendência de curto prazo (variação hoje vs 30d)
2. Distância do preço em relação ao histórico (se próximo de máxima = sinal de atenção)
3. Adequação ao perfil do investidor

Retorne APENAS este JSON (sem markdown):
{{
  "score": <número 0-100>,
  "label": "<Comprar|Aguardar|Reduzir>",
  "color": "<green|amber|red>",
  "resumo": "<1 frase direta e personalizada>",
  "fatores": [
    {{"nome": "Tendência", "valor": <0-100>, "desc": "<curto texto>"}},
    {{"nome": "Posição histórica", "valor": <0-100>, "desc": "<curto texto>"}},
    {{"nome": "Adequação ao perfil", "valor": <0-100>, "desc": "<curto texto>"}}
  ]
}}"""

    raw = await _or_call(
        or_key,
        messages=[
            {"role": "system", "content": "Analista financeiro. Responda APENAS JSON válido."},
            {"role": "user",   "content": prompt}
        ],
        max_tokens=400, temperature=0.3, timeout=15
    )
    raw = raw.replace("```json", "").replace("```", "").strip()
    return _safe_json(raw, "score momento")

# ══════════════════════════════════════════════════════════════════════════════
#  SIMULADOR DE CENÁRIOS (feature 'pro' — exemplo de uso de require_plan)
# ══════════════════════════════════════════════════════════════════════════════

class ScenarioIn(BaseModel):
    scenario_desc: str
    affected_assets: list[str]
    portfolio_snapshot: str

@app.post("/api/scenario/simulate")
async def simulate_scenario(body: ScenarioIn, user: dict = Depends(auth.require_plan("pro"))):
    profile = data_repo.get_profile(user["id"])
    or_key = _user_or_key(user)
    if not or_key:
        raise HTTPException(503, "Chave OpenRouter não configurada.")

    name     = profile.get("display_name", "investidor")
    inv_type = profile.get("investor_type", "moderado")

    prompt = f"""Você é um analista de risco financeiro. Simule o impacto do cenário abaixo no portfólio.

Cenário hipotético: "{body.scenario_desc}"
Portfólio atual: {body.portfolio_snapshot[:600]}
Ativos afetados mencionados: {', '.join(body.affected_assets)}
Perfil do investidor: {name} ({inv_type})

Analise o impacto provável em cada ativo do portfólio e no total.

Retorne APENAS este JSON (sem markdown):
{{
  "cenario": "<resumo do cenário em 1 frase>",
  "impacto_total": "<positivo|negativo|neutro>",
  "variacao_estimada_pct": <número, ex: -12.5>,
  "narrativa": "<2 frases explicando o raciocínio>",
  "ativos": [
    {{"id": "<id>", "nome": "<nome>", "impacto_pct": <número>, "explicacao": "<curto>"}}
  ],
  "recomendacao": "<1 ação prática para o investidor>"
}}"""

    raw = await _or_call(
        or_key,
        messages=[
            {"role": "system", "content": "Analista de risco. Responda APENAS JSON válido."},
            {"role": "user",   "content": prompt}
        ],
        max_tokens=500, temperature=0.4, timeout=20
    )
    raw = raw.replace("```json", "").replace("```", "").strip()
    return _safe_json(raw, "simulação de cenário")

# ══════════════════════════════════════════════════════════════════════════════
#  RELATÓRIO MENSAL PDF (feature 'pro' — exemplo de uso de require_plan)
# ══════════════════════════════════════════════════════════════════════════════

class ReportIn(BaseModel):
    portfolio_snapshot: str
    market_context:     str
    period_label:       Optional[str] = None

@app.post("/api/report/generate")
async def generate_report(body: ReportIn, user: dict = Depends(auth.require_plan("pro"))):
    profile = data_repo.get_profile(user["id"])
    or_key = _user_or_key(user)
    if not or_key:
        raise HTTPException(503, "Chave OpenRouter não configurada.")

    name     = profile.get("display_name", "Investidor")
    inv_type = profile.get("investor_type", "moderado")
    goal     = data_repo.get_portfolio_goal(user["id"]).get("goal_label", "")
    period   = body.period_label or datetime.now().strftime("%B %Y")

    prompt = f"""Assessor financeiro de {name}. Gere relatório mensal em JSON.

Período: {period} | Perfil: {inv_type} | Meta: {goal or 'não definida'}
Portfólio: {body.portfolio_snapshot[:400]}
Mercado: {body.market_context[:200]}

JSON exato (sem markdown, sem explicação):
{{"titulo":"Relatório EconRadar — {period}","investidor":"{name}","periodo":"{period}","resumo_executivo":"2 frases sobre desempenho","destaques":[{{"tipo":"positivo","texto":"ponto positivo"}},{{"tipo":"atencao","texto":"ponto de atenção"}}],"analise_portfolio":"1 parágrafo sobre os ativos","oportunidades":"1 frase sobre oportunidades","riscos":"1 frase sobre riscos","recomendacao_mes":"1 ação concreta","nota_perfil":"observação para perfil {inv_type}"}}"""

    raw = await _or_call(
        or_key,
        messages=[
            {"role": "system", "content": "Assessor financeiro. Responda APENAS JSON válido, sem markdown."},
            {"role": "user",   "content": prompt}
        ],
        max_tokens=1200, temperature=0.4, timeout=30
    )
    raw = raw.replace("```json", "").replace("```", "").strip()
    match = re.search(r'\{[\s\S]*\}', raw)
    if not match:
        raise HTTPException(502, f"Formato inválido: {raw[:100]}")
    try:
        result = json.loads(match.group(0))
    except json.JSONDecodeError as e:
        raise HTTPException(502, f"JSON incompleto - tente novamente. Detalhe: {str(e)[:80]}")

    data_repo.add_night_summary(
        user["id"], result.get("resumo_executivo", ""), 0,
        type_="monthly_report", period_label=period, full_report=result,
    )
    return result

@app.get("/api/report/history")
def report_history(user: dict = Depends(auth.require_plan("pro"))):
    return data_repo.get_night_summaries(user["id"], type_="monthly_report", limit=12)

# ── Health ─────────────────────────────────────────────────────────────────────
@app.get("/api/health")
def health():
    return {"status": "ok", "version": "2.0.0-multiuser"}

@app.get("/api/health/keys")
async def check_keys(user: dict = Depends(auth.get_current_user)):
    or_key = _user_or_key(user)
    result = {"openrouter": {"configured": bool(or_key), "valid": None, "error": None}}
    if or_key:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={"Authorization": f"Bearer {or_key}", "Content-Type": "application/json"},
                    json={"model": OR_MODEL, "messages": [{"role": "user", "content": "ok"}], "max_tokens": 1}
                )
            if r.status_code == 401:
                result["openrouter"]["valid"] = False
                result["openrouter"]["error"] = "Chave inválida ou expirada (401)"
            elif r.status_code in (200, 400, 429, 404):
                result["openrouter"]["valid"] = True
            else:
                result["openrouter"]["valid"] = False
                result["openrouter"]["error"] = f"Erro {r.status_code}"
        except Exception as e:
            result["openrouter"]["valid"] = False
            result["openrouter"]["error"] = str(e)[:100]
    else:
        result["openrouter"]["error"] = "Chave não configurada"
    return result

# ── Iniciar direto com python main.py ──────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
