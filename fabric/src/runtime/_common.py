"""Helpers shared by the runtime domain routers."""

from fastapi import HTTPException


async def _or_404(value, what: str):
    """Return `value`, or raise a plain-JSON 404 when the upstream had no such record."""
    if value is None:
        raise HTTPException(status_code=404, detail=f"{what} not found")
    return value
