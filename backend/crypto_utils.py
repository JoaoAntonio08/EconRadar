"""
crypto_utils.py — Criptografia de dados sensíveis em repouso.

Usado para:
  - api_keys.enc_key       (chaves Finnhub/OpenRouter do usuário)
  - b3_positions.enc_payload (quantidade + corretora da carteira B3)

Algoritmo: Fernet (AES-128-CBC + HMAC-SHA256, autenticado). A chave NUNCA
fica no banco nem no repositório — só existe na variável de ambiente
ENCRYPTION_KEY do servidor. Se o banco vazar sem o .env, os dados
permanecem ilegíveis.

Gere uma chave nova com:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
from __future__ import annotations

import json
import os

from cryptography.fernet import Fernet, InvalidToken

_ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "")

if not _ENCRYPTION_KEY:
    raise RuntimeError(
        "ENCRYPTION_KEY não configurada. Gere uma com "
        "`python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"` "
        "e defina no .env. Sem ela, dados sensíveis (API keys, posições B3) não podem ser cifrados."
    )

_fernet = Fernet(_ENCRYPTION_KEY.encode() if isinstance(_ENCRYPTION_KEY, str) else _ENCRYPTION_KEY)


def encrypt_str(plain: str) -> bytes:
    """Cifra uma string e retorna bytes prontos para coluna VARBINARY."""
    return _fernet.encrypt(plain.encode("utf-8"))


def decrypt_str(token: bytes) -> str:
    try:
        return _fernet.decrypt(bytes(token)).decode("utf-8")
    except InvalidToken as e:
        raise ValueError("Falha ao decifrar dado: token inválido ou ENCRYPTION_KEY incorreta.") from e


def encrypt_json(obj: dict) -> bytes:
    return encrypt_str(json.dumps(obj, ensure_ascii=False, default=str))


def decrypt_json(token: bytes) -> dict:
    return json.loads(decrypt_str(token))
