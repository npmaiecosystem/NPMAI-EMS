"""
The mobile/desktop app (Layers 8/9) calls these on login/logout and
whenever FCM issues a fresh token (which happens periodically - apps
must not assume a token is permanent). Kept as a FastAPI router, same
mount pattern as app/layer5/chat_service.py.
"""
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

router = APIRouter()


class RegisterTokenRequest(BaseModel):
    fcm_token: str = Field(..., min_length=1)
    building_id: str = Field(..., min_length=1)
    user_id: str = Field(..., min_length=1)
    platform: str = Field(..., pattern="^(android|ios|desktop_windows|desktop_mac|desktop_linux)$")


class UnregisterTokenRequest(BaseModel):
    fcm_token: str = Field(..., min_length=1)


def get_pool(request: Request):
    return request.app.state.db_pool


@router.post("/layer6/device-tokens/register", status_code=status.HTTP_200_OK)
async def register_token(payload: RegisterTokenRequest, pool=Depends(get_pool)):
    # ON CONFLICT: the SAME token re-registering (e.g. app reopened,
    # same install) just refreshes last_seen_at/is_active - a token that
    # was previously deactivated (TOKEN_INVALID from a failed send) gets
    # correctly reactivated if the same physical token somehow becomes
    # valid again (rare, but re-registration is the right recovery path
    # either way, not a reason to reject the request).
    await pool.execute(
        """
        INSERT INTO device_tokens (fcm_token, building_id, user_id, platform, is_active, last_seen_at)
        VALUES ($1, $2, $3, $4, TRUE, now())
        ON CONFLICT (fcm_token) DO UPDATE SET
            building_id = EXCLUDED.building_id,
            user_id = EXCLUDED.user_id,
            platform = EXCLUDED.platform,
            is_active = TRUE,
            last_seen_at = now()
        """,
        payload.fcm_token, payload.building_id, payload.user_id, payload.platform,
    )
    return {"status": "registered"}


@router.post("/layer6/device-tokens/unregister", status_code=status.HTTP_200_OK)
async def unregister_token(payload: UnregisterTokenRequest, pool=Depends(get_pool)):
    result = await pool.execute(
        "UPDATE device_tokens SET is_active = FALSE WHERE fcm_token = $1", payload.fcm_token
    )
    if result == "UPDATE 0":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Token not found")
    return {"status": "unregistered"}
