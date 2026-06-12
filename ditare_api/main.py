from fastapi import FastAPI, File, UploadFile, HTTPException, Request, Depends, Header, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import httpx
import os
import logging
import redis
import time
import secrets
import json
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from pydantic_settings import BaseSettings
from pydantic import BaseModel
import jwt
from jwt import PyJWKClient
from jwt.exceptions import InvalidTokenError

# Structured JSON logging in production
if os.environ.get("ENV") == "production":
    class JSONFormatter(logging.Formatter):
        def format(self, record):
            log_obj = {
                "timestamp": self.formatTime(record),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
            if hasattr(record, "request_id"):
                log_obj["request_id"] = record.request_id
            if record.exc_info:
                log_obj["exception"] = self.formatException(record.exc_info)
            return json.dumps(log_obj, default=str)

    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
else:
    logging.basicConfig(level=logging.INFO)

logger = logging.getLogger("ditare-api")

# Configuration
class Settings(BaseSettings):
    groq_api_key: str = ""
    openai_api_key: str = ""
    redis_url: str = "redis://localhost:6379/0"
    apple_team_id: str = ""  # Set after DIT-0
    apple_bundle_id: str = ""  # Ditare macOS app bundle ID
    revenuecat_api_key: str = ""  # RevenueCat REST API key for entitlement checks
    revenuecat_webhook_secret: str = ""  # Set when RevenueCat project exists
    env: str = "development"
    jwt_secret: str = ""  # HS256 secret for session tokens
    
    class Config:
        env_file = ".env"

settings = Settings()

# Redis client
redis_client = redis.from_url(settings.redis_url, decode_responses=True)

# Rate limiter
limiter = Limiter(key_func=get_remote_address)

app = FastAPI(
    title="Ditare API",
    description="Backend API for Ditare macOS dictation app",
    version="0.1.0"
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS — restricted to known origins only
# Note: macOS native apps do not send Origin headers, so CORS does not apply.
ALLOWED_ORIGINS = [
    "https://ditare.app",
    "https://ditare.vercel.app",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Provider configs
PROVIDERS = {
    "groq": {
        "url": "https://api.groq.com/openai/v1/audio/transcriptions",
        "key": settings.groq_api_key,
        "models": ["whisper-large-v3"]
    },
    "openai": {
        "url": "https://api.openai.com/v1/audio/transcriptions",
        "key": settings.openai_api_key,
        "models": ["gpt-4o-transcribe", "whisper-1"]
    }
}

# Auth / Entitlement helpers
API_KEY_PREFIX = "ditare_api_key_"

def get_auth_token(authorization: Optional[str] = Header(None)) -> Optional[str]:
    if not authorization:
        return None
    if authorization.lower().startswith("bearer "):
        return authorization[7:]
    return authorization

async def check_pro_entitlement(token: str) -> bool:
    """Check if user has Pro entitlement via RevenueCat REST API with Redis caching."""
    if not token:
        return False

    # In development, allow all valid-looking tokens
    if settings.env == "development":
        return True

    # Get user_id from token
    user_id = get_user_id(token)
    if not user_id:
        return False

    # 1. Check Redis cache first (5-minute TTL).
    # NOTE: deliberately a separate key from the persistent webhook state
    # (entitlement:pro:<user_id>) so the short-TTL API cache never clobbers it.
    cache_key = f"entitlement:pro:cache:{user_id}"
    cached = redis_client.get(cache_key)
    if cached is not None:
        logger.debug(f"Entitlement cache hit for {user_id}")
        return cached == "active"

    # 2. No cache — call RevenueCat REST API
    if not settings.revenuecat_api_key:
        logger.warning("RevenueCat API key not configured; falling back to webhook-only entitlement")
        # Fallback: check webhook-set entitlement (no TTL, persistent)
        webhook_state = redis_client.get(f"entitlement:pro:{user_id}")
        return webhook_state == "active"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"https://api.revenuecat.com/v1/subscribers/{user_id}",
                headers={"Authorization": f"Bearer {settings.revenuecat_api_key}"}
            )

            if resp.status_code == 200:
                data = resp.json()
                subscriber = data.get("subscriber", {})
                entitlements = subscriber.get("entitlements", {})
                pro_entitlement = entitlements.get("pro")
                if not pro_entitlement:
                    is_active = False
                else:
                    expires_date = pro_entitlement.get("expires_date")
                    if expires_date is None:
                        # Lifetime/promotional entitlements have no expiry
                        is_active = True
                    else:
                        try:
                            expires_at = datetime.fromisoformat(expires_date.replace("Z", "+00:00"))
                            is_active = expires_at > datetime.now(timezone.utc)
                        except (ValueError, TypeError):
                            logger.warning(f"Unparseable expires_date for {user_id}: {expires_date!r}")
                            is_active = False

                # Cache result in Redis with 5-minute TTL (300 seconds)
                cache_value = "active" if is_active else "inactive"
                redis_client.setex(cache_key, 300, cache_value)
                logger.info(f"RevenueCat entitlement check for {user_id}: {cache_value}")
                return is_active
            elif resp.status_code == 404:
                # User not found in RevenueCat
                redis_client.setex(cache_key, 300, "inactive")
                logger.info(f"RevenueCat subscriber not found: {user_id}")
                return False
            else:
                logger.warning(f"RevenueCat API error {resp.status_code} for {user_id}")
                # On error, fall back to webhook state if available
                webhook_state = redis_client.get(f"entitlement:pro:{user_id}")
                return webhook_state == "active"
    except Exception as e:
        logger.exception(f"RevenueCat entitlement check failed for {user_id}")
        # On exception, fall back to webhook state if available
        webhook_state = redis_client.get(f"entitlement:pro:{user_id}")
        return webhook_state == "active"

def get_user_id(token: str) -> Optional[str]:
    """Extract user ID from session token."""
    if not token:
        return None
    # First check Redis mapping
    user_id = redis_client.get(f"token:user:{token}")
    if user_id:
        return user_id
    # Fallback: verify JWT and extract sub
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"], issuer="ditare-api")
        return payload.get("sub")
    except Exception:
        return None

# Apple JWT verification helpers
APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"

async def verify_apple_token(identity_token: str, apple_user_id: str, bundle_id: str) -> Dict[str, Any]:
    """Verify Apple identity token (RS256 JWT) and return claims."""
    try:
        # Fetch JWKS and create client (run blocking I/O in thread pool)
        loop = __import__('asyncio').get_event_loop()
        jwks_client = PyJWKClient(APPLE_JWKS_URL, cache_keys=True)
        signing_key = await loop.run_in_executor(None, jwks_client.get_signing_key_from_jwt, identity_token)
        
        # Decode and verify token
        payload = jwt.decode(
            identity_token,
            signing_key.key,
            algorithms=["RS256"],
            audience=bundle_id,
            issuer="https://appleid.apple.com"
        )
        
        # Verify sub claim matches apple_user_id
        if payload.get("sub") != apple_user_id:
            raise ValueError("sub claim does not match apple_user_id")
        
        # Verify token is not expired (jwt.decode handles exp automatically)
        # Verify issued at is not in the future (with 60s leeway)
        iat = payload.get("iat")
        if iat and iat > time.time() + 60:
            raise ValueError("Token issued in the future")
        
        return payload
    except InvalidTokenError as e:
        logger.warning(f"Apple token verification failed: {e}")
        raise HTTPException(status_code=401, detail=f"Invalid Apple identity token: {e}")
    except Exception as e:
        logger.exception("Apple token verification error")
        raise HTTPException(status_code=401, detail=f"Token verification failed: {e}")

def generate_session_token(user_id: str, apple_sub: str) -> str:
    """Generate HS256 session JWT with 30-day expiry."""
    if not settings.jwt_secret:
        raise RuntimeError("JWT_SECRET is not configured")
    now = int(time.time())
    expires_in = 2592000  # 30 days
    payload = {
        "sub": user_id,
        "apple_sub": apple_sub,
        "iat": now,
        "exp": now + expires_in,
        "iss": "ditare-api"
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")

def upsert_user(apple_user_id: str, email: Optional[str], full_name: Optional[str]) -> None:
    """Upsert user in Redis."""
    user_key = f"user:{apple_user_id}"
    user_data = {
        "apple_user_id": apple_user_id,
        "updated_at": str(int(time.time()))
    }
    if email:
        user_data["email"] = email
    if full_name:
        user_data["full_name"] = full_name
    redis_client.hset(user_key, mapping=user_data)

# Request/Response models
class CleanupRequest(BaseModel):
    text: str
    profile: str = "auto"  # raw, auto, email, chat, code, medical

class CleanupResponse(BaseModel):
    ok: bool
    text: str
    chars: int
    profile: str

class MeResponse(BaseModel):
    user_id: Optional[str]
    entitlement: str  # "free" | "pro"
    provider: str = "groq"
    model: str = "whisper-large-v3"

class AuthExchangeRequest(BaseModel):
    identity_token: str
    apple_user_id: str
    email: Optional[str] = None
    full_name: Optional[str] = None

class AuthExchangeResponse(BaseModel):
    ok: bool
    session_token: str
    expires_in: int
    user_id: str

class RevenueCatWebhook(BaseModel):
    event: Dict[str, Any]

# Endpoints
@app.get("/healthz")
async def healthz():
    """Health check endpoint for load balancers and monitoring."""
    redis_ok = False
    try:
        redis_client.ping()
        redis_ok = True
    except Exception:
        pass
    
    return {
        "status": "ok",
        "version": "0.1.0",
        "region": "gru",
        "redis_connected": redis_ok,
        "groq_configured": bool(settings.groq_api_key),
        "openai_configured": bool(settings.openai_api_key)
    }

@app.get("/")
async def root():
    return {
        "service": "Ditare API",
        "version": "0.1.0",
        "docs": "/docs",
        "healthz": "/healthz"
    }

@app.get("/me", response_model=MeResponse)
async def me(authorization: Optional[str] = Header(None)):
    """Get current user info and entitlement."""
    token = get_auth_token(authorization)
    user_id = get_user_id(token) if token else None
    is_pro = await check_pro_entitlement(token) if token else False
    
    return MeResponse(
        user_id=user_id,
        entitlement="pro" if is_pro else "free",
        provider="groq",
        model="whisper-large-v3"
    )

@app.post("/transcribe")
@limiter.limit("30/minute")
async def transcribe(
    request: Request,
    audio: UploadFile = File(..., description="Audio file (m4a, mp3, wav, etc.)"),
    language: Optional[str] = Form(""),
    prompt: Optional[str] = Form(""),
    authorization: Optional[str] = Header(None)
):
    """
    Transcribe audio using Groq Whisper (Pro-only).
    
    - **audio**: The audio file to transcribe
    - **language**: ISO 639-1 language code (e.g. "pt", "en"). Empty = auto-detect.
    - **prompt**: Optional vocabulary prompt to bias recognition (max ~224 tokens)
    """
    # Pro-only check
    token = get_auth_token(authorization)
    if not await check_pro_entitlement(token):
        raise HTTPException(status_code=403, detail="Pro subscription required")
    
    provider = "groq"
    model = "whisper-large-v3"
    provider_cfg = PROVIDERS[provider]
    
    # Check settings directly (PROVIDERS dict captures key at import time)
    if not settings.groq_api_key:
        raise HTTPException(status_code=403, detail="Groq API key not configured")
    
    files = {"file": (audio.filename or "audio.m4a", audio.file, audio.content_type or "audio/m4a")}
    data = {"model": model, "response_format": "text"}
    if language:
        data["language"] = language
    if prompt:
        data["prompt"] = prompt[:800]
    
    headers = {"Authorization": f"Bearer {settings.groq_api_key}"}
    
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            logger.info(f"Transcribing for user {get_user_id(token)}")
            resp = await client.post(provider_cfg["url"], files=files, data=data, headers=headers)
            
            if resp.status_code != 200:
                logger.error(f"Groq error: {resp.status_code} - {resp.text}")
                raise HTTPException(status_code=resp.status_code, detail=f"Transcription error: {resp.text}")
            
            text = resp.text.strip()
            only_punct = all(c in " \t\n!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~" for c in text)
            if only_punct and text:
                logger.warning("Transcription output was punctuation-only; returning empty text")
                text = ""
            
            return {"ok": True, "text": text, "chars": len(text), "provider": provider, "model": model}
            
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Transcription request timed out")
    except Exception as e:
        logger.exception("Transcription failed")
        raise HTTPException(status_code=500, detail=f"Transcription failed: {str(e)}")

@app.post("/cleanup", response_model=CleanupResponse)
@limiter.limit("60/minute")
async def cleanup(
    request: Request,
    body: CleanupRequest,
    authorization: Optional[str] = Header(None)
):
    """
    Clean up / post-process transcribed text using OpenAI GPT-4o-mini (Pro-only).
    
    - **text**: The raw transcribed text to clean up
    - **profile**: Cleanup profile — raw (no-op), auto, email, chat, code, medical
    """
    # Pro-only check
    token = get_auth_token(authorization)
    if not await check_pro_entitlement(token):
        raise HTTPException(status_code=403, detail="Pro subscription required")
    
    if not settings.openai_api_key:
        raise HTTPException(status_code=403, detail="OpenAI API key not configured")
    
    # Profile-based system prompts (same as macOS app)
    SYSTEM_PROMPTS = {
        "raw": "Return the text exactly as provided, with no changes.",
        "auto": "Clean up the text for the current context. Fix capitalization and punctuation. Keep the original meaning.",
        "email": "Format this as a professional email. Add proper greeting and sign-off if missing. Fix grammar and punctuation.",
        "chat": "Format this as casual chat text. Keep it natural and conversational.",
        "code": "Format this as clean code or technical documentation. Fix syntax if needed.",
        "medical": "Format this as professional medical documentation. Use proper medical terminology and structure."
    }
    
    system_prompt = SYSTEM_PROMPTS.get(body.profile, SYSTEM_PROMPTS["auto"])
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                json={
                    "model": "gpt-4o-mini",
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": body.text}
                    ],
                    "temperature": 0.3
                }
            )
            
            if resp.status_code != 200:
                logger.error(f"OpenAI error: {resp.status_code} - {resp.text}")
                raise HTTPException(status_code=resp.status_code, detail=f"Cleanup error: {resp.text}")
            
            result = resp.json()
            cleaned_text = result["choices"][0]["message"]["content"].strip()
            
            return CleanupResponse(ok=True, text=cleaned_text, chars=len(cleaned_text), profile=body.profile)
            
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Cleanup request timed out")
    except Exception as e:
        logger.exception("Cleanup failed")
        raise HTTPException(status_code=500, detail=f"Cleanup failed: {str(e)}")

@app.post("/auth/exchange", response_model=AuthExchangeResponse)
@limiter.limit("10/minute")
async def auth_exchange(request: Request, body: AuthExchangeRequest):
    """
    Exchange Apple identity token for Ditare session token.
    
    Verifies the Apple identity token (RS256 JWT) against Apple's public keys,
    upserts the user in Redis, generates a session JWT, and stores the mapping.
    """
    # Check required configuration
    if not settings.apple_team_id or not settings.apple_bundle_id:
        raise HTTPException(
            status_code=501,
            detail="Apple auth not yet configured. Set APPLE_TEAM_ID and APPLE_BUNDLE_ID."
        )
    
    # Validate input
    if not body.identity_token or not body.apple_user_id:
        raise HTTPException(status_code=401, detail="Missing identity_token or apple_user_id")
    
    # Verify Apple identity token
    claims = await verify_apple_token(
        body.identity_token,
        body.apple_user_id,
        settings.apple_bundle_id
    )
    
    # Upsert user in Redis.
    # Prefer the verified email claim from the Apple token; only fall back to
    # the client-provided email when the token carries no email claim.
    email = claims.get("email")
    if not email and body.email:
        logger.info(f"No email claim in Apple token for {body.apple_user_id}; using client-provided email")
        email = body.email
    upsert_user(body.apple_user_id, email, body.full_name)
    
    # Generate session token
    session_token = generate_session_token(body.apple_user_id, claims.get("sub", body.apple_user_id))
    
    # Store token→user mapping in Redis with 30-day TTL
    redis_client.setex(f"token:user:{session_token}", 2592000, body.apple_user_id)
    
    logger.info(f"Auth exchange successful for user {body.apple_user_id}")
    
    return AuthExchangeResponse(
        ok=True,
        session_token=session_token,
        expires_in=2592000,
        user_id=body.apple_user_id
    )

@app.post("/webhooks/revenuecat")
@limiter.limit("100/minute")
async def revenuecat_webhook(request: Request, body: RevenueCatWebhook):
    """
    Idempotent RevenueCat webhook handler.

    Handles subscription events: INITIAL_PURCHASE, RENEWAL, CANCELLATION, etc.
    - Verifies webhook signature via shared secret
    - Deduplicates by event.id (stores processed IDs in Redis)
    - Updates Redis entitlement:pro:<user_id> based on event type
    """
    if not settings.revenuecat_webhook_secret:
        raise HTTPException(
            status_code=501,
            detail="RevenueCat webhook secret not configured"
        )

    # 1. Verify webhook signature (RevenueCat sends Authorization: Bearer <secret>)
    auth_header = request.headers.get("authorization", "")
    expected = f"Bearer {settings.revenuecat_webhook_secret}"
    if not secrets.compare_digest(auth_header, expected):
        logger.warning("RevenueCat webhook signature mismatch")
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    event = body.event
    event_id = event.get("id")
    event_type = event.get("type", "UNKNOWN")
    app_user_id = event.get("app_user_id")

    if not event_id:
        logger.warning("RevenueCat webhook missing event.id")
        raise HTTPException(status_code=400, detail="Missing event.id")
    if not app_user_id:
        logger.warning("RevenueCat webhook missing app_user_id")
        raise HTTPException(status_code=400, detail="Missing app_user_id")

    # 2. Idempotency: dedupe by event.id (Redis key with 7-day TTL)
    dedupe_key = f"revenuecat:processed:{event_id}"
    if not redis_client.set(dedupe_key, "1", nx=True, ex=604800):
        logger.info(f"RevenueCat event {event_id} already processed; skipping")
        return {"ok": True, "idempotent": True}

    # 3. Update entitlement state based on event type
    entitlement_key = f"entitlement:pro:{app_user_id}"
    active_events = {
        "INITIAL_PURCHASE",
        "RENEWAL",
        "NON_RENEWING_PURCHASE",
        "PRODUCT_CHANGE",
        "UNCANCELLATION",
    }
    inactive_events = {
        "CANCELLATION",
        "EXPIRATION",
        "BILLING_ISSUE",
        "SUBSCRIPTION_PAUSED",
    }

    if event_type in active_events:
        redis_client.set(entitlement_key, "active")
        logger.info(f"Set pro entitlement active for {app_user_id} (event={event_type})")
    elif event_type in inactive_events:
        redis_client.set(entitlement_key, "inactive")
        logger.info(f"Set pro entitlement inactive for {app_user_id} (event={event_type})")
    else:
        logger.info(f"No entitlement change for {app_user_id} (event={event_type})")

    logger.info(f"RevenueCat event {event_id} ({event_type}) processed for {app_user_id}")
    return {"ok": True, "received": True, "event_type": event_type}
