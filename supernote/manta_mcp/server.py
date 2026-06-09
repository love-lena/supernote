"""Manta MCP server (FastMCP, streamable-HTTP).

Wraps the self-hosted Supernote cloud as MCP tools so an agent on any tailnet
machine can list, read, search, push, and delete documents — and a pushed
document auto-syncs to the Manta device (the cloud emits its change event on
upload). It reuses ``supernote.client`` for auth + endpoints, so it is a thin
sibling to the cloud, not a reimplementation.

Deployment: run as its own container/process on the same host as the cloud and
bind it to the tailnet (publish on the 100.x IP, or front it with Tailscale
Serve). It is unauthenticated at the MCP layer by default — the tailnet IS the
boundary, exactly like the cloud. Set ``MANTA_MCP_BEARER`` to require a bearer
token as defense-in-depth.

Config (env):
  SUPERNOTE_CLOUD_URL   cloud base URL (default http://localhost:8080). Named to
                        avoid the cloud's own SUPERNOTE_HOST (its bind address).
  SUPERNOTE_TOKEN       access token; OR
  SUPERNOTE_EMAIL/_PASSWORD  account creds (the server logs in once, caches token)
  MANTA_MCP_HOST/_PORT  bind address (default 0.0.0.0:9000)
  MANTA_MCP_BEARER      optional bearer token required on /mcp

v1 scope: text/markdown/PDF read natively; ``.note`` handwriting files are
returned as opaque metadata (OCR reading lands in v2 as ``read_note``).
"""

from __future__ import annotations

import io
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastmcp import FastMCP

from supernote.client import Supernote
from supernote.client.exceptions import SupernoteException

logger = logging.getLogger(__name__)

_HOST = os.environ.get("SUPERNOTE_CLOUD_URL", "http://localhost:8080")

# Extensions we decode and return as text. Everything else is "binary" in v1.
_TEXT_EXTS = {
    ".md", ".markdown", ".txt", ".text", ".rst", ".log",
    ".json", ".csv", ".tsv", ".yaml", ".yml", ".toml", ".ini",
    ".html", ".htm", ".xml", ".py", ".js", ".ts", ".sh", ".css",
}
_MAX_TEXT_BYTES = 200_000          # cap returned text so tools stay context-friendly
_SEARCH_MAX_FOLDERS = 250          # traversal budget for search_documents
_FOLDER_TAG = "folder"

mcp = FastMCP(
    name="manta-cloud",
    instructions=(
        "Read, list, search, push, and delete documents on Lena's self-hosted "
        "Supernote cloud. Paths are absolute and case-sensitive, e.g. "
        "'/EXPORT/My Doc.pdf' or '/NOTE/Note/Inbox.note'. Call list_documents "
        "or search_documents to discover exact paths before reading — they are "
        "not guessable. Pushing a document auto-syncs it to the Manta device. "
        "'.note' handwriting files are opaque in v1 (read_note arrives in v2)."
    ),
)


# --------------------------------------------------------------------------- #
# Session / auth — reuse the same client + token machinery the `cloud` CLI uses
# --------------------------------------------------------------------------- #

_token: str | None = None


async def _get_token() -> str:
    """Return an access token, logging in once if only creds are configured."""
    global _token
    if _token:
        return _token
    if env_token := os.environ.get("SUPERNOTE_TOKEN"):
        _token = env_token
        return _token
    email = os.environ.get("SUPERNOTE_EMAIL")
    password = os.environ.get("SUPERNOTE_PASSWORD")
    if not (email and password):
        raise RuntimeError(
            "No credentials: set SUPERNOTE_TOKEN, or SUPERNOTE_EMAIL + "
            "SUPERNOTE_PASSWORD."
        )
    async with await Supernote.login(email, password, host=_HOST) as sn:
        _token = sn.token
    logger.info("manta-mcp: logged in to %s, cached token", _HOST)
    return _token


@asynccontextmanager
async def _session():
    token = await _get_token()
    async with Supernote.from_token(token, host=_HOST) as sn:
        yield sn


async def _exec(fn):
    """Run ``fn(sn)`` in a session; on an auth error, re-login once and retry."""
    global _token
    try:
        async with _session() as sn:
            return await fn(sn)
    except SupernoteException as err:
        logger.warning("manta-mcp: call failed (%s); re-authenticating once", err)
        _token = None
        async with _session() as sn:
            return await fn(sn)


def _entry_dict(entry: Any, parent: str) -> dict[str, Any]:
    path = entry.path_display or f"{parent.rstrip('/')}/{entry.name}"
    if not path.startswith("/"):
        path = "/" + path  # normalize to the absolute form the instructions promise
    return {
        "name": entry.name,
        "path": path,
        "type": "folder" if entry.tag == _FOLDER_TAG else "file",
        "size": entry.size,
        "modified": entry.last_update_time,
        "downloadable": entry.is_downloadable,
    }


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #


@mcp.tool(annotations={"readOnlyHint": True})
async def list_documents(path: str = "/") -> list[dict]:
    """List the entries (files and folders) directly under a cloud folder.

    `path` is an absolute folder path; defaults to the root. Returns each entry's
    name, absolute path, type (file/folder), size in bytes, and modified time.
    """
    async def _do(sn):
        resp = await sn.device.list_folder(path)
        return [_entry_dict(e, path) for e in resp.entries]

    return await _exec(_do)


@mcp.tool(annotations={"readOnlyHint": True})
async def read_document(path: str) -> dict:
    """Read a document's content by absolute path.

    Text files (markdown, txt, json, csv, code, …) are returned decoded. PDFs
    are returned as extracted text when a text layer exists. Binary files and
    `.note` handwriting notebooks are returned as metadata only (their content
    is not rendered in v1). Large text is truncated.
    """
    async def _do(sn):
        data = await sn.device.download_content(path=path)
        return _render_content(path, data)

    return await _exec(_do)


@mcp.tool(annotations={"readOnlyHint": True})
async def search_documents(query: str, limit: int = 20) -> list[dict]:
    """Find documents whose name matches `query` (case-insensitive substring).

    Walks the folder tree from the root and returns matching files and folders
    with their absolute paths. This is a NAME search — semantic/content search
    requires the AI pipeline, which is disabled on this deployment.
    """
    needle = query.lower()
    results: list[dict] = []

    async def _do(sn):
        queue = ["/"]
        visited = 0
        while queue and visited < _SEARCH_MAX_FOLDERS and len(results) < limit:
            folder = queue.pop(0)
            visited += 1
            try:
                resp = await sn.device.list_folder(folder)
            except SupernoteException:
                continue
            for e in resp.entries:
                d = _entry_dict(e, folder)
                if needle in e.name.lower():
                    results.append(d)
                    if len(results) >= limit:
                        break
                if d["type"] == "folder":
                    queue.append(d["path"])
        return results

    out = await _exec(_do)
    if len(out) >= limit:
        logger.info("manta-mcp: search hit the result cap (%d)", limit)
    return out


@mcp.tool
async def push_document(path: str, content: str) -> dict:
    """Upload UTF-8 text `content` to absolute `path` on the cloud.

    This creates or overwrites the file and triggers the Manta device to sync it
    down (no manual tap). Use for pushing notes, drafts, or generated documents.
    For binary uploads, use the cloud directly in v1.
    """
    async def _do(sn):
        body = content.encode("utf-8")
        await sn.device.upload_content(path, body)
        return {"path": path, "bytes": len(body), "synced_to_device": True}

    return await _exec(_do)


@mcp.tool(annotations={"destructiveHint": True})
async def delete_document(path: str) -> dict:
    """Delete a file or folder at absolute `path` (moves it to the cloud recycle).

    Destructive. Confirm the path with list_documents/search_documents first.
    """
    async def _do(sn):
        await sn.device.delete_by_path(path)
        return {"path": path, "deleted": True}

    return await _exec(_do)


@mcp.tool
async def make_folder(path: str) -> dict:
    """Create a folder at absolute `path` on the cloud."""
    async def _do(sn):
        await sn.device.create_folder(path, equipment_no="WEB")
        return {"path": path, "created": True}

    return await _exec(_do)


# --------------------------------------------------------------------------- #
# Content rendering
# --------------------------------------------------------------------------- #


def _render_content(path: str, data: bytes) -> dict:
    ext = os.path.splitext(path)[1].lower()
    base = {"path": path, "size": len(data), "ext": ext}

    if ext in _TEXT_EXTS:
        text, truncated = _decode_text(data)
        return {**base, "kind": "text", "truncated": truncated, "content": text}

    if ext == ".pdf":
        text = _pdf_text(data)
        if text is None:
            return {
                **base, "kind": "pdf",
                "content": None,
                "note": "PDF text extraction unavailable (install the manta-mcp "
                        "extra for pypdf).",
            }
        if not text.strip():
            return {
                **base, "kind": "pdf", "content": None,
                "note": "No text layer — likely a handwriting/.note export. OCR "
                        "reading lands in v2 (read_note).",
            }
        text, truncated = _truncate(text)
        return {**base, "kind": "pdf", "truncated": truncated, "content": text}

    if ext == ".note":
        return {
            **base, "kind": "note", "content": None,
            "note": "Handwriting notebook — opaque in v1. read_note (OCR) is v2.",
        }

    return {
        **base, "kind": "binary", "content": None,
        "note": "Binary file — not rendered in v1.",
    }


def _decode_text(data: bytes) -> tuple[str, bool]:
    text = data.decode("utf-8", errors="replace")
    return _truncate(text)


def _truncate(text: str) -> tuple[str, bool]:
    if len(text.encode("utf-8")) <= _MAX_TEXT_BYTES:
        return text, False
    return text[: _MAX_TEXT_BYTES // 2] + "\n…[truncated]…", True


def _pdf_text(data: bytes) -> str | None:
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as err:  # malformed PDF, encryption, etc.
        logger.warning("manta-mcp: pdf extract failed for: %s", err)
        return ""


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    host = os.environ.get("MANTA_MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MANTA_MCP_PORT", "9000"))

    auth = None
    if bearer := os.environ.get("MANTA_MCP_BEARER"):
        from fastmcp.server.auth.providers.bearer import StaticTokenVerifier

        auth = StaticTokenVerifier(tokens={bearer: {"client_id": "manta"}})
        logger.info("manta-mcp: bearer auth enabled")

    if auth is not None:
        mcp.auth = auth
    logger.info("manta-mcp: serving on http://%s:%d/mcp (cloud=%s)", host, port, _HOST)
    mcp.run(transport="http", host=host, port=port)


if __name__ == "__main__":
    main()
