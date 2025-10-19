from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from .db import engine, Base, async_session
from .routes import admin, files
from .ws import websocket_endpoint
from .config import settings
from . import models  # noqa
from sqlalchemy import select, insert

app = FastAPI(title="Priority Streams Chat")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"], allow_credentials=True)

app.mount("/static", StaticFiles(directory=str(__file__).rsplit("/", 1)[0] + "/static"), name="static")

@app.get("/")
async def root_index():
    return FileResponse(str(__file__).rsplit("/", 1)[0] + "/static/index.html")

@app.get("/admin-ui")
async def admin_index():
    return FileResponse(str(__file__).rsplit("/", 1)[0] + "/static/admin.html")

@app.get("/health")
async def health():
    return {"ok": True}

@app.on_event("startup")
async def on_startup():
    # async with engine.begin() as conn:
    #     await conn.run_sync(Base.metadata.create_all)
    if settings.DEV_MODE:
        async with async_session() as s:
            from .models import User, Channel, ChannelParticipant
            for uid, name in ((1, "Alice"), (2, "Bob")):
                q = await s.execute(select(User).where(User.id == uid))
                if not q.scalar_one_or_none():
                    await s.execute(insert(User).values(id=uid, display_name=name, email=f"{name.lower()}@demo.local"))
            q = await s.execute(select(Channel).where(Channel.id == 1))
            if not q.scalar_one_or_none():
                await s.execute(insert(Channel).values(id=1, name="General", is_group=True))
            for uid in (1, 2):
                q = await s.execute(select(ChannelParticipant).where(ChannelParticipant.channel_id == 1, ChannelParticipant.user_id == uid))
                if not q.scalar_one_or_none():
                    await s.execute(insert(ChannelParticipant).values(channel_id=1, user_id=uid, role="member"))
            await s.commit()

app.include_router(admin.router)
app.include_router(files.router)

@app.websocket("/ws")
async def ws_route(ws: WebSocket):
    await websocket_endpoint(ws)
