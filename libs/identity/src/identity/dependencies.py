"""
FastAPI Dependencies for Identity and Tenant Resolution.
Follows Hexagonal Architecture by adapting the HTTP layer to the Identity domain.
"""

import contextlib
import logging
from collections.abc import AsyncGenerator
from typing import Any

import jwt
from config.settings import get_settings
from database.models import DatabaseShard, Tenant
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2AuthorizationCodeBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from identity.application.use_cases import ResolveTenantUseCase
from identity.infrastructure.repositories import SQLAlchemyIdentityRepository

logger = logging.getLogger(__name__)

# Use Authorization Code flow for proper ZITADEL SSO integration via Swagger UI
_settings = get_settings()
oauth2_scheme = OAuth2AuthorizationCodeBearer(
    authorizationUrl=_settings.identity.authorization_url,
    tokenUrl=_settings.identity.token_url,
    scopes={"openid": "OpenID Connect", "profile": "Profile information", "email": "Email address"},
    auto_error=False,
)

jwks_client = jwt.PyJWKClient(_settings.identity.jwks_url)


async def get_raw_jwt(token: str | None = Depends(oauth2_scheme)) -> dict[str, Any]:
    """
    Extracts and validates the JWT issued by Zitadel.
    In a true enterprise environment, this fetches the JWKS from Zitadel
    to verify the RSA signature.
    """
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    settings = get_settings()

    try:
        signing_key = await run_in_threadpool(jwks_client.get_signing_key_from_jwt, token)
        payload = jwt.decode(
            token,
            key=signing_key.key,
            algorithms=["RS256"],
            audience=settings.identity.audience,
            issuer=settings.identity.issuer,
            options={"require": ["iss", "aud", "exp"]},
        )
        return payload
    except jwt.PyJWTError as e:
        logger.error(f"JWT Validation failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e


async def get_identity_repository(
    request: Request,
) -> AsyncGenerator[SQLAlchemyIdentityRepository, None]:
    """Dependency that yields a repository bound to the Global database session."""
    db_router = getattr(request.app.state, "db_router", None)
    if not db_router:
        raise RuntimeError("DatabaseRouter not initialized in app state")

    async_gen = db_router.get_global_session()
    global_session: AsyncSession = await async_gen.__anext__()
    try:
        yield SQLAlchemyIdentityRepository(global_session)
    finally:
        with contextlib.suppress(StopAsyncIteration):
            await async_gen.__anext__()


async def get_resolve_tenant_use_case(
    repo: SQLAlchemyIdentityRepository = Depends(get_identity_repository),
) -> ResolveTenantUseCase:
    """Dependency that constructs the pure Application use case."""
    return ResolveTenantUseCase(repo)


async def get_current_tenant_id(
    token_payload: dict[str, Any] = Depends(get_raw_jwt),
    use_case: ResolveTenantUseCase = Depends(get_resolve_tenant_use_case),
) -> int:
    """
    Resolves the external Zitadel user email from the JWT to our internal global DB tenant_id.
    """
    email = token_payload.get("email")
    name = token_payload.get("name")
    if not email:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Token does not contain an email claim",
        )

    try:
        return await use_case.execute(str(email), str(name) if name else "")
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e),
        ) from e
    except RuntimeError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        ) from e


async def get_tenant_session(
    request: Request, tenant_id: int = Depends(get_current_tenant_id)
) -> AsyncGenerator[AsyncSession, None]:
    """
    Yields an AsyncSession dynamically bound to the correct database shard,
    with PostgreSQL Row-Level Security (RLS) automatically applied.
    """
    db_router = getattr(request.app.state, "db_router", None)
    if not db_router:
        raise RuntimeError("DatabaseRouter not initialized in app state")

    # 1. Fetch routing info from Global DB
    async_gen_global = db_router.get_global_session()
    global_session: AsyncSession = await async_gen_global.__anext__()
    try:
        # We need the DatabaseShard info
        # This could be heavily cached in memory to avoid a query on every request!
        stmt = (
            select(Tenant, DatabaseShard)
            .join(DatabaseShard, Tenant.shard_id == DatabaseShard.id)
            .where(Tenant.id == tenant_id)
        )
        result = await global_session.execute(stmt)
        tenant, shard = result.one()
        shard_key = shard.name
        shard_url = shard.dsn
    finally:
        with contextlib.suppress(StopAsyncIteration):
            await async_gen_global.__anext__()

    # 2. Yield the RLS-secured tenant session
    async_gen_tenant = db_router.get_tenant_session(tenant_id, shard_key, shard_url)
    tenant_session: AsyncSession = await async_gen_tenant.__anext__()
    try:
        yield tenant_session
    finally:
        with contextlib.suppress(StopAsyncIteration):
            await async_gen_tenant.__anext__()
