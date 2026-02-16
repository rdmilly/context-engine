"""Backup and restore for ContextEngine.

Creates timestamped backup points containing:
- Master context (markdown file)
- Nudges and anomalies (JSON)
- ChromaDB collection exports (JSON)
- Session files (optional, can be large)
- Metadata

Backup directory: /app/data/backups/YYYY-MM-DD_HHMMSS/
"""

import json
import shutil
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel
from typing import List

from config import DATA_DIR, SESSIONS_DIR
from services import kb_gateway, chromadb_client
from utils.logging_ import logger
from services import minio_client

router = APIRouter()


class RestoreRequest(BaseModel):
    backup_name: str
    components: List[str] = None


BACKUP_DIR = DATA_DIR / "backups"
MAX_BACKUPS = 10  # Keep last N backups


def _backup_path(timestamp: str = None) -> Path:
    """Generate backup directory path."""
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    return BACKUP_DIR / timestamp


def _prune_old_backups():
    """Keep only the most recent MAX_BACKUPS backups."""
    if not BACKUP_DIR.exists():
        return
    backups = sorted(BACKUP_DIR.iterdir(), key=lambda p: p.name, reverse=True)
    for old in backups[MAX_BACKUPS:]:
        try:
            shutil.rmtree(old)
            logger.info(f"Backup: pruned old backup {old.name}")
        except Exception as e:
            logger.warning(f"Backup: failed to prune {old.name}: {e}")


@router.get("/api/backup/list")
async def list_backups():
    """List available backup points."""
    if not BACKUP_DIR.exists():
        return {"backups": [], "count": 0}

    backups = []
    for bp in sorted(BACKUP_DIR.iterdir(), key=lambda p: p.name, reverse=True):
        if not bp.is_dir():
            continue
        meta_file = bp / "metadata.json"
        meta = {}
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text())
            except Exception:
                pass
        backups.append({
            "name": bp.name,
            "timestamp": meta.get("timestamp", bp.name),
            "size_bytes": meta.get("total_size_bytes", 0),
            "components": meta.get("components", []),
        })

    # Also list remote backups from MinIO
    remote = minio_client.list_remote_backups()
    # Merge: mark local ones, add remote-only ones
    local_names = {b["name"] for b in backups}
    for b in backups:
        b["location"] = "local+minio" if b["name"] in {r["name"] for r in remote} else "local"
    for r in remote:
        if r["name"] not in local_names:
            backups.append(r)

    return {"backups": backups, "count": len(backups), "minio_available": minio_client.is_available()}


@router.post("/api/backup/create")
async def create_backup(include_sessions: bool = False):
    """Create a new backup point.

    Args:
        include_sessions: Also backup session files (can be large).
    """
    ts = datetime.now(timezone.utc)
    ts_str = ts.strftime("%Y-%m-%d_%H%M%S")
    bp = _backup_path(ts_str)
    bp.mkdir(parents=True, exist_ok=True)

    components = []
    total_size = 0

    # 1. Master context
    try:
        mc = kb_gateway.read_master_context()
        if mc:
            mc_file = bp / "master-context.md"
            mc_file.write_text(mc, encoding="utf-8")
            components.append("master_context")
            total_size += len(mc.encode())
            logger.info(f"Backup: master context ({len(mc)} bytes)")
    except Exception as e:
        logger.warning(f"Backup: master context failed: {e}")

    # 2. Nudges
    try:
        nudges_src = DATA_DIR / "nudges.json"
        if nudges_src.exists():
            shutil.copy2(nudges_src, bp / "nudges.json")
            components.append("nudges")
            total_size += nudges_src.stat().st_size
    except Exception as e:
        logger.warning(f"Backup: nudges failed: {e}")

    # 3. Anomalies
    try:
        anomalies_src = DATA_DIR / "anomalies.json"
        if anomalies_src.exists():
            shutil.copy2(anomalies_src, bp / "anomalies.json")
            components.append("anomalies")
            total_size += anomalies_src.stat().st_size
    except Exception as e:
        logger.warning(f"Backup: anomalies failed: {e}")

    # 4. ChromaDB collection export
    try:
        client = chromadb_client.get_chromadb()
        export = {}
        for col_name in ["sessions", "project_archive", "decisions", "failures", "entities", "patterns"]:
            try:
                col = client.get_collection(col_name)
                count = col.count()
                if count > 0:
                    data = col.get(include=["documents", "metadatas"])
                    export[col_name] = {
                        "count": count,
                        "ids": data["ids"],
                        "documents": data["documents"],
                        "metadatas": data["metadatas"],
                    }
            except Exception as e:
                logger.warning(f"Backup: collection {col_name} export failed: {e}")

        if export:
            export_file = bp / "chromadb-export.json"
            export_content = json.dumps(export, indent=2, default=str)
            export_file.write_text(export_content, encoding="utf-8")
            components.append("chromadb")
            total_size += len(export_content.encode())
            logger.info(f"Backup: ChromaDB ({len(export)} collections)")
    except Exception as e:
        logger.warning(f"Backup: ChromaDB export failed: {e}")

    # 5. Session files (optional)
    if include_sessions:
        try:
            sessions_dir = bp / "sessions"
            sessions_dir.mkdir(exist_ok=True)
            session_files = list(SESSIONS_DIR.glob("*.json"))
            for sf in session_files:
                shutil.copy2(sf, sessions_dir / sf.name)
                total_size += sf.stat().st_size
            components.append(f"sessions ({len(session_files)} files)")
            logger.info(f"Backup: {len(session_files)} session files")
        except Exception as e:
            logger.warning(f"Backup: session files failed: {e}")

    # 6. Write metadata
    meta = {
        "timestamp": ts.isoformat(),
        "name": ts_str,
        "components": components,
        "total_size_bytes": total_size,
    }
    (bp / "metadata.json").write_text(json.dumps(meta, indent=2))

    # Prune old backups
    _prune_old_backups()

    # Upload to MinIO
    minio_result = minio_client.upload_backup(bp, ts_str)

    return {
        "success": True,
        "backup_name": ts_str,
        "components": components,
        "total_size_bytes": total_size,
        "minio": minio_result,
        "message": f"Backup created with {len(components)} components.",
    }


@router.post("/api/backup/restore")
async def restore_backup(request: RestoreRequest):
    """Restore from a backup point."""
    backup_name = request.backup_name
    components = request.components
    bp = _backup_path(backup_name)
    if not bp.exists():
        # Try downloading from MinIO
        logger.info(f"Restore: backup '{backup_name}' not local, checking MinIO...")
        dl_result = minio_client.download_backup(backup_name, bp)
        if not dl_result.get("success"):
            return {"error": f"Backup '{backup_name}' not found locally or in MinIO"}
        logger.info(f"Restore: downloaded {dl_result['downloaded']} files from MinIO")

    if components is None:
        components = ["master_context", "nudges", "anomalies", "chromadb"]

    restored = []

    # 1. Master context
    if "master_context" in components:
        mc_file = bp / "master-context.md"
        if mc_file.exists():
            try:
                content = mc_file.read_text(encoding="utf-8")
                kb_gateway.write_master_context(content, f"ContextEngine: restore from backup {backup_name}")
                restored.append("master_context")
            except Exception as e:
                logger.error(f"Restore: master context failed: {e}")

    # 2. Nudges
    if "nudges" in components:
        nudges_file = bp / "nudges.json"
        if nudges_file.exists():
            try:
                shutil.copy2(nudges_file, DATA_DIR / "nudges.json")
                restored.append("nudges")
            except Exception as e:
                logger.error(f"Restore: nudges failed: {e}")

    # 3. Anomalies
    if "anomalies" in components:
        anomalies_file = bp / "anomalies.json"
        if anomalies_file.exists():
            try:
                shutil.copy2(anomalies_file, DATA_DIR / "anomalies.json")
                restored.append("anomalies")
            except Exception as e:
                logger.error(f"Restore: anomalies failed: {e}")

    # 4. ChromaDB
    if "chromadb" in components:
        export_file = bp / "chromadb-export.json"
        if export_file.exists():
            try:
                export = json.loads(export_file.read_text())
                client = chromadb_client.get_chromadb()
                for col_name, data in export.items():
                    try:
                        col = client.get_or_create_collection(col_name)
                        # Upsert to avoid duplicates
                        if data["ids"]:
                            col.upsert(
                                ids=data["ids"],
                                documents=data["documents"],
                                metadatas=data["metadatas"],
                            )
                            logger.info(f"Restore: {col_name} — {len(data['ids'])} docs")
                    except Exception as e:
                        logger.error(f"Restore: collection {col_name} failed: {e}")
                restored.append("chromadb")
            except Exception as e:
                logger.error(f"Restore: ChromaDB import failed: {e}")

    return {
        "success": len(restored) > 0,
        "backup_name": backup_name,
        "restored": restored,
        "message": f"Restored {len(restored)} components from backup {backup_name}.",
    }
