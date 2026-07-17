# LessWrong → Manta Inbox (RSS-style reading mailbox)

**Status:** design / proposed
**Date:** 2026-06-29

## Goal

Give the Manta a "mailbox" for LessWrong posts: a scheduled job pulls selected
LessWrong content, renders each post as a reflowable EPUB, and pushes it into
`/INBOX/LessWrong/` on the self-hosted cloud — which auto-syncs to the device.
No native RSS reader exists on the Manta; this fakes one well by reusing the
existing cloud + auto-sync + EPUB toolchain.

## Sources (what lands in the inbox)

A merged, de-duplicated set from three kinds of source, all queried via the
LessWrong **GraphQL API** (`https://www.lesswrong.com/graphql`). RSS `feed.xml`
is *not* used for discovery: its `tagId`/`userId` params are unofficial and
`tagId` is silently ignored today (verified 2026-06-29 — a `tagId` feed returned
unrelated posts). GraphQL is uniform and reliable.

| Source | Endpoint | Query | Filter |
|---|---|---|---|
| Curated | **AlignmentForum** | `posts(terms:{view:"curated"})` | always included |
| Author: Buck (`rx7xLaHCh3m7Po385`) | LessWrong | `posts(terms:{view:"userPosts", userId})` | always included |
| Author: ryan_greenblatt (`dfZAq9eZxs4BB4Ji5`) | LessWrong | `posts(terms:{view:"userPosts", userId})` | always included |
| Author: Alex Mallen (`gnHJfWPpHPMZkoySr`) | LessWrong | `posts(terms:{view:"userPosts", userId})` | always included |
| Tag: AI Control (`F5gRQdEQHzi3tQ5Ay`) | LessWrong | `posts(terms:{view:"new", filterSettings:{tags:[{tagId, filterMode:"Required"}]}})` | `baseScore >= 15` |
| Tag: Redwood Research (`dHfxtPwAmrij4KEce`) | LessWrong | same shape | `baseScore >= 15` |

### Source selection — why this split (decided 2026-06-29)

AlignmentForum runs the **same ForumMagnum/GraphQL backend** as LessWrong (same
post/user/tag `_id`s) but is af-scoped. Cross-checked against the configured
filter:

- **Curated → AlignmentForum.** AF's `curated` view is almost entirely alignment
  research (the configured authors + the Redwood org show up organically),
  whereas LW's `curated` is broad (it served e.g. "More Failed Eggless Choux").
  Big signal win, zero extra config — just a different endpoint for one view.
- **Authors → LessWrong (all posts).** Only ~53–67% of these authors' recent
  posts cross-post to AF, and the LW-only remainder is research-dense (e.g.
  Mallen's *Incriminating misaligned AI models via distillation*, Greenblatt's
  *Reward Hacking Without Egregious Misalignment*). AF-scoping them would drop
  real work. No karma floor on authors (their off-topic posts can be high-karma,
  so a floor wouldn't separate signal from noise anyway).
- **Tags → LessWrong.** Tag `filterSettings` queries return **0 results** on the
  AF endpoint; they only work on LW. (AF karma is a separate, ~5× smaller
  `afBaseScore`, so a `baseScore`-15 floor would not transfer cleanly anyway.)

Endpoints are config fields: `curated_url` (default AF) and `graphql_url`
(default LW, used for authors + tags).

- **Karma floor** (`baseScore >= 15`) applies to *tag* sources only — curated and
  named authors are always included regardless of score. (The AI Control tag has
  ~721 posts, many low-karma; the floor trims noise.)
- IDs above were resolved via GraphQL and verified 2026-06-29. "Redwood Research"
  has no user account, so it is covered as a **tag**.
- Full post body is fetched per post with
  `post(selector:{_id}){result{title htmlBody postedAt pageUrl user{displayName}}}`
  — feed/listing items carry only an excerpt.

## Pipeline

1. **Discover** — run each source query, collect `_id`s (cap each source, e.g. 20
   most-recent, so a query never floods).
2. **Dedup** — drop any `_id` already in the state file. On **first run**, do a
   bounded **backfill**: push the most-recent N per source (default 5), mark the
   rest seen. Thereafter, new-only.
3. **Fetch** — `htmlBody` + metadata per new `_id`.
4. **Render EPUB** — build a markdown doc (YAML frontmatter: title, author, date,
   source URL) from the HTML body, then `pandoc` → reflowable EPUB using the
   vendored `footnotes.lua` + `epub.css`. Math (occasional MathJax) is converted
   to MathML; device support is best-effort (test on hardware).
5. **Push** — `await sn.device.upload_content("/INBOX/LessWrong/<slug>.epub",
   bytes, equipment_no="WEB")`. `equipment_no="WEB"` marks it a non-device upload
   so the Socket.IO push fires a device sync. Ensure the folder exists first via
   `create_folder`.
6. **Record** — append pushed `_id`s to the state file.

## Reuse (do not reinvent)

- **Conversion:** mirror `~/.agents/skills/manta/editorial.sh`'s EPUB path
  (`pandoc … --lua-filter footnotes.lua --css epub.css`). **Vendor** copies of
  `footnotes.lua` + `epub.css` into the repo (`supernote/integrations/assets/`)
  so the tool is self-contained on pikachu (the skill dir does not exist there).
- **Push/auth:** mirror `supernote/manta_mcp/server.py` — `Supernote.from_token` /
  `Supernote.login`, env config `SUPERNOTE_CLOUD_URL` / `SUPERNOTE_TOKEN` /
  `SUPERNOTE_EMAIL`+`SUPERNOTE_PASSWORD`.

## Code layout

- `supernote/integrations/__init__.py`
- `supernote/integrations/lesswrong.py` — fetch (GraphQL), render (pandoc), push
  (client), state. CLI entry: `python -m supernote.integrations.lesswrong`.
  Flags: `--once` (default), `--loop --interval <dur>`, `--state <path>`,
  `--backfill N`, `--karma-floor N`, `--dry-run`.
- `supernote/integrations/assets/{footnotes.lua,epub.css}` — vendored.
- `script/lesswrong` — thin "Scripts to Rule Them All" wrapper.
- `tests/integrations/test_lesswrong.py` — unit tests (GraphQL parse, dedup,
  karma filter, slug/markdown build, state round-trip) with mocked HTTP + pandoc.

Strict-typed (the new module is *not* under the mypy-excluded `notebook/`/`cli/`).

## Deployment (pikachu) — deployed + verified 2026-06-29

A dedicated image (`Dockerfile.lesswrong`, adds `pandoc`) + a compose service
alongside `supernote` + `manta-mcp`:

```yaml
  lesswrong-inbox:
    image: supernote:lesswrong
    build: { context: ., dockerfile: Dockerfile.lesswrong }
    depends_on: [supernote]
    restart: unless-stopped
    # CMD in the image: --loop --interval 6h --state /data/lesswrong-seen.json
    environment:
      SUPERNOTE_CLOUD_URL: http://supernote:8080   # pikachu remaps this to :7832
      SUPERNOTE_EMAIL: ${SUPERNOTE_EMAIL:?}
      SUPERNOTE_PASSWORD: ${SUPERNOTE_PASSWORD:?}
    volumes:
      - lesswrong-state:/data        # dedicated; see SELinux note
volumes:
  lesswrong-state:
```

**SELinux `:Z` gotcha (learned the hard way on pikachu, an Enforcing host):** do
*not* mount the cloud's `${SUPERNOTE_DATA_DIR}:/data:Z` into this service. `:Z`
relabels the bind to a *private* MCS category for this container, which
relabels the cloud's live SQLite DB out from under the running `supernote`
container (different category) — reads survive on its open fd but **all writes
fail with "database is locked"**, blocking device sync. Use a dedicated named
volume instead. If it ever happens: `docker compose up -d --force-recreate
supernote` re-labels `/data` back to the cloud container.

**Port remap:** pikachu runs the cloud on `:7832` (not 8080), so the deployed
service sets `SUPERNOTE_CLOUD_URL: http://supernote:7832`.

First-run seed used `--backfill 3` → 17 EPUBs into `/INBOX/LessWrong` (verified
via the manta-cloud MCP); the loop then runs steady-state (0 re-pushed).

## Out of scope (v1)

- PDF redline output (EPUB only; reflow is the win on the Manta).
- Comments, shortform, sequences.
- Per-author/tag dedup of cross-posted items beyond `_id` identity.
- Incremental GraphQL cursors — bounded most-recent-N polling is enough at this cadence.
