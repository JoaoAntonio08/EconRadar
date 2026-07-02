// api.js — cliente para o EconRadar Backend
// Inclua no HTML: <script src="api.js"></script>

const API_BASE = window.ECONRADAR_API || 'http://localhost:8000/api';

// ── Token JWT ─────────────────────────────────────────────
function getToken()          { return localStorage.getItem('er_token'); }
function setToken(t)         { localStorage.setItem('er_token', t); }
function getRefreshToken()   { return localStorage.getItem('er_refresh'); }
function setRefreshToken(t)  { localStorage.setItem('er_refresh', t); }
function clearToken()        {
  localStorage.removeItem('er_token');
  localStorage.removeItem('er_refresh');
  localStorage.removeItem('er_user');
}
function getUser()           { try { return JSON.parse(localStorage.getItem('er_user') || 'null'); } catch { return null; } }
function setUser(u)          { localStorage.setItem('er_user', JSON.stringify(u)); }
function isLoggedIn()        { return !!getToken(); }

// ── Fetch autenticado (com refresh automático em 401) ──────
let _refreshInFlight = null;

async function _doRefresh() {
  const refreshToken = getRefreshToken();
  if (!refreshToken) return false;
  try {
    const res = await fetch(API_BASE + '/auth/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: refreshToken }),
    });
    if (!res.ok) return false;
    const data = await res.json();
    setToken(data.access_token);
    setRefreshToken(data.refresh_token);
    return true;
  } catch {
    return false;
  }
}

async function apiFetch(path, options = {}, _isRetry = false) {
  const token = getToken();
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
  if (token) headers['Authorization'] = `Bearer ${token}`;

  const res = await fetch(API_BASE + path, { ...options, headers });

  if (res.status === 401) {
    // O access token dura pouco (30 min) de propósito — tenta renovar
    // silenciosamente com o refresh token antes de deslogar o usuário.
    if (!_isRetry && getRefreshToken()) {
      if (!_refreshInFlight) _refreshInFlight = _doRefresh().finally(() => { _refreshInFlight = null; });
      const refreshed = await _refreshInFlight;
      if (refreshed) return apiFetch(path, options, true);
    }
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
  async _afterAuth(data) {
    setToken(data.access_token);
    setRefreshToken(data.refresh_token);
    // login/register não devolvem o perfil completo — busca em seguida
    try {
      const me = await apiFetch('/auth/me');
      setUser(me);
      return me;
    } catch {
      setUser({ display_name: data.display_name });
      return { display_name: data.display_name };
    }
  },
  async login(identifier, password) {
    const data = await apiFetch('/auth/login', { method: 'POST', body: JSON.stringify({ username: identifier, password }) });
    return this._afterAuth(data);
  },
  async register(email, username, password, displayName) {
    const data = await apiFetch('/auth/register', {
      method: 'POST',
      body: JSON.stringify({ email, username, password, display_name: displayName || username }),
    });
    return this._afterAuth(data);
  },
  async logout() {
    const refreshToken = getRefreshToken();
    if (refreshToken) {
      try { await fetch(API_BASE + '/auth/logout', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: refreshToken }),
      }); } catch {}
    }
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
    <div style="font-size:13px;color:var(--text3,#888);margin-bottom:20px">Acesse sua conta ou crie uma gratuitamente</div>

    <div style="display:flex;gap:4px;margin-bottom:20px;background:var(--bg2,#252836);border-radius:10px;padding:4px">
      <div id="er-tab-login" data-active="1" onclick="erTab('login')" style="flex:1;text-align:center;padding:8px;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;background:var(--accent,#4f8dff);color:#fff">Entrar</div>
      <div id="er-tab-register" onclick="erTab('register')" style="flex:1;text-align:center;padding:8px;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;color:var(--text2,#aaa)">Criar conta</div>
    </div>

    <div id="er-register-fields" style="display:none">
      <div style="margin-bottom:12px">
        <input id="er-inp-name" placeholder="Nome" style="width:100%;box-sizing:border-box;padding:10px 12px;border-radius:8px;border:1px solid var(--border,#2a2d3e);background:var(--bg2,#252836);color:var(--text1,#fff);font-size:13px"/>
      </div>
      <div style="margin-bottom:12px">
        <input id="er-inp-email" type="email" placeholder="E-mail" style="width:100%;box-sizing:border-box;padding:10px 12px;border-radius:8px;border:1px solid var(--border,#2a2d3e);background:var(--bg2,#252836);color:var(--text1,#fff);font-size:13px"/>
      </div>
    </div>

    <div style="margin-bottom:12px">
      <input id="er-inp-username" placeholder="Usuário" style="width:100%;box-sizing:border-box;padding:10px 12px;border-radius:8px;border:1px solid var(--border,#2a2d3e);background:var(--bg2,#252836);color:var(--text1,#fff);font-size:13px"/>
      <div id="er-username-hint" style="display:none;font-size:11px;color:var(--text3,#888);margin-top:4px">Login também pode ser feito com o e-mail.</div>
    </div>
    <div style="margin-bottom:8px">
      <input id="er-inp-pass" type="password" placeholder="Senha" style="width:100%;box-sizing:border-box;padding:10px 12px;border-radius:8px;border:1px solid var(--border,#2a2d3e);background:var(--bg2,#252836);color:var(--text1,#fff);font-size:13px"/>
    </div>
    <div id="er-pass-hint" style="display:none;font-size:11px;color:var(--text3,#888);margin-bottom:12px">Mínimo de 8 caracteres.</div>

    <div id="er-auth-err" style="display:none;color:#ff5063;font-size:12px;margin-bottom:12px"></div>

    <button id="er-submit-btn" onclick="erSubmitAuth()" style="width:100%;padding:11px;border-radius:10px;border:none;background:linear-gradient(135deg,var(--accent,#4f8dff),#7c5cfc);color:#fff;font-size:14px;font-weight:600;cursor:pointer">Entrar</button>
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
  document.getElementById('er-register-fields').style.display = isLogin ? 'none' : '';
  document.getElementById('er-username-hint').style.display   = isLogin ? '' : 'none';
  document.getElementById('er-pass-hint').style.display       = isLogin ? 'none' : '';
  document.getElementById('er-inp-username').placeholder      = isLogin ? 'Usuário ou e-mail' : 'Usuário';
  document.getElementById('er-submit-btn').textContent        = isLogin ? 'Entrar' : 'Criar conta grátis';
  document.getElementById('er-tab-login').style.background    = isLogin ? 'var(--accent,#4f8dff)' : 'transparent';
  document.getElementById('er-tab-login').style.color         = isLogin ? '#fff' : 'var(--text2,#aaa)';
  document.getElementById('er-tab-register').style.background = isLogin ? 'transparent' : 'var(--accent,#4f8dff)';
  document.getElementById('er-tab-register').style.color      = isLogin ? 'var(--text2,#aaa)' : '#fff';
  document.getElementById('er-tab-login').dataset.active    = isLogin ? '1' : '';
  document.getElementById('er-tab-register').dataset.active = isLogin ? '' : '1';
  document.getElementById('er-auth-err').style.display = 'none';
};

window.erSubmitAuth = async function() {
  const isLogin  = document.getElementById('er-tab-login').dataset.active === '1';
  const username = document.getElementById('er-inp-username').value.trim();
  const password = document.getElementById('er-inp-pass').value;
  const errEl    = document.getElementById('er-auth-err');
  errEl.style.display = 'none';

  try {
    if (isLogin) {
      if (!username || !password) throw new Error('Preencha usuário/e-mail e senha.');
      await Auth.login(username, password);
    } else {
      const name  = document.getElementById('er-inp-name').value.trim();
      const email = document.getElementById('er-inp-email').value.trim();
      if (!name || !email || !username || !password) throw new Error('Preencha todos os campos.');
      if (password.length < 8) throw new Error('A senha deve ter ao menos 8 caracteres.');
      await Auth.register(email, username, password, name);
    }
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

// ── Score de Momento (v1.4) ───────────────────────────────
window.Score = {
  async moment(asset) {
    return apiFetch('/score/moment', { method:'POST', body: JSON.stringify(asset) });
  }
};

// ── Simulador de Cenários (v1.4) ──────────────────────────
window.Scenario = {
  async simulate(desc, affectedAssets, portfolioSnapshot) {
    return apiFetch('/scenario/simulate', {
      method: 'POST',
      body: JSON.stringify({
        scenario_desc:      desc,
        affected_assets:    affectedAssets,
        portfolio_snapshot: portfolioSnapshot,
      })
    });
  }
};

// ── Relatório Mensal (v1.4) ───────────────────────────────
window.Report = {
  async generate(portfolioSnapshot, marketContext, periodLabel) {
    return apiFetch('/report/generate', {
      method: 'POST',
      body: JSON.stringify({
        portfolio_snapshot: portfolioSnapshot,
        market_context:     marketContext,
        period_label:       periodLabel || null,
      })
    });
  },
  async history() { return apiFetch('/report/history'); }
};
