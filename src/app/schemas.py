from datetime import datetime

from pydantic import BaseModel, Field
from typing import Optional, Any

class MessageIn(BaseModel):
    channel_id: int
    content: Optional[str] = None
    parent_message_id: Optional[int] = None
    meta: dict[str, Any] = Field(default_factory=dict)

class MessageOut(BaseModel):
    id: int
    channel_id: int
    stream_id: int
    sender_id: int
    content: Optional[str]
    meta: dict

class PriorityPolicyIn(BaseModel):
    msg_rate_rps: int
    upload_bps: int
    download_bps: int
    burst: int = 10
    enabled: bool = True
    updated_by: Optional[int] = None   # was str

class PriorityPolicyOut(PriorityPolicyIn):
    id: int
    stream_id: int
    updated_at: datetime

class ChannelCreate(BaseModel):
    name: str
    is_group: bool = True