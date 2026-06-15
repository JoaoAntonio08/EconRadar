"""
EconRadar Backend — Versão 1.0 (Fundação Sólida)
- Rate limiting via slowapi
- Cache inteligente em memória
- Indicadores técnicos (RSI, MACD, Bollinger)
- Nível de experiência no perfil
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
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from jose import JWTError, jwt
from datetime import datetime, timedelta, timezone
import httpx, json, os, re, xml.etree.ElementTree as _ET, bcrypt as _bcrypt
from pathlib import Path
from typing import Any, Optional
import time, math, io, csv

# slowapi
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

def _check_password(plain: str, hashed: str) -> bool:
    return _bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))

# ── Configuração ───────────────────────────────────────────────────────────────
SECRET_KEY    = os.getenv("SECRET_KEY", "")
ALGORITHM     = "HS256"
TOKEN_HOURS   = int(os.getenv("TOKEN_EXPIRE_H", "24"))
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
DATA_FILE     = Path(__file__).parent / "data" / "data.json"

# ── Rate Limiter ───────────────────────────────────────────────────────────────
limiter = Limiter(key_func=get_remote_address)

# ── Cache Inteligente em Memória ───────────────────────────────────────────────
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
        self._store[key] = {
            "value": value,
            "expires_at": time.time() + ttl_seconds,
        }

    def invalidate(self, key: str):
        self._store.pop(key, None)

    def clear(self):
        self._store.clear()

    def size(self) -> int:
        # clean expired first
        now = time.time()
        self._store = {k: v for k, v in self._store.items() if v["expires_at"] > now}
        return len(self._store)

cache = SimpleCache()

# ── JSON Storage ───────────────────────────────────────────────────────────────
def load_data() -> dict:
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_data(data: dict):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)

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
            parts = [b.get("text","") for b in content if isinstance(b, dict) and b.get("type") == "text"]
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

# ── JWT ────────────────────────────────────────────────────────────────────────
bearer = HTTPBearer()

def create_token() -> str:
    exp = datetime.now(timezone.utc) + timedelta(hours=TOKEN_HOURS)
    return jwt.encode({"sub": "admin", "exp": exp}, SECRET_KEY, algorithm=ALGORITHM)

def verify_token(creds: HTTPAuthorizationCredentials = Depends(bearer)):
    try:
        jwt.decode(creds.credentials, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="Token inválido ou expirado")

# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(title="EconRadar API", version="1.0.0")
app.state.limiter = limiter

@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    retry = getattr(exc, "retry_after", 60)
    return JSONResponse(
        status_code=429,
        content={
            "error": "rate_limit",
            "message": "Muitas requisições. Tente novamente em breve.",
            "retry_after": retry
        }
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED,
    allow_origin_regex=r"https://.*\.ngrok(-free)?\.app|https://.*\.ngrok\.io|https://.*\.ngrok-free\.dev",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Arquivos estáticos ─────────────────────────────────────────────────────────
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

# ── Auth ───────────────────────────────────────────────────────────────────────
class LoginIn(BaseModel):
    username: str
    password: str

@app.post("/api/auth/login")
@limiter.limit("10/minute")
async def login(request: Request, body: LoginIn):
    data = load_data()
    user = data["user"]
    if body.username.strip().lower() != user["username"].lower():
        raise HTTPException(status_code=401, detail="Usuário ou senha incorretos")
    if not _check_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Usuário ou senha incorretos")
    result = _grant_xp(data, "login")
    save_data(data)
    return {
        "access_token": create_token(),
        "token_type": "bearer",
        "display_name": data["profile"]["display_name"],
        "level": result,
    }

@app.get("/api/auth/me", dependencies=[Depends(verify_token)])
def me():
    data = load_data()
    return {"username": data["user"]["username"], "display_name": data["profile"]["display_name"]}

# ── Perfil ─────────────────────────────────────────────────────────────────────
class ProfileIn(BaseModel):
    display_name:     Optional[str] = None
    investor_type:    Optional[str] = None
    note:             Optional[str] = None
    interests:        list[str] | None = None
    experience_level: Optional[str] = None   # "beginner" | "intermediate" | "advanced"

@app.get("/api/users/profile", dependencies=[Depends(verify_token)])
def get_profile():
    return load_data()["profile"]

@app.put("/api/users/profile", dependencies=[Depends(verify_token)])
def update_profile(body: ProfileIn):
    data = load_data()
    allowed_exp = {"beginner", "intermediate", "advanced"}
    updates = body.model_dump(exclude_none=True)
    if "experience_level" in updates and updates["experience_level"] not in allowed_exp:
        raise HTTPException(400, "experience_level inválido. Use: beginner, intermediate, advanced")
    for k, v in updates.items():
        data["profile"][k] = v
    save_data(data)
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

@app.get("/api/users/config", dependencies=[Depends(verify_token)])
def get_config():
    return load_data()["config"]

@app.put("/api/users/config", dependencies=[Depends(verify_token)])
def update_config(body: ConfigIn):
    data = load_data()
    for k, v in body.model_dump(exclude_none=True).items():
        data["config"][k] = v
    save_data(data)
    return {"ok": True}

# ── API Keys ───────────────────────────────────────────────────────────────────
class ApiKeyIn(BaseModel):
    provider: str
    key:      str

@app.put("/api/users/apikeys", dependencies=[Depends(verify_token)])
def upsert_apikey(body: ApiKeyIn):
    data = load_data()
    data["api_keys"][body.provider] = body.key
    save_data(data)
    return {"ok": True}

@app.get("/api/users/apikeys/{provider}/exists", dependencies=[Depends(verify_token)])
def apikey_exists(provider: str):
    return {"exists": bool(load_data()["api_keys"].get(provider))}

# ── Cache Management ───────────────────────────────────────────────────────────
@app.delete("/api/cache", dependencies=[Depends(verify_token)])
def clear_cache():
    cache.clear()
    return {"ok": True, "message": "Cache limpo com sucesso."}

@app.get("/api/cache/stats", dependencies=[Depends(verify_token)])
def cache_stats():
    return {"entries": cache.size()}

# ══════════════════════════════════════════════════════════════════════════════
#  INDICADORES TÉCNICOS — Cálculo puro em Python
# ══════════════════════════════════════════════════════════════════════════════

async def _fh_candle(symbol: str, resolution: str = "D", count: int = 60) -> list[float]:
    """Busca dados históricos do Finnhub e retorna lista de preços de fechamento."""
    cache_key = f"candle:{symbol}:{resolution}:{count}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    now = int(time.time())
    from_ts = now - (count * 86400 * 2)  # margem para fins de semana

    data = load_data()
    fh_key_rt = FH_KEY or data.get("api_keys", {}).get("finnhub", "")
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
        # Só aplica cache se habilitado
        if data.get("config", {}).get("cache_enabled", True):
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
    # Alinha os dois arrays pelo menor (ema26 é menor)
    diff = len(ema12) - len(ema26)
    ema12_aligned = ema12[diff:]
    macd_line = [e12 - e26 for e12, e26 in zip(ema12_aligned, ema26)]
    signal_line = _calc_ema(macd_line, 9)
    if not signal_line:
        raise HTTPException(422, "Dados insuficientes para linha de sinal MACD")
    diff2 = len(macd_line) - len(signal_line)
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

@app.post("/api/indicators/rsi", dependencies=[Depends(verify_token)])
async def indicator_rsi(body: IndicatorIn):
    period = body.period or 14
    closes = await _fh_candle(body.symbol, count=max(100, period * 3))
    rsi = _calc_rsi(closes, period)
    signal = "overbought" if rsi >= 70 else ("oversold" if rsi <= 30 else "neutral")
    return {"symbol": body.symbol, "rsi": rsi, "signal": signal, "period": period}

@app.post("/api/indicators/macd", dependencies=[Depends(verify_token)])
async def indicator_macd(body: IndicatorIn):
    closes = await _fh_candle(body.symbol, count=200)
    macd, signal, histogram = _calc_macd(closes)
    trend = "bullish" if histogram > 0 else "bearish"
    return {"symbol": body.symbol, "macd": macd, "signal": signal, "histogram": histogram, "trend": trend}

@app.post("/api/indicators/bollinger", dependencies=[Depends(verify_token)])
async def indicator_bollinger(body: IndicatorIn):
    period = body.period or 20
    closes = await _fh_candle(body.symbol, count=max(100, period * 3))
    upper, middle, lower = _calc_bollinger(closes, period)
    current = round(closes[-1], 4)
    band_range = upper - lower
    if band_range > 0:
        rel = (current - lower) / band_range
        if rel >= 0.9:
            position = "near_upper"
        elif rel <= 0.1:
            position = "near_lower"
        elif rel >= 0.5:
            position = "above_middle"
        else:
            position = "below_middle"
    else:
        position = "above_middle"
    return {
        "symbol": body.symbol,
        "upper": upper, "middle": middle, "lower": lower,
        "current_price": current, "position": position,
        "period": period
    }

@app.post("/api/indicators/all", dependencies=[Depends(verify_token)])
async def indicator_all(body: IndicatorIn):
    closes = await _fh_candle(body.symbol, count=200)

    # RSI
    try:
        rsi_val = _calc_rsi(closes, 14)
        rsi_sig = "overbought" if rsi_val >= 70 else ("oversold" if rsi_val <= 30 else "neutral")
        rsi_result = {"rsi": rsi_val, "signal": rsi_sig, "period": 14}
    except Exception as e:
        rsi_result = {"error": str(e)}

    # MACD
    try:
        macd, sig, hist = _calc_macd(closes)
        macd_result = {"macd": macd, "signal": sig, "histogram": hist, "trend": "bullish" if hist > 0 else "bearish"}
    except Exception as e:
        macd_result = {"error": str(e)}

    # Bollinger
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

@app.get("/api/chat/sessions", dependencies=[Depends(verify_token)])
def list_sessions():
    data = load_data()
    sessions = data.get("chat_sessions", [])
    return sorted(sessions, key=lambda s: s.get("updated_at",""), reverse=True)[:50]

@app.post("/api/chat/sessions", dependencies=[Depends(verify_token)])
def create_session():
    data = load_data()
    sid = (max((s["id"] for s in data["chat_sessions"]), default=0)) + 1
    now = datetime.now().isoformat()
    session = {"id": sid, "title": "Nova conversa", "messages": [], "created_at": now, "updated_at": now}
    data["chat_sessions"].append(session)
    save_data(data)
    return {"id": sid, "title": "Nova conversa"}

@app.delete("/api/chat/sessions/{session_id}", dependencies=[Depends(verify_token)])
def delete_session(session_id: int):
    data = load_data()
    data["chat_sessions"] = [s for s in data["chat_sessions"] if s["id"] != session_id]
    save_data(data)
    return {"ok": True}

@app.get("/api/chat/sessions/{session_id}/messages", dependencies=[Depends(verify_token)])
def get_messages(session_id: int):
    data = load_data()
    session = next((s for s in data["chat_sessions"] if s["id"] == session_id), None)
    if not session:
        raise HTTPException(404, "Sessão não encontrada")
    return session.get("messages", [])

@app.post("/api/chat/send")
@limiter.limit("20/minute")
async def send_message(request: Request, body: SendIn, _=Depends(verify_token)):
    data = load_data()
    profile = data["profile"]

    if body.session_id:
        session = next((s for s in data["chat_sessions"] if s["id"] == body.session_id), None)
        if not session:
            raise HTTPException(404, "Sessão não encontrada")
    else:
        sid = (max((s["id"] for s in data["chat_sessions"]), default=0)) + 1
        now = datetime.now().isoformat()
        session = {"id": sid, "title": "Nova conversa", "messages": [], "created_at": now, "updated_at": now}
        data["chat_sessions"].append(session)

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

    or_key = OR_KEY or load_data().get("api_keys", {}).get("openrouter", "")
    if not or_key:
        raise HTTPException(503, "Chave OpenRouter não configurada.")

    reply = await _or_call(
        or_key,
        messages=[{"role": "system", "content": system_prompt}] + messages,
        max_tokens=1000, temperature=0.7, timeout=20
    )

    now = datetime.now().isoformat()
    session["messages"].append({"role": "user",      "content": body.message, "created_at": now})
    session["messages"].append({"role": "assistant", "content": reply,        "created_at": now})
    session["updated_at"] = now
    if session["title"] == "Nova conversa":
        session["title"] = body.message[:60] + ("…" if len(body.message) > 60 else "")

    if not profile.get("ai_used"):
        data["profile"]["ai_used"] = True

    _grant_xp(data, "chat_message")
    save_data(data)
    return {"session_id": session["id"], "reply": reply}

# ── Resumo Noturno ─────────────────────────────────────────────────────────────
class SummaryIn(BaseModel):
    context:      str
    instab_score: int
    crit_count:   int
    assets_json:  Any = None

@app.post("/api/summary/generate")
@limiter.limit("5/minute")
async def generate_summary(request: Request, body: SummaryIn, _=Depends(verify_token)):
    data = load_data()

    # Cache do resumo (TTL 5 min)
    cache_key = f"summary:{hash(body.context[:100])}"
    if data.get("config", {}).get("cache_enabled", True):
        cached = cache.get(cache_key)
        if cached:
            return cached

    or_key = OR_KEY or data.get("api_keys", {}).get("openrouter", "")
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
    raw = raw.replace("```json","").replace("```","").strip()
    match = re.search(r'\{[\s\S]*\}', raw)
    if not match:
        raise HTTPException(502, f"IA retornou formato inválido: {raw[:150]}")

    parsed = json.loads(match.group(0))

    data["night_summaries"].append({
        "summary_text": parsed.get("resumo",""),
        "instab_score": body.instab_score,
        "generated_at": datetime.now().isoformat()
    })
    data["night_summaries"] = data["night_summaries"][-30:]
    save_data(data)

    if data.get("config", {}).get("cache_enabled", True):
        cache.set(cache_key, parsed, ttl_seconds=300)

    return parsed

@app.get("/api/summary/history", dependencies=[Depends(verify_token)])
def summary_history():
    data = load_data()
    return list(reversed(data.get("night_summaries", [])))

# ── Market Proxy ───────────────────────────────────────────────────────────────
async def _fh(path: str, params: dict, data: dict = None):
    fh_key_rt = FH_KEY or load_data().get("api_keys", {}).get("finnhub", "")
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
async def market_quote(request: Request, symbol: str, _=Depends(verify_token)):
    # Cache 60s para quotes
    data = load_data()
    cache_key = f"quote:{symbol}"
    if data.get("config", {}).get("cache_enabled", True):
        cached = cache.get(cache_key)
        if cached:
            return cached
    result = await _fh("/quote", {"symbol": symbol}, data)
    if data.get("config", {}).get("cache_enabled", True):
        cache.set(cache_key, result, ttl_seconds=60)
    return result

@app.get("/api/market/forex")
async def market_forex(base: str = "USD"):
    return await _fh("/forex/rates", {"base": base}, load_data())

@app.get("/api/market/news")
async def market_news(category: str = "general"):
    return await _fh("/news", {"category": category}, load_data())

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
    syms = symbols.replace(' ','').split(',')[:10]
    yf_symbols = ','.join(syms)
    url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={yf_symbols}&fields=regularMarketPrice,regularMarketChangePercent,regularMarketPreviousClose"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=15, headers=headers) as client:
            r = await client.get(url)
        if r.status_code != 200:
            raise HTTPException(502, f"Yahoo Finance error: {r.status_code}")
        ydata = r.json()
        results = {}
        for q in ydata.get("quoteResponse", {}).get("result", []):
            sym = q.get("symbol","")
            results[sym] = {
                "c":   q.get("regularMarketPrice", 0),
                "dp":  q.get("regularMarketChangePercent", 0),
                "pc":  q.get("regularMarketPreviousClose", 0),
            }
        return results
    except (httpx.ReadTimeout, httpx.ConnectTimeout):
        raise HTTPException(504, "Yahoo Finance timeout")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Yahoo Finance error: {str(e)[:100]}")

SYMBOL_MAP = {
    "SPY":  ("SPY500-BRL", "sp500"),
    "QQQ":  ("QQQ-BRL",    "nasdaq"),
    "DIA":  ("DIA-BRL",    "dow"),
    "EWZ":  ("EWZ-BRL",    "brazil"),
    "USO":  ("USO-BRL",    "oil"),
    "BVSP": ("IBOVESPA-BRL","ibov"),
}

@app.get("/api/proxy/finnhub/quote")
async def proxy_finnhub_quote(symbol: str):
    try:
        result = await _fh("/quote", {"symbol": symbol}, load_data())
        if result and result.get("c", 0) > 0:
            return result
    except HTTPException:
        pass
    return {"c": 0, "d": 0, "dp": 0, "h": 0, "l": 0, "o": 0, "pc": 0, "t": 0, "_unavailable": True}

GNEWS_TOPICS = {
    "general": "https://news.google.com/rss/search?q=mercado+financeiro+economia&hl=pt-BR&gl=BR&ceid=BR:pt-419",
    "forex":   "https://news.google.com/rss/search?q=câmbio+dólar+euro+forex&hl=pt-BR&gl=BR&ceid=BR:pt-419",
    "crypto":  "https://news.google.com/rss/search?q=bitcoin+ethereum+criptomoeda&hl=pt-BR&gl=BR&ceid=BR:pt-419",
    "merger":  "https://news.google.com/rss/search?q=fusão+aquisição+M%26A+empresa&hl=pt-BR&gl=BR&ceid=BR:pt-419",
}

@app.get("/api/proxy/finnhub/news")
async def proxy_finnhub_news(category: str = "general"):
    try:
        result = await _fh("/news", {"category": category}, load_data())
        if result and len(result) > 0:
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
                "category": category,
                "datetime": ts,
                "headline": title,
                "id": abs(hash(link)),
                "image": "",
                "related": "",
                "source": source,
                "summary": title,
                "url": link,
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

@app.get("/api/portfolio", dependencies=[Depends(verify_token)])
def get_portfolio():
    return load_data()["portfolio"]

@app.post("/api/portfolio/assets", dependencies=[Depends(verify_token)])
def add_asset(body: PortfolioAsset):
    data = load_data()
    assets = data["portfolio"]["assets"]
    assets = [a for a in assets if a["id"] != body.id]
    assets.append({
        "id":        body.id,
        "name":      body.name,
        "pair":      body.pair,
        "amount":    body.amount,
        "buy_price": body.buy_price,
        "buy_date":  body.buy_date or datetime.now().strftime("%Y-%m-%d"),
        "added_at":  datetime.now().isoformat(),
    })
    data["portfolio"]["assets"] = assets
    _grant_xp(data, "portfolio_add", 10)
    save_data(data)
    return {"ok": True}

@app.delete("/api/portfolio/assets/{asset_id}", dependencies=[Depends(verify_token)])
def remove_asset(asset_id: str):
    data = load_data()
    data["portfolio"]["assets"] = [a for a in data["portfolio"]["assets"] if a["id"] != asset_id]
    save_data(data)
    return {"ok": True}

@app.put("/api/portfolio/goal", dependencies=[Depends(verify_token)])
def update_goal(body: PortfolioGoal):
    data = load_data()
    for k, v in body.model_dump(exclude_none=True).items():
        data["portfolio"][k] = v
    save_data(data)
    return {"ok": True}

# ══════════════════════════════════════════════════════════════════════════════
#  IMPORTAÇÃO DE CARTEIRA B3 VIA CSV
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/portfolio/import-csv", dependencies=[Depends(verify_token)])
async def import_portfolio_csv(file: UploadFile = File(...)):
    """
    Importa extrato de posição da B3 em CSV.
    Colunas esperadas: Produto, Instituição, Conta, Código de Negociação,
                       Tipo, Escriturador, Quantidade, Quantidade Disponível,
                       Quantidade Indisponível, Motivo
    """
    if not file.filename.lower().endswith(".csv"):
        raise HTTPException(400, "Envie um arquivo .csv")

    content = await file.read()
    # Detecta encoding (B3 exporta em latin-1)
    for enc in ("utf-8-sig", "latin-1", "utf-8"):
        try:
            text = content.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise HTTPException(400, "Não foi possível decodificar o arquivo. Salve como UTF-8.")

    reader = csv.DictReader(io.StringIO(text), delimiter=";")

    # Normaliza nomes de colunas (remove espaços, BOM, etc.)
    def norm(s):
        return s.strip().lstrip("\ufeff").lower()

    positions = []
    errors = []
    row_num = 0
    for row in reader:
        row_num += 1
        normalized = {norm(k): v.strip() for k, v in row.items() if k}

        # Mapeamento flexível de colunas
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

        positions.append({
            "symbol": symbol,
            "quantity": quantity,
            "institution": institution,
            "type": asset_type,
        })

    if not positions:
        raise HTTPException(422, f"Nenhuma posição válida encontrada no CSV. Erros: {'; '.join(errors[:5])}")

    data = load_data()
    # Garante que a chave existe
    if "b3_positions" not in data["portfolio"]:
        data["portfolio"]["b3_positions"] = []
    data["portfolio"]["b3_positions"] = positions
    data["portfolio"]["b3_imported_at"] = datetime.now().isoformat()
    save_data(data)

    return {
        "ok": True,
        "imported": len(positions),
        "skipped": len(errors),
        "positions": positions,
        "warnings": errors[:10],
    }

@app.get("/api/portfolio/b3", dependencies=[Depends(verify_token)])
def get_b3_positions():
    data = load_data()
    portfolio = data.get("portfolio", {})
    return {
        "positions": portfolio.get("b3_positions", []),
        "imported_at": portfolio.get("b3_imported_at"),
    }

@app.delete("/api/portfolio/b3", dependencies=[Depends(verify_token)])
def clear_b3_positions():
    data = load_data()
    data["portfolio"]["b3_positions"] = []
    data["portfolio"].pop("b3_imported_at", None)
    save_data(data)
    return {"ok": True}

# ══════════════════════════════════════════════════════════════════════════════
#  SISTEMA DE NÍVEIS / XP
# ══════════════════════════════════════════════════════════════════════════════

LEVEL_THRESHOLDS = {"iniciante": 0, "intermediario": 100, "avancado": 300}
LEVEL_LABELS     = {
    "iniciante":     "Iniciante",
    "intermediario": "Intermediário",
    "avancado":      "Avançado",
}
XP_EVENTS = {
    "login":         5,
    "chat_message":  8,
    "portfolio_add": 10,
    "alert_set":     5,
    "profile_complete": 15,
}

def _grant_xp(data: dict, event: str, pts: Optional[int] = None) -> dict:
    pts = pts or XP_EVENTS.get(event, 0)
    profile = data["profile"]
    old_level = profile.get("level", "iniciante")
    profile["xp"] = profile.get("xp", 0) + pts

    new_level = old_level
    for lvl, threshold in sorted(LEVEL_THRESHOLDS.items(), key=lambda x: -x[1]):
        if profile["xp"] >= threshold:
            new_level = lvl
            break

    profile["level"] = new_level
    leveled_up = new_level != old_level
    return {"xp": profile["xp"], "level": new_level, "leveled_up": leveled_up, "level_label": LEVEL_LABELS[new_level]}

@app.get("/api/level", dependencies=[Depends(verify_token)])
def get_level():
    data = load_data()
    p = data["profile"]
    xp = p.get("xp", 0)
    level = p.get("level", "iniciante")
    next_thresholds = {k: v for k, v in LEVEL_THRESHOLDS.items() if v > LEVEL_THRESHOLDS.get(level, 0)}
    next_xp = min(next_thresholds.values()) if next_thresholds else None
    return {
        "xp": xp,
        "level": level,
        "level_label": LEVEL_LABELS.get(level, "Iniciante"),
        "next_level_xp": next_xp,
        "progress_pct": int(min(100, (xp / next_xp * 100))) if next_xp else 100,
    }

@app.post("/api/level/event", dependencies=[Depends(verify_token)])
def register_event(event: str):
    data = load_data()
    result = _grant_xp(data, event)
    save_data(data)
    return result

# ══════════════════════════════════════════════════════════════════════════════
#  JARVIS — IA PROATIVA
# ══════════════════════════════════════════════════════════════════════════════

class JarvisIn(BaseModel):
    market_context: str
    portfolio_value: Optional[float] = None
    last_insight_at: Optional[str] = None

@app.post("/api/jarvis/insight", dependencies=[Depends(verify_token)])
async def jarvis_insight(body: JarvisIn):
    data = load_data()
    profile = data["profile"]
    portfolio = data["portfolio"]
    or_key = OR_KEY or data.get("api_keys", {}).get("openrouter", "")
    if not or_key:
        raise HTTPException(503, "Chave OpenRouter não configurada.")

    name     = profile.get("display_name", "investidor")
    inv_type = profile.get("investor_type", "moderado")
    level    = profile.get("level", "iniciante")
    assets   = portfolio.get("assets", [])
    goal     = portfolio.get("goal_label", "")

    portfolio_str = ""
    if assets:
        lines = [f"- {a['name']}: {a['amount']} unidades (comprado a R${a['buy_price']:.2f})" for a in assets]
        portfolio_str = "Portfólio do usuário:\n" + "\n".join(lines)
    else:
        portfolio_str = "Usuário ainda não cadastrou portfólio."

    prompt = f"""Você é o EconRadar, assessor financeiro pessoal de {name} (perfil {inv_type}, nível {level}).

{portfolio_str}
Meta financeira: {goal or 'não definida'}
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

    raw = raw.replace("```json","").replace("```","").strip()
    match = re.search(r'\{[\s\S]*\}', raw)
    if not match:
        return {"insight": "", "type": "none"}

    result = json.loads(match.group(0))

    if result.get("insight"):
        data["jarvis_insights"].append({
            **result,
            "generated_at": datetime.now().isoformat()
        })
        data["jarvis_insights"] = data["jarvis_insights"][-50:]
        save_data(data)

    return result

@app.get("/api/jarvis/history", dependencies=[Depends(verify_token)])
def jarvis_history():
    data = load_data()
    return list(reversed(data.get("jarvis_insights", [])[:20]))

# ══════════════════════════════════════════════════════════════════════════════
#  v1.4 — SCORE DE MOMENTO
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

@app.post("/api/score/moment", dependencies=[Depends(verify_token)])
async def score_moment(body: ScoreIn):
    data    = load_data()
    profile = data["profile"]
    or_key = OR_KEY or data.get("api_keys", {}).get("openrouter", "")
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
    match = re.search(r'\{[\s\S]*\}', raw)
    if not match:
        raise HTTPException(502, f"Formato inválido: {raw[:100]}")
    return json.loads(match.group(0))

# ══════════════════════════════════════════════════════════════════════════════
#  v1.4 — SIMULADOR DE CENÁRIOS
# ══════════════════════════════════════════════════════════════════════════════

class ScenarioIn(BaseModel):
    scenario_desc: str
    affected_assets: list[str]
    portfolio_snapshot: str

@app.post("/api/scenario/simulate", dependencies=[Depends(verify_token)])
async def simulate_scenario(body: ScenarioIn):
    data    = load_data()
    profile = data["profile"]
    or_key = OR_KEY or data.get("api_keys", {}).get("openrouter", "")
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
    match = re.search(r'\{[\s\S]*\}', raw)
    if not match:
        raise HTTPException(502, f"Formato inválido: {raw[:100]}")
    return json.loads(match.group(0))

# ══════════════════════════════════════════════════════════════════════════════
#  v1.4 — RELATÓRIO MENSAL PDF
# ══════════════════════════════════════════════════════════════════════════════

class ReportIn(BaseModel):
    portfolio_snapshot: str
    market_context:     str
    period_label:       Optional[str] = None

@app.post("/api/report/generate", dependencies=[Depends(verify_token)])
async def generate_report(body: ReportIn):
    data    = load_data()
    profile = data["profile"]
    or_key = OR_KEY or data.get("api_keys", {}).get("openrouter", "")
    if not or_key:
        raise HTTPException(503, "Chave OpenRouter não configurada.")

    name     = profile.get("display_name", "Investidor")
    inv_type = profile.get("investor_type", "moderado")
    level    = profile.get("level", "iniciante")
    goal     = data["portfolio"].get("goal_label", "")
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

    data["night_summaries"].append({
        "summary_text": result.get("resumo_executivo", ""),
        "instab_score": 0,
        "generated_at": datetime.now().isoformat(),
        "type": "monthly_report",
        "period": period,
        "full_report": result
    })
    data["night_summaries"] = data["night_summaries"][-30:]
    save_data(data)
    return result

@app.get("/api/report/history", dependencies=[Depends(verify_token)])
def report_history():
    data = load_data()
    reports = [s for s in data.get("night_summaries", []) if s.get("type") == "monthly_report"]
    return list(reversed(reports[-12:]))

# ── Health ─────────────────────────────────────────────────────────────────────
@app.get("/api/health")
def health():
    return {"status": "ok", "version": "1.0.0-foundation"}

@app.get("/api/health/keys", dependencies=[Depends(verify_token)])
async def check_keys():
    data = load_data()
    or_key = OR_KEY or data.get("api_keys", {}).get("openrouter", "")
    result = {
        "openrouter": {"configured": bool(or_key), "valid": None, "error": None},
    }
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
