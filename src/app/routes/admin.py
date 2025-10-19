import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, insert
from ..db import async_session
from ..models import User, Stream, PriorityPolicy, Channel, ChannelParticipant
from ..schemas import PriorityPolicyIn, PriorityPolicyOut, ChannelCreate

router = APIRouter(prefix="/admin", tags=["admin"])

async def get_db() -> AsyncSession:
    async with async_session() as s:
        yield s

# ---------- USERS ----------
@router.get("/users")
async def list_users(db: AsyncSession = Depends(get_db)):
    q = await db.execute(select(User))
    rows = q.scalars().all()
    return [
        {"id": u.id, "email": u.email, "display_name": u.display_name}
        for u in rows
    ]

@router.post("/users")
async def create_user(
    id: int | None = None,
    email: str | None = None,
    display_name: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    # дозволяємо явно задати id (зручно для демо)
    values = {"email": email, "display_name": display_name}
    if id is not None:
        values["id"] = id
    res = await db.execute(insert(User).values(**values).returning(User.id))
    new_id = res.scalar_one()
    await db.commit()
    q = await db.execute(select(User).where(User.id == new_id))
    u = q.scalar_one()
    return {"id": u.id, "email": u.email, "display_name": u.display_name}

# ---------- CHANNELS ----------
@router.get("/channels")
async def list_channels(db: AsyncSession = Depends(get_db)):
    q = await db.execute(select(Channel))
    rows = q.scalars().all()
    return [{"id": c.id, "name": c.name, "is_group": c.is_group} for c in rows]

@router.post("/channels")
async def create_channel(body: ChannelCreate, db: AsyncSession = Depends(get_db)):
    res = await db.execute(
        insert(Channel)
        .values(name=body.name, is_group=body.is_group)
        .returning(Channel.id)
    )
    cid = res.scalar_one()
    await db.commit()
    return {"id": cid, "name": body.name, "is_group": body.is_group}

# ---------- PARTICIPANTS ----------
@router.post("/channels/{channel_id}/participants")
async def add_participant(
    channel_id: int,
    user_id: int,
    role: str = "member",
    db: AsyncSession = Depends(get_db),
):
    # перевіряємо канал
    q = await db.execute(select(Channel).where(Channel.id == channel_id))
    if not q.scalar_one_or_none():
        raise HTTPException(404, "channel not found")

    # ГОЛОВНЕ: якщо користувача не існує — створюємо (щоб не падати на FK)
    uq = await db.execute(select(User).where(User.id == user_id))
    u = uq.scalar_one_or_none()
    if not u:
        await db.execute(
            insert(User).values(id=user_id, display_name=f"User {user_id}", email=None)
        )

    # додаємо учасника, якщо ще не доданий
    q2 = await db.execute(
        select(ChannelParticipant).where(
            ChannelParticipant.channel_id == channel_id,
            ChannelParticipant.user_id == user_id,
        )
    )
    if not q2.scalar_one_or_none():
        await db.execute(
            insert(ChannelParticipant).values(
                channel_id=channel_id, user_id=user_id, role=role
            )
        )
    await db.commit()
    return {"ok": True}

@router.get("/channels/{channel_id}/participants")
async def list_participants(channel_id: int, db: AsyncSession = Depends(get_db)):
    q = await db.execute(
        select(ChannelParticipant).where(ChannelParticipant.channel_id == channel_id)
    )
    rows = q.scalars().all()
    return [
        {"id": p.id, "channel_id": p.channel_id, "user_id": p.user_id, "role": p.role}
        for p in rows
    ]

# ---------- STREAMS & POLICIES ----------
@router.get("/streams/{channel_id}")
async def list_streams(channel_id: int, db: AsyncSession = Depends(get_db)):
    q = await db.execute(
        select(Stream, PriorityPolicy)
        .select_from(Stream)
        .outerjoin(PriorityPolicy, PriorityPolicy.stream_id == Stream.id)
        .where(Stream.channel_id == channel_id)
    )
    rows = q.all()
    out = []
    for s, p in rows:
        out.append(
            {
                "id": s.id,
                "channel_id": s.channel_id,
                "owner_user_id": s.owner_user_id,
                "policy": None
                if p is None
                else {
                    "id": p.id,
                    "msg_rate_rps": p.msg_rate_rps,
                    "upload_bps": p.upload_bps,
                    "download_bps": p.download_bps,
                    "burst": p.burst,
                    "enabled": p.enabled,
                    "updated_by": p.updated_by,
                    "updated_at": p.updated_at.isoformat(),
                },
            }
        )
    return out

@router.put("/streams/{stream_id}/policy")
async def upsert_policy(
    stream_id: int,
    body: PriorityPolicyIn,
    db: AsyncSession = Depends(get_db),
):
    q = await db.execute(
        select(PriorityPolicy).where(PriorityPolicy.stream_id == stream_id)
    )
    p = q.scalar_one_or_none()
    policy_data = {
        "stream_id": stream_id,
        "msg_rate_rps": body.msg_rate_rps,
        "upload_bps": body.upload_bps,
        "download_bps": body.download_bps,
        "burst": body.burst,
        "enabled": body.enabled,
        "updated_by": body.updated_by,
    }
    if p:
        await db.execute(
            update(PriorityPolicy).where(PriorityPolicy.id == p.id).values(**policy_data)
        )
        await db.commit()
        q2 = await db.execute(
            select(PriorityPolicy).where(PriorityPolicy.id == p.id)
        )
        row = q2.scalar_one()
        return PriorityPolicyOut(
            id=row.id,
            stream_id=row.stream_id,
            msg_rate_rps=row.msg_rate_rps,
            upload_bps=row.upload_bps,
            download_bps=row.download_bps,
            burst=row.burst,
            enabled=row.enabled,
            updated_at=row.updated_at,
        )
    else:
        res = await db.execute(
            insert(PriorityPolicy).values(**policy_data).returning(PriorityPolicy.id)
        )
        new_id = res.scalar_one()
        await db.commit()
        q2 = await db.execute(
            select(PriorityPolicy).where(PriorityPolicy.id == new_id)
        )
        row = q2.scalar_one()
        return PriorityPolicyOut(
            id=row.id,
            stream_id=row.stream_id,
            msg_rate_rps=row.msg_rate_rps,
            upload_bps=row.upload_bps,
            download_bps=row.download_bps,
            burst=row.burst,
            enabled=row.enabled,
            updated_by=row.updated_by,
            updated_at=row.updated_at,
        )


@router.post("/channels/{channel_id}/ensure_streams")
async def ensure_streams(channel_id: int, db: AsyncSession = Depends(get_db)):
    # перевіряємо, що канал існує
    q = await db.execute(select(Channel).where(Channel.id == channel_id))
    ch = q.scalar_one_or_none()
    if not ch:
        raise HTTPException(404, "channel not found")

    # всі учасники каналу
    qp = await db.execute(
        select(ChannelParticipant.user_id).where(ChannelParticipant.channel_id == channel_id)
    )
    user_ids = {row[0] for row in qp.all()}

    if not user_ids:
        return {"created": [], "already": []}

    # існуючі стріми для каналу
    qs = await db.execute(
        select(Stream.owner_user_id).where(Stream.channel_id == channel_id)
    )
    have = {row[0] for row in qs.all()}

    # кого бракує
    missing = sorted(user_ids - have)
    created_ids: list[int] = []

    for uid in missing:
        res = await db.execute(
            insert(Stream)
            .values(channel_id=channel_id, owner_user_id=uid)
            .returning(Stream.id)
        )
        created_ids.append(res.scalar_one())

    if created_ids:
        await db.commit()

    return {"created": created_ids, "already": sorted(have)}
