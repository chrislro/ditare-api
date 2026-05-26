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
from typing import Optional, Dict, Any
from pydantic_settings import BaseSettings
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ditare-api")

# Configuration
class Settings(BaseSettings):
    groq_api_key: str = ""
    openai_api_key: str = ""
    redis_url: str = "redis://localhost:6379/0"
    apple_team_id: str = ""  # Set after DIT-0
    revenuecat_webhook_secret: str = ""  # Set when RevenueCat project exists
    env: str = "development"
    
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

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # TODO: restrict to ditare app + landing page
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

def check_pro_entitlement(token: str) -> bool:
    """Check if user has Pro entitlement. Stub until RevenueCat webhooks are wired."""
    if not token:
        return False
    # TODO: Implement proper entitlement check via RevenueCat or Apple
    # For now, allow all valid-looking tokens in development
    if settings.env == "development":
        return True
    # Check Redis for active entitlement
    return redis_client.get(f"entitlement:pro:{token}") == "active"

def get_user_id(token: str) -> Optional[str]:
    """Extract user ID from token. Stub implementation."""
    if not token:
        return None
    # TODO: Implement proper token verification
    return redis_client.get(f"token:user:{token}")

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
    authorization_code: Optional[str] = None

class AuthExchangeResponse(BaseModel):
    ok: bool
    api_key: str
    user_id: str
    entitlement: str

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
    is_pro = check_pro_entitlement(token) if token else False
    
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
    if not check_pro_entitlement(token):
        raise HTTPException(status_code=403, detail="Pro subscription required")
    
    provider = "groq"
    model = "whisper-large-v3"
    provider_cfg = PROVIDERS[provider]
    
    if not provider_cfg["key"]:
        raise HTTPException(status_code=500, detail="Groq API key not configured")
    
    files = {"file": (audio.filename or "audio.m4a", audio.file, audio.content_type or "audio/m4a")}
    data = {"model": model, "response_format": "text"}
    if language:
        data["language"] = language
    if prompt:
        data["prompt"] = prompt[:800]
    
    headers = {"Authorization": f"Bearer {provider_cfg['key']}"}
    
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            logger.info(f"Transcribing for user {get_user_id(token)}")
            resp = await client.post(provider_cfg["url"], files=files, data=data, headers=headers)
            
            if resp.status_code != 200:
                logger.error(f"Groq error: {resp.status_code} - {resp.text}")
                raise HTTPException(status_code=resp.status_code, detail=f"Transcription error: {resp.text}")
            
            text = resp.text.strip()
            only_punct = all(c in " \t\n!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~" for c in text)
            if only_punct:
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
    if not check_pro_entitlement(token):
        raise HTTPException(status_code=403, detail="Pro subscription required")
    
    if not settings.openai_api_key:
        raise HTTPException(status_code=500, detail="OpenAI API key not configured")
    
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
async def auth_exchange(body: AuthExchangeRequest):
    """
    Exchange Apple identity token for Ditare API key.
    
    **Stub — implement after DIT-0 (Apple Developer Program active).**
    Requires Apple Team ID verification.
    """
    if not settings.apple_team_id:
        raise HTTPException(
            status_code=501, 
            detail="Apple auth not yet configured. Complete DIT-0 first."
        )
    
    # TODO: Verify Apple identity token with Apple servers
    # TODO: Create or lookup user
    # TODO: Generate API key and store in Redis
    # TODO: Check RevenueCat entitlement
    
    raise HTTPException(status_code=501, detail="Not yet implemented — waiting for DIT-0")

@app.post("/webhooks/revenuecat")
@limiter.limit("100/minute")
async def revenuecat_webhook(request: Request, body: RevenueCatWebhook):
    """
    Idempotent RevenueCat webhook handler.
    
    **Stub — wire when RevenueCat project exists.**
    Handles subscription events: INITIAL_PURCHASE, RENEWAL, CANCELLATION, etc.
    """
    if not settings.revenuecat_webhook_secret:
        raise HTTPException(
            status_code=501,
            detail="RevenueCat webhook secret not configured"
        )
    
    # TODO: Verify webhook signature
    # TODO: Handle events idempotently (dedupe by event_id)
    # TODO: Update Redis entitlement state
    
    logger.info(f"RevenueCat event received: {body.event.get('type', 'unknown')}")
    return {"ok": True, "received": True}
