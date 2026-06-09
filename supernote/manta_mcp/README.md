# Manta MCP

A Model Context Protocol server over the self-hosted Supernote cloud. Lets an
agent on any tailnet machine list, read, search, push, and delete documents on
the cloud — and a pushed document auto-syncs to the Manta device.

It is a **sibling** to the cloud server: it reuses `supernote.client` for auth
and endpoints and talks to the cloud over HTTP. It is *not* the AI/insights MCP
that ships gated-off inside `supernote.server`.

## Tools (v1)

| Tool | Kind | Notes |
|---|---|---|
| `list_documents(path="/")` | read | entries under a folder |
| `read_document(path)` | read | text/markdown/PDF returned as text; `.note`/binary = metadata only |
| `search_documents(query, limit=20)` | read | **name** match (semantic search needs the AI pipeline, which is off) |
| `push_document(path, content)` | write | UTF-8 upload; auto-syncs to the Manta |
| `delete_document(path)` | destructive | moves to recycle |
| `make_folder(path)` | write | |

`.note` handwriting reading (OCR) is **v2** (`read_note`).

## Run

```bash
pip install -e '.[manta-mcp]'

# auth: a token, or account creds (logs in once and caches the token)
export SUPERNOTE_CLOUD_URL=http://localhost:8080     # the cloud, NOT 0.0.0.0
export SUPERNOTE_EMAIL=you@supernote.local
export SUPERNOTE_PASSWORD=...
# export SUPERNOTE_TOKEN=...                          # alternative to email/password
# export MANTA_MCP_BEARER=...                          # optional: require a bearer on /mcp

manta-mcp                                              # serves http://0.0.0.0:9000/mcp
```

### As a sibling container (co-located with the cloud)

```bash
docker build -f Dockerfile.manta-mcp -t supernote:manta-mcp .
docker run -d --name manta-mcp \
  -p <tailnet-ip>:9000:9000 \
  -e SUPERNOTE_CLOUD_URL=http://<cloud-host>:8080 \
  -e SUPERNOTE_EMAIL=you@supernote.local \
  -e SUPERNOTE_PASSWORD=... \
  supernote:manta-mcp
```

Bind it to the tailnet (publish on the `100.x` IP or front it with Tailscale
Serve), exactly like the cloud. The tailnet is the security boundary; set
`MANTA_MCP_BEARER` for defense-in-depth.

## Connect an agent

```bash
# Claude Code (add --scope user for all projects; --header for bearer)
claude mcp add --transport http manta-cloud http://<tailnet-ip>:9000/mcp
```

Claude Desktop / Claude.ai: Settings → Connectors → Add custom connector → paste
the URL.

## Smoke test

```bash
npx @modelcontextprotocol/inspector --cli http://localhost:9000/mcp \
  --transport http --method tools/list
```
