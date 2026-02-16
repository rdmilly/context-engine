"""MinIO client for backup storage."""

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
    global _client
    if _client is not None:
        return _client
    if not MINIO_ACCESS_KEY or not MINIO_SECRET_KEY:
        return None
    try:
        _client = Minio(MINIO_ENDPOINT, access_key=MINIO_ACCESS_KEY, secret_key=MINIO_SECRET_KEY, secure=MINIO_SECURE)
        if not _client.bucket_exists(MINIO_BUCKET):
            _client.make_bucket(MINIO_BUCKET)
        return _client
    except Exception as e:
        logger.warning(f"MinIO connection failed: {e}")
        _client = None
        return None


def upload_backup(backup_dir: Path, backup_name: str) -> dict:
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
            client.fput_object(MINIO_BUCKET, object_name, str(filepath))
            uploaded += 1
        except S3Error as e:
            errors.append(f"{filepath.name}: {e}")
    return {"success": uploaded > 0, "uploaded": uploaded, "errors": errors, "bucket": MINIO_BUCKET, "prefix": backup_name}


def download_backup(backup_name: str, target_dir: Path) -> dict:
    client = get_minio()
    if client is None:
        return {"success": False, "error": "MinIO not available", "downloaded": 0}
    target_dir.mkdir(parents=True, exist_ok=True)
    downloaded = 0
    errors = []
    try:
        for obj in client.list_objects(MINIO_BUCKET, prefix=f"{backup_name}/", recursive=True):
            filename = obj.object_name.split("/")[-1]
            try:
                client.fget_object(MINIO_BUCKET, obj.object_name, str(target_dir / filename))
                downloaded += 1
            except S3Error as e:
                errors.append(f"{filename}: {e}")
    except S3Error as e:
        return {"success": False, "error": str(e), "downloaded": 0}
    return {"success": downloaded > 0, "downloaded": downloaded, "errors": errors}


def list_remote_backups() -> List[dict]:
    client = get_minio()
    if client is None:
        return []
    try:
        prefixes = set()
        for obj in client.list_objects(MINIO_BUCKET, recursive=True):
            parts = obj.object_name.split("/")
            if len(parts) >= 2:
                prefixes.add(parts[0])
        backups = []
        for prefix in sorted(prefixes, reverse=True):
            meta = {}
            try:
                response = client.get_object(MINIO_BUCKET, f"{prefix}/metadata.json")
                meta = json.loads(response.read().decode())
                response.close()
                response.release_conn()
            except Exception:
                pass
            backups.append({"name": prefix, "timestamp": meta.get("timestamp", prefix), "location": "minio"})
        return backups
    except S3Error:
        return []


def is_available() -> bool:
    client = get_minio()
    if client is None:
        return False
    try:
        client.bucket_exists(MINIO_BUCKET)
        return True
    except Exception:
        return False
