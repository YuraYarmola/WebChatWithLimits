import os
import asyncio
import time
from starlette.requests import Request
from fastapi import APIRouter, UploadFile, File, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import insert, select, update
from ..db import async_session
from ..models import Attachment, Message, Stream, ChannelParticipant, PriorityPolicy
from ..config import settings
from ..rate_limiter import TokenBucket
import redis.asyncio as redis
from fastapi.responses import StreamingResponse
import aiofiles
from ..ws import manager, get_or_create_stream

router = APIRouter(prefix="/files", tags=["files"])

async def get_db() -> AsyncSession:
    async with async_session() as s:
        yield s

async def get_redis() -> redis.Redis:
    return redis.from_url(settings.REDIS_URL)

async def ensure_member(db: AsyncSession, channel_id: int, user_id: int) -> bool:
    q = await db.execute(select(ChannelParticipant).where(ChannelParticipant.channel_id == channel_id, ChannelParticipant.user_id == user_id))
    return q.scalar_one_or_none() is not None

@router.put("/upload_raw")
async def upload_raw(
    request: Request,
    channel_id: int,
    user_id: int,
    filename: str,
    size: int,
    content_type: str = "application/octet-stream",
    db: AsyncSession = Depends(get_db),
):
    if not await ensure_member(db, channel_id, user_id):
        raise HTTPException(403, "not a channel member")

    stream_id = await get_or_create_stream(db, channel_id, user_id)

    # політика аплоаду
    q = await db.execute(
        select(PriorityPolicy).where(
            PriorityPolicy.stream_id == stream_id,
            PriorityPolicy.enabled == True
        )
    )
    p = q.scalar_one_or_none()
    upload_bps = int(p.upload_bps) if p else int(settings.DEFAULT_UPLOAD_BPS)
    burst_cap  = max(1, int(upload_bps * 2))  # місткість бакета

    # placeholder повідомлення
    res_msg = await db.execute(
        insert(Message).values(
            channel_id=channel_id,
            stream_id=stream_id,
            sender_id=user_id,
            content=None,
            meta={"kind": "file", "file_name": filename}
        ).returning(Message.id)
    )
    message_id = res_msg.scalar_one()
    await db.commit()

    r = await get_redis()
    limiter = TokenBucket(r)

    save_dir = settings.UPLOAD_DIR
    os.makedirs(save_dir, exist_ok=True)
    dest_path = os.path.join(save_dir, f"{message_id}_{filename}")

    total = 0
    window = 0
    t0 = time.monotonic()
    last_emit = t0

    # дрібні рівні «тіки» ~50/сек
    tick_hz = 50.0
    tick_bytes = max(1024, int(upload_bps / tick_hz))
    tick_bytes = min(tick_bytes, 64 * 1024, burst_cap)

    async with aiofiles.open(dest_path, "wb") as out:
        async for chunk in request.stream():
            if not chunk:
                continue
            mv = memoryview(chunk)
            off = 0
            while off < len(mv):
                want = min(len(mv) - off, tick_bytes)
                granted = await limiter.grant(
                    TokenBucket.key(stream_id, "up"),
                    rate=float(upload_bps),
                    capacity=float(burst_cap),
                    want=float(want),
                )
                if granted > 0:
                    await out.write(mv[off:off + granted])
                    off += granted
                    total += granted
                    window += granted

                    now = time.monotonic()
                    if now - last_emit >= 0.5:
                        bps = window / max(1e-6, (now - last_emit))
                        elapsed = now - t0
                        await manager.send_user(user_id, {
                            "type": "file.upload.progress",
                            "message_id": message_id,
                            "bytes": total,
                            "total": size,
                            "bps": int(bps),
                            "elapsed_ms": int(elapsed * 1000),
                        })
                        last_emit = now
                        window = 0
                else:
                    # віддати керування петлі, без видимих фризів
                    await asyncio.sleep(0.005)

    # фінальний прогрес 100%
    now = time.monotonic()
    elapsed = now - t0
    try:
        await manager.send_user(user_id, {
            "type": "file.upload.progress",
            "message_id": message_id,
            "bytes": total,
            "total": size,
            "bps": int(total / max(1e-6, elapsed)),
            "elapsed_ms": int(elapsed * 1000),
        })
    except Exception:
        pass

    # створюємо attachment
    res_att = await db.execute(
        insert(Attachment).values(
            message_id=message_id,
            file_name=filename,
            content_type=content_type,
            size=total,
            storage_path=dest_path
        ).returning(Attachment.id)
    )
    att_id = res_att.scalar_one()

    # оновити meta + сигнал у канал
    await db.execute(
        update(Message).where(Message.id == message_id).values(
            meta={"kind": "file", "attachment_id": att_id, "file_name": filename, "size": total}
        )
    )
    await db.commit()

    await manager.broadcast_channel(channel_id, {
        "type": "message.new",
        "message": {
            "id": message_id,
            "channel_id": channel_id,
            "stream_id": stream_id,
            "sender_id": user_id,
            "content": None,
            "meta": {"kind":"file","attachment_id": att_id,"file_name": filename,"size": total}
        }
    })

    await r.close()
    return {"attachment_id": att_id, "message_id": message_id, "size": total}




@router.post("/upload")
async def upload_file(channel_id: int, user_id: int, file: UploadFile = File(...), db: AsyncSession = Depends(get_db)):
    if not await ensure_member(db, channel_id, user_id):
        raise HTTPException(403, "not a channel member")
    res_stream = await db.execute(select(Stream).where(Stream.channel_id == channel_id, Stream.owner_user_id == user_id))
    st = res_stream.scalar_one_or_none()
    if not st:
        res_new = await db.execute(insert(Stream).values(channel_id=channel_id, owner_user_id=user_id).returning(Stream.id))
        stream_id = res_new.scalar_one()
    else:
        stream_id = st.id
    res_msg = await db.execute(insert(Message).values(channel_id=channel_id, stream_id=stream_id, sender_id=user_id, content=None, meta={"kind": "file"}).returning(Message.id))
    message_id = res_msg.scalar_one()
    await db.commit()

    r = await get_redis()
    limiter = TokenBucket(r)
    save_dir = settings.UPLOAD_DIR
    os.makedirs(save_dir, exist_ok=True)
    dest_path = os.path.join(save_dir, f"{message_id}_{file.filename}")

    q = await db.execute(select(PriorityPolicy).where(PriorityPolicy.stream_id == stream_id, PriorityPolicy.enabled == True))
    p = q.scalar_one_or_none()
    upload_bps = p.upload_bps if p else settings.DEFAULT_UPLOAD_BPS

    total = 0
    chunk = 64 * 1024
    # before the loop
    t0 = time.monotonic()
    last_emit = t0
    window_bytes = 0

    async with aiofiles.open(dest_path, "wb") as out:
        while True:
            data = await file.read(chunk)
            if not data:
                break

            cost = len(data)
            allowed = await limiter.allow(
                TokenBucket.key(stream_id, "up"),
                rate=float(upload_bps),
                capacity=float(upload_bps * 2),
                cost=float(cost)
            )
            if not allowed:
                await asyncio.sleep(max(0.01, chunk / max(1.0, upload_bps)))
                # (опціонально повторна перевірка, якщо хочеш)

            await out.write(data)
            total += cost
            window_bytes += cost

            now = time.monotonic()
            # надсилаємо прогрес ~2 рази/сек
            if now - last_emit >= 0.5:
                # середня швидкість за вікно
                bps = window_bytes / max(1e-6, (now - last_emit))
                elapsed = now - t0
                await manager.send_user(user_id, {
                    "type": "file.upload.progress",
                    "message_id": message_id,
                    "bytes": total,
                    "bps": int(bps),
                    "elapsed_ms": int(elapsed * 1000),
                })
                last_emit = now
                window_bytes = 0

    res_att = await db.execute(insert(Attachment).values(
        message_id=message_id,
        file_name=file.filename,
        content_type=file.content_type or "application/octet-stream",
        size=total,
        storage_path=dest_path
    ).returning(Attachment.id))
    att_id = res_att.scalar_one()

    # 2) оновили meta повідомлення
    await db.execute(update(Message).where(Message.id == message_id).values(
        meta={"kind": "file", "attachment_id": att_id, "file_name": file.filename, "size": total}
    ))
    await db.commit()

    # 3) розіслали в канал chat-подію
    await manager.broadcast_channel(channel_id, {
        "type": "message.new",
        "message": {
            "id": message_id,
            "channel_id": channel_id,
            "stream_id": stream_id,
            "sender_id": user_id,
            "content": None,
            "meta": {"kind": "file", "attachment_id": att_id, "file_name": file.filename, "size": total}
        }
    })
    # 4) повернули відповідачу
    return {"attachment_id": att_id, "message_id": message_id, "size": total}

@router.get("/{attachment_id}/download")
async def download_file(attachment_id: int, user_id: int, db: AsyncSession = Depends(get_db)):
    # 1) знайти вкладення і перевірити доступ
    q = await db.execute(
        select(Attachment, Message)
        .join(Message, Message.id == Attachment.message_id)
        .where(Attachment.id == attachment_id)
    )
    row = q.first()
    if not row:
        raise HTTPException(404, "not found")
    att, msg = row
    if not await ensure_member(db, msg.channel_id, user_id):
        raise HTTPException(403, "forbidden")

    # 2) створити/знайти stream ДЛЯ ПОТОЧНОГО КОРИСТУВАЧА (ХТО КАЧАЄ)
    res_stream = await db.execute(
        select(Stream).where(
            Stream.channel_id == msg.channel_id,
            Stream.owner_user_id == user_id
        )
    )
    dst = res_stream.scalar_one_or_none()
    if not dst:
        dst_id = (await db.execute(
            insert(Stream)
            .values(channel_id=msg.channel_id, owner_user_id=user_id)
            .returning(Stream.id)
        )).scalar_one()
    else:
        dst_id = dst.id

    # 3) витягнути ПОЛІТИКУ ДЛЯ ОТРИМУВАЧА (саме його ліміт застосовується)
    q_dst = await db.execute(
        select(PriorityPolicy).where(
            PriorityPolicy.stream_id == dst_id,
            PriorityPolicy.enabled == True
        )
    )
    p_dst = q_dst.scalar_one_or_none()
    dst_bps = int(p_dst.download_bps) if p_dst else int(settings.DEFAULT_DOWNLOAD_BPS)

    # 4) токен-бакет лише для отримувача
    r = await get_redis()
    limiter_dst = TokenBucket(r)

    # параметри плавної подачі
    tick_hz = 50.0                      # ~50 тiків/сек
    file_chunk = 64 * 1024
    burst_cap = max(1, int(dst_bps * 2))
    tick_bytes = max(1024, int(dst_bps / tick_hz))
    tick_bytes = min(tick_bytes, file_chunk, burst_cap)
    prime_bytes = 16 * 1024             # миттєвий старт у браузері

    async def streamer():
        try:
            async with aiofiles.open(att.storage_path, "rb") as f:
                # миттєво віддаємо трохи даних (з урахуванням токенів отримувача)
                first = await f.read(prime_bytes)
                if first:
                    g = await limiter_dst.grant(
                        TokenBucket.key(dst_id, "down"),
                        rate=float(dst_bps),
                        capacity=float(burst_cap),
                        want=float(len(first)),
                    )
                    if g > 0:
                        yield bytes(first[:g])
                        await asyncio.sleep(0)  # віддати керування петлі
                    remain = first[g:]
                else:
                    remain = b""

                # дозлив залишок "first"
                mv = memoryview(remain)
                off = 0
                while off < len(mv):
                    want = min(len(mv) - off, tick_bytes)
                    g = await limiter_dst.grant(
                        TokenBucket.key(dst_id, "down"),
                        rate=float(dst_bps),
                        capacity=float(burst_cap),
                        want=float(want),
                    )
                    if g > 0:
                        yield mv[off:off + g]
                        off += g
                        await asyncio.sleep(0)
                    else:
                        await asyncio.sleep(0.005)

                # основний цикл
                while True:
                    data = await f.read(file_chunk)
                    if not data:
                        break
                    mv = memoryview(data)
                    off = 0
                    while off < len(mv):
                        want = min(len(mv) - off, tick_bytes)
                        g = await limiter_dst.grant(
                            TokenBucket.key(dst_id, "down"),
                            rate=float(dst_bps),
                            capacity=float(burst_cap),
                            want=float(want),
                        )
                        if g > 0:
                            yield mv[off:off + g]
                            off += g
                            await asyncio.sleep(0)
                        else:
                            await asyncio.sleep(0.005)
        finally:
            await r.close()

    headers = {
        "Content-Type": att.content_type or "application/octet-stream",
        "Content-Length": str(att.size),
        "Content-Disposition": f'attachment; filename="{att.file_name}"',
        "Cache-Control": "no-store, no-transform",
        "X-Accel-Buffering": "no",  # якщо стоїть Nginx — вимкнути буферизацію
    }
    return StreamingResponse(streamer(), headers=headers)
