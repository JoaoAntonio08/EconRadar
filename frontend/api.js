// api.js — cliente para o EconRadar Backend
// Inclua no HTML: <script src="api.js"></script>

const API_BASE = window.ECONRADAR_API || 'http://localhost:8000/api';

// ── Token JWT ─────────────────────────────────────────────
function getToken()        { return localStorage.getItem('er_token'); }
function setToken(t)       { localStorage.setItem('er_token', t); }
function clearToken()      { localStorage.removeItem('er_token'); localStorage.removeItem('er_user'); }
function getUser()         { try { return JSON.parse(localStorage.getItem('er_user') || 'null'); } catch { return null; } }
function setUser(u)        { localStorage.setItem('er_user', JSON.stringify(u)); }
function isLoggedIn()      { return !!getToken(); }

// ── Fetch autenticado ─────────────────────────────────────
async function apiFetch(path, options = {}) {
  const token = getToken();
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
  if (token) headers['Authorization'] = `Bearer ${token}`;

  const res = await fetch(API_BASE + path, { ...options, headers });

  if (res.status === 401) {
    clearToken();
    window.location.href = '/';
    throw new Error('Sessão expirada. Faça login novamente.');
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || `Erro ${res.status}`);
  }
  return res.json();
}

// ── Auth ──────────────────────────────────────────────────
const Auth = {
  async login(username, password) {
    const data = await apiFetch('/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) });
    setToken(data.access_token);
    setUser({ id: data.user_id, email: data.email, display_name: data.display_name });
    return data;
  },
  logout() {
    clearToken();
    window.location.href = '/';
  }
};

// ── Perfil ────────────────────────────────────────────────
const Profile = {
  get()         { return apiFetch('/users/profile'); },
  update(data)  { return apiFetch('/users/profile', { method: 'PUT', body: JSON.stringify(data) }); },
};

// ── Config ────────────────────────────────────────────────
const Config = {
  get()         { return apiFetch('/users/config'); },
  update(data)  { return apiFetch('/users/config', { method: 'PUT', body: JSON.stringify(data) }); },
};

// ── Chat ──────────────────────────────────────────────────
const Chat = {
  listSessions()              { return apiFetch('/chat/sessions'); },
  createSession()             { return apiFetch('/chat/sessions', { method: 'POST' }); },
  deleteSession(id)           { return apiFetch(`/chat/sessions/${id}`, { method: 'DELETE' }); },
  getMessages(sessionId)      { return apiFetch(`/chat/sessions/${sessionId}/messages`); },
  send(message, sessionId, context, history) {
    return apiFetch('/chat/send', { method: 'POST', body: JSON.stringify({ message, session_id: sessionId, context, history }) });
  },
};

// ── Resumo noturno ────────────────────────────────────────
const Summary = {
  generate(context, instab_score, crit_count, assets_json) {
    return apiFetch('/summary/generate', { method: 'POST', body: JSON.stringify({ context, instab_score, crit_count, assets_json }) });
  },
  history() { return apiFetch('/summary/history'); },
};

// ── Market (proxy Finnhub) ────────────────────────────────
const Market = {
  quote(symbol)         { return apiFetch(`/market/quote?symbol=${symbol}`); },
  forex(base = 'USD')   { return apiFetch(`/market/forex?base=${base}`); },
  news(category = 'general') { return apiFetch(`/market/news?category=${category}`); },
};

// ── Modal de autenticação ─────────────────────────────────
function buildAuthModal() {
  if (document.getElementById('er-auth-modal')) return;
  const el = document.createElement('div');
  el.id = 'er-auth-modal';
  el.innerHTML = `
<div id="er-auth-backdrop" style="position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:9999;display:flex;align-items:center;justify-content:center">
  <div style="background:var(--bg1,#1a1d2e);border:1px solid var(--border,#2a2d3e);border-radius:16px;padding:32px;width:340px;font-family:var(--font-b,'sans-serif')">
    <div style="font-size:22px;font-weight:700;color:var(--text1,#fff);margin-bottom:4px">EconRadar</div>
    <div style="font-size:13px;color:var(--text3,#888);margin-bottom:24px">Faça login para continuar</div>

    <div style="margin-bottom:12px">
      <input id="er-inp-email" placeholder="Usuário" style="width:100%;box-sizing:border-box;padding:10px 12px;border-radius:8px;border:1px solid var(--border,#2a2d3e);background:var(--bg2,#252836);color:var(--text1,#fff);font-size:13px"/>
    </div>
    <div style="margin-bottom:20px">
      <input id="er-inp-pass" type="password" placeholder="Senha" style="width:100%;box-sizing:border-box;padding:10px 12px;border-radius:8px;border:1px solid var(--border,#2a2d3e);background:var(--bg2,#252836);color:var(--text1,#fff);font-size:13px"/>
    </div>

    <div id="er-auth-err" style="display:none;color:#ff5063;font-size:12px;margin-bottom:12px"></div>

    <button onclick="erSubmitAuth()" style="width:100%;padding:11px;border-radius:10px;border:none;background:linear-gradient(135deg,var(--accent,#4f8dff),#7c5cfc);color:#fff;font-size:14px;font-weight:600;cursor:pointer">Entrar</button>
  </div>
</div>`;
  document.body.appendChild(el);
}

function showAuthModal() {
  buildAuthModal();
  document.getElementById('er-auth-modal').style.display = '';
}

function hideAuthModal() {
  const el = document.getElementById('er-auth-modal');
  if (el) el.style.display = 'none';
}

window.erTab = function(tab) {
  const isLogin = tab === 'login';
  document.getElementById('er-register-name').style.display = isLogin ? 'none' : '';
  document.getElementById('er-tab-login').style.background    = isLogin ? 'var(--accent,#4f8dff)' : 'transparent';
  document.getElementById('er-tab-login').style.color         = isLogin ? '#fff' : 'var(--text2,#aaa)';
  document.getElementById('er-tab-register').style.background = isLogin ? 'transparent' : 'var(--accent,#4f8dff)';
  document.getElementById('er-tab-register').style.color      = isLogin ? 'var(--text2,#aaa)' : '#fff';
  document.getElementById('er-tab-login').dataset.active    = isLogin ? '1' : '';
  document.getElementById('er-tab-register').dataset.active = isLogin ? '' : '1';
};

window.erSubmitAuth = async function() {
  const username = document.getElementById('er-inp-email').value.trim();
  const password = document.getElementById('er-inp-pass').value;
  const errEl    = document.getElementById('er-auth-err');
  errEl.style.display = 'none';
  try {
    await Auth.login(username, password);
    hideAuthModal();
    window.dispatchEvent(new CustomEvent('er:loggedin'));
  } catch(e) {
    errEl.textContent = e.message;
    errEl.style.display = '';
  }
};

// ── Init ──────────────────────────────────────────────────
// Ao carregar a página, verifica se há sessão ativa
document.addEventListener('DOMContentLoaded', () => {
  if (!isLoggedIn()) {
    if (window.location.pathname !== '/' && !window.location.pathname.includes('landing')) {
      window.location.href = '/';
    }
  } else {
    window.dispatchEvent(new CustomEvent('er:loggedin'));
  }
});

// ── Portfolio ─────────────────────────────────────────────
window.Portfolio = {
  async get()               { return apiFetch('/portfolio'); },
  async addAsset(asset)     { return apiFetch('/portfolio/assets', { method:'POST', body: JSON.stringify(asset) }); },
  async removeAsset(id)     { return apiFetch(`/portfolio/assets/${id}`, { method:'DELETE' }); },
  async setGoal(goal)       { return apiFetch('/portfolio/goal', { method:'PUT', body: JSON.stringify(goal) }); },
};

// ── Level / XP ────────────────────────────────────────────
window.Level = {
  async get()               { return apiFetch('/level'); },
  async event(name)         { return apiFetch(`/level/event?event=${name}`, { method:'POST' }); },
};

// ── Jarvis (IA proativa) ──────────────────────────────────
window.Jarvis = {
  async insight(marketCtx, portfolioValue) {
    return apiFetch('/jarvis/insight', {
      method: 'POST',
      body: JSON.stringify({ market_context: marketCtx, portfolio_value: portfolioValue })
    });
  },
  async history() { return apiFetch('/jarvis/history'); },
};
