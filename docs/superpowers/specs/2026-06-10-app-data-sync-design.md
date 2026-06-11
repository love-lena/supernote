# App Data Sync (Todo + Digest) — design

**Date:** 2026-06-10
**Status:** approved; implementing digest first.

## Problem

The Manta reports **"App Data Sync" failed** right after **"Private Cloud Sync Completed"**
succeeds. "App Data Sync" is the device syncing two app datasets to the cloud:

- **Digest** — the device's excerpt/highlight feature. Cloud API name: `summary`.
- **Todo** — the device's planner tasks. Cloud API name: `schedule`.

## Evidence (from `~/supernote-data/system/trace.log`, ~10k requests)

Device user-agent `okhttp/4.8.0`, `equipmentNo SN100D20042933`. Failing calls, every sync:

| Device call | Status | Cause |
| --- | --- | --- |
| `POST /api/file/schedule/group/all` (body `{"maxResults":"200"}`) | 404 ×952 | No route at that path. Our schedule code is at invented REST paths `/api/schedule/groups`, `/api/schedule/tasks` that the device never calls. |
| `DELETE /api/file/delete/summary` (body `{"id":…}`) | 404/405 ×884 | Path registered `POST`-only; device uses `DELETE`. |
| `PUT /api/file/update/summary` (rich body, incl. `.mark` handwriting refs) | 404/405 ×2 | Path registered `POST`-only; device uses `PUT`. |

Working today (200): `add/summary`, `query/summary/hash`, `query/summary/group`,
`upload/apply/summary`. So digest *creation* already round-trips; only edit/delete fail.

Confirmed by enumerating the current route table: both summary paths exist but bind only
`POST`; no `/api/file/schedule/*` path exists at all. (A bare aiohttp probe shows this
version returns 405 for wrong-method-on-known-path and 404 for unknown-path; the running
container is ~a week stale, which is why the trace shows 404 where current code would 405.
Either way both are failures and a rebuild is required to pick up any fix.)

Why it was never caught: our own `supernote/client/summary.py` calls these two endpoints
with `POST`, so the test suite exercised `POST`, never the device's `DELETE`/`PUT`.

## Fix 1 — Digest (small, surgical) — DOING FIRST

Decision: **match the device protocol and fix our client to match** (keep client a faithful
mirror of the device, consistent with the rest of the client).

- `supernote/server/routes/summary.py`
  - `/api/file/update/summary`: `@routes.post` → `@routes.put`
  - `/api/file/delete/summary`: `@routes.post` → `@routes.delete`
  - (handler bodies unchanged — DTOs already parse the device's payloads)
- `supernote/client/client.py`: add `delete` + `delete_json` helpers (mirror `put`/`put_json`).
- `supernote/client/summary.py`: `update_summary` → `put_json`; `delete_summary` → `delete_json`.
- Tests:
  - New failing integration test issuing the device's raw `DELETE /api/file/delete/summary`
    and `PUT /api/file/update/summary`, asserting 200 (TDD red → green).
  - Existing `tests/server/routes/test_summary.py` continues to pass through the updated client.

**Verification:** suite green + lint; then rebuild the container and one device sync, confirm
trace shows 200 and the digest half of "App Data Sync" no longer flags.

## Fix 2 — Todo / schedule (real reimplementation) — ITERATIVE, AFTER DIGEST

The schedule feature was built against a guessed REST shape, not the device protocol. The
device family is `/api/file/schedule/...` with flat POST bodies. The DB layer
(`ScheduleTaskDO` / `ScheduleTaskGroupDO` / `ScheduleService`) is reusable; only the HTTP
surface (paths, DTO/VO shapes) must change.

We currently only know the **first** call (`group/all`, body `{"maxResults":"200"}`); the
device aborts after its 404, so subsequent endpoints are unknown. Approach is necessarily
incremental:

1. Implement a best-guess `POST /api/file/schedule/group/all` returning a valid VO
   (model the envelope on `query/summary/group`'s `{success,totalRecords,totalPages,currentPage,pageSize,…List}`).
2. Lena triggers a device sync; read the trace for the *next* call the device makes.
3. Implement that endpoint; repeat until "App Data Sync" completes.
4. Keep or retire the old `/api/schedule/*` REST API (web-UI only; device never uses it) —
   decide once the real surface is known.

## Out of scope

- Gemini-derived digests (OCR) — `SUPERNOTE_GEMINI_API_KEY` intentionally unset; the device's
  own manual digests don't need it.
