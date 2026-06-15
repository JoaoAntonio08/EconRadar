"""
EconRadar Backend — versão simples
- Sem banco de dados
- Usuário fixo: Admin / 12345
- Dados salvos em data/data.json
- Chaves de API ficam no servidor (seguras)
"""

from dotenv import load_dotenv
from pathlib import Path

# Procura .env em backend/ e na raiz do projeto
_env_paths = [
    Path(__file__).parent / ".env",          # backend/.env
    Path(__file__).parent.parent / ".env",   # raiz/.env
]
for _p in _env_paths:
    if _p.exists():
        load_dotenv(_p)
        break
else:
    load_dotenv()  # fallback padrão

from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from jose import JWTError, jwt
from datetime import datetime, timedelta, timezone
import httpx, json, os, re, xml.etree.ElementTree as _ET, bcrypt as _bcrypt
from pathlib import Path

def _check_password(plain: str, hashed: str) -> bool:
    return _bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
from typing import Any, Optional

# ── Configuração ───────────────────────────────────────────────────────────────
SECRET_KEY    = os.getenv("SECRET_KEY", "")
ALGORITHM     = "HS256"
TOKEN_HOURS   = int(os.getenv("TOKEN_EXPIRE_H", "24"))
OR_KEY        = os.getenv("OPENROUTER_API_KEY", "")
FH_KEY        = os.getenv("FINNHUB_API_KEY", "")
OR_MODEL      = os.getenv("OR_MODEL", "google/gemma-4-31b-it:free")
OR_MODEL_FALLBACK = os.getenv("OR_MODEL_FALLBACK", "nvidia/nemotron-3-ultra-550b-a55b:free")
ALLOWED_ENV    = os.getenv("ALLOWED_ORIGINS", "").strip()
# Origens permitidas: localhost padrão + qualquer domínio ngrok + o que vier do .env
_default_origins = [
    "http://localhost:8000", "http://127.0.0.1:8000",
    "http://localhost:5500", "http://127.0.0.1:5500",
    "http://localhost:3000", "http://127.0.0.1:3000",
]
ALLOWED = _default_origins + ([o.strip() for o in ALLOWED_ENV.split(",") if o.strip()] if ALLOWED_ENV else [])
DATA_FILE     = Path(__file__).parent / "data" / "data.json"

# ── JSON Storage ───────────────────────────────────────────────────────────────
def load_data() -> dict:
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_data(data: dict):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)

# ── OpenRouter helper com fallback ────────────────────────────────────────────
def _extract_or_content(resp_json: dict) -> str:
    """Extrai texto da resposta OpenRouter de forma robusta."""
    try:
        choices = resp_json.get("choices") or []
        if not choices:
            return ""
        msg = choices[0].get("message") or {}
        # content pode ser string, None, ou lista (tool_use)
        content = msg.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            # pega blocos de texto dentro da lista
            parts = [b.get("text","") for b in content if isinstance(b, dict) and b.get("type") == "text"]
            return " ".join(parts).strip()
    except Exception:
        pass
    return ""

async def _or_call(or_key: str, messages: list, max_tokens: int = 1000,
                   temperature: float = 0.5, timeout: int = 45) -> str:
    """Chama OpenRouter com fallback automático se o modelo primário retornar vazio."""
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
                    # Chave inválida — não adianta tentar outros modelos com a mesma chave
                    raise HTTPException(401, "Chave OpenRouter inválida ou expirada. Gere uma nova em openrouter.ai/keys e atualize o .env ou Configurações.")
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED,
    allow_origin_regex=r"https://.*\.ngrok(-free)?\.app|https://.*\.ngrok\.io|https://.*\.ngrok-free\.dev",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Arquivos estáticos (frontend) ─────────────────────────────────────────────
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
def login(body: LoginIn):
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
    display_name:  Optional[str] = None
    investor_type: Optional[str] = None
    note:          Optional[str] = None
    interests:     list[str] | None = None

@app.get("/api/users/profile", dependencies=[Depends(verify_token)])
def get_profile():
    return load_data()["profile"]

@app.put("/api/users/profile", dependencies=[Depends(verify_token)])
def update_profile(body: ProfileIn):
    data = load_data()
    for k, v in body.model_dump(exclude_none=True).items():
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

# ── API Keys (salvas no servidor, nunca expostas) ──────────────────────────────
class ApiKeyIn(BaseModel):
    provider: str
    key:      str

@app.put("/api/users/apikeys", dependencies=[Depends(verify_token)])
def upsert_apikey(body: ApiKeyIn):
    data = load_data()
    data["api_keys"][body.provider] = body.key   # salvo no servidor, nunca retornado
    save_data(data)
    return {"ok": True}

@app.get("/api/users/apikeys/{provider}/exists", dependencies=[Depends(verify_token)])
def apikey_exists(provider: str):
    return {"exists": bool(load_data()["api_keys"].get(provider))}

# ── Chat (proxy OpenRouter) ────────────────────────────────────────────────────
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

@app.post("/api/chat/send", dependencies=[Depends(verify_token)])
async def send_message(body: SendIn):
    data = load_data()
    profile = data["profile"]

    # Busca ou cria sessão
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

    system_prompt = f"""Você é o EconRadar, consultor de mercados financeiros para {name}.
Perfil do investidor: {inv}. Foco: {ints or 'não definido'}.{f' Nota: "{note}"' if note else ''}
{f'Contexto de mercado: {body.context}' if body.context else ''}
Responda em português, seja direto e analítico. Máximo 4 parágrafos."""

    messages = (body.history or []) + [{"role": "user", "content": body.message}]

    # Usa chave do usuário salva ou a do .env
    or_key = OR_KEY or load_data().get("api_keys", {}).get("openrouter", "")
    if not or_key:
        raise HTTPException(503, "Chave OpenRouter não configurada. Verifique o .env ou Configurações.")

    reply = await _or_call(
        or_key,
        messages=[{"role": "system", "content": system_prompt}] + messages,
        max_tokens=1000, temperature=0.7, timeout=20
    )

    # Salva mensagens
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

@app.post("/api/summary/generate", dependencies=[Depends(verify_token)])
async def generate_summary(body: SummaryIn):
    data = load_data()
    or_key = OR_KEY or load_data().get("api_keys", {}).get("openrouter", "")
    if not or_key:
        raise HTTPException(503, "Chave OpenRouter não configurada. Verifique o .env ou Configurações.")

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

    # Salva histórico
    data["night_summaries"].append({
        "summary_text": parsed.get("resumo",""),
        "instab_score": body.instab_score,
        "generated_at": datetime.now().isoformat()
    })
    data["night_summaries"] = data["night_summaries"][-30:]  # guarda os últimos 30
    save_data(data)
    return parsed

@app.get("/api/summary/history", dependencies=[Depends(verify_token)])
def summary_history():
    data = load_data()
    return list(reversed(data.get("night_summaries", [])))

# ── Market Proxy (Finnhub) ─────────────────────────────────────────────────────
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
async def market_quote(symbol: str):
    return await _fh("/quote", {"symbol": symbol}, load_data())

@app.get("/api/market/forex")
async def market_forex(base: str = "USD"):
    return await _fh("/forex/rates", {"base": base}, load_data())

@app.get("/api/market/news")
async def market_news(category: str = "general"):
    return await _fh("/news", {"category": category}, load_data())


# ── Proxy AwesomeAPI (forex/metais/ibov — CORS *) ─────────────────────────────
@app.get("/api/proxy/awesome")
async def proxy_awesome(pairs: str):
    """Proxy para economia.awesomeapi.com.br — evita CORS no frontend."""
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(f"https://economia.awesomeapi.com.br/json/last/{pairs}")
    except (httpx.ReadTimeout, httpx.ConnectTimeout):
        raise HTTPException(504, "AwesomeAPI timeout")
    if r.status_code != 200:
        raise HTTPException(502, f"AwesomeAPI error: {r.status_code}")
    return r.json()

# ── Proxy CoinGecko ────────────────────────────────────────────────────────────
@app.get("/api/proxy/coingecko")
async def proxy_coingecko(ids: str, vs_currencies: str = "usd,brl", include_24hr_change: str = "true"):
    """Proxy para api.coingecko.com — evita CORS e rate-limit no frontend."""
    params = {"ids": ids, "vs_currencies": vs_currencies, "include_24hr_change": include_24hr_change}
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get("https://api.coingecko.com/api/v3/simple/price", params=params)
    if r.status_code != 200:
        raise HTTPException(502, f"CoinGecko error: {r.status_code}")
    return r.json()

# ── Proxy Yahoo Finance (stocks + indices + commodities) ────────────────────────
@app.get("/api/proxy/yahoo")
async def proxy_yahoo(symbols: str):
    """Busca cotações via Yahoo Finance — suporta SPY, QQQ, DIA, ^BVSP, CL=F (WTI)"""
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
        data = r.json()
        results = {}
        for q in data.get("quoteResponse", {}).get("result", []):
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

# ── Proxy Finnhub quote ────────────────────────────────────────────────────────
# Finnhub free tier blocks /quote for most symbols — returns empty or 403.
# We map common symbols to AwesomeAPI pairs which work reliably.
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
    # Try Finnhub first; fall back gracefully on any error
    try:
        result = await _fh("/quote", {"symbol": symbol}, load_data())
        if result and result.get("c", 0) > 0:
            return result
    except HTTPException:
        pass
    # Return a neutral placeholder so frontend doesn't crash
    return {"c": 0, "d": 0, "dp": 0, "h": 0, "l": 0, "o": 0, "pc": 0, "t": 0, "_unavailable": True}

# ── Proxy Finnhub news (Google Finance RSS fallback) ─────────────────────────
GNEWS_TOPICS = {
    "general": "https://news.google.com/rss/search?q=mercado+financeiro+economia&hl=pt-BR&gl=BR&ceid=BR:pt-419",
    "forex":   "https://news.google.com/rss/search?q=câmbio+dólar+euro+forex&hl=pt-BR&gl=BR&ceid=BR:pt-419",
    "crypto":  "https://news.google.com/rss/search?q=bitcoin+ethereum+criptomoeda&hl=pt-BR&gl=BR&ceid=BR:pt-419",
    "merger":  "https://news.google.com/rss/search?q=fusão+aquisição+M%26A+empresa&hl=pt-BR&gl=BR&ceid=BR:pt-419",
}

@app.get("/api/proxy/finnhub/news")
async def proxy_finnhub_news(category: str = "general"):
    # Try Finnhub first
    try:
        result = await _fh("/news", {"category": category}, load_data())
        if result and len(result) > 0:
            return result
    except HTTPException:
        pass

    # Fallback: Google Finance RSS → same shape as Finnhub news items
    url = GNEWS_TOPICS.get(category, GNEWS_TOPICS["general"])
    try:
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "Mozilla/5.0"}) as client:
            r = await client.get(url)
        if r.status_code != 200:
            return []
        root = _ET.fromstring(r.text)
        ns = {"media": "http://search.yahoo.com/mrss/"}
        items = []
        import time
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
    except Exception as e:
        return []

# ══════════════════════════════════════════════════════════════════════════════
#  PORTFÓLIO PESSOAL (v1.1)
# ══════════════════════════════════════════════════════════════════════════════

class PortfolioAsset(BaseModel):
    id:        str           # ex: "btc", "usd"
    name:      str
    pair:      str
    amount:    float         # quantidade (BTC, USD, etc.)
    buy_price: float         # preço pago por unidade em BRL
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
    # substitui se já existir mesmo id
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
#  SISTEMA DE NÍVEIS / XP (v1.1)
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
    """Adiciona XP e atualiza nível. Retorna {xp, level, leveled_up}."""
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
    # XP necessário para o próximo nível
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
#  JARVIS — IA PROATIVA (v1.2)
# ══════════════════════════════════════════════════════════════════════════════

class JarvisIn(BaseModel):
    market_context: str        # JSON string com cotações atuais
    portfolio_value: Optional[float] = None
    last_insight_at: Optional[str] = None

@app.post("/api/jarvis/insight", dependencies=[Depends(verify_token)])
async def jarvis_insight(body: JarvisIn):
    """
    Gera um insight proativo baseado no portfólio + mercado + perfil.
    A IA decide SE há algo relevante — pode retornar insight vazio.
    """
    data = load_data()
    profile = data["profile"]
    portfolio = data["portfolio"]
    or_key = OR_KEY or load_data().get("api_keys", {}).get("openrouter", "")
    if not or_key:
        raise HTTPException(503, "Chave OpenRouter não configurada. Verifique o .env ou Configurações.")

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

    # Salva histórico de insights não-vazios
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

# ── Health ─────────────────────────────────────────────────────────────────────
@app.get("/api/health")
def health():
    return {"status": "ok", "version": "1.1.0"}

@app.get("/api/health/keys", dependencies=[Depends(verify_token)])
async def check_keys():
    """Verifica se as chaves de API estão configuradas e válidas."""
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
                    json={"model": OR_MODEL,
                          "messages": [{"role": "user", "content": "ok"}],
                          "max_tokens": 1}
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
    """
    Retorna um Score de Momento 0–100 para o ativo,
    com breakdown de fatores e recomendação curta.
    """
    data    = load_data()
    profile = data["profile"]
    or_key = OR_KEY or load_data().get("api_keys", {}).get("openrouter", "")
    if not or_key:
        raise HTTPException(503, "Chave OpenRouter não configurada. Verifique o .env ou Configurações.")

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
    scenario_desc: str          # ex: "dólar sobe para R$7,00"
    affected_assets: list[str]  # ids dos ativos afetados
    portfolio_snapshot: str     # JSON string do portfólio atual

@app.post("/api/scenario/simulate", dependencies=[Depends(verify_token)])
async def simulate_scenario(body: ScenarioIn):
    """Simula impacto de um cenário hipotético no portfólio."""
    data    = load_data()
    profile = data["profile"]
    or_key = OR_KEY or load_data().get("api_keys", {}).get("openrouter", "")
    if not or_key:
        raise HTTPException(503, "Chave OpenRouter não configurada. Verifique o .env ou Configurações.")

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
    portfolio_snapshot: str     # JSON string do portfólio com P&L
    market_context:     str     # resumo do mercado
    period_label:       Optional[str] = None  # ex: "Maio 2025"

@app.post("/api/report/generate", dependencies=[Depends(verify_token)])
async def generate_report(body: ReportIn):
    """Gera relatório mensal em JSON (frontend converte para PDF via jsPDF)."""
    data    = load_data()
    profile = data["profile"]
    or_key = OR_KEY or load_data().get("api_keys", {}).get("openrouter", "")
    if not or_key:
        raise HTTPException(503, "Chave OpenRouter não configurada. Verifique o .env ou Configurações.")

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

    # Salva histórico
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

# ── Iniciar direto com python main.py ──────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
