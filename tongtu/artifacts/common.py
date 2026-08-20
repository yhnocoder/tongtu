from __future__ import annotations

from pydantic import BaseModel


class Manifest(BaseModel):
    status: str
    warnings: list[str] = []
    message: str = ""
