from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import os
from os.path import dirname, abspath, join

from dotenv import load_dotenv

# ── Project root (derived from backend/app/main.py → project root) ──
_BACKEND_DIR = dirname(dirname(abspath(__file__)))  # backend/
PROJECT_ROOT = dirname(_BACKEND_DIR)  # project root (parent of backend/)

# ── Load .env from project root BEFORE importing the app package, since
#    module-level constructors (agent.llm_client.LLMClient, tools.DockerRunner)
#    read env vars at import time. ──
load_dotenv(join(PROJECT_ROOT, ".env"))

# ── App logging: give every `app.*` logger an INFO StreamHandler to stderr.
#    Without this, Python's default config drops INFO records entirely (only a
#    WARNING+ lastResort reaches journal), so agent run health would be silent.
#    uvicorn keeps its own access/error handlers (propagate off), so nothing
#    double-prints. Must run BEFORE `.routers` imports so constructor-time
#    logs (agent client selection, etc.) are captured too. ──
import logging
import sys


def _configure_logging():
    lg = logging.getLogger("app")
    lg.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
    if not lg.handlers:  # idempotent across reloads
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"))
        lg.addHandler(handler)
    lg.propagate = False  # handled here; never bubble to root


_configure_logging()

from .database import engine, Base
from sqlalchemy import inspect, text
from .routers import users, gpus, containers, mode, agent, monitor
from .services.host_ip import get_host_ip

# Create all tables
Base.metadata.create_all(bind=engine)
if "ssh_username" not in {column["name"] for column in inspect(engine).get_columns("container_instances")}:
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE container_instances ADD COLUMN ssh_username VARCHAR(64)"))
from .services.image_presets import migrate_legacy_presets
migrate_legacy_presets()

app = FastAPI(title="GPU Resource Manager", version="2.0.0")


@app.get("/api/host-ip")
def host_ip():
    return {"host_ip": get_host_ip()}

cors_origins = os.getenv("CORS_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(users.router)
app.include_router(gpus.router)
app.include_router(containers.router)
app.include_router(mode.router)
app.include_router(agent.router)
app.include_router(monitor.router)


# Serve frontend production build
FRONTEND_BUILD = os.path.join(PROJECT_ROOT, "frontend", "build")
INDEX_HTML = os.path.join(FRONTEND_BUILD, "index.html")


if os.path.isdir(FRONTEND_BUILD):
    # Serve static assets (JS, CSS, images, etc.)
    app.mount("/static", StaticFiles(directory=os.path.join(FRONTEND_BUILD, "static")), name="static")

    # SPA catch-all: serve index.html for all non-API, non-static routes
    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        # Don't intercept API routes (they're handled by routers above)
        return FileResponse(INDEX_HTML)


@app.on_event("startup")
def cleanup_stale_containers():
    """On startup: release stale allocations from power loss / crash."""
    try:
        from .database import SessionLocal
        from . import models
        from datetime import datetime
        from .services.docker_runner import DockerRunner

        db = SessionLocal()
        docker = DockerRunner()
        now = datetime.utcnow()

        stale = db.query(models.ContainerInstance).filter(
            models.ContainerInstance.status == "running"
        ).all()

        cleaned = 0
        for inst in stale:
            if docker.is_container_running(inst.container_id) is False:
                db.query(models.GpuAllocation).filter(
                    models.GpuAllocation.container_instance_id == inst.id,
                    models.GpuAllocation.released_at.is_(None)
                ).update({"released_at": now})
                inst.status = "stopped"
                inst.stopped_at = now
                cleaned += 1

        db.commit()
        db.close()
        if cleaned:
            print(f"Startup cleanup: released {cleaned} stale container(s) after restart")
    except Exception as e:
        print(f"Startup cleanup warning (non-fatal): {e}")

    # Start background monitor (hourly disk snapshot / retention). Non-fatal.
    try:
        from .agent.monitor import start_monitor_thread
        start_monitor_thread()
    except Exception as e:
        print(f"Monitor thread start warning (non-fatal): {e}")

    # Start idle-GPU auto-stop thread (every ~30 min). Non-fatal.
    try:
        from .agent.idle_gpu import start_idle_gpu_thread
        start_idle_gpu_thread()
    except Exception as e:
        print(f"Idle-GPU thread start warning (non-fatal): {e}")

    try:
        from .agent.ssh_watchdog import start_ssh_watchdog_thread
        start_ssh_watchdog_thread()
    except Exception as e:
        print(f"SSH watchdog start warning (non-fatal): {e}")

    try:
        from .agent.gpu_conflict_monitor import start_gpu_conflict_thread
        start_gpu_conflict_thread()
    except Exception as e:
        print(f"GPU conflict monitor start warning (non-fatal): {e}")
