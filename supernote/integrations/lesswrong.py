"""LessWrong -> Manta inbox.

A scheduled "reading mailbox": pull selected LessWrong posts via the GraphQL
API, render each as a reflowable EPUB, and push it to the self-hosted cloud
(which auto-syncs to the Manta device). Sources are config-driven — curated,
named authors, and tags (with per-tag karma floors) all live in a YAML config.

Design notes live in ``docs/superpowers/specs/2026-06-29-lesswrong-inbox-design.md``.
"""

from __future__ import annotations

import argparse
import asyncio
import html as _html
import json
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mashumaro.config import TO_DICT_ADD_OMIT_NONE_FLAG, BaseConfig
from mashumaro.mixins.yaml import DataClassYAMLMixin

logger = logging.getLogger(__name__)

GRAPHQL_URL = "https://www.lesswrong.com/graphql"
# AlignmentForum shares LessWrong's GraphQL backend (same post/user/tag ids) but
# is af-scoped, so its `curated` view is a far better-targeted alignment feed
# than LW's broad curation. Author + tag queries stay on LW: only ~60% of these
# authors' posts cross-post to AF, and tag `filterSettings` returns nothing on
# the AF endpoint (verified 2026-06-29).
ALIGNMENT_FORUM_GRAPHQL_URL = "https://www.alignmentforum.org/graphql"

# Vendored pandoc assets (copied from the `manta` skill) so this tool is
# self-contained on a deploy host where the skill dir does not exist.
_ASSETS = Path(__file__).parent / "assets"

# Fields requested for each post in a listing query.
_POST_FIELDS = "results { _id title postedAt baseScore user { displayName } }"


# --------------------------------------------------------------------------- #
# Config (YAML-driven; see default_config for the shipped preset)
# --------------------------------------------------------------------------- #


@dataclass
class AuthorSource(DataClassYAMLMixin):
    """An author whose posts are always pulled (no karma floor)."""

    name: str
    user_id: str


@dataclass
class TagSource(DataClassYAMLMixin):
    """A tag whose posts are pulled when at or above ``karma_floor``."""

    name: str
    tag_id: str
    karma_floor: int = 15


@dataclass
class LessWrongConfig(DataClassYAMLMixin):
    """What lands in the inbox and how. Fully expressible as YAML."""

    curated: bool = True
    authors: list[AuthorSource] = field(default_factory=list)
    tags: list[TagSource] = field(default_factory=list)
    backfill: int = 5
    """On first run, push this many most-recent posts per source; suppress the rest."""
    per_source_limit: int = 20
    """How many recent posts to fetch per source per poll."""
    dest_folder: str = "/INBOX/LessWrong"
    graphql_url: str = GRAPHQL_URL
    """Endpoint for author + tag queries (LessWrong)."""
    curated_url: str = ALIGNMENT_FORUM_GRAPHQL_URL
    """Endpoint for the `curated` view (AlignmentForum — better-targeted)."""

    class Config(BaseConfig):
        omit_none = True
        code_generation_options = [TO_DICT_ADD_OMIT_NONE_FLAG]  # type: ignore[list-item]

    @classmethod
    def load(cls, config_file: str | Path | None = None) -> "LessWrongConfig":
        """Load from a YAML file, falling back to the shipped default preset."""
        if config_file is not None:
            path = Path(config_file)
            if path.exists():
                return cls.from_yaml(path.read_text())
        return default_config()


def default_config() -> LessWrongConfig:
    """The shipped preset: curated + 3 named authors + 2 tags (karma >= 15).

    IDs resolved/verified via the LessWrong GraphQL API on 2026-06-29.
    """
    return LessWrongConfig(
        curated=True,
        authors=[
            AuthorSource("Buck", "rx7xLaHCh3m7Po385"),
            AuthorSource("ryan_greenblatt", "dfZAq9eZxs4BB4Ji5"),
            AuthorSource("Alex Mallen", "gnHJfWPpHPMZkoySr"),
        ],
        tags=[
            TagSource("AI Control", "F5gRQdEQHzi3tQ5Ay", karma_floor=15),
            TagSource("Redwood Research", "dHfxtPwAmrij4KEce", karma_floor=15),
        ],
    )


# --------------------------------------------------------------------------- #
# Post model
# --------------------------------------------------------------------------- #


@dataclass
class PostRef:
    """A post as seen in a listing query."""

    id: str
    title: str
    author: str
    posted_at: str
    base_score: int
    source: str


@dataclass
class PostDetail:
    """A post's full body, fetched per-id."""

    html_body: str
    page_url: str
    title: str


@dataclass
class DiscoveredSource:
    """Posts found for one configured source, with the karma floor to apply
    (``None`` for curated/authors, which are always included)."""

    label: str
    karma_floor: int | None
    posts: list[PostRef]


@dataclass
class RunPlan:
    to_push: list[PostRef]
    new_seen: set[str]


# --------------------------------------------------------------------------- #
# GraphQL queries + parsing
# --------------------------------------------------------------------------- #


def posts_query(
    kind: str, limit: int, *, user_id: str | None = None, tag_id: str | None = None
) -> str:
    """Build a `posts` listing query for a source ``kind``."""
    if kind == "curated":
        terms = f'view: "curated", limit: {limit}'
    elif kind == "author":
        terms = f'view: "userPosts", userId: "{user_id}", limit: {limit}'
    elif kind == "tag":
        terms = (
            'view: "new", filterSettings: { tags: [{ tagId: '
            f'"{tag_id}", filterMode: "Required" }}] }}, limit: {limit}'
        )
    else:
        raise ValueError(f"unknown source kind: {kind!r}")
    return f"query {{ posts(input: {{ terms: {{ {terms} }} }}) {{ {_POST_FIELDS} }} }}"


def post_detail_query(post_id: str) -> str:
    """Build a single-`post` query for the full body."""
    return (
        "query { post(input: { selector: { _id: "
        f'"{post_id}" }} }}) {{ result {{ title htmlBody pageUrl }} }} }}'
    )


def parse_posts(response: dict[str, Any], source: str) -> list[PostRef]:
    """Parse a `posts` listing response into PostRefs, tagging the source."""
    posts_node = (response.get("data") or {}).get("posts") or {}
    results = posts_node.get("results") or []
    out: list[PostRef] = []
    for r in results:
        user = r.get("user") or {}
        out.append(
            PostRef(
                id=r["_id"],
                title=r.get("title") or "",
                author=user.get("displayName") or "",
                posted_at=r.get("postedAt") or "",
                base_score=int(r.get("baseScore") or 0),
                source=source,
            )
        )
    return out


def parse_post_detail(response: dict[str, Any]) -> PostDetail:
    """Parse a single-`post` response into a PostDetail."""
    result = ((response.get("data") or {}).get("post") or {}).get("result") or {}
    return PostDetail(
        html_body=result.get("htmlBody") or "",
        page_url=result.get("pageUrl") or "",
        title=result.get("title") or "",
    )


# --------------------------------------------------------------------------- #
# Planning: karma floor, dedup, backfill
# --------------------------------------------------------------------------- #


def _passes(post: PostRef, karma_floor: int | None) -> bool:
    return karma_floor is None or post.base_score >= karma_floor


def plan_run(
    sources: list[DiscoveredSource],
    seen: set[str],
    backfill: int,
    first_run: bool,
) -> RunPlan:
    """Decide which posts to push and the updated seen-set.

    Steady state: push every eligible (passes its source's karma floor),
    not-yet-seen post; mark those seen. First run: per source, push only the
    ``backfill`` most-recent eligible posts but mark *all* eligible posts seen
    so the backlog never floods later. Posts below a karma floor are never
    marked seen, so they can qualify later if their score rises.
    """
    new_seen = set(seen)
    chosen: dict[str, PostRef] = {}

    for src in sources:
        eligible = [
            p for p in src.posts if _passes(p, src.karma_floor) and p.id not in seen
        ]
        if first_run:
            recent_first = sorted(eligible, key=lambda p: p.posted_at, reverse=True)
            for p in recent_first[:backfill]:
                chosen.setdefault(p.id, p)
            for p in eligible:  # suppress the rest of the backlog
                new_seen.add(p.id)
        else:
            for p in eligible:
                chosen.setdefault(p.id, p)
                new_seen.add(p.id)

    to_push = sorted(chosen.values(), key=lambda p: p.posted_at, reverse=True)
    return RunPlan(to_push=to_push, new_seen=new_seen)


# --------------------------------------------------------------------------- #
# Filenames + HTML assembly
# --------------------------------------------------------------------------- #

_RESERVED = set('<>:"|?*')


def slugify(title: str) -> str:
    """A filesystem-safe, human-readable rendering of a post title."""
    # Normalize all whitespace (incl. NBSP \xa0 / other Unicode spaces, which are
    # not `isprintable`) to a plain space FIRST, so it survives the filter below
    # as a space rather than being dropped (which would join adjacent words).
    s = re.sub(r"\s+", " ", title)
    s = s.replace("/", " ").replace("\\", " ")
    s = "".join(c for c in s if c not in _RESERVED and c.isprintable())
    s = re.sub(r"\s+", " ", s).strip(" .")
    s = s[:100].strip(" .")
    return s or "untitled"


def epub_filename(post: PostRef) -> str:
    """`YYYY-MM-DD Title [id].epub` — chronological + readable, with the post id
    as a stable suffix so two posts sharing a date+title (or the same truncated
    slug) never collide on one cloud path (the VFS replaces by path)."""
    return f"{post.posted_at[:10]} {slugify(post.title)} [{post.id}].epub"


def build_html_document(post: PostRef, html_body: str, page_url: str) -> str:
    """Wrap a post body in a standalone HTML document for pandoc."""
    title = _html.escape(post.title)
    author = _html.escape(post.author)
    date = post.posted_at[:10]
    url = _html.escape(page_url, quote=True)
    return (
        '<!DOCTYPE html>\n<html><head><meta charset="utf-8">'
        f"<title>{title}</title></head><body>\n"
        f"<h1>{title}</h1>\n"
        f'<p class="byline"><em>{author} · {date}</em></p>\n'
        f'<p class="source">Source: <a href="{url}">{url}</a></p>\n'
        "<hr/>\n"
        f"{html_body}\n"
        "</body></html>\n"
    )


def render_epub(html_document: str, out_path: Path) -> None:
    """Render an HTML document to a reflowable EPUB via pandoc + vendored assets."""
    cmd = [
        "pandoc",
        "-f",
        "html",
        "-t",
        "epub",
        "--quiet",
        "--lua-filter",
        str(_ASSETS / "footnotes.lua"),
        "--css",
        str(_ASSETS / "epub.css"),
        "-o",
        str(out_path),
    ]
    subprocess.run(cmd, input=html_document.encode("utf-8"), check=True)


# --------------------------------------------------------------------------- #
# Push + state
# --------------------------------------------------------------------------- #


async def push_epub(sn: Any, dest_folder: str, filename: str, data: bytes) -> None:
    """Ensure the destination folder exists, then upload the EPUB.

    Uploaded with ``equipment_no="WEB"`` (a non-device upload) so the cloud's
    Socket.IO push fires a device sync.
    """
    try:
        await sn.device.create_folder(dest_folder, equipment_no="WEB")
    except Exception as e:  # folder likely already exists; upload still proceeds
        logger.debug("create_folder(%s) skipped: %s", dest_folder, e)
    path = f"{dest_folder.rstrip('/')}/{filename}"
    await sn.device.upload_content(path, data, equipment_no="WEB")


def load_seen(path: str | Path) -> set[str]:
    """Load the set of already-pushed post ids (empty if the file is absent)."""
    p = Path(path)
    if not p.exists():
        return set()
    data = json.loads(p.read_text())
    return set(data.get("seen", []))


def save_seen(path: str | Path, ids: set[str]) -> None:
    """Persist the seen-set as a stable (sorted) JSON file."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"seen": sorted(ids)}, indent=2) + "\n")


# --------------------------------------------------------------------------- #
# Discovery + orchestration
# --------------------------------------------------------------------------- #


async def graphql(session: Any, url: str, query: str) -> dict[str, Any]:
    """POST a GraphQL query and return the parsed JSON response.

    Raises on a GraphQL-level ``errors`` array — a 200 response can still carry
    errors with a null ``data``, and we must not let that silently become empty
    data that downstream turns into a near-empty EPUB.
    """
    async with session.post(url, json={"query": query}) as resp:
        resp.raise_for_status()
        data: dict[str, Any] = await resp.json()
    if data.get("errors"):
        raise RuntimeError(f"GraphQL errors from {url}: {data['errors']}")
    return data


async def discover(session: Any, config: LessWrongConfig) -> list[DiscoveredSource]:
    """Query every configured source and return its posts + karma floor."""
    sources: list[DiscoveredSource] = []
    limit = config.per_source_limit
    if config.curated:
        resp = await graphql(session, config.curated_url, posts_query("curated", limit))
        sources.append(DiscoveredSource("curated", None, parse_posts(resp, "curated")))
    for author in config.authors:
        label = f"author:{author.name}"
        resp = await graphql(
            session,
            config.graphql_url,
            posts_query("author", limit, user_id=author.user_id),
        )
        sources.append(DiscoveredSource(label, None, parse_posts(resp, label)))
    for tag in config.tags:
        label = f"tag:{tag.name}"
        resp = await graphql(
            session, config.graphql_url, posts_query("tag", limit, tag_id=tag.tag_id)
        )
        sources.append(
            DiscoveredSource(label, tag.karma_floor, parse_posts(resp, label))
        )
    return sources


async def fetch_detail(
    session: Any, config: LessWrongConfig, post_id: str
) -> PostDetail:
    """Fetch a single post's full body."""
    resp = await graphql(session, config.graphql_url, post_detail_query(post_id))
    return parse_post_detail(resp)


def post_url(post: PostRef) -> str:
    """Fallback canonical URL when the API omits pageUrl."""
    return f"https://www.lesswrong.com/posts/{post.id}"


async def run_once(
    config: LessWrongConfig,
    state_path: str | Path,
    session: Any,
    sn: Any,
    *,
    first_run: bool | None = None,
    dry_run: bool = False,
    workdir: Path | None = None,
) -> RunPlan:
    """One discover -> plan -> render -> push -> persist cycle.

    ``first_run`` defaults to "the state file does not exist yet" (triggers the
    bounded backfill). Posts whose EPUB render fails are left out of the saved
    seen-set so they are retried next run.
    """
    seen = load_seen(state_path)
    fr = first_run if first_run is not None else not Path(state_path).exists()
    if workdir is None:
        workdir = Path(tempfile.mkdtemp(prefix="lw-inbox-"))

    sources = await discover(session, config)
    plan = plan_run(sources, seen, config.backfill, fr)
    logger.info(
        "LessWrong inbox: %d post(s) to push (first_run=%s, dry_run=%s)",
        len(plan.to_push),
        fr,
        dry_run,
    )

    failed: set[str] = set()
    for post in plan.to_push:
        try:
            detail = await fetch_detail(session, config, post.id)
            if not detail.html_body.strip():
                # A missing/empty body would render an almost-empty EPUB; treat
                # it as a failure so the post is retried instead of marked seen.
                raise ValueError(f"empty htmlBody for post {post.id} ({post.title})")
            document = build_html_document(
                post, detail.html_body, detail.page_url or post_url(post)
            )
            filename = epub_filename(post)
            epub_path = workdir / filename
            render_epub(document, epub_path)
            if dry_run:
                logger.info("[dry-run] would push %s", filename)
            else:
                await push_epub(
                    sn, config.dest_folder, filename, epub_path.read_bytes()
                )
                logger.info("pushed %s", filename)
        except Exception:
            logger.exception("failed to process post %s (%s)", post.id, post.title)
            failed.add(post.id)

    if not dry_run:
        save_seen(state_path, plan.new_seen - failed)
    return plan


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> int:
    """Parse a duration like ``6h``/``30m``/``90s``/``45`` into seconds."""
    text = text.strip()
    if text and text[-1] in _DURATION_UNITS:
        return int(text[:-1]) * _DURATION_UNITS[text[-1]]
    return int(text)


_LOGIN_ATTEMPTS = 3


async def _make_cloud_session(host: str) -> Any:
    """Authenticate to the cloud the same way manta-mcp does (env-driven).

    Retries the login a few times: the login challenge is a server-side DB write
    and the cloud's SQLite can transiently report "database is locked" under
    concurrent sync load, which would otherwise drop a whole poll cycle.
    """
    from supernote.client import Supernote

    if token := os.environ.get("SUPERNOTE_TOKEN"):
        return Supernote.from_token(token, host=host)
    email = os.environ.get("SUPERNOTE_EMAIL")
    password = os.environ.get("SUPERNOTE_PASSWORD")
    if not (email and password):
        raise RuntimeError(
            "No credentials: set SUPERNOTE_TOKEN, or SUPERNOTE_EMAIL + SUPERNOTE_PASSWORD."
        )
    last_exc: Exception | None = None
    for attempt in range(1, _LOGIN_ATTEMPTS + 1):
        try:
            return await Supernote.login(email, password, host=host)
        except Exception as e:
            last_exc = e
            logger.warning(
                "login attempt %d/%d failed: %s", attempt, _LOGIN_ATTEMPTS, e
            )
            if attempt < _LOGIN_ATTEMPTS:
                await asyncio.sleep(2 * attempt)
    raise RuntimeError(f"login failed after {_LOGIN_ATTEMPTS} attempts") from last_exc


async def _run(args: argparse.Namespace) -> None:
    import aiohttp

    config = LessWrongConfig.load(args.config)
    if args.backfill is not None:
        config.backfill = args.backfill
    if args.karma_floor is not None:
        for tag in config.tags:
            tag.karma_floor = args.karma_floor
    if args.dest is not None:
        config.dest_folder = args.dest

    host = os.environ.get("SUPERNOTE_CLOUD_URL", "http://localhost:8080")
    interval = parse_duration(args.interval) if args.loop else 0

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                if args.dry_run:
                    await run_once(
                        config,
                        args.state,
                        session=session,
                        sn=None,
                        dry_run=True,
                        first_run=None,
                    )
                else:
                    # Authenticate fresh each run: a long-running loop must never
                    # operate on an expired token (server web tokens default to
                    # ~24h). The `async with` also closes the session each cycle.
                    async with await _make_cloud_session(host) as sn:
                        await run_once(
                            config,
                            args.state,
                            session=session,
                            sn=sn,
                            dry_run=False,
                            first_run=None,
                        )
            except Exception:
                logger.exception("LessWrong inbox run failed")
            if not args.loop:
                break
            logger.info("sleeping %ds until next poll", interval)
            await asyncio.sleep(interval)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="supernote-lesswrong",
        description="Pull selected LessWrong posts into the Manta /INBOX as EPUBs.",
    )
    parser.add_argument("--config", help="Path to a LessWrong inbox YAML config.")
    parser.add_argument(
        "--state",
        default="lesswrong-seen.json",
        help="Path to the seen-ids state file (default: ./lesswrong-seen.json).",
    )
    parser.add_argument(
        "--loop", action="store_true", help="Run forever on an interval."
    )
    parser.add_argument(
        "--interval", default="6h", help="Poll interval for --loop (e.g. 6h)."
    )
    parser.add_argument(
        "--backfill", type=int, help="Override first-run backfill count."
    )
    parser.add_argument(
        "--karma-floor", type=int, help="Override the karma floor for all tag sources."
    )
    parser.add_argument("--dest", help="Override the destination folder.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Discover + render but do not push or persist.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging."
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
