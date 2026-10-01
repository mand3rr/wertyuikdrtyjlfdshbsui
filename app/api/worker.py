import asyncio
import json
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger

from app.services.browser_login import open_browser_with_cookies

router = APIRouter()


class WorkerManager:
    def __init__(self):
        self._workers: dict[str, WebSocket] = {}

    def register(self, worker_id: str, ws: WebSocket):
        self._workers[worker_id] = ws
        logger.info(f"Worker registered: {worker_id} (total: {len(self._workers)})")

    def unregister(self, worker_id: str):
        self._workers.pop(worker_id, None)
        logger.info(f"Worker unregistered: {worker_id} (total: {len(self._workers)})")

    @property
    def count(self) -> int:
        return len(self._workers)

    async def dispatch(self, account: dict) -> dict[str, Any]:
        """Send a browser_open task to the first available worker and return its response."""
        if not self._workers:
            return {"status": "error", "message": "No workers connected. Friend needs to run worker.py"}

        worker_id, ws = next(iter(self._workers.items()))
        try:
            await ws.send_json({"type": "browser_open", "account": account})
            # Wait for response with a timeout
            response = await asyncio.wait_for(self._wait_response(ws), timeout=30.0)
            return response
        except asyncio.TimeoutError:
            return {"status": "error", "message": "Worker did not respond in time"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def _wait_response(self, ws: WebSocket) -> dict[str, Any]:
        while True:
            msg = await ws.receive_json()
            if msg.get("type") in ("browser_opened", "error"):
                return msg


worker_manager = WorkerManager()


@router.websocket("/api/ws/worker")
async def worker_websocket(ws: WebSocket):
    await ws.accept()
    # Each worker gets a unique ID based on its first message
    worker_id = None
    try:
        data = await ws.receive_json()
        worker_id = data.get("worker_id", f"worker-{id(ws)}")
        worker_manager.register(worker_id, ws)
        await ws.send_json({"type": "registered", "worker_id": worker_id})

        while True:
            msg = await ws.receive_json()
            # Worker can send status updates, we just log them
            logger.debug(f"Worker [{worker_id}]: {msg}")
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"Worker [{worker_id}] error: {e}")
    finally:
        if worker_id:
            worker_manager.unregister(worker_id)
