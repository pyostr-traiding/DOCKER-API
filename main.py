# =============================================================================
# FastAPI WebSocket backend for Docker dashboard
# -----------------------------------------------------------------------------
# Версия 4.2 — добавлена простая авторизация по статичному токену.
# =============================================================================
import json
import os

import psutil
import docker
import asyncio

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, status

# == Конфигурация =============================================================
load_dotenv()
ACCESS_TOKEN = os.getenv('ACCESS_TOKEN')  # ← замени потом на значение из nFile

app = FastAPI(title="Docker WebSocket API", version="4.2")
client = docker.from_env()


# == Helpers ==================================================================

def sys_snapshot():
    """Единый формат метрик системы."""
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk = psutil.disk_usage("/")
    cpu = psutil.cpu_percent(interval=None)
    return {
        "cpu_percent": cpu,
        "memory": {
            "total_mb": round(mem.total / 1024 / 1024, 2),
            "used_mb": round(mem.used / 1024 / 1024, 2),
            "available_mb": round(mem.available / 1024 / 1024, 2),
            "percent_used": mem.percent,
        },
        "swap": {
            "total_mb": round(swap.total / 1024 / 1024, 2),
            "used_mb": round(swap.used / 1024 / 1024, 2),
            "percent_used": round((swap.used / swap.total * 100), 2) if swap.total else 0.0
        },
        "disk": {
            "total_gb": round(disk.total / 1024 / 1024 / 1024, 2),
            "used_gb": round(disk.used / 1024 / 1024 / 1024, 2),
            "free_gb": round(disk.free / 1024 / 1024 / 1024, 2),
            "percent_used": disk.percent,
        },
    }


def brief_container(c: docker.models.containers.Container):
    """Сокращённое описание контейнера для плиток."""
    ports = c.attrs.get("NetworkSettings", {}).get("Ports", {})
    return {
        "id": c.short_id,
        "name": c.name,
        "status": c.status,
        "image": c.image.tags,
        "ports": ports
    }


# == Background: periodic updates ============================================

async def stream_updates(websocket: WebSocket, interval: int = 1):
    """Отправляет обновления по системе и контейнерам каждые interval сек."""
    try:
        while True:
            containers = client.containers.list(all=True)
            payload = {
                "type": "update",
                "containers": [brief_container(c) for c in containers],
                "system": sys_snapshot(),
            }
            await websocket.send_text(json.dumps(payload))
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print("stream_updates stopped:", e)


# == Background: logs streaming ==============================================

async def _async_iter(iterator):
    """Преобразует обычный итератор в асинхронный."""
    loop = asyncio.get_event_loop()
    while True:
        try:
            item = await loop.run_in_executor(None, next, iterator)
        except StopIteration:
            break
        yield item


async def stream_container_logs(websocket: WebSocket, container_id: str):
    """Отправляет логи контейнера в реальном времени (docker logs -f)."""
    try:
        container = client.containers.get(container_id)
        log_stream = container.logs(stream=True, follow=True, tail=10)

        async for log_line in _async_iter(log_stream):
            text = log_line.decode("utf-8", errors="ignore").strip()
            if text:
                await websocket.send_text(
                    json.dumps({"type": "log_line", "container": container.name, "data": text})
                )
    except docker.errors.NotFound:
        await websocket.send_text(json.dumps({"error": f"Container '{container_id}' not found", "data": {"id": container_id}}))
    except asyncio.CancelledError:
        pass
    except Exception as e:
        await websocket.send_text(json.dumps({"error": f"Log stream error: {str(e)}"}))


# == Main WS endpoint =========================================================

@app.websocket("/ws")
async def websocket_docker(websocket: WebSocket):
    # --- Проверка токена ---
    token = websocket.query_params.get("token")
    if token != ACCESS_TOKEN:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        print("❌ Unauthorized WebSocket connection attempt.")
        return

    print("✅ Authorized WebSocket connection.")
    await websocket.accept()

    update_task = asyncio.create_task(stream_updates(websocket))
    log_task = None

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_text(json.dumps({"error": "Invalid JSON"}))
                continue

            action = msg.get("action")
            args = msg.get("data", {})

            # --- list ---
            if action == "list_containers":
                containers = client.containers.list(all=True)
                data = [brief_container(c) for c in containers]
                await websocket.send_text(json.dumps({"type": "containers", "data": data}))

            # --- get info ---
            elif action == "get_container":
                cid = args.get("id")
                try:
                    c = client.containers.get(cid)
                    attrs = c.attrs or {}
                    ports = attrs.get("NetworkSettings", {}).get("Ports", {})
                    mounts = attrs.get("Mounts", [])
                    created = attrs.get("Created")
                    data = {
                        "id": c.short_id,
                        "name": c.name,
                        "status": c.status,
                        "image": c.image.tags,
                        "created": created,
                        "ports": ports,
                        "mounts": mounts
                    }
                    await websocket.send_text(json.dumps({"type": "container_info", "data": data}))
                except docker.errors.NotFound:
                    await websocket.send_text(json.dumps({"error": f"Container '{cid}' not found", "data": {"id": cid}}))

            # --- restart ---
            elif action == "restart":
                cid = args.get("id")
                try:
                    c = client.containers.get(cid)
                    await websocket.send_text(json.dumps({"type": "status", "data": {"id": cid, "action": "restarting"}}))
                    c.restart()
                    await websocket.send_text(json.dumps({"message": f"Container '{c.name}' restarted", "data": {"id": cid}}))
                except docker.errors.NotFound:
                    await websocket.send_text(json.dumps({"error": f"Container '{cid}' not found", "data": {"id": cid}}))

            # --- start ---
            elif action == "start":
                cid = args.get("id")
                try:
                    c = client.containers.get(cid)
                    await websocket.send_text(json.dumps({"type": "status", "data": {"id": cid, "action": "starting"}}))
                    c.start()
                    await websocket.send_text(json.dumps({"message": f"Container '{c.name}' started", "data": {"id": cid}}))
                except docker.errors.NotFound:
                    await websocket.send_text(json.dumps({"error": f"Container '{cid}' not found", "data": {"id": cid}}))

            # --- stop ---
            elif action == "stop":
                cid = args.get("id")
                try:
                    c = client.containers.get(cid)
                    await websocket.send_text(json.dumps({"type": "status", "data": {"id": cid, "action": "stopping"}}))
                    c.stop()
                    await websocket.send_text(json.dumps({"message": f"Container '{c.name}' stopped", "data": {"id": cid}}))
                except docker.errors.NotFound:
                    await websocket.send_text(json.dumps({"error": f"Container '{cid}' not found", "data": {"id": cid}}))

            # --- remove ---
            elif action == "remove":
                cid = args.get("id")
                try:
                    c = client.containers.get(cid)
                    await websocket.send_text(json.dumps({"type": "status", "data": {"id": cid, "action": "removing"}}))
                    c.remove(force=True)
                    await websocket.send_text(json.dumps({"message": f"Container '{c.name}' removed", "data": {"id": cid}}))
                except docker.errors.NotFound:
                    await websocket.send_text(json.dumps({"error": f"Container '{cid}' not found", "data": {"id": cid}}))

            # --- logs (tail) ---
            elif action == "logs":
                cid = args.get("id")
                tail = int(args.get("tail", 50))
                try:
                    c = client.containers.get(cid)
                    logs = c.logs(tail=tail).decode("utf-8", errors="ignore").splitlines()
                    await websocket.send_text(json.dumps({"type": "logs", "container": c.name, "data": logs}))
                except docker.errors.NotFound:
                    await websocket.send_text(json.dumps({"error": f"Container '{cid}' not found", "data": {"id": cid}}))

            # --- logs stream ---
            elif action == "stream_logs":
                cid = args.get("id")
                try:
                    if log_task:
                        log_task.cancel()
                    client.containers.get(cid)  # валидация
                    log_task = asyncio.create_task(stream_container_logs(websocket, cid))
                    await websocket.send_text(json.dumps({"message": f"Started streaming logs for '{cid}'", "data": {"id": cid}}))
                except docker.errors.NotFound:
                    await websocket.send_text(json.dumps({"error": f"Container '{cid}' not found", "data": {"id": cid}}))

            # --- stop stream ---
            elif action == "stop_stream":
                if log_task:
                    log_task.cancel()
                    log_task = None
                    await websocket.send_text(json.dumps({"message": "Log stream stopped"}))
                else:
                    await websocket.send_text(json.dumps({"message": "No active log stream"}))
            # --- FULL RESTART ---
            elif action == "full_restart":
                order = [
                    "bybit-stream",
                    "streaming-candle-bybit",
                    "admin-panel",
                    "panel-celery-tasks",
                    "panel-beat",
                    "signals-btc-1",
                    "monitoring",
                    "combinator"
                ]

                async def restart_one(name):
                    try:
                        container = client.containers.get(name)
                        await websocket.send_text(json.dumps({"type": "fr_step", "data": f"Stopping {name}…"}))
                        container.stop()
                        await asyncio.sleep(1)

                        await websocket.send_text(json.dumps({"type": "fr_step", "data": f"Starting {name}…"}))
                        container.start()
                        await asyncio.sleep(1)

                        await websocket.send_text(json.dumps({"type": "fr_step", "data": f"{name} restarted"}))
                    except Exception as e:
                        await websocket.send_text(json.dumps({"error": f"Failed {name}: {e}"}))

                # запускаем фоновой задачей
                async def full_restart():
                    await websocket.send_text(json.dumps({"type": "fr_begin"}))

                    for name in order:
                        await restart_one(name)

                    await websocket.send_text(json.dumps({"type": "fr_done"}))

                asyncio.create_task(full_restart())

            # --- system resources ---
            elif action == "system_resources":
                stats = sys_snapshot()
                await websocket.send_text(json.dumps({"type": "system", "data": stats}))

            else:
                await websocket.send_text(json.dumps({"error": f"Unknown action: {action}"}))

    except WebSocketDisconnect:
        print("Client disconnected")

    finally:
        if log_task:
            log_task.cancel()
        update_task.cancel()
        print("All tasks stopped")
