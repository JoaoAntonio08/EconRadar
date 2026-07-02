-- ════════════════════════════════════════════════════════════════════════════
-- EconRadar — Schema PostgreSQL
-- Multi-usuário, com planos pagos e dados sensíveis (B3 / chaves de API)
-- criptografados em repouso na camada de aplicação (ver backend/crypto_utils.py)
--
-- Rode este arquivo já conectado ao banco 'econradar' (não cria o banco
-- sozinho — isso é feito uma vez manualmente, ver DATABASE_SETUP.md).
-- ════════════════════════════════════════════════════════════════════════════

-- Função utilitária: atualiza automaticamente a coluna updated_at em qualquer
-- UPDATE, equivalente ao "ON UPDATE CURRENT_TIMESTAMP" do MySQL/MariaDB.
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = CURRENT_TIMESTAMP;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ── Usuários ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
  id                      BIGSERIAL PRIMARY KEY,
  email                   VARCHAR(255)     NOT NULL UNIQUE,
  username                VARCHAR(60)      NOT NULL UNIQUE,
  password_hash           VARCHAR(255)     NOT NULL,   -- bcrypt
  role                    VARCHAR(20)      NOT NULL DEFAULT 'user' CHECK (role IN ('user','admin')),
  is_active               SMALLINT         NOT NULL DEFAULT 1,
  email_verified          SMALLINT         NOT NULL DEFAULT 0,
  failed_login_attempts   SMALLINT         NOT NULL DEFAULT 0,
  locked_until            TIMESTAMP        NULL,
  created_at              TIMESTAMP        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at              TIMESTAMP        NOT NULL DEFAULT CURRENT_TIMESTAMP
);
DROP TRIGGER IF EXISTS trg_users_updated_at ON users;
CREATE TRIGGER trg_users_updated_at BEFORE UPDATE ON users
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ── Assinaturas / planos pagos ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS subscriptions (
  id                        BIGSERIAL PRIMARY KEY,
  user_id                   BIGINT NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
  plan                      VARCHAR(20) NOT NULL DEFAULT 'free' CHECK (plan IN ('free','pro','premium')),
  status                    VARCHAR(20) NOT NULL DEFAULT 'active' CHECK (status IN ('active','trialing','past_due','canceled','incomplete')),
  provider                  VARCHAR(40)  NULL,
  provider_customer_id      VARCHAR(120) NULL,
  provider_subscription_id  VARCHAR(120) NULL,
  current_period_start      TIMESTAMP NULL,
  current_period_end        TIMESTAMP NULL,
  cancel_at_period_end      SMALLINT NOT NULL DEFAULT 0,
  created_at                TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at                TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_sub_provider_id ON subscriptions(provider_subscription_id);
DROP TRIGGER IF EXISTS trg_subscriptions_updated_at ON subscriptions;
CREATE TRIGGER trg_subscriptions_updated_at BEFORE UPDATE ON subscriptions
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE IF NOT EXISTS subscription_events (
  id           BIGSERIAL PRIMARY KEY,
  user_id      BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  event_type   VARCHAR(60) NOT NULL,
  raw_payload  JSONB NULL,
  created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_subevt_user ON subscription_events(user_id, created_at);

-- ── Perfil ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS profiles (
  user_id            BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  display_name       VARCHAR(120) NOT NULL DEFAULT '',
  investor_type      VARCHAR(20) NOT NULL DEFAULT 'moderado' CHECK (investor_type IN ('conservador','moderado','arrojado')),
  note               VARCHAR(500) NOT NULL DEFAULT '',
  interests          JSONB NULL,
  alerts_seen        INT NOT NULL DEFAULT 0,
  ai_used            SMALLINT NOT NULL DEFAULT 0,
  level              VARCHAR(20) NOT NULL DEFAULT 'iniciante' CHECK (level IN ('iniciante','intermediario','avancado')),
  xp                 INT NOT NULL DEFAULT 0,
  experience_level   VARCHAR(20) NOT NULL DEFAULT 'beginner' CHECK (experience_level IN ('beginner','intermediate','advanced')),
  member_since       DATE NOT NULL
);

-- ── Configurações do usuário ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS user_configs (
  user_id          BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  autorefresh      SMALLINT NOT NULL DEFAULT 1,
  interval_sec     INT NOT NULL DEFAULT 60,
  currency         VARCHAR(8) NOT NULL DEFAULT 'BRL',
  threshold        NUMERIC(6,2) NOT NULL DEFAULT 2.00,
  accent_color     VARCHAR(20) NOT NULL DEFAULT '#4f8dff',
  compact_mode     SMALLINT NOT NULL DEFAULT 0,
  show_instab      SMALLINT NOT NULL DEFAULT 1,
  animations       SMALLINT NOT NULL DEFAULT 1,
  alert_strong     SMALLINT NOT NULL DEFAULT 1,
  alert_interest   SMALLINT NOT NULL DEFAULT 1,
  news_interest    SMALLINT NOT NULL DEFAULT 1,
  cache_enabled    SMALLINT NOT NULL DEFAULT 1
);

-- ── Portfólio (ativos cadastrados manualmente) ───────────────────────────────
CREATE TABLE IF NOT EXISTS portfolio_assets (
  id          BIGSERIAL PRIMARY KEY,
  user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  asset_id    VARCHAR(30)  NOT NULL,
  name        VARCHAR(120) NOT NULL,
  pair        VARCHAR(30)  NOT NULL,
  amount      NUMERIC(24,8) NOT NULL,
  buy_price   NUMERIC(18,4) NOT NULL,
  buy_date    DATE NULL,
  added_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (user_id, asset_id)
);

CREATE TABLE IF NOT EXISTS portfolio_goals (
  user_id        BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  goal_amount    NUMERIC(18,2) NULL,
  goal_label     VARCHAR(120) NULL,
  goal_deadline  DATE NULL
);

-- ── Posições B3 (importadas via CSV) — DADOS SENSÍVEIS ───────────────────────
CREATE TABLE IF NOT EXISTS b3_positions (
  id           BIGSERIAL PRIMARY KEY,
  user_id      BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  symbol       VARCHAR(20) NOT NULL,
  asset_type   VARCHAR(40) NOT NULL DEFAULT '',
  enc_payload  BYTEA NOT NULL,
  created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_b3_user ON b3_positions(user_id);

CREATE TABLE IF NOT EXISTS portfolio_b3_meta (
  user_id      BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  imported_at  TIMESTAMP NULL
);

-- ── Chaves de API de terceiros (Finnhub, OpenRouter etc.) — SEMPRE CIFRADAS ──
CREATE TABLE IF NOT EXISTS api_keys (
  id          BIGSERIAL PRIMARY KEY,
  user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  provider    VARCHAR(40) NOT NULL,
  enc_key     BYTEA NOT NULL,
  created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (user_id, provider)
);
DROP TRIGGER IF EXISTS trg_api_keys_updated_at ON api_keys;
CREATE TRIGGER trg_api_keys_updated_at BEFORE UPDATE ON api_keys
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ── Chat (Jarvis / consultor IA) ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS chat_sessions (
  id          BIGSERIAL PRIMARY KEY,
  user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  title       VARCHAR(160) NOT NULL DEFAULT 'Nova conversa',
  created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_session_user ON chat_sessions(user_id, updated_at);
DROP TRIGGER IF EXISTS trg_chat_sessions_updated_at ON chat_sessions;
CREATE TRIGGER trg_chat_sessions_updated_at BEFORE UPDATE ON chat_sessions
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE IF NOT EXISTS chat_messages (
  id          BIGSERIAL PRIMARY KEY,
  session_id  BIGINT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
  role        VARCHAR(20) NOT NULL CHECK (role IN ('user','assistant')),
  content     TEXT NOT NULL,
  created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_msg_session ON chat_messages(session_id);

-- ── Resumos noturnos / relatórios mensais ────────────────────────────────────
CREATE TABLE IF NOT EXISTS night_summaries (
  id            BIGSERIAL PRIMARY KEY,
  user_id       BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  summary_text  TEXT NOT NULL,
  instab_score  SMALLINT NOT NULL DEFAULT 0,
  type          VARCHAR(30) NOT NULL DEFAULT 'daily',
  period_label  VARCHAR(40) NULL,
  full_report   JSONB NULL,
  generated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_summary_user ON night_summaries(user_id, generated_at);

-- ── Insights proativos (Jarvis) ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS jarvis_insights (
  id            BIGSERIAL PRIMARY KEY,
  user_id       BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  insight       TEXT NOT NULL,
  type          VARCHAR(30) NOT NULL,
  asset         VARCHAR(30) NULL,
  urgency       VARCHAR(20) NULL,
  generated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_jarvis_user ON jarvis_insights(user_id, generated_at);

-- ── Refresh tokens (permite logout / revogação real de sessão) ──────────────
CREATE TABLE IF NOT EXISTS refresh_tokens (
  id          BIGSERIAL PRIMARY KEY,
  user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash  CHAR(64) NOT NULL UNIQUE,
  expires_at  TIMESTAMP NOT NULL,
  revoked     SMALLINT NOT NULL DEFAULT 0,
  user_agent  VARCHAR(255) NULL,
  ip_address  VARCHAR(45) NULL,
  created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_refresh_user ON refresh_tokens(user_id);

-- ── Reset de senha ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS password_reset_tokens (
  id          BIGSERIAL PRIMARY KEY,
  user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash  CHAR(64) NOT NULL UNIQUE,
  expires_at  TIMESTAMP NOT NULL,
  used        SMALLINT NOT NULL DEFAULT 0,
  created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ── Auditoria de eventos sensíveis (login, troca de senha, troca de plano) ──
CREATE TABLE IF NOT EXISTS audit_log (
  id          BIGSERIAL PRIMARY KEY,
  user_id     BIGINT NULL,
  event       VARCHAR(60) NOT NULL,
  ip_address  VARCHAR(45) NULL,
  meta        JSONB NULL,
  created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id, created_at);
