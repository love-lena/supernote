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
# 1. On the OLD host: stop the cloud so SQLite is quiesced (no mid-write copy).
docker stop supernote

# 2. Copy the data dir to the new host (any transport).
rsync -av /path/to/old/supernote-data/  newhost:/srv/supernote-data/

# 3. On the NEW host: point .env at it and start.
#    SUPERNOTE_DATA_DIR=/srv/supernote-data
docker compose up -d --build
```

`config.yaml` (with `secret_key`) travels inside the data dir, so existing device
and CLI tokens stay valid — no forced re-auth beyond the URL change below.

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

1. **Manta device:** set its private-cloud server URL to `http://<new-magicdns>:8080`.
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
