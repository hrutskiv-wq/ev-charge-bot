import json
import logging
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from app.database import connection

logger = logging.getLogger(__name__)

OCPI_SUCCESS_CODE = 1000
CALLBACK_SUCCESS_MESSAGE = "CDR already processed"

class CDRRequest(BaseModel):
    id: str = Field(..., min_length=1)
    session_id: str = Field(..., min_length=1)
    auth_id: int = Field(..., gt=0)
    total_energy: float = Field(default=0.0, ge=0.0)
    total_cost: float = Field(default=0.0, ge=0.0)

    @field_validator("total_energy", "total_cost", mode="before")
    @classmethod
    def parse_float(cls, v):
        return float(v) if isinstance(v, str) else v

class OCPIResponse(BaseModel):
    status_code: int
    status_message: str
    timestamp: str

    @staticmethod
    def create(status_code: int = OCPI_SUCCESS_CODE, status_message: str = "Success") -> dict:
        return {
            "status_code": status_code,
            "status_message": status_message,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        }

async def receive_cdr(cdr: CDRRequest):
    if not connection.db_pool:
        raise HTTPException(status_code=500, detail="DB error")
    
    cdr_data = cdr.model_dump()
    async with connection.db_pool.acquire() as conn:
        async with conn.transaction():
            exists = await conn.fetchval("SELECT id FROM ocpi_cdrs WHERE cdr_id = $1", cdr_data["id"])
            if exists:
                return OCPIResponse.create(status_message=CALLBACK_SUCCESS_MESSAGE)

            await conn.execute(
                "INSERT INTO ocpi_cdrs (cdr_id, user_id, session_id, total_energy, total_cost, raw_payload) VALUES ($1, $2, $3, $4, $5, $6)",
                cdr_data["id"], cdr_data["auth_id"], cdr_data["session_id"], cdr_data["total_energy"], cdr_data["total_cost"], json.dumps(cdr_data)
            )
            
            await conn.execute(
                "INSERT INTO kw_transactions (user_id, type, amount, session_id, description) VALUES ($1, $2, $3, $4, $5)",
                cdr_data["auth_id"], "withdrawal", -cdr_data["total_cost"], cdr_data["session_id"], "Charge"
            )
            
    return OCPIResponse.create()
