from __future__ import annotations

import datetime as dt
import uuid

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from livekit.api import AccessToken, VideoGrants
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    livekit_api_key: str = Field(alias="LIVEKIT_API_KEY")
    livekit_api_secret: str = Field(alias="LIVEKIT_API_SECRET")
    livekit_public_url: str = Field(alias="LIVEKIT_PUBLIC_URL")
    token_server_port: int = Field(default=8080, alias="TOKEN_SERVER_PORT")
    token_ttl_minutes: int = Field(default=60, alias="TOKEN_TTL_MINUTES")
    token_cors_origins: str = Field(
        default="http://localhost:3000,http://localhost:5173",
        alias="TOKEN_CORS_ORIGINS",
    )

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.token_cors_origins.split(",") if origin.strip()]


settings = Settings()

app = FastAPI(title="Nusuk Token Server", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/token")
async def create_token(
    room: str | None = Query(default=None),
    identity: str | None = Query(default=None),
) -> dict[str, str]:
    room_name = room or f"room-{uuid.uuid4().hex[:8]}"
    participant_identity = identity or f"user-{uuid.uuid4().hex[:8]}"

    try:
        token = (
            AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
            .with_identity(participant_identity)
            .with_name(participant_identity)
            .with_ttl(dt.timedelta(minutes=settings.token_ttl_minutes))
            .with_grants(
                VideoGrants(
                    room_join=True,
                    room=room_name,
                    can_publish=True,
                    can_subscribe=True,
                    can_publish_data=True,
                )
            )
            .to_jwt()
        )
    except Exception as exc:  # pragma: no cover - simple API surface
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {
        "token": token,
        "url": settings.livekit_public_url,
        "room": room_name,
        "identity": participant_identity,
    }
