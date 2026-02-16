"""MinIO client for backup storage.

Handles upload/download of backup archives to MinIO object storage.
Gracefully degrades if MinIO is unavailable — local backups still work.
"""

import io
import json
from pathlib import Path
from typing import Optional, List

from minio import Minio
from minio.error import S3Error

from config import MINIO_ENDPOINT, MINIO_ACCESS_KEY, MINIO_SECRET_KEY, MINIO_BUCKET, MINIO_SECURE
from utils.logging_ import logger


_client: Optional[Minio] = None


def get_minio() -> Optional[Minio]:
    """Get or create MinIO client. Returns None if not configured."""
    global _client
    if _client is not None:
        return _client
    if not MINIO_ACCESS_KEY or not MINIO_SECRET_KEY:
        logger.warning("MinIO: not configured (no credentials)")
        return None
    try:
        _client = Minio(
            MINIO_ENDPOINT,
            access_key=MINIO_ACCESS_KEY,
            secret_key=MINIO_SECRET_KEY,
            secure=MINIO_SECURE,
        )
        # Ensure bucket exists
        if not _client.bucket_exists(MINIO_BUCKET):
            _client.make_bucket(MINIO_BUCKET)
            logger.info(f"MinIO: created bucket '{MINIO_BUCKET}'")
        logger.info(f"MinIO: connected to {MINIO_ENDPOINT}, bucket '{MINIO_BUCKET}'")
        return _client
    except Exception as e:
        logger.warning(f"MinIO: connection failed: {e}")
        _client = None
        return None


def upload_backup(backup_dir: Path, backup_name: str) -> dict:
    """Upload all files from a local backup directory to MinIO.

    Files are stored as: {backup_name}/{filename}
    Returns dict with upload results.
    """
    client = get_minio()
    if client is None:
        return {"success": False, "error": "MinIO not available", "uploaded": 0}

    uploaded = 0
    errors = []

    for filepath in backup_dir.iterdir():
        if not filepath.is_file():
            continue
        object_name = f"{backup_name}/{filepath.name}"
        try:
            client.fput_object(
                MINIO_BUCKET,
                object_name,
                str(filepath),
            )
            uploaded += 1
            logger.info(f"MinIO: uploaded {object_name} ({filepath.stat().st_size} bytes)")
        except S3Error as e:
            errors.append(f"{filepath.name}: {e}")
            logger.error(f"MinIO: upload failed for {object_name}: {e}")

    return {
        "success": uploaded > 0,
        "uploaded": uploaded,
        "errors": errors,
        "bucket": MINIO_BUCKET,
        "prefix": backup_name,
    }


def download_backup(backup_name: str, target_dir: Path) -> dict:
    """Download backup files from MinIO to a local directory.

    Returns dict with download results.
    """
    client = get_minio()
    if client is None:
        return {"success": False, "error": "MinIO not available", "downloaded": 0}

    target_dir.mkdir(parents=True, exist_ok=True)
    downloaded = 0
    errors = []

    try:
        objects = client.list_objects(MINIO_BUCKET, prefix=f"{backup_name}/", recursive=True)
        for obj in objects:
            filename = obj.object_name.split("/")[-1]
            target_path = target_dir / filename
            try:
                client.fget_object(MINIO_BUCKET, obj.object_name, str(target_path))
                downloaded += 1
                logger.info(f"MinIO: downloaded {obj.object_name}")
            except S3Error as e:
                errors.append(f"{filename}: {e}")
    except S3Error as e:
        return {"success": False, "error": str(e), "downloaded": 0}

    return {
        "success": downloaded > 0,
        "downloaded": downloaded,
        "errors": errors,
    }


def list_remote_backups() -> List[dict]:
    """List backup prefixes in MinIO bucket."""
    client = get_minio()
    if client is None:
        return []

    try:
        prefixes = set()
        objects = client.list_objects(MINIO_BUCKET, recursive=True)
        for obj in objects:
            parts = obj.object_name.split("/")
            if len(parts) >= 2:
                prefixes.add(parts[0])

        backups = []
        for prefix in sorted(prefixes, reverse=True):
            # Get metadata if it exists
            meta = {}
            try:
                response = client.get_object(MINIO_BUCKET, f"{prefix}/metadata.json")
                meta = json.loads(response.read().decode())
                response.close()
                response.release_conn()
            except Exception:
                pass
            backups.append({
                "name": prefix,
                "timestamp": meta.get("timestamp", prefix),
                "size_bytes": meta.get("total_size_bytes", 0),
                "components": meta.get("components", []),
                "location": "minio",
            })
        return backups
    except S3Error as e:
        logger.warning(f"MinIO: list failed: {e}")
        return []


def is_available() -> bool:
    """Check if MinIO is configured and reachable."""
    client = get_minio()
    if client is None:
        return False
    try:
        client.bucket_exists(MINIO_BUCKET)
        return True
    except Exception:
        return False
