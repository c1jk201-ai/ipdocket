from __future__ import annotations

import logging
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import BinaryIO

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
from flask import current_app

from app.utils.runtime_config import runtime_config_int, runtime_config_str, runtime_storage_type

logger = logging.getLogger(__name__)


class StorageBackend(ABC):
    """Abstract base class for storage backends."""

    @abstractmethod
    def save(self, stream: BinaryIO, path: str, size: int | None = None) -> str:
        """
        Save stream to storage at the given path.
        Returns the stored path (may be modified by backend).
        """
        pass

    @abstractmethod
    def open(self, path: str) -> BinaryIO:
        """Open a file for reading (binary mode)."""
        pass

    @abstractmethod
    def delete(self, path: str) -> bool:
        """Delete a file. returns True if deleted or didn't exist."""
        pass

    @abstractmethod
    def exists(self, path: str) -> bool:
        """Check if file exists."""
        pass

    @abstractmethod
    def size(self, path: str) -> int:
        """Get file size in bytes."""
        pass


class LocalStorageBackend(StorageBackend):
    """Filesystem-based storage backend."""

    def __init__(self, root_dir: str | Path):
        self.root = Path(root_dir).resolve()

    def _resolve(self, path: str) -> Path:
        """Safely resolve path within root."""
        # Normalize and strip leading slashes
        clean = str(path).strip("/\\")
        full = (self.root / clean).resolve()
        if not full.is_relative_to(self.root):
            raise ValueError(f"Path traversal detected: {path}")
        return full

    def save(self, stream: BinaryIO, path: str, size: int | None = None) -> str:
        try:
            full_path = self._resolve(path)
            full_path.parent.mkdir(parents=True, exist_ok=True)

            # Use a temp file for atomicity if needed, but for simplicity here we write directly
            # or could use shutil.copyfileobj if stream is a file-like object
            with full_path.open("wb") as f:
                shutil.copyfileobj(stream, f)

            return str(full_path.relative_to(self.root)).replace("\\", "/")
        except Exception as e:
            try:
                current_app.logger.error(f"LocalStorageBackend.save failed for {path}: {e}")
            except RuntimeError:
                logger.error("LocalStorageBackend.save failed for %s: %s", path, e)
            raise

    def open(self, path: str) -> BinaryIO:
        full_path = self._resolve(path)
        if not full_path.exists():
            raise FileNotFoundError(f"File not found: {path}")
        return full_path.open("rb")

    def delete(self, path: str) -> bool:
        try:
            full_path = self._resolve(path)
            if full_path.exists():
                full_path.unlink()
                # Best effort cleanup of empty parents
                self._prune_empty_parents(full_path)
            return True
        except Exception as e:
            try:
                current_app.logger.warning(f"LocalStorageBackend.delete failed for {path}: {e}")
            except RuntimeError:
                logger.warning("LocalStorageBackend.delete failed for %s: %s", path, e)
            return False

    def exists(self, path: str) -> bool:
        try:
            full_path = self._resolve(path)
            return full_path.exists()
        except Exception:
            return False

    def size(self, path: str) -> int:
        full_path = self._resolve(path)
        return full_path.stat().st_size

    def _prune_empty_parents(self, leaf_path: Path) -> None:
        try:
            parent = leaf_path.parent
            while parent.is_relative_to(self.root) and parent != self.root:
                if any(parent.iterdir()):
                    break
                parent.rmdir()
                parent = parent.parent
        except Exception:
            return


class S3StorageBackend(StorageBackend):
    """S3-compatible storage backend."""

    def __init__(self):
        self.endpoint_url = runtime_config_str("S3_ENDPOINT", "")
        self.region_name = runtime_config_str("S3_REGION", "sgp1")
        self.access_key = runtime_config_str("S3_ACCESS_KEY", "")
        self.secret_key = runtime_config_str("S3_SECRET_KEY", "")
        self.bucket = runtime_config_str("S3_BUCKET", "")
        self.presigned_expiry = runtime_config_int("S3_PRESIGNED_EXPIRY", 3600)

        self._client = None

    @property
    def client(self):
        if self._client is None:
            config = BotoConfig(region_name=self.region_name, signature_version="s3v4")
            self._client = boto3.client(
                "s3",
                endpoint_url=self.endpoint_url,
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key,
                config=config,
            )
        return self._client

    def save(self, stream: BinaryIO, path: str, size: int | None = None) -> str:
        # S3 keys should not start with slash
        key = str(path).strip("/\\").replace("\\", "/")

        extra_args = {}
        # If we knew mime type, we could set ContentType here, but the interface
        # is currently simple.

        try:
            self.client.upload_fileobj(stream, self.bucket, key, ExtraArgs=extra_args)
            return key
        except ClientError as e:
            try:
                current_app.logger.error(f"S3 upload failed for {key}: {e}")
            except RuntimeError:
                logger.error("S3 upload failed for %s: %s", key, e)
            raise

    def open(self, path: str) -> BinaryIO:
        key = str(path).strip("/\\").replace("\\", "/")
        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=key)
            return obj["Body"]  # This is a streaming body
        except ClientError as e:
            try:
                current_app.logger.error(f"S3 open failed for {key}: {e}")
            except RuntimeError:
                logger.error("S3 open failed for %s: %s", key, e)
            raise FileNotFoundError(f"S3 Object not found: {key}")

    def delete(self, path: str) -> bool:
        key = str(path).strip("/\\").replace("\\", "/")
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as e:
            try:
                current_app.logger.warning(f"S3 delete failed for {key}: {e}")
            except RuntimeError:
                logger.warning("S3 delete failed for %s: %s", key, e)
            return False

    def exists(self, path: str) -> bool:
        key = str(path).strip("/\\").replace("\\", "/")
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError:
            return False

    def size(self, path: str) -> int:
        key = str(path).strip("/\\").replace("\\", "/")
        try:
            head = self.client.head_object(Bucket=self.bucket, Key=key)
            return head["ContentLength"]
        except ClientError:
            return 0


def get_storage_backend(
    storage_type: str | None = None,
    *,
    upload_root: str | Path | None = None,
) -> StorageBackend:
    """Factory to get backend instance."""
    stype = (storage_type or runtime_storage_type()).lower()

    if stype == "s3":
        if not runtime_config_str("S3_BUCKET", ""):
            raise ValueError("S3_BUCKET is not configured")
        return S3StorageBackend()
    else:
        # Default to local (prefer runtime app config when available).
        root_dir = upload_root
        if root_dir is None:
            try:
                root_dir = current_app.config.get("UPLOAD_FOLDER")
            except Exception:
                root_dir = None
        return LocalStorageBackend(root_dir or runtime_config_str("UPLOAD_FOLDER", "uploads"))
