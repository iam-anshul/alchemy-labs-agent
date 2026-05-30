"""Static bearer-token authentication.

Token format in .env ``API_AUTH_TOKENS``: ``token1:user_alice|token2:user_bob``
"""
from __future__ import annotations

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from config import get_settings

_bearer_scheme = HTTPBearer()


def _parse_auth_tokens(raw: str) -> dict[str, str]:
    """Parse ``token:user_id`` pairs separated by ``|``."""
    tokens: dict[str, str] = {}
    for pair in raw.split("|"):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        token, user_id = pair.split(":", 1)
        tokens[token.strip()] = user_id.strip()
    return tokens


_TOKEN_MAP: dict[str, str] = _parse_auth_tokens(get_settings().api_auth_tokens)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
) -> str:
    """FastAPI dependency: extract and validate Bearer token, return user_id."""
    user_id = _TOKEN_MAP.get(credentials.credentials)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Invalid token")
    return user_id
