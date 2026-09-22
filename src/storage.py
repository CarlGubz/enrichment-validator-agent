"""Storage abstraction so a caller (Logic Apps, a Foundry Hosted Agent client,
or the local CLI) can pass a blob path as input and get the validated CSV
written back to blob storage automatically -- no code in agent.py needs to
know which backend is in play. Selection is by the STORAGE_BACKEND env var.

LocalStorage works today for the CLI and offline testing against test-data/.
AzureBlobStorage downloads/uploads against a real Storage account. Both
expose the same fetch_input(ref) -> local temp path / write_rows(rows, name)
-> location contract, mirroring this dealer's other Foundry agents (see
normalizer-mapper-agent/core/storage.py).

write_rows() writes into the SAME directory the main input was fetched
from (tracked by fetch_input(ref, track=True)) -- e.g. input
"fmg-outbound/20260917-235351-976c79/NEO_enriched.csv" with
write_rows(rows, "NEO_final.csv") produces
"fmg-outbound/20260917-235351-976c79/NEO_final.csv". There is no separate
run-id subfolder for the output; the caller picks the final filename.
"""

from __future__ import annotations

import base64
import os
import tempfile
from pathlib import Path
from typing import List, Optional
from urllib.parse import unquote, urlsplit

from .io_utils import rows_to_csv_text

_ENV_STORAGE_BACKEND = "STORAGE_BACKEND"  # "local" (default) | "azure_blob"
_ENV_LOCAL_OUTPUT_DIR = "LOCAL_OUTPUT_DIR"
_ENV_OUTPUT_CONTAINER = "OUTPUT_CONTAINER"  # fallback when the input container isn't "<x>-inbound"
_ENV_STORAGE_CONNECTION_STRING = "AZURE_STORAGE_CONNECTION_STRING"
_ENV_STORAGE_ACCOUNT_URL = "AZURE_STORAGE_ACCOUNT_URL"


class LocalStorage:
    def __init__(self, output_dir: Optional[str] = None):
        self.output_dir = output_dir or os.environ.get(_ENV_LOCAL_OUTPUT_DIR, "_out")
        # Set by fetch_input(ref, track=True) from the main input's own
        # parent directory, so write_rows() can write alongside it.
        self._input_directory: Optional[str] = None

    def fetch_input(self, ref: dict, track: bool = True) -> str:
        """ref = {'path': ...} or {'content_base64': ..., 'filename': ...}.
        Returns a local path. `track=True` (the main input) records its
        parent directory for write_rows(); pass `track=False` for the
        reference/master-data file so it doesn't override that."""
        if ref.get("path"):
            if track:
                self._input_directory = str(Path(ref["path"]).parent)
            return ref["path"]
        if ref.get("content_base64"):
            data = base64.b64decode(ref["content_base64"])
            fd, tmp = tempfile.mkstemp(suffix="_" + ref.get("filename", "input.csv"))
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            return tmp
        raise ValueError("input ref must contain 'path' or 'content_base64'")

    def write_rows(self, rows: List[dict], name: str) -> str:
        directory = Path(self._input_directory) if self._input_directory else Path(self.output_dir)
        dest = directory / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(rows_to_csv_text(rows), encoding="utf-8")
        return str(dest)


def _derive_output_container(input_container: Optional[str], default_container: str) -> str:
    """The output container mirrors the input container's name, with "inbound"
    swapped for "outbound" (e.g. "fmg-inbound" -> "fmg-outbound"). When the
    input container is already known but doesn't contain "inbound" (e.g.
    this agent was handed a file that already lives in "fmg-outbound",
    written there by an upstream enrichment/matching step), results are
    written back into that SAME container -- not a separate, likely
    nonexistent `default_container` ("outputs"), which previously caused a
    "the specified container does not exist" error for exactly this case.
    `default_container` is only used when the input container is unknown at
    all (e.g. the input arrived as content_base64)."""
    if not input_container:
        return default_container
    lower = input_container.lower()
    if "inbound" in lower:
        idx = lower.index("inbound")
        return input_container[:idx] + "outbound" + input_container[idx + len("inbound"):]
    return input_container


class AzureBlobStorage:
    """Requires azure-storage-blob (+ azure-identity for Entra ID auth).
    `ref["path"]` in fetch_input accepts three shapes, in order of how
    directly they name a blob:

      1. A full blob URL with a SAS token already embedded
         (``https://acct.blob.core.windows.net/container/blob.csv?sv=...&sig=...``)
         -- used exactly as given, no extra auth needed.
      2. A full blob URL with no SAS token -- authenticated via
         AZURE_STORAGE_CONNECTION_STRING if set, else Entra ID
         (DefaultAzureCredential: the Hosted Agent's managed identity in
         Azure, or `az login` locally) against that URL's own account.
      3. A bare "<container>/<blob_name>" path (no scheme), e.g.
         "fmg-inbound/NEO.csv" -- resolved against
         AZURE_STORAGE_CONNECTION_STRING if set, else AZURE_STORAGE_ACCOUNT_URL
         via Entra ID.

    Outputs are written back to the same storage account, in a container
    derived from the *main input* blob's container -- see
    _derive_output_container -- falling back to OUTPUT_CONTAINER when that
    can't be determined, and in the same blob "directory" the input was
    read from. This is what lets a caller hand this agent
    "fmg-outbound/20260917-235351-976c79/NEO_enriched.csv" and get
    "fmg-outbound/20260917-235351-976c79/NEO_final.csv" back, without
    naming the output container or directory explicitly.
    """

    def __init__(self):
        self.conn = os.environ.get(_ENV_STORAGE_CONNECTION_STRING, "")
        self.account_url = os.environ.get(_ENV_STORAGE_ACCOUNT_URL, "")
        self.default_container = os.environ.get(_ENV_OUTPUT_CONTAINER, "outputs")
        # Set by fetch_input(ref, track=True) from the main input blob's own
        # path, so write_rows() can derive the matching output container and
        # write alongside it in the same blob "directory". Fetching the
        # *reference* file must pass track=False so it doesn't clobber these.
        self._input_container: Optional[str] = None
        self._input_directory: Optional[str] = None

    @staticmethod
    def _split_container_and_blob(path: str) -> tuple:
        """Returns (container, blob_name) for either a full blob URL or a
        bare "<container>/<blob_name>" path. Raises if there's no blob name
        after the container (a SAS-token URL has no fixed blob_name to
        split out, so it isn't handled here -- see _blob_client)."""
        if path.startswith("http://") or path.startswith("https://"):
            container, _, blob_name = unquote(urlsplit(path).path).lstrip("/").partition("/")
        else:
            container, _, blob_name = path.partition("/")
        if not blob_name:
            raise ValueError(f"blob path {path!r} has no blob name after the container")
        return container, blob_name

    def _blob_client(self, path: str):
        if path.startswith("http://") or path.startswith("https://"):
            parsed = urlsplit(path)
            if parsed.query:  # SAS token (or other pre-authorized query) already embedded
                from azure.storage.blob import BlobClient
                return BlobClient.from_blob_url(path)
            container, blob_name = self._split_container_and_blob(path)
            account_url = f"{parsed.scheme}://{parsed.netloc}"
            return self._client_for(container, blob_name, account_url)
        # Bare "<container>/<blob_name>" relative to the configured storage account.
        container, blob_name = self._split_container_and_blob(path)
        return self._client_for(container, blob_name, self.account_url or None)

    def _client_for(self, container: str, blob_name: str, account_url: Optional[str] = None):
        if self.conn:
            from azure.storage.blob import BlobServiceClient
            service = BlobServiceClient.from_connection_string(self.conn)
            return service.get_blob_client(container=container, blob=blob_name)
        if not account_url:
            raise RuntimeError(
                "Neither AZURE_STORAGE_CONNECTION_STRING nor AZURE_STORAGE_ACCOUNT_URL "
                "is set, and no full blob URL host to fall back to Entra ID auth "
                "against -- pass a full blob URL or set one of those env vars."
            )
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobClient
        return BlobClient(
            account_url=account_url, container_name=container, blob_name=blob_name,
            credential=DefaultAzureCredential(),
        )

    def _service_client(self):
        """BlobServiceClient for the configured output account (connection
        string wins, else Entra ID via AZURE_STORAGE_ACCOUNT_URL) -- used for
        output uploads."""
        from azure.storage.blob import BlobServiceClient
        if self.conn:
            return BlobServiceClient.from_connection_string(self.conn)
        if self.account_url:
            from azure.identity import DefaultAzureCredential
            return BlobServiceClient(account_url=self.account_url, credential=DefaultAzureCredential())
        raise RuntimeError(
            "Neither AZURE_STORAGE_CONNECTION_STRING nor AZURE_STORAGE_ACCOUNT_URL is "
            "set -- required to write outputs."
        )

    def fetch_input(self, ref: dict, track: bool = True) -> str:
        """Downloads to a temp file and returns its local path (same contract
        as LocalStorage.fetch_input) -- the rest of the pipeline never needs
        to know the source was Blob storage.

        `track=True` (the main input) records the blob's container and
        directory-within-container, so write_rows() can derive the matching
        output container and write alongside this blob. Pass `track=False`
        when fetching the reference/master-data file so it doesn't override
        the main input's tracked location.
        """
        if ref.get("content_base64"):
            data = base64.b64decode(ref["content_base64"])
            fd, tmp = tempfile.mkstemp(suffix="_" + ref.get("filename", "input.csv"))
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            return tmp

        path = ref.get("path")
        if not path:
            raise ValueError("input ref must contain 'path' or 'content_base64'")

        if track:
            container, blob_name = self._split_container_and_blob(path)
            self._input_container = container
            self._input_directory = blob_name.rsplit("/", 1)[0] if "/" in blob_name else None

        blob_client = self._blob_client(path)
        filename = ref.get("filename") or os.path.basename(unquote(path.split("?")[0]))
        fd, tmp = tempfile.mkstemp(suffix="_" + (filename or "input"))
        with os.fdopen(fd, "wb") as fh:
            blob_client.download_blob().readinto(fh)
        return tmp

    def write_rows(self, rows: List[dict], name: str) -> str:
        container = _derive_output_container(self._input_container, self.default_container)
        blob_name = f"{self._input_directory}/{name}" if self._input_directory else name
        service = self._service_client()
        blob_client = service.get_blob_client(container=container, blob=blob_name)
        blob_client.upload_blob(rows_to_csv_text(rows).encode("utf-8"), overwrite=True)
        # Bare "<container>/<blob_name>" -- mirrors the shorthand accepted for
        # input paths, not a full URL.
        return f"{container}/{blob_name}"


def get_storage():
    backend = os.environ.get(_ENV_STORAGE_BACKEND, "local").strip().lower()
    if backend == "azure_blob":
        return AzureBlobStorage()
    return LocalStorage()
