"""
bootstrap.py — Auto-setup executado ANTES do backend subir.

Objetivo: manter a simplicidade de antes (só preencher o .env e rodar
iniciar.bat) mesmo agora que existe um banco de verdade (PostgreSQL) por
trás.

O que ele faz, na ordem:
  1. Garante que backend/.env existe e tem SECRET_KEY e ENCRYPTION_KEY —
     gera e GRAVA no .env automaticamente se estiverem faltando (só essas
     duas; a senha do banco tem que ser preenchida por você mesmo).
  2. Tenta conectar no PostgreSQL com as credenciais do .env.
  3. Se a tabela `users` ainda não existir, aplica db/schema.sql sozinho
     (não precisa rodar `psql -f schema.sql` na mão).
  4. Em qualquer falha, imprime uma mensagem curta e clara em português
     (não um traceback do Python) e encerra com código de erro, para o
     iniciar.bat conseguir manter a janela aberta mostrando o motivo.
"""
from __future__ import annotations

import os
import re
import secrets
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).parent
ENV_PATH = BACKEND_DIR / ".env"
SCHEMA_PATH = BACKEND_DIR / "db" / "schema.sql"


def _fail(msg: str) -> "NoReturn":
    print("\n" + "=" * 60)
    print("  ERRO NA INICIALIZAÇÃO DO ECONRADAR")
    print("=" * 60)
    print(msg)
    print("=" * 60 + "\n")
    sys.exit(1)


def _ensure_env_file() -> None:
    if not ENV_PATH.exists():
        example = BACKEND_DIR / ".env.example"
        if example.exists():
            ENV_PATH.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
            print("[setup] backend/.env não existia — criei uma cópia de .env.example.")
        else:
            ENV_PATH.write_text("", encoding="utf-8")


def _get_env_value(text: str, key: str) -> str:
    m = re.search(rf"^{key}=(.*)$", text, re.MULTILINE)
    return m.group(1).strip() if m else ""


def _set_env_value(text: str, key: str, value: str) -> str:
    if re.search(rf"^{key}=.*$", text, re.MULTILINE):
        return re.sub(rf"^{key}=.*$", f"{key}={value}", text, flags=re.MULTILINE)
    sep = "\n" if text and not text.endswith("\n") else ""
    return text + sep + f"{key}={value}\n"


def _auto_generate_missing_keys() -> None:
    """Gera SECRET_KEY e ENCRYPTION_KEY automaticamente se estiverem vazios,
    e grava de volta no .env — assim você só precisa preencher a senha do banco."""
    text = ENV_PATH.read_text(encoding="utf-8")
    changed = False

    if not _get_env_value(text, "SECRET_KEY"):
        text = _set_env_value(text, "SECRET_KEY", secrets.token_urlsafe(48))
        changed = True
        print("[setup] SECRET_KEY não estava definida — gerei uma nova automaticamente.")

    if not _get_env_value(text, "ENCRYPTION_KEY"):
        from cryptography.fernet import Fernet
        text = _set_env_value(text, "ENCRYPTION_KEY", Fernet.generate_key().decode())
        changed = True
        print("[setup] ENCRYPTION_KEY não estava definida — gerei uma nova automaticamente.")
        print("        (guarde uma cópia do .env em local seguro: perder essa chave")
        print("         torna irrecuperáveis as chaves de API e posições B3 já salvas)")

    if changed:
        ENV_PATH.write_text(text, encoding="utf-8")


def _check_db_password() -> None:
    text = ENV_PATH.read_text(encoding="utf-8")
    if not _get_env_value(text, "DB_PASSWORD"):
        _fail(
            "O backend/.env está sem DB_PASSWORD preenchida.\n\n"
            "Isso é a senha do usuário do PostgreSQL que a aplicação usa (não a\n"
            "senha do seu usuário Windows). Abra backend\\.env e preencha:\n\n"
            "    DB_PASSWORD=sua_senha_aqui\n\n"
            "Se você ainda não criou esse usuário/banco no PostgreSQL, veja o\n"
            "passo-a-passo em DATABASE_SETUP.md."
        )


def _apply_schema_if_needed() -> None:
    import psycopg2
    import psycopg2.errors

    host = os.getenv("DB_HOST", "127.0.0.1")
    port = int(os.getenv("DB_PORT", "5432"))
    user = os.getenv("DB_USER", "econradar_app")
    password = os.getenv("DB_PASSWORD", "")
    dbname = os.getenv("DB_NAME", "econradar")

    try:
        conn = psycopg2.connect(host=host, port=port, user=user, password=password,
                                 dbname=dbname, connect_timeout=5)
    except psycopg2.OperationalError as e:
        msg = str(e)
        if "password authentication failed" in msg:
            _fail(
                f"O PostgreSQL recusou a senha do usuário '{user}'.\n\n"
                "Confira DB_USER e DB_PASSWORD no backend\\.env. Se esqueceu a senha,\n"
                "redefina com (rodando como o superusuário 'postgres'):\n\n"
                f"    ALTER USER {user} WITH PASSWORD 'nova_senha';"
            )
        if "does not exist" in msg and "database" in msg:
            _fail(
                f"O banco de dados '{dbname}' ainda não existe no PostgreSQL.\n\n"
                "Crie-o uma única vez (rodando como o superusuário 'postgres'):\n\n"
                f"    CREATE USER {user} WITH PASSWORD 'sua_senha';\n"
                f"    CREATE DATABASE {dbname} OWNER {user};\n\n"
                "Veja o passo-a-passo completo em DATABASE_SETUP.md."
            )
        if "Connection refused" in msg or "could not connect" in msg or "timeout expired" in msg:
            _fail(
                f"Não consegui conectar ao PostgreSQL em {host}:{port}.\n\n"
                "O serviço do PostgreSQL parece estar desligado. No Windows, abra os\n"
                "'Serviços' (services.msc) e verifique se o 'postgresql-x64-...' está\n"
                "rodando (Iniciar), ou abra o pgAdmin para conferir."
            )
        _fail(f"Erro ao conectar no PostgreSQL: {msg}")

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT to_regclass('public.users') IS NOT NULL AS has_users_table"
            )
            has_users_table = cur.fetchone()[0]

        if not has_users_table:
            print("[setup] Tabelas do banco ainda não existem — criando agora (primeira vez)...")
            if not SCHEMA_PATH.exists():
                _fail(f"Não encontrei o schema em {SCHEMA_PATH}.")
            sql_text = SCHEMA_PATH.read_text(encoding="utf-8")
            with conn.cursor() as cur:
                cur.execute(sql_text)
            conn.commit()
            print("[setup] Tabelas criadas com sucesso.")
    except psycopg2.errors.InsufficientPrivilege:
        conn.rollback()
        _fail(
            f"O usuário '{user}' não tem permissão para criar tabelas em '{dbname}'.\n\n"
            "Isso costuma acontecer quando o usuário do app não é o dono do banco.\n"
            "Rode uma única vez, como o superusuário 'postgres':\n\n"
            f"    ALTER DATABASE {dbname} OWNER TO {user};\n\n"
            "Veja o passo-a-passo completo em DATABASE_SETUP.md."
        )
    except psycopg2.Error as e:
        conn.rollback()
        _fail(f"Erro ao criar as tabelas: {e}")
    finally:
        conn.close()


def main() -> None:
    from dotenv import load_dotenv

    _ensure_env_file()
    load_dotenv(ENV_PATH, override=True)
    _auto_generate_missing_keys()
    load_dotenv(ENV_PATH, override=True)
    _check_db_password()
    _apply_schema_if_needed()
    print("[setup] Tudo certo — iniciando o servidor.\n")


if __name__ == "__main__":
    main()
