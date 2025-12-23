import os
import asyncio
import datetime
from contextlib import asynccontextmanager
from dotenv import load_dotenv

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from telethon import TelegramClient, events
from telethon.tl.functions.contacts import ImportContactsRequest
from telethon.tl.types import (
    InputPhoneContact,
    UserStatusOnline,
    UserStatusOffline,
    UpdateUserStatus
)

# ---------------- WINDOWS FIX ----------------
asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# ---------------- ENV ----------------
load_dotenv()

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_NAME = os.getenv("SESSION_NAME", "tracker")
TARGET_PHONE = os.getenv("TARGET_PHONE")
TARGET_NAME = os.getenv("TARGET_NAME", "User")

# ---------------- GLOBAL STATE ----------------
clients = set()

state = {
    "name": TARGET_NAME,
    "status": "CONNECTING",
    "last_seen": "--",
    "duration": ""
}

online_since = None
offline_task = None
OFFLINE_DELAY = 40  # seconds

# ---------------- BROADCAST ----------------
async def broadcast():
    for ws in list(clients):
        try:
            await ws.send_json(state)
        except Exception:
            clients.discard(ws)

# ---------------- DELAYED OFFLINE ----------------
async def delayed_offline():
    global online_since
    await asyncio.sleep(OFFLINE_DELAY)

    # Confirm real offline
    if online_since is None:
        now = datetime.datetime.utcnow()
        state["status"] = "OFFLINE"
        state["last_seen"] = now.strftime("%Y-%m-%d %H:%M:%S")
        state["duration"] = ""
        await broadcast()

# ---------------- TELEGRAM WORKER ----------------
async def telegram_worker():
    global online_since, offline_task

    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    await client.start()
    print("✅ Telegram connected")

    # Resolve user
    try:
        user = await client.get_entity(TARGET_PHONE)
    except Exception:
        contact = InputPhoneContact(0, TARGET_PHONE, TARGET_NAME, "")
        res = await client(ImportContactsRequest([contact]))
        user = res.users[0]

    user_id = user.id
    print(f"🎯 Tracking {TARGET_NAME}")

    # -------- INITIAL STATUS SYNC --------
    now = datetime.datetime.utcnow()

    if isinstance(user.status, UserStatusOnline):
        state["status"] = "ONLINE"
        online_since = now
        state["duration"] = ""
    else:
        state["status"] = "OFFLINE"
        if isinstance(user.status, UserStatusOffline) and user.status.was_online:
            state["last_seen"] = user.status.was_online.strftime("%Y-%m-%d %H:%M:%S")
        else:
            state["last_seen"] = "--"

    await broadcast()

    # -------- LIVE STATUS UPDATES --------
    @client.on(events.Raw)
    async def handler(event):
        global online_since, offline_task

        if not isinstance(event, UpdateUserStatus):
            return
        if event.user_id != user_id:
            return

        now = datetime.datetime.utcnow()

        # ---------- ONLINE (IMMEDIATE) ----------
        if isinstance(event.status, UserStatusOnline):
            if offline_task:
                offline_task.cancel()
                offline_task = None

            if state["status"] != "ONLINE":
                state["status"] = "ONLINE"
                online_since = now
                state["duration"] = ""
                await broadcast()

        # ---------- OFFLINE (DELAYED 40s) ----------
        elif isinstance(event.status, UserStatusOffline):
            online_since = None

            if offline_task:
                offline_task.cancel()

            offline_task = asyncio.create_task(delayed_offline())

    await client.run_until_disconnected()

# ---------------- FASTAPI LIFESPAN ----------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(telegram_worker())
    yield
    task.cancel()

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")

# ---------------- WEBSOCKET ----------------
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    await ws.send_json(state)

    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        clients.discard(ws)

# ---------------- WEB PAGE ----------------
@app.get("/")
async def index():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())
