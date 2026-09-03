from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import os
from os.path import dirname, abspath, join

from dotenv import load_dotenv

from .database import engine, Base
from .routers import users, gpus, containers, mode, agent

# ── Project root (derived from backend/app/main.py → project root) ──
_BACKEND_DIR = dirname(dirname(abspath(__file__)))  # backend/
PROJECT_ROOT = dirname(_BACKEND_DIR)  # project root (parent of backend/)

# ── Load .env from project root ──
load_dotenv(join(PROJECT_ROOT, ".env"))

# Create all tables
Base.metadata.create_all(bind=engine)

app = FastAPI(title="GPU Resource Manager", version="2.0.0")

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
            if not docker.is_container_running(inst.container_id):
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
