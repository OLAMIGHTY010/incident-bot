import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import v4_async_monitor

# Keep access logs enabled for debugging
logging.getLogger("uvicorn.access").setLevel(logging.INFO)

# ── WebSocket Manager ──────────────────────────────────────────────────────
class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except Exception:
                pass

manager = ConnectionManager()

import threading

uvicorn_loop = None

def dashboard_logger(msg, bot_type="both"):
    try:
        if uvicorn_loop and not uvicorn_loop.is_closed():
            msg_type = f"log_{bot_type}" if bot_type in ["ack", "fill"] else "log"
            asyncio.run_coroutine_threadsafe(
                manager.broadcast(json.dumps({"type": msg_type, "message": msg})), 
                uvicorn_loop
            )
    except Exception:
        pass

v4_async_monitor.log_callback = dashboard_logger

def dashboard_metric(key, val):
    try:
        if uvicorn_loop and not uvicorn_loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                manager.broadcast(json.dumps({"type": "metric", "key": key, "value": val})), 
                uvicorn_loop
            )
    except Exception:
        pass

v4_async_monitor.metric_callback = dashboard_metric

def dashboard_unknown_incident(key):
    try:
        if uvicorn_loop and not uvicorn_loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                manager.broadcast(json.dumps({"type": "unknown_incident", "key": key})), 
                uvicorn_loop
            )
    except Exception:
        pass

v4_async_monitor.unknown_incident_callback = dashboard_unknown_incident

# ── Global State ───────────────────────────────────────────────────────────
bot_thread_ack = None
bot_thread_fill = None

def run_bot_ack_thread():
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        while True:
            try:
                loop.run_until_complete(v4_async_monitor.main_ack())
            except Exception as e:
                import traceback
                tb = traceback.format_exc()
                print(tb)
                dashboard_logger(f"[red]Acknowledger Crashed. Auto-restarting in 5s...\n{e}[/red]", "ack")
                import time
                time.sleep(5)
    finally:
        if uvicorn_loop and not uvicorn_loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                manager.broadcast(json.dumps({"type": "status_ack", "status": "stopped"})),
                uvicorn_loop
            )
        loop.close()

def run_bot_fill_thread():
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        while True:
            try:
                loop.run_until_complete(v4_async_monitor.main_fill())
            except Exception as e:
                import traceback
                tb = traceback.format_exc()
                print(tb)
                dashboard_logger(f"[red]Filler Crashed. Auto-restarting in 5s...\n{e}[/red]", "fill")
                import time
                time.sleep(5)
    finally:
        if uvicorn_loop and not uvicorn_loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                manager.broadcast(json.dumps({"type": "status_fill", "status": "stopped"})),
                uvicorn_loop
            )
        loop.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global uvicorn_loop
    uvicorn_loop = asyncio.get_running_loop()
    yield


app = FastAPI(lifespan=lifespan)

# Mount static folder for frontend files
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/")
async def get_index():
    index_file = static_dir / "index.html"
    if not index_file.exists():
        return HTMLResponse("<h1>Static files not found!</h1>")
    return HTMLResponse(index_file.read_text(encoding="utf-8"))


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

@app.post("/api/start_ack")
async def start_ack_bot():
    global bot_thread_ack
    if bot_thread_ack and bot_thread_ack.is_alive():
        return {"status": "already_running"}
    
    bot_thread_ack = threading.Thread(target=run_bot_ack_thread, daemon=True)
    bot_thread_ack.start()
    
    await manager.broadcast(json.dumps({"type": "status_ack", "status": "running"}))
    return {"status": "started"}

@app.post("/api/start_fill")
async def start_fill_bot():
    global bot_thread_fill
    if bot_thread_fill and bot_thread_fill.is_alive():
        return {"status": "already_running"}
    
    bot_thread_fill = threading.Thread(target=run_bot_fill_thread, daemon=True)
    bot_thread_fill.start()
    
    await manager.broadcast(json.dumps({"type": "status_fill", "status": "running"}))
    return {"status": "started"}

@app.post("/api/stop_ack")
async def stop_ack_bot():
    await manager.broadcast(json.dumps({"type": "status_ack", "status": "stopped"}))
    return {"status": "stopped"}

@app.post("/api/stop_fill")
async def stop_fill_bot():
    await manager.broadcast(json.dumps({"type": "status_fill", "status": "stopped"}))
    return {"status": "stopped"}


@app.get("/api/status")
async def bot_status():
    ack_running = bot_thread_ack is not None and bot_thread_ack.is_alive()
    fill_running = bot_thread_fill is not None and bot_thread_fill.is_alive()
    return {
        "status_ack": "running" if ack_running else "stopped",
        "status_fill": "running" if fill_running else "stopped"
    }


@app.get("/api/memory")
async def get_memory():
    if v4_async_monitor.MEMORY_FILE.exists():
        try:
            data = json.loads(v4_async_monitor.MEMORY_FILE.read_text(encoding="utf-8"))
            return data
        except Exception:
            return {}
    return {}

class MemoryUpdateRequest(BaseModel):
    key: str
    priority: str
    rc_description: str
    rc_category: str
    rc_responsibility: str

@app.post("/api/memory")
async def update_memory(req: MemoryUpdateRequest):
    import datetime
    new_data = {
        "priority": req.priority,
        "rc_description": req.rc_description,
        "rc_category": req.rc_category,
        "rc_responsibility": req.rc_responsibility,
        "saved_at": datetime.datetime.now().isoformat(),
        "user_provided": True
    }
    await v4_async_monitor.save_to_memory({req.key: new_data})
    return {"status": "success"}

class MemoryDeleteRequest(BaseModel):
    key: str

@app.post("/api/memory/delete")
async def delete_memory(req: MemoryDeleteRequest):
    async with v4_async_monitor.memory_lock:
        if v4_async_monitor.MEMORY_FILE.exists():
            data = json.loads(v4_async_monitor.MEMORY_FILE.read_text(encoding="utf-8"))
            if req.key in data:
                del data[req.key]
                v4_async_monitor.MEMORY_FILE.write_text(json.dumps(data, indent=4))
    return {"status": "success"}

class ApiKeyRequest(BaseModel):
    key: str

@app.get("/api/key")
async def get_api_key():
    import os
    from dotenv import load_dotenv
    load_dotenv()
    return {"key": os.environ.get("OPENROUTER_API_KEY", "")}

@app.post("/api/key")
async def set_api_key(req: ApiKeyRequest):
    import os
    env_path = Path(".env")
    lines = []
    found = False
    if env_path.exists():
        lines = env_path.read_text().splitlines()
        for i, line in enumerate(lines):
            if line.startswith("OPENROUTER_API_KEY="):
                lines[i] = f"OPENROUTER_API_KEY={req.key}"
                found = True
                break
    if not found:
        lines.append(f"OPENROUTER_API_KEY={req.key}")
    
    env_path.write_text("\n".join(lines) + "\n")
    os.environ["OPENROUTER_API_KEY"] = req.key
    return {"status": "success"}

if __name__ == "__main__":
    import uvicorn
    print("Starting dashboard server on http://127.0.0.1:8000")
    uvicorn.run("dashboard:app", host="127.0.0.1", port=8000, reload=True)
