"""
The interactive chat console backend - a FastAPI router meant to be
mounted into a small dedicated app (or into an existing gateway), kept
separate from Server/app/main.py (Layer 2's ingestion app) for the same
reason Layer 3/4/5's processing loops are separate processes: chat
requests are user-facing and latency-sensitive in a DIFFERENT way than
ingestion (a slow LLM call blocking ingestion would be bad; equally, a
burst of chat traffic should never be able to starve MQTT/HTTPS
telemetry handling).
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.config import settings
from app.layer5.grounding_check import check_grounding
from app.layer5.prompt_templates import CHAT_SYSTEM_PROMPT
from app.layer5.retrieval import build_chat_context, context_to_prompt_json
from app.llm.base import LLMError
from app.llm.registry import generate_with_failover
from app.llm.usage_tracker import SpendCapExceeded, check_spend_cap, record_usage

logger = logging.getLogger(__name__)
router = APIRouter()


class ChatRequest(BaseModel):
    building_id: str = Field(..., min_length=1)
    question: str = Field(..., min_length=1, max_length=2000)


class ChatResponse(BaseModel):
    answer: str
    grounded: bool
    provider_used: str


def get_pool(request: Request):
    return request.app.state.db_pool


@router.post("/layer5/chat", response_model=ChatResponse)
async def chat(payload: ChatRequest, pool=Depends(get_pool)):
    try:
        await check_spend_cap(pool, payload.building_id)
    except SpendCapExceeded as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc))

    context = await build_chat_context(pool, payload.building_id, payload.question)
    context_json = context_to_prompt_json(context)
    known_ids = {j["panel_id"] for j in context.joints} | {j["breaker_id"] for j in context.joints}

    try:
        response = await generate_with_failover(
            system_prompt=CHAT_SYSTEM_PROMPT,
            user_prompt=f"Relevant joint data:\n{context_json}\n\nQuestion: {payload.question}",
            max_output_tokens=settings.layer5_max_output_tokens,
        )
    except LLMError as exc:
        logger.exception("All LLM providers failed for chat request, building=%s", payload.building_id)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "No LLM provider currently available - try again shortly") from exc

    await record_usage(pool, payload.building_id, purpose="chat_console", response=response)

    grounding = check_grounding(response.text, known_ids)
    if not grounding.is_grounded:
        logger.warning(
            "Chat response for building=%s referenced ungrounded IDs %s - flagged, not blocked "
            "(see app/layer5/grounding_check.py's module docstring for why)",
            payload.building_id, grounding.ungrounded_ids,
        )

    return ChatResponse(answer=response.text, grounded=grounding.is_grounded, provider_used=response.provider)
