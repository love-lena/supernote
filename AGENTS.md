# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## What this is

A **fork of `allenporter/supernote`** (`upstream` remote) — an all-in-one Python toolkit for Ratta Supernote devices: parse `.note` files, drive the Supernote Cloud API, and self-host a Supernote **Private Cloud** server. `origin` is the fork; pull upstream with `git fetch upstream && git rebase upstream/main`.

This fork carries additions that aren't upstream — know them before rebasing, since they're where conflicts land:
- **Socket.IO device auto-sync** (`server/socketio_app.py`, `server/events.py`) — push channel so the device syncs without a manual tap.
- **Conflict-cascade fix** (`server/services/vfs.py::replace_file_version` + `retention_versions`) — uploads soft-replace the prior version instead of inserting a duplicate row.
- **`manta-mcp`** (`supernote/manta_mcp/`) — a sibling MCP server over the cloud.
- **Device "App Data Sync" (To-Do + Digest)** — the device's To-Do and Digest sync, reverse-engineered from on-device traffic (see `docs/superpowers/specs/2026-06-10-app-data-sync-design.md`). **Digest:** `server/routes/summary.py` binds `update`/`delete` to `PUT`/`DELETE` (the device's verbs), not `POST`. **To-Do:** `server/routes/schedule_device.py` implements the real `/api/file/schedule/*` protocol (group/all, task/all, task create, `PUT task/list` batch edit/complete, `DELETE task/{id}`) on a device-shaped schema (`server/db/models/schedule.py` — string PKs, tombstones; migration `7f3c19a2b8d4`); the old invented `/api/schedule/*` REST API was removed. Not yet implemented: groups, `/sort`, incremental `nextSyncToken`.
- **`docker-compose.yml` + `docs/deploy.md`** — the self-host/migration runbook.

## Common commands

Scripts follow the "Scripts to Rule Them All" pattern and prefer `uv` (fall back to pip/pytest). Run from repo root.

```bash
./script/bootstrap          # uv sync --all-extras + dev deps + pre-commit hooks (one-time)
./script/test               # full pytest suite
./script/test tests/server/services/test_vfs.py        # single file
./script/test -k replace_file_version                  # single test by name
./script/lint               # pre-commit run --all-files (ruff + mypy)
./script/server             # supernote serve --ephemeral (clean transient instance)
./script/db_revision "msg"  # alembic autogenerate a migration (-> supernote/alembic/versions/)
```

`uv run mypy supernote` type-checks directly. Note: **`supernote/notebook/` and `supernote/cli/` are excluded** from mypy/ty (see `pyproject.toml`); everything else is strict (`disallow_untyped_defs`, etc.).

Requires **Python ≥ 3.13**. `pytest` runs in asyncio auto mode.

### Ephemeral mode — the fast dev loop

`supernote serve --ephemeral` boots a clean server with a pre-made user (`debug@example.com` / `password`) and prints the matching `supernote cloud login` command. Use it for any sync/API change rather than touching a real data dir.

## Architecture

### Packages (installed via extras in `pyproject.toml`)

- **`supernote/notebook/`** — the `.note` binary parser/converter (PNG/SVG/PDF/TXT). Core, always installed. Forked from `supernote-tool`.
- **`supernote/client/`** — async (`aiohttp`) Cloud API client. `client.py` is low-level; `device.py`/`web.py` are the device- and web-equipment surfaces; `auth.py` has `AbstractAuth`/`FileCacheAuth` (token cache at `~/.cache/supernote.pkl`). The `manta` skill and `manta-mcp` both build on this. (`[client]` extra.)
- **`supernote/server/`** — the self-hosted Private Cloud. (`[server]` extra.)
- **`supernote/manta_mcp/`** — MCP server, a **sibling** to the cloud (separate process/port, talks to it over HTTP via `supernote.client`). NOT the AI/insights MCP that lives gated inside `server/mcp/`. (`[manta-mcp]` extra.)

### The server reimplements a documented protocol

The original Supernote cloud is Java/Spring; this server matches its wire protocol in Python/aiohttp. **`supernote/server/ARCHITECTURE.md` is the protocol contract** (auth challenge-response, sync session lifecycle, `upload/apply`→`finish`, `UserFileDO`/`FileActionDO` semantics). Read it before changing any endpoint — the device is an opaque client that expects exact behavior.

### Request flow

`web.Application` (`server/app.py`) → **routes** (`server/routes/`, thin handlers) → **services** (`server/services/`, business logic, dependency-injected) → **`vfs.py`** (virtual filesystem: the `UserFileDO` rows / hierarchy) + **`blob.py`** (content storage) → SQLAlchemy + aiosqlite, schema via **Alembic** (`server/db/`, `supernote/alembic/`).

`app.py` also attaches the Socket.IO server (`socket.io` path) and, when configured, mounts the AI MCP (an ASGI app via `aiohttp_asgi`) on a separate `mcp_port`.

### Two upload paths — keep them in sync

Device sync and the web/API upload are **separate handlers**: `routes/file_device.py` (→ `services/file.py::finish_upload`) and `routes/file_web.py` (→ `upload_finish_web`). Any change to upload/finish semantics must be made in **both** — the conflict-cascade fix had to patch both call sites. Both now call `vfs.replace_file_version`.

### The conflict fix (why it exists)

Upstream's `create_file` is INSERT-always, so re-uploading the same path created multiple active rows at one path; the device then spawned `_CONFLICT_<timestamp>` copies. `vfs.replace_file_version` instead deactivates prior versions (`is_active="Y"→"N"`, kept as history, pruned beyond `retention_versions`, default 10) and inserts the new active row. Identical re-syncs (same md5) are a no-op. There is no version/mtime in the `.note` format or protocol, so this soft-replace assumes a single writer per path (true for a personal device).

### Socket.IO sync — hard-won, non-obvious

The device ships an **old okhttp Socket.IO v2 client that only speaks Engine.IO v3**, so `python-socketio` is pinned **`>=4.6,<5`** (see `pyproject.toml` comment + `constraints.txt`); 5.x/engineio 4.x reject EIO=3. Other gotchas baked into `socketio_app.py`, learned on-device (don't "fix" without re-testing on hardware):
- The device sends a **`ratta_ping`** app-level heartbeat ~every 25s and drops if you don't reply **`ratta_pong`**.
- The only reliable way to make the device sync-after-upload is a **hard TCP reset** (`request.transport.abort()`) — clean Socket.IO/engine.io closes do NOT prompt a fast reconnect.
- A resync fired **mid-sync** (between `synchronous/start` and `/end`) corrupts the device's sync and creates conflicts — `request_resync` checks sync locks and **defers** to the next `synchronous/end`.
- The device's own uploads carry its `equipment_no` and are **skipped** to avoid a self-trigger loop.

### AI is off in this deployment

Gemini OCR/synthesis/semantic-search require `SUPERNOTE_GEMINI_API_KEY`, which is intentionally unset here. So `manta-mcp`'s `search_documents` is **name-match only**, and `.note` files read as metadata only (no OCR). Don't assume semantic features work.

## Conventions

- **Data models:** `mashumaro` dataclasses (`DataClassJSONMixin` / YAML mixin), `omit_none=True` + `TO_DICT_ADD_OMIT_NONE_FLAG`. Server DTOs/VOs live in `server/` models and must match the client's.
- **Async everywhere** for I/O; `logging.getLogger(__name__)`.
- **Typing:** modern syntax (`str | None`, `list[T]`, `typing.Protocol`); explicit hints on test functions/fixtures too.
- **Tests:** `pytest` + asyncio-auto; fixtures in `tests/conftest.py`; prefer `unittest.mock.patch` over `monkeypatch`. Tests mirror the package layout under `tests/`.
- **Config:** `ServerConfig` (`server/config.py`) loads `config/config.yaml` then applies `SUPERNOTE_*` env overrides. New options need: a field + docstring, an env override in `load()`, and wiring in `app.py`.

## Fork hygiene & deployment

- **Commit email is set repo-locally** to a noreply address to keep personal/work email off the public fork — don't change `user.email` here.
- **Deploy/migrate** via `docker-compose.yml` (builds both the cloud and `manta-mcp` images) — full runbook in `docs/deploy.md` (SELinux `:Z`, tailnet binding, port-remap caveats, backup + migration ordering). Keep host-specific addresses/secrets in the gitignored `.env`, never committed.
- Single source of state is the bind-mounted data dir (SQLite DB + blobs + `config/config.yaml` holding `secret_key`). Backing up = archiving that dir.
