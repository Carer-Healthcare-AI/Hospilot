import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from config import settings
from api.routes.auth import require_auth

router = APIRouter()


@router.post("/simulation/{topic}")
async def proxy_simulation(topic: str, request: Request, _user: dict = Depends(require_auth)):
    if not settings.sim_base_url:
        raise HTTPException(status_code=503, detail="simulation service not configured (SIM_BASE_URL)")

    body = await request.json()
    url = f"{settings.sim_base_url}/simulation/{topic}"

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.post(url, json=body)
        except httpx.ConnectTimeout:
            raise HTTPException(status_code=504, detail="simulation service timeout")
        except httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail=f"simulation service unreachable: {exc}")

    return JSONResponse(content=resp.json(), status_code=resp.status_code)
