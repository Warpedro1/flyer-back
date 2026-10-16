"""Errors seen by the app (#33), recorded in `app_errors` (#25 phase 1).

Only with a session (decision 2026-10-16): errors before login stay in the browser
console. The row has the user as a hash and the text without personal data.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status

from app.api.deps import get_current_user
from app.core.limiter import limiter
from app.models.schemas import ClientErrorIn
from app.services import error_log

router = APIRouter(prefix="/telemetry", tags=["telemetry"])


@router.post("/client-error", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("10/minute")
async def report_client_error(
    request: Request,
    body: ClientErrorIn,
    _user_id: Annotated[str, Depends(get_current_user)],
) -> Response:
    await error_log.record_error(
        source="client",
        code=body.code or "client_error",
        message=body.message,
        route=body.route,
        details={"api_request_id": body.request_id, "stack": body.stack},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
