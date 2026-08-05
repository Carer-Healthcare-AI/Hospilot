import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

import bcrypt
import jwt
from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from config import settings
from db.hasura import hasura
from schemas.models import SignupRequest, LoginRequest

logger = logging.getLogger("auth")
router = APIRouter()

_bearer = HTTPBearer(auto_error=False)

# Token schema version. Bumped to 2 when multi-tenancy added org_id to the
# claims -- require_active_user rejects anything older, forcing a re-login.
TOKEN_VERSION = 2


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


# ── helpers ────────────────────────────────────────────────────────────────────

def _create_token(user: dict) -> str:
    payload = {
        "sub":          user["id"],
        "username":     user["username"],
        "display_name": user["display_name"],
        "role":         user["role"],
        "org_id":       user.get("org_id"),   # None for super_admin (platform-level)
        "ver":          TOKEN_VERSION,
        "exp":          datetime.now(timezone.utc) + timedelta(hours=settings.jwt_expiry_hours),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def _decode_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])


def _user_response(user: dict, token: str, org_name: str | None = None) -> dict:
    return {
        "token": token,
        "user": {
            "id":           user["id"],
            "username":     user["username"],
            "display_name": user["display_name"],
            "role":         user["role"],
            "org_id":       user.get("org_id"),
            "org_name":     org_name,
        },
    }


async def _org_name(org_id: str | None) -> str | None:
    """Display name of an org from the routing-registry cache (no extra query
    on the hot path); None for super_admin / unknown."""
    if not org_id:
        return None
    try:
        orgs = await hasura.ensure_org_registry()
        return (orgs.get(org_id) or {}).get("name")
    except Exception:  # noqa: BLE001
        return None


# ── auth context + FastAPI dependencies ────────────────────────────────────────

@dataclass(frozen=True)
class AuthContext:
    """Identity + tenant scope of the caller, derived from a verified JWT."""
    user_id: str
    username: str
    display_name: str
    role: str
    org_id: str | None      # None only for super_admin

    def is_super(self) -> bool:
        return self.role == "super_admin"


async def require_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    if not credentials:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        return _decode_token(credentials.credentials)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


async def require_active_user(claims: dict = Depends(require_auth)) -> AuthContext:
    """Valid current-version token -> AuthContext. Pure-JWT, no DB hit."""
    if claims.get("ver") != TOKEN_VERSION:
        # Pre-multi-tenancy token: no org claim -- force a re-login.
        raise HTTPException(status_code=401, detail="Session expired, please log in again")
    role = claims.get("role", "")
    org_id = claims.get("org_id")
    if role != "super_admin" and not org_id:
        raise HTTPException(status_code=401, detail="Session expired, please log in again")
    return AuthContext(
        user_id=claims["sub"],
        username=claims.get("username", ""),
        display_name=claims.get("display_name", ""),
        role=role,
        org_id=org_id,
    )


def require_role(*roles: str):
    """Dependency factory: caller must hold one of `roles`.

    super_admin implicitly passes every role check (platform-level).
    """
    allowed = set(roles) | {"super_admin"}

    async def _dep(ctx: AuthContext = Depends(require_active_user)) -> AuthContext:
        if ctx.role not in allowed:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return ctx

    return _dep


# ── endpoints ──────────────────────────────────────────────────────────────────

@router.post("/auth/signup", status_code=202)
async def signup(body: SignupRequest):
    existing = await hasura.get_user_by_username(body.username)
    if existing:
        raise HTTPException(status_code=409, detail="Username already taken")

    org = await hasura.get_org(body.org_id)
    if not org or org.get("status") != "active":
        raise HTTPException(status_code=400, detail="Unknown or inactive organization")

    password_hash = _hash_password(body.password)
    user = await hasura.create_user(
        username=body.username,
        password_hash=password_hash,
        display_name=body.display_name,
        role=body.role,
        org_id=body.org_id,
        status="pending",
    )
    logger.info("signup (pending)  username=%s  role=%s  org=%s",
                user["username"], user["role"], body.org_id)
    # No token: the account must be approved first (org admin for doctors/
    # approvers, super_admin for admins).
    return {
        "status": "pending",
        "message": "Account created. Awaiting approval by your organization admin.",
    }


@router.post("/auth/login")
async def login(body: LoginRequest):
    user = await hasura.get_user_by_username(body.username.strip().lower())
    if not user or not _verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid username or password")

    status = user.get("status", "active")
    if status == "pending":
        raise HTTPException(status_code=403, detail="Account awaiting approval")
    if status != "active":
        raise HTTPException(status_code=403, detail="Account is not active")

    token = _create_token(user)
    logger.info("login  username=%s  role=%s", user["username"], user["role"])
    return _user_response(user, token, org_name=await _org_name(user.get("org_id")))


@router.get("/auth/me")
async def me(ctx: AuthContext = Depends(require_active_user)):
    return {
        "id":           ctx.user_id,
        "username":     ctx.username,
        "display_name": ctx.display_name,
        "role":         ctx.role,
        "org_id":       ctx.org_id,
        "org_name":     await _org_name(ctx.org_id),
    }
