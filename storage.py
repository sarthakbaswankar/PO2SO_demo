"""
storage.py
==========
OCI Object Storage operations:
  - list PDFs in the POPDFS input folder
  - download a PDF to memory
  - move (copy + delete) to processed/ or error/
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Generator

import oci

from config import StorageConfig

log = logging.getLogger(__name__)


class StorageClient:
    def __init__(self, cfg: StorageConfig) -> None:
        self.cfg = cfg
        # Load OCI config from file (uses profile DEFAULT or as configured)
        oci_config = oci.config.from_file(
            file_location=cfg.oci_config_path,
            profile_name=cfg.oci_profile,
        )
        self._client = oci.object_storage.ObjectStorageClient(oci_config)
        log.info("OCI Object Storage client initialised (namespace=%s, bucket=%s)",
                 cfg.namespace, cfg.bucket_name)

    # ── List ──────────────────────────────────────────────────────────────────
    def list_pdf_objects(self) -> Generator[str, None, None]:
        """Yield object names of all PDF files in the input folder."""
        response = self._client.list_objects(
            namespace_name=self.cfg.namespace,
            bucket_name=self.cfg.bucket_name,
            prefix=self.cfg.input_folder,
            fields="name",
        )
        for obj in response.data.objects:
            name: str = obj.name
            if name.lower().endswith(".pdf") and name != self.cfg.input_folder:
                log.debug("Found PDF: %s", name)
                yield name

    # ── Download ──────────────────────────────────────────────────────────────
    def download_pdf(self, object_name: str) -> bytes:
        """Download a PDF object and return its raw bytes."""
        log.info("Downloading %s", object_name)
        response = self._client.get_object(
            namespace_name=self.cfg.namespace,
            bucket_name=self.cfg.bucket_name,
            object_name=object_name,
        )
        data = response.data.content
        log.info("Downloaded %s (%d bytes)", object_name, len(data))
        return data

    def upload_pdf(self, file_bytes: bytes, filename: str) -> str:
        """Upload a PDF into the input folder (POPDFS/). Returns the object name.

        Used by the frontend so users can upload a PO directly instead of
        dropping it into the bucket manually.
        """
        object_name = f"{self.cfg.input_folder}{filename}"
        self._client.put_object(
            namespace_name=self.cfg.namespace,
            bucket_name=self.cfg.bucket_name,
            object_name=object_name,
            put_object_body=file_bytes,
            content_type="application/pdf",
        )
        log.info("Uploaded %s (%d bytes)", object_name, len(file_bytes))
        return object_name

    # ── Move helpers ──────────────────────────────────────────────────────────
    def _move(self, source_name: str, target_folder: str) -> str:
        """Copy object to target_folder, wait for completion, then delete source.

        Returns the new object name (full key including target_folder).
        """
        filename = source_name.rsplit("/", 1)[-1]
        target_name = target_folder + filename

        # Server-side copy — this is asynchronous on OCI's side
        copy_details = oci.object_storage.models.CopyObjectDetails(
            source_object_name=source_name,
            destination_namespace=self.cfg.namespace,
            destination_bucket=self.cfg.bucket_name,
            destination_object_name=target_name,
            destination_region=self.cfg.region,
        )
        resp = self._client.copy_object(
            namespace_name=self.cfg.namespace,
            bucket_name=self.cfg.bucket_name,
            copy_object_details=copy_details,
        )

        # Wait for the copy work request to complete BEFORE deleting the source.
        # Without this wait, the delete can race ahead and the file is lost.
        wr_id = resp.headers.get("opc-work-request-id") if resp.headers else None
        if wr_id:
            self._wait_for_work_request(wr_id)

        log.info("Copied %s → %s", source_name, target_name)

        # Delete source
        self._client.delete_object(
            namespace_name=self.cfg.namespace,
            bucket_name=self.cfg.bucket_name,
            object_name=source_name,
        )
        log.info("Deleted source %s", source_name)
        return target_name

    def _wait_for_work_request(self, work_request_id: str, timeout_s: int = 60) -> None:
        """Poll a copy work request until it completes (or fails / times out)."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            wr = self._client.get_work_request(work_request_id=work_request_id).data
            if wr.status == "COMPLETED":
                return
            if wr.status in ("FAILED", "CANCELED"):
                raise RuntimeError(
                    f"OCI copy work request {work_request_id} ended with status={wr.status}"
                )
            time.sleep(1)
        raise TimeoutError(
            f"OCI copy work request {work_request_id} did not complete in {timeout_s}s"
        )

    def move_to_processed(self, object_name: str) -> str:
        return self._move(object_name, self.cfg.processed_folder)

    def move_to_error(self, object_name: str) -> str:
        return self._move(object_name, self.cfg.error_folder)

    # ── Pre-Authenticated Request (PAR) URL ──────────────────────────────────
    def create_par_url(
        self,
        object_name: str,
        ttl_seconds: int,
        name_prefix: str = "po2so-",
    ) -> str:
        """Create a Pre-Authenticated Request (PAR) for downloading an object.

        Returns a full HTTPS URL anyone can use to download the object, without
        needing OCI credentials. The URL is valid until the PAR expires.

        Notes:
          - PAR access type is OBJECT_READ (download only — no listing, no overwrite).
          - Expiry is calculated as now (UTC) + ttl_seconds.
          - PAR name must be unique per bucket; we suffix with the object name
            and a timestamp to avoid collisions on repeated runs.
        """
        if not object_name:
            raise ValueError("object_name is required")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")

        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)

        # PAR name: prefix + filename + epoch. Keeps names readable in the OCI
        # console and avoids collision when the same file is re-processed.
        safe_name = object_name.rsplit("/", 1)[-1].replace(" ", "_")
        par_name = f"{name_prefix}{safe_name}-{int(time.time())}"

        details = oci.object_storage.models.CreatePreauthenticatedRequestDetails(
            name=par_name,
            object_name=object_name,
            access_type="ObjectRead",
            time_expires=expires_at,
        )

        resp = self._client.create_preauthenticated_request(
            namespace_name=self.cfg.namespace,
            bucket_name=self.cfg.bucket_name,
            create_preauthenticated_request_details=details,
        )
        par = resp.data

        # The full URL combines the regional endpoint with the access_uri from the PAR.
        # access_uri starts with "/p/..." so we strip any trailing slash from the host.
        host = f"https://objectstorage.{self.cfg.region}.oraclecloud.com"
        full_url = host.rstrip("/") + par.access_uri

        log.info(
            "Created PAR for %s: id=%s expires=%s",
            object_name, par.id, expires_at.isoformat(),
        )
        return full_url