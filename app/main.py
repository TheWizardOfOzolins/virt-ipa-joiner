import asyncio
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from prometheus_fastapi_instrumentator import Instrumentator

from app.routers import webhook
from app.services.k8s import run_controller
from app.config import logger
from app.task_tracker import cancel_all_tasks
from app.health import is_ipa_healthy

# Holds the controller task so probes can inspect its state.
_controller_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _controller_task
    version = os.getenv("APP_VERSION", "unknown")
    logger.info(f"Starting virt-ipa-joiner controller version: {version}")

    _controller_task = asyncio.create_task(run_controller())

    yield

    # --- Shutdown ---
    logger.info("Shutting down virt-ipa-joiner...")
    _controller_task.cancel()
    try:
        await _controller_task
    except asyncio.CancelledError:
        pass

    # Cancel any in-flight keytab pollers / delayed event senders.
    await cancel_all_tasks()


app = FastAPI(lifespan=lifespan)

# Expose /metrics for Prometheus scraping.
Instrumentator().instrument(app).expose(app)


@app.get("/healthz")
async def healthz():
    """Liveness probe — fails if the controller task has exited unexpectedly."""
    if _controller_task is None or _controller_task.done():
        raise HTTPException(status_code=503, detail="controller task has exited")
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    """Readiness probe — fails if the controller is not running or IPA is unreachable."""
    if _controller_task is None or _controller_task.done():
        raise HTTPException(status_code=503, detail="controller not running")
    if not is_ipa_healthy():
        raise HTTPException(status_code=503, detail="IPA is unreachable")
    return {"status": "ready"}


app.include_router(webhook.router)
