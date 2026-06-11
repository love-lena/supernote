# Self-hosting & migrating the cloud

The private cloud has exactly **one** piece of state: the data dir bind-mounted at
`/data` (SQLite DB, document/notebook blobs, and `config/config.yaml` which holds
`secret_key`). The container images are built from this repo, and the `manta-mcp`
service is stateless. So moving hosts = move the data dir, rebuild the images,
repoint the clients.

`docker-compose.yml` brings up both services. The MCP reaches the cloud by service
name (`http://supernote:8080`), so there's no `host.docker.internal` and it behaves
the same on Linux, macOS, Docker, and Podman.

## Bring it up (any host)

```bash
git clone https://github.com/love-lena/supernote && cd supernote
cp .env.example .env        # fill in SUPERNOTE_EMAIL / SUPERNOTE_PASSWORD
docker compose up -d --build        # or: podman compose up -d --build
```

## Migrating to a new machine

Images rebuild from the repo, so only the ~300 MB data dir and your `.env` move.

```bash
# 1. On the DEVICE: trigger one final manual sync and wait for it to finish,
#    then take the device offline (Wi-Fi off / airplane mode). It stays offline
#    until step 6 — anything it syncs to the old host after the copy is lost.

# 2. On the OLD host: stop the cloud so SQLite is quiesced (no mid-write copy).
docker stop supernote

# 3. Back up the data dir. This archive IS a complete cloud backup —
#    restoring = untar it and point SUPERNOTE_DATA_DIR at the result.
tar -czf supernote-data-$(date +%F).tgz -C /path/to/old supernote-data

# 4. If the new host is on a DIFFERENT tailnet, switch this machine to it now:
#    `tailscale login` (adds the new account) or `tailscale switch <profile>`.
#    This is the point of no return for old-tailnet connectivity — which is why
#    it comes AFTER the device's final sync (step 1) and the backup (step 3),
#    and BEFORE the copy, which needs the new network.

# 5. Copy the data dir to the new host (any transport).
rsync -av /path/to/old/supernote-data/  newhost:/srv/supernote-data/

# 6. On the NEW host: point .env at it and start.
#    SUPERNOTE_DATA_DIR=/srv/supernote-data
docker compose up -d --build

# 7. On the DEVICE: update the private-cloud server URL to the new host
#    (see "Repoint the clients" below), make sure the device can reach the new
#    network (rejoin Wi-Fi / new tailnet path), THEN re-enable Wi-Fi and sync.
```

Keep the old host's container **stopped** (or remove it) once the new one is live —
two clouds with diverged copies of the same data dir is how you manufacture
conflicts. The tarball from step 3 is the rollback path, not the old container.

`config.yaml` (with `secret_key`) travels inside the data dir, so existing device
and CLI tokens stay valid — no forced re-auth beyond the URL change below.

## Changing the published port

If `8080`/`9000` are already taken on the new host, remap only the **published
(host) side** and leave the container-internal port alone:

```yaml
# supernote service — host 7832 -> container 8080
ports:
  - "${BIND_ADDR:-0.0.0.0}:7832:8080"
# leave SUPERNOTE_PORT: 8080, and manta-mcp's SUPERNOTE_CLOUD_URL: http://supernote:8080
```

Changing *every* `8080` (the internal `SUPERNOTE_PORT` and the MCP's
`SUPERNOTE_CLOUD_URL`) also works, but only if you recreate **both** containers:
a `manta-mcp` started against the old URL keeps it until recreated, so it silently
dials a port nothing listens on and every tool call times out (the cloud itself is
fine — the CLI/device hit the published port and work). Fix:
`docker compose up -d --force-recreate manta-mcp`. The device and CLI always use
the published host port regardless.

## Fedora / RHEL (SELinux)

- **Bind mount:** the compose file mounts the data dir with `:Z`, which relabels it
  to a container-private SELinux context. Without it the container gets
  `Permission denied` on `/data`. SELinux labels don't survive `rsync` from a
  non-SELinux host (e.g. macOS) — `:Z` re-applies them on first start, so that's fine.
- **Podman:** works with the same compose file (`podman compose up -d`). Prefer
  **rootful** podman to match the file ownership in the image; rootless needs
  `--userns=keep-id`-style mapping and host-dir chown, which this file doesn't set up.

## Tailscale (the network is the boundary)

After `tailscale up` on the new host, use its **MagicDNS name** (e.g.
`cloudhost.tailnet-xxxx.ts.net`) everywhere instead of the raw `100.x` IP, so a
future IP change never breaks anything.

- **firewalld:** Fedora filters by zone, but Docker inserts its own iptables rules
  and can bypass firewalld. To keep the cloud tailnet-only, the reliable lever is
  `BIND_ADDR=<host-tailscale-ip>` in `.env` (publishes 8080/9000 on the tailscale
  interface only) rather than firewalld rules. Rootful Podman integrates with
  firewalld more predictably if you prefer the zone approach
  (`firewall-cmd --zone=trusted --add-interface=tailscale0`).

## Repoint the clients off the old tailnet

1. **Manta device:** while still offline, set its private-cloud server URL to
   `http://<new-magicdns>:8080`, then re-enable Wi-Fi and trigger a sync. Verify
   the round trip before trusting it: edit any note, sync, and confirm the file's
   timestamp updates on the new cloud (`supernote cloud ls`).
2. **CLI / manta skill:** `supernote cloud login <account> --url http://<new-magicdns>:8080`
   (rewrites the cached token + host).
3. **MCP registration:** `claude mcp remove manta-cloud` then
   `claude mcp add --transport http manta-cloud http://<new-magicdns>:9000/mcp`.

## Token lifetime

The web/CLI JWT defaults to `auth.expiration_hours: 24` — fine for a multi-user
server, but on a single-user tailnet-bound self-host it just means a re-login
every day. Raise it in `config/config.yaml` (the value travels with the data dir):

```yaml
auth:
  expiration_hours: 8760   # 1 year; device tokens already get 10 years
```

Restart the cloud to load it; existing tokens keep their original expiry, so
re-login once afterward to mint a long-lived one.

## Production hygiene

Don't set `SUPERNOTE_DEBUG_EMIT` on the new host — it enables `POST /api/debug/emit`
(a push-protocol eval harness) and is off by default.
