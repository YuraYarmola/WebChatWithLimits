import json
from typing import Dict, Set

from fastapi import WebSocket, WebSocketDisconnect
from sqlalchemy import select, insert
from sqlalchemy.ext.asyncio import AsyncSession
from .db import async_session
from .models import ChannelParticipant, Stream, Message, PriorityPolicy
from .schemas import MessageIn
from .config import settings
from .rate_limiter import TokenBucket
import redis.asyncio as redis

class ConnectionManager:
    def __init__(self):
        # user_id -> {WebSocket, ...}
        self.user_sockets: Dict[int, Set[WebSocket]] = {}
        # channel_id -> {user_id, ...}
        self.channel_subs: Dict[int, Set[int]] = {}
        # user_id -> {channel_id, ...}  (для чистого відписування)
        self.user_channels: Dict[int, Set[int]] = {}
        # ws -> user_id (для коректного disconnect)
        self.ws_to_user: Dict[WebSocket, int] = {}

    async def connect(self, user_id: int, websocket: WebSocket):
        await websocket.accept()
        self.user_sockets.setdefault(user_id, set()).add(websocket)
        self.ws_to_user[websocket] = user_id

    def disconnect(self, websocket: WebSocket):
        user_id = self.ws_to_user.pop(websocket, None)
        if user_id is None:
            return

        # прибираємо сокет користувача
        sockets = self.user_sockets.get(user_id)
        if sockets:
            sockets.discard(websocket)
            if not sockets:
                # якщо не лишилося сокетів цього user — виписуємо з усіх каналів
                for ch in list(self.user_channels.get(user_id, set())):
                    subs = self.channel_subs.get(ch)
                    if subs:
                        subs.discard(user_id)
                        if not subs:
                            self.channel_subs.pop(ch, None)
                self.user_channels.pop(user_id, None)
                self.user_sockets.pop(user_id, None)

    def join_channel(self, user_id: int, channel_id: int):
        self.channel_subs.setdefault(channel_id, set()).add(user_id)
        self.user_channels.setdefault(user_id, set()).add(channel_id)

    def leave_channel(self, user_id: int, channel_id: int):
        subs = self.channel_subs.get(channel_id)
        if subs:
            subs.discard(user_id)
            if not subs:
                self.channel_subs.pop(channel_id, None)
        uc = self.user_channels.get(user_id)
        if uc:
            uc.discard(channel_id)
            if not uc and not self.user_sockets.get(user_id):
                self.user_channels.pop(user_id, None)

    async def send_user(self, user_id: int, payload: dict):
        data = json.dumps(payload)
        for ws in list(self.user_sockets.get(user_id, ())):
            try:
                await ws.send_text(data)
            except Exception:
                # прибрати мертвий сокет
                self.disconnect(ws)

    async def broadcast_channel(self, channel_id: int, payload: dict):
        users = self.channel_subs.get(channel_id, set())
        data = json.dumps(payload)
        for uid in list(users):
            for ws in list(self.user_sockets.get(uid, ())):
                try:
                    await ws.send_text(data)
                except Exception:
                    self.disconnect(ws)

manager = ConnectionManager()

async def get_redis() -> redis.Redis:
    return redis.from_url(settings.REDIS_URL)

async def get_or_create_stream(session: AsyncSession, channel_id: int, owner_user_id: int) -> int:
    q = await session.execute(select(Stream).where(Stream.channel_id == channel_id, Stream.owner_user_id == owner_user_id))
    st = q.scalar_one_or_none()
    if st:
        return st.id
    res = await session.execute(insert(Stream).values(channel_id=channel_id, owner_user_id=owner_user_id).returning(Stream.id))
    st_id = res.scalar_one()
    await session.commit()
    return st_id

async def ensure_member(session: AsyncSession, channel_id: int, user_id: int) -> bool:
    q = await session.execute(select(ChannelParticipant).where(ChannelParticipant.channel_id == channel_id, ChannelParticipant.user_id == user_id))
    return q.scalar_one_or_none() is not None

async def load_policy(session: AsyncSession, stream_id: int):
    q = await session.execute(select(PriorityPolicy).where(PriorityPolicy.stream_id == stream_id, PriorityPolicy.enabled == True))
    p = q.scalar_one_or_none()
    if p:
        return p.msg_rate_rps, p.upload_bps, p.download_bps, p.burst
    return (
        settings.DEFAULT_MSG_RATE_RPS,
        settings.DEFAULT_UPLOAD_BPS,
        settings.DEFAULT_DOWNLOAD_BPS,
        settings.DEFAULT_BURST,
    )

async def websocket_endpoint(websocket: WebSocket):
    user_id = int(websocket.query_params.get("user_id"))
    await manager.connect(user_id, websocket)  # <— нове
    r = await get_redis()
    limiter = TokenBucket(r)
    async with async_session() as db:
        try:
            while True:
                msg = await websocket.receive_text()
                data = json.loads(msg)
                action = data.get("action")

                if action == "join_channel":
                    channel_id = int(data["channel_id"])
                    if not await ensure_member(db, channel_id, user_id):
                        await websocket.send_text(json.dumps({"type": "error", "error": "not_member"}))
                        continue
                    manager.join_channel(user_id, channel_id)  # <— нове
                    await websocket.send_text(json.dumps({"type": "joined", "channel_id": channel_id}))

                elif action == "leave_channel":
                    channel_id = int(data["channel_id"])
                    manager.leave_channel(user_id, channel_id)  # <— нове
                    await websocket.send_text(json.dumps({"type": "left", "channel_id": channel_id}))

                elif action == "send_message":
                    payload = MessageIn(**data["payload"])
                    if not await ensure_member(db, payload.channel_id, user_id):
                        await websocket.send_text(json.dumps({"type": "error", "error": "not_member"}))
                        continue
                    stream_id = await get_or_create_stream(db, payload.channel_id, user_id)
                    msg_rate, up_bps, down_bps, burst = await load_policy(db, stream_id)
                    allowed = await limiter.allow(TokenBucket.key(stream_id, "msgs"),
                                                  rate=float(msg_rate), capacity=float(burst), cost=1.0)
                    if not allowed:
                        await websocket.send_text(json.dumps({"type": "throttled", "reason": "msg_rate"}))
                        continue

                    res = await db.execute(
                        insert(Message).values(
                            channel_id=payload.channel_id,
                            stream_id=stream_id,
                            sender_id=user_id,
                            parent_message_id=payload.parent_message_id,
                            content=payload.content,
                            meta=payload.meta,
                        ).returning(Message.id)
                    )
                    new_id = res.scalar_one()
                    await db.commit()

                    out = {
                        "type": "message.new",
                        "message": {
                            "id": new_id,
                            "channel_id": payload.channel_id,
                            "stream_id": stream_id,
                            "sender_id": user_id,
                            "content": payload.content,
                            "meta": payload.meta,
                        }
                    }
                    await manager.broadcast_channel(payload.channel_id, out)
                else:
                    await websocket.send_text(json.dumps({"type": "error", "error": "unknown_action"}))
        except WebSocketDisconnect:
            pass
        finally:
            manager.disconnect(websocket)   # <— важливо: повне прибирання
            await r.close()