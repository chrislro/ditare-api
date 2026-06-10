# Ditare API

FastAPI backend for the Ditare macOS dictation app.

## Overview

- **Region:** Fly.io `gru` (São Paulo, Brazil)
- **Domain:** `api.ditare.app`
- **Health check:** `GET /healthz`

## Endpoints

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/healthz` | GET | No | Health check |
| `/me` | GET | Yes | User info + entitlement |
| `/transcribe` | POST | Pro | Audio transcription (Groq Whisper) |
| `/cleanup` | POST | Pro | Text cleanup (OpenAI GPT-4o-mini) |
| `/auth/exchange` | POST | No | Apple identity token exchange |
| `/webhooks/revenuecat` | POST | No | RevenueCat subscription webhooks |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `GROQ_API_KEY` | Groq API key for transcription |
| `OPENAI_API_KEY` | OpenAI API key for cleanup |
| `REDIS_URL` | Redis connection string |
| `APPLE_TEAM_ID` | Apple Developer Team ID |
| `APPLE_BUNDLE_ID` | Ditare macOS app bundle ID |
| `REVENUECAT_API_KEY` | RevenueCat REST API key (for entitlement checks) |
| `REVENUECAT_WEBHOOK_SECRET` | RevenueCat webhook secret |
| `JWT_SECRET` | HS256 secret for session tokens |
| `ENV` | `development` or `production` |

## Local Development

```bash
# Start Redis
docker run -d -p 6379:6379 redis:7-alpine

# Install dependencies
pip install -r requirements.txt

# Run server
uvicorn ditare_api.main:app --reload
```

## Deploy

```bash
fly deploy
```

## Smoke Test

```bash
curl https://api.ditare.app/healthz
```
