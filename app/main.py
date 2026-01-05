import asyncio
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.routers import webhook
from app.services.k8s import run_controller
from app.config import logger


@asynccontextmanager
async def lifespan(app: FastAPI):
    version = os.getenv("APP_VERSION", "unknown")
    logger.info(f"Starting virt-joiner controller version: {version}")

    # Start the background controller
    controller_task = asyncio.create_task(run_controller())

    yield  # Application runs here

    # Shutdown logic
    logger.info("Shutting down virt-joiner...")
    controller_task.cancel()
    try:
        await controller_task
    except asyncio.CancelledError:
        pass

app = FastAPI(lifespan=lifespan)

@app.get("/healthz")
async def healthz():
    """
    Simple health check for Kubernetes Liveness/Readiness probes.
    """
    return {"status": "ok"}

# Register the router
app.include_router(webhook.router)
