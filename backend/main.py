"""
EconRadar Backend — versão simples
- Sem banco de dados
- Usuário fixo: Admin / 12345
- Dados salvos em data/data.json
- Chaves de API ficam no servidor (seguras)
"""

from dotenv import load_dotenv
load_dotenv()

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
from typing import Any

# ── Configuração ───────────────────────────────────────────────────────────────
SECRET_KEY    = os.getenv("SECRET_KEY", "econradar-secret-key-troque-isso")
ALGORITHM     = "HS256"
TOKEN_HOURS   = int(os.getenv("TOKEN_EXPIRE_H", "24"))
OR_KEY        = os.getenv("OPENROUTER_API_KEY", "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")
FH_KEY        = os.getenv("FINNHUB_API_KEY", "d87jkq1r01qmhakg2qrgd87jkq1r01qmhakg2qs0")
OR_MODEL      = os.getenv("OR_MODEL", "nvidia/nemotron-3-nano-30b-a3b:free")
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
    return {
        "access_token": create_token(),
        "token_type": "bearer",
        "display_name": data["profile"]["display_name"],
    }

@app.get("/api/auth/me", dependencies=[Depends(verify_token)])
def me():
    data = load_data()
    return {"username": data["user"]["username"], "display_name": data["profile"]["display_name"]}

# ── Perfil ─────────────────────────────────────────────────────────────────────
class ProfileIn(BaseModel):
    display_name:  str | None = None
    investor_type: str | None = None
    note:          str | None = None
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
    threshold:      float | None = None
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
    session_id: int | None = None
    message:    str
    context:    str | None = None
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
    or_key = OR_KEY
    if not or_key:
        raise HTTPException(503, "Chave OpenRouter não configurada.")

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {or_key}", "Content-Type": "application/json"},
            json={"model": OR_MODEL, "messages": [{"role":"system","content":system_prompt}] + messages,
                  "max_tokens": 1000, "temperature": 0.7}
        )

    if resp.status_code != 200:
        raise HTTPException(502, f"Erro OpenRouter: {resp.text[:200]}")

    reply = resp.json()["choices"][0]["message"]["content"]

    # Salva mensagens
    now = datetime.now().isoformat()
    session["messages"].append({"role": "user",      "content": body.message, "created_at": now})
    session["messages"].append({"role": "assistant", "content": reply,        "created_at": now})
    session["updated_at"] = now
    if session["title"] == "Nova conversa":
        session["title"] = body.message[:60] + ("…" if len(body.message) > 60 else "")

    if not profile.get("ai_used"):
        data["profile"]["ai_used"] = True

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
    or_key = OR_KEY
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

    async with httpx.AsyncClient(timeout=45) as client:
        resp = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {or_key}", "Content-Type": "application/json"},
            json={"model": OR_MODEL,
                  "messages": [
                      {"role": "system", "content": "Você é um analista financeiro. Responda APENAS com JSON válido."},
                      {"role": "user", "content": prompt}
                  ],
                  "max_tokens": 2000, "temperature": 0.3}
        )

    if resp.status_code != 200:
        raise HTTPException(502, f"Erro OpenRouter: {resp.text[:200]}")

    raw = resp.json()["choices"][0]["message"]["content"].strip()
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
    params["token"] = FH_KEY
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

# ── Health ─────────────────────────────────────────────────────────────────────
@app.get("/api/health")
def health():
    return {"status": "ok", "version": "1.0.0"}

# ── Iniciar direto com python main.py ──────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
