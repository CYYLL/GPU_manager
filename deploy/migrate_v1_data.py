#!/usr/bin/env python3
"""v1 → v2 container-data migration.

Copies the *container inventory* tracked by the v1 DB (/amax/gpu_manager) into
the v2 DB (/amax/gpu_manager_v2), matching v2's schema. This is the "data
half" of the v1→v2 cutover — run it BEFORE switching nginx/systemd so users
see their existing containers on v2 immediately.

What it migrates (v1 → v2):
  - gpu_images        : preset image rows (v2 starts empty; create/rebuild UI needs them)
  - container_instances : all 15 inventory rows; id/user_id/container_id preserved
  - gpu_allocations   : ONLY currently-active allocations (released_at IS NULL)
                        — historical released rows are v1's audit trail, not carried over

What it does NOT do:
  - users: already present in v2 with identical id/quota/role (verified == on all 12)
  - chat_messages / cleanup_logs / container_events / disk_snapshots / admin_alerts:
    operational history, intentionally fresh on v2
  - It never touches /amax/gpu_manager (read-only source), and in --apply backs up
    the v2 DB before the first write.

The docker containers themselves already exist on this host; v2 only needs the DB
rows to show ownership. `container_id` is the full docker sha (docker = source of
truth for running/stopped via /api/containers reconcile-on-read).

Usage:
  /opt/anaconda3/bin/python deploy/migrate_v1_data.py            # dry-run preview
  /opt/anaconda3/bin/python deploy/migrate_v1_data.py --apply    # backup + write

Idempotent: rows whose container_id already exists in v2 are skipped, so a
partial/duplicate run is safe.
"""
import argparse
import datetime
import os
import shutil
import sqlite3
import sys

V1_DB = "/amax/gpu_manager/backend/gpu_resource_manager.db"
V2_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "backend", "gpu_resource_manager.db")
BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backup")

# Columns as defined in backend/app/models.py (v2) — explicit INSERT so the
# differing v1/v2 column order is irrelevant.
V2_CONTAINER_COLS = [
    "id", "user_id", "container_id", "image", "gpu_ids", "gpu_count",
    "status", "cpu_limit", "memory_limit", "assigned_port", "access_password",
    "created_at", "started_at", "stopped_at", "env_vars", "cleanup_protected",
]
V2_ALLOC_COLS = ["id", "gpu_id", "container_instance_id", "user_id",
                 "allocated_at", "released_at"]
V2_IMAGE_COLS = ["id", "name", "image", "description", "min_gpu",
                 "recommended_gpu", "created_by", "created_at"]


_PATHS = {}  # id(connection) -> path, for error messages


def db(path):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    _PATHS[id(c)] = path
    return c


def col_superset(c, table, needed):
    have = {r["name"] for r in c.execute("PRAGMA table_info(%s)" % table)}
    missing = [n for n in needed if n not in have]
    if missing:
        raise SystemExit("ERROR: %s.%s missing columns %s (unexpected schema?)"
                         % (_PATHS.get(id(c), "?"), table, missing))
    return True


def now():
    return datetime.datetime.utcnow().isoformat(sep=" ", timespec="milliseconds")


def load_container_rows(c1):
    rows = c1.execute("SELECT * FROM container_instances ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def load_active_alloc_rows(c1):
    rows = c1.execute(
        "SELECT * FROM gpu_allocations WHERE released_at IS NULL ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def load_image_rows(c1):
    return [dict(r) for r in c1.execute("SELECT * FROM gpu_images ORDER BY id")]


def main():
    ap = argparse.ArgumentParser(description="v1→v2 container data migration")
    ap.add_argument("--apply", action="store_true",
                    help="write to v2 (default is read-only dry-run). "
                         "Backs up the v2 DB first.")
    ap.add_argument("--v1", default=V1_DB)
    ap.add_argument("--v2", default=V2_DB)
    args = ap.parse_args()

    v1_exists = os.path.exists(args.v1)
    v2_exists = os.path.exists(args.v2)
    if not v1_exists:
        raise SystemExit("v1 DB not found: %s" % args.v1)
    if not v2_exists:
        raise SystemExit("v2 DB not found: %s (start v2 once so it creates its schema)" % args.v2)

    c1 = db(args.v1)
    c2 = db(args.v2)

    col_superset(c1, "users", ["id", "username"])
    col_superset(c1, "container_instances", [c for c in V2_CONTAINER_COLS if c != "env_vars" and c != "cleanup_protected"])
    col_superset(c2, "users", ["id", "username"])
    col_superset(c2, "container_instances", V2_CONTAINER_COLS)
    col_superset(c2, "gpu_allocations", V2_ALLOC_COLS)
    col_superset(c2, "gpu_images", V2_IMAGE_COLS)

    # ---- users parity report ----
    v1_users = {r["id"]: r["username"] for r in c1.execute("SELECT id,username FROM users")}
    v2_users = {r["id"]: r["username"] for r in c2.execute("SELECT id,username FROM users")}
    v1_owning = {r["user_id"] for r in c1.execute("SELECT DISTINCT user_id FROM container_instances")}
    print("== 用户对照 ==")
    print("  v1 users=%d  v2 users=%d" % (len(v1_users), len(v2_users)))
    for uid in sorted(v1_owning):
        mark = "== 存在" if uid in v2_users else "!! v2 缺该 user_id"
        print("    容器归属 user_id=%d (%s)  %s" % (uid, v1_users.get(uid, "?"), mark))

    # ---- inventory to insert ----
    crows = load_container_rows(c1)
    arows = load_active_alloc_rows(c1)
    irows = load_image_rows(c1)

    existing_cids = {r["container_id"] for r in c2.execute("SELECT container_id FROM container_instances")}
    new_cids = []
    for r in crows:
        cid = r["container_id"]
        if cid in existing_cids:
            print("  [skip] container_id %s already in v2" % cid[:12])
        else:
            new_cids.append(r)

    new_alloc = []
    existing_cinst = {r["id"] for r in c2.execute("SELECT id FROM container_instances")}
    for r in arows:
        if r["container_instance_id"] in existing_cinst or any(
                n["id"] == r["container_instance_id"] for n in new_cids):
            new_alloc.append(r)
        else:
            print("  [skip alloc] container_instance_id=%d not migrating" % r["container_instance_id"])

    new_images = []
    existing_img = {r["image"] for r in c2.execute("SELECT image FROM gpu_images")}
    for r in irows:
        if r["image"] in existing_img:
            print("  [skip image] %s already in v2" % r["image"])
        else:
            new_images.append(r)

    # ---- report ----
    print("\n== 迁移预览 ==")
    print("  gpu_images      : %d 条待迁" % len(new_images))
    for r in new_images:
        print("      id=%s %-14s -> %s" % (r["id"], r["name"], r["image"]))
    print("  container_instances: %d / %d 条待迁" % (len(new_cids), len(crows)))
    running = [r for r in new_cids if r["status"] == "running"]
    stopped = [r for r in new_cids if r["status"] != "running"]
    print("    其中 running=%d  stopped=%d" % (len(running), len(stopped)))
    for r in new_cids:
        print("      #%-3s %-10s %-8s gpu=%-12s port=%-6s img=%s" % (
            r["id"], v2_users.get(r["user_id"], v1_users.get(r["user_id"], "?")),
            r["status"], r["gpu_ids"], r["assigned_port"] or "-", r["image"]))
    print("  gpu_allocations : %d 条待迁(仅 active)" % len(new_alloc))
    for r in new_alloc:
        print("      gpu_id=%s -> container_instance_id=%s (user=%s)" % (
            r["gpu_id"], r["container_instance_id"],
            v2_users.get(r["user_id"], "?")))

    # ---- consequences / warnings ----
    print("\n== 注意 ==")
    print("  - v2 清理引擎只删除 stopped 容器(运行中一律跳过)。迁入的 %d 个 stopped 容器"
          "将成为清理候选;若需保护,请用 UI/API 设 cleanup_protected,或迁移后处理。"
          % len(stopped))
    if not args.apply:
        print("\n(dry-run,未写入。加 --apply 执行;执行前会自动备份 v2 DB)")

    if not args.apply:
        return 0

    # ---- write ----
    ts = now().replace(" ", "_").replace(":", "-")
    os.makedirs(BACKUP_DIR, exist_ok=True)
    backup_path = os.path.join(BACKUP_DIR, "gpu_resource_manager.db.%s" % ts)
    shutil.copy2(args.v2, backup_path)
    print("\n==> 已备份 v2 DB -> %s" % backup_path)

    try:
        for r in new_images:
            c2.execute("INSERT INTO gpu_images (%s) VALUES (%s)" % (
                ",".join(V2_IMAGE_COLS),
                ",".join("?" * len(V2_IMAGE_COLS))),
                [r[c] for c in V2_IMAGE_COLS])
        for r in new_cids:
            # v1 has no env_vars/cleanup_protected columns → use v2 defaults
            # (env_vars={} so a rebuild reproduces "no extra env", matching v1)
            vals = []
            for c in V2_CONTAINER_COLS:
                if c == "env_vars":
                    vals.append(r.get("env_vars", "{}"))
                elif c == "cleanup_protected":
                    vals.append(0)
                else:
                    vals.append(r[c])
            c2.execute("INSERT INTO container_instances (%s) VALUES (%s)" % (
                ",".join(V2_CONTAINER_COLS),
                ",".join("?" * len(V2_CONTAINER_COLS))), vals)
        for r in new_alloc:
            c2.execute("INSERT INTO gpu_allocations (%s) VALUES (%s)" % (
                ",".join(V2_ALLOC_COLS),
                ",".join("?" * len(V2_ALLOC_COLS))),
                [r[c] for c in V2_ALLOC_COLS])
        c2.commit()
    except Exception:
        c2.rollback()
        print("ERROR: write failed, rolled back. v2 DB untouched (pre-write backup still saved).")
        raise

    print("\n==> 迁移完成: images=%d containers=%d allocs=%d"
          % (len(new_images), len(new_cids), len(new_alloc)))
    print("    验证: 重启 :8000 后端后 GET /api/containers(用任意用户登录)应能看到容器。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
