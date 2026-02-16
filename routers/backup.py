"""Backup and restore for ContextEngine."""

import json
import shutil
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List
from fastapi import APIRouter
from pydantic import BaseModel
from config import DATA_DIR, SESSIONS_DIR
from services import kb_gateway, chromadb_client, minio_client
from utils.logging_ import logger

router = APIRouter()


class RestoreRequest(BaseModel):
    backup_name: str
    components: List[str] = None


BACKUP_DIR = DATA_DIR / "backups"
MAX_BACKUPS = 10


def _backup_path(timestamp: str = None) -> Path:
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    return BACKUP_DIR / timestamp


def _prune_old_backups():
    if not BACKUP_DIR.exists(): return
    for old in sorted(BACKUP_DIR.iterdir(), key=lambda p: p.name, reverse=True)[MAX_BACKUPS:]:
        try: shutil.rmtree(old)
        except: pass


@router.get("/api/backup/list")
async def list_backups():
    if not BACKUP_DIR.exists(): return {"backups": [], "count": 0}
    backups = []
    for bp in sorted(BACKUP_DIR.iterdir(), key=lambda p: p.name, reverse=True):
        if not bp.is_dir(): continue
        meta = {}
        meta_file = bp / "metadata.json"
        if meta_file.exists():
            try: meta = json.loads(meta_file.read_text())
            except: pass
        backups.append({"name": bp.name, "timestamp": meta.get("timestamp", bp.name), "size_bytes": meta.get("total_size_bytes", 0), "components": meta.get("components", []), "location": "local"})
    remote = minio_client.list_remote_backups()
    local_names = {b["name"] for b in backups}
    for b in backups:
        b["location"] = "local+minio" if b["name"] in {r["name"] for r in remote} else "local"
    for r in remote:
        if r["name"] not in local_names: backups.append(r)
    return {"backups": backups, "count": len(backups), "minio_available": minio_client.is_available()}


@router.post("/api/backup/create")
async def create_backup(include_sessions: bool = False):
    ts = datetime.now(timezone.utc)
    ts_str = ts.strftime("%Y-%m-%d_%H%M%S")
    bp = _backup_path(ts_str)
    bp.mkdir(parents=True, exist_ok=True)
    components = []
    total_size = 0
    try:
        mc = kb_gateway.read_master_context()
        if mc:
            (bp / "master-context.md").write_text(mc, encoding="utf-8")
            components.append("master_context")
            total_size += len(mc.encode())
    except: pass
    for name in ["nudges.json", "anomalies.json"]:
        try:
            src = DATA_DIR / name
            if src.exists():
                shutil.copy2(src, bp / name)
                components.append(name.replace(".json", ""))
                total_size += src.stat().st_size
        except: pass
    try:
        client = chromadb_client.get_chromadb()
        export = {}
        for col_name in ["sessions", "project_archive", "decisions", "failures", "entities", "patterns"]:
            try:
                col = client.get_collection(col_name)
                if col.count() > 0:
                    data = col.get(include=["documents", "metadatas"])
                    export[col_name] = {"count": col.count(), "ids": data["ids"], "documents": data["documents"], "metadatas": data["metadatas"]}
            except: pass
        if export:
            content = json.dumps(export, indent=2, default=str)
            (bp / "chromadb-export.json").write_text(content, encoding="utf-8")
            components.append("chromadb")
            total_size += len(content.encode())
    except: pass
    if include_sessions:
        try:
            sd = bp / "sessions"
            sd.mkdir(exist_ok=True)
            sfs = list(SESSIONS_DIR.glob("*.json"))
            for sf in sfs:
                shutil.copy2(sf, sd / sf.name)
                total_size += sf.stat().st_size
            components.append(f"sessions ({len(sfs)} files)")
        except: pass
    (bp / "metadata.json").write_text(json.dumps({"timestamp": ts.isoformat(), "name": ts_str, "components": components, "total_size_bytes": total_size}, indent=2))
    _prune_old_backups()
    minio_result = minio_client.upload_backup(bp, ts_str)
    return {"success": True, "backup_name": ts_str, "components": components, "total_size_bytes": total_size, "minio": minio_result}


@router.post("/api/backup/restore")
async def restore_backup(request: RestoreRequest):
    bp = _backup_path(request.backup_name)
    if not bp.exists():
        dl = minio_client.download_backup(request.backup_name, bp)
        if not dl.get("success"): return {"error": f"Backup '{request.backup_name}' not found"}
    components = request.components or ["master_context", "nudges", "anomalies", "chromadb"]
    restored = []
    if "master_context" in components:
        mc_file = bp / "master-context.md"
        if mc_file.exists():
            try:
                kb_gateway.write_master_context(mc_file.read_text(encoding="utf-8"), f"Restore from {request.backup_name}")
                restored.append("master_context")
            except: pass
    for name in ["nudges", "anomalies"]:
        if name in components:
            src = bp / f"{name}.json"
            if src.exists():
                try: shutil.copy2(src, DATA_DIR / f"{name}.json"); restored.append(name)
                except: pass
    if "chromadb" in components:
        ef = bp / "chromadb-export.json"
        if ef.exists():
            try:
                export = json.loads(ef.read_text())
                client = chromadb_client.get_chromadb()
                for col_name, data in export.items():
                    try:
                        col = client.get_or_create_collection(col_name)
                        if data["ids"]: col.upsert(ids=data["ids"], documents=data["documents"], metadatas=data["metadatas"])
                    except: pass
                restored.append("chromadb")
            except: pass
    return {"success": len(restored) > 0, "backup_name": request.backup_name, "restored": restored}
