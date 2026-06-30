"""Tests for the LessWrong -> Manta inbox integration."""

import json
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from supernote.integrations.lesswrong import (
    AuthorSource,
    DiscoveredSource,
    LessWrongConfig,
    PostRef,
    TagSource,
    build_html_document,
    default_config,
    discover,
    epub_filename,
    load_seen,
    parse_duration,
    parse_post_detail,
    parse_posts,
    plan_run,
    posts_query,
    push_epub,
    render_epub,
    run_once,
    save_seen,
    slugify,
)

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


def test_default_config_has_curated_three_authors_two_tags() -> None:
    """The shipped default mirrors the spec: curated + 3 authors + 2 tags."""
    cfg = default_config()
    assert cfg.curated is True
    assert {a.name for a in cfg.authors} == {"Buck", "ryan_greenblatt", "Alex Mallen"}
    assert {a.user_id for a in cfg.authors} == {
        "rx7xLaHCh3m7Po385",
        "dfZAq9eZxs4BB4Ji5",
        "gnHJfWPpHPMZkoySr",
    }
    assert {t.name for t in cfg.tags} == {"AI Control", "Redwood Research"}
    assert all(t.karma_floor == 15 for t in cfg.tags)
    # curated comes from AlignmentForum (better-targeted); authors+tags from LW
    assert "alignmentforum.org" in cfg.curated_url
    assert "lesswrong.com" in cfg.graphql_url


def test_config_is_yaml_round_trippable() -> None:
    """Config is fully expressible as YAML (so it is config-driven)."""
    cfg = LessWrongConfig(
        curated=False,
        authors=[AuthorSource(name="Zvi", user_id="zzz")],
        tags=[TagSource(name="Rationality", tag_id="ttt", karma_floor=30)],
        backfill=3,
    )
    restored = LessWrongConfig.from_yaml(cfg.to_yaml())
    assert restored == cfg


def test_config_load_falls_back_to_default_when_file_absent(tmp_path: Path) -> None:
    cfg = LessWrongConfig.load(tmp_path / "nope.yaml")
    assert cfg == default_config()


def test_config_load_reads_file(tmp_path: Path) -> None:
    custom = LessWrongConfig(curated=False, authors=[AuthorSource("Zvi", "z1")])
    f = tmp_path / "lw.yaml"
    f.write_text(custom.to_yaml())
    assert LessWrongConfig.load(f) == custom


# --------------------------------------------------------------------------- #
# GraphQL query construction
# --------------------------------------------------------------------------- #


def test_posts_query_curated() -> None:
    q = posts_query("curated", limit=20)
    assert "curated" in q
    assert "20" in q


def test_posts_query_author_uses_user_posts_view_and_id() -> None:
    q = posts_query("author", limit=5, user_id="rx7xLaHCh3m7Po385")
    assert "userPosts" in q
    assert "rx7xLaHCh3m7Po385" in q


def test_posts_query_tag_uses_required_filter_setting() -> None:
    q = posts_query("tag", limit=5, tag_id="F5gRQdEQHzi3tQ5Ay")
    assert "filterSettings" in q
    assert "Required" in q
    assert "F5gRQdEQHzi3tQ5Ay" in q


# --------------------------------------------------------------------------- #
# Response parsing
# --------------------------------------------------------------------------- #

_POSTS_JSON = {
    "data": {
        "posts": {
            "results": [
                {
                    "_id": "abc",
                    "title": "Offline Monitoring",
                    "postedAt": "2026-06-28T13:20:54.692Z",
                    "baseScore": 9,
                    "user": {"displayName": "Frederik"},
                },
                {
                    "_id": "def",
                    "title": "Door's Locked",
                    "postedAt": "2026-06-24T19:13:08.027Z",
                    "baseScore": 68,
                    "user": {"displayName": "Prakrat"},
                },
            ]
        }
    }
}


def test_parse_posts_extracts_fields_and_source_label() -> None:
    posts = parse_posts(_POSTS_JSON, source="tag:AI Control")
    assert [p.id for p in posts] == ["abc", "def"]
    assert posts[0].title == "Offline Monitoring"
    assert posts[0].author == "Frederik"
    assert posts[0].base_score == 9
    assert posts[0].posted_at == "2026-06-28T13:20:54.692Z"
    assert posts[1].base_score == 68
    assert all(p.source == "tag:AI Control" for p in posts)


def test_parse_posts_tolerates_missing_user() -> None:
    blob = {
        "data": {
            "posts": {
                "results": [{"_id": "x", "title": "T", "postedAt": "z", "baseScore": 1}]
            }
        }
    }
    posts = parse_posts(blob, source="curated")
    assert posts[0].author == ""


def test_parse_post_detail_returns_body_and_url() -> None:
    blob = {
        "data": {
            "post": {
                "result": {
                    "title": "Offline Monitoring",
                    "htmlBody": "<p>hello</p>",
                    "pageUrl": "https://www.lesswrong.com/posts/abc/offline",
                }
            }
        }
    }
    detail = parse_post_detail(blob)
    assert detail.html_body == "<p>hello</p>"
    assert detail.page_url == "https://www.lesswrong.com/posts/abc/offline"


# --------------------------------------------------------------------------- #
# Planning: karma floor, dedup, backfill
# --------------------------------------------------------------------------- #


def _p(id: str, score: int, posted: str, source: str) -> PostRef:
    return PostRef(
        id=id,
        title=id.upper(),
        author="a",
        posted_at=posted,
        base_score=score,
        source=source,
    )


def test_plan_steady_state_pushes_unseen_eligible() -> None:
    sources = [
        DiscoveredSource(
            label="tag:AI Control",
            karma_floor=15,
            posts=[
                _p("low", 9, "2026-06-28", "tag:AI Control"),  # below floor -> skip
                _p("hi", 40, "2026-06-27", "tag:AI Control"),
                _p("seen", 99, "2026-06-26", "tag:AI Control"),
            ],
        ),
    ]
    plan = plan_run(sources, seen={"seen"}, backfill=5, first_run=False)
    assert [p.id for p in plan.to_push] == ["hi"]
    assert "hi" in plan.new_seen and "seen" in plan.new_seen
    # below-floor posts are NOT marked seen (may cross the floor later)
    assert "low" not in plan.new_seen


def test_plan_authors_ignore_karma_floor() -> None:
    sources = [
        DiscoveredSource(
            label="author:Buck",
            karma_floor=None,
            posts=[_p("a1", 2, "2026-06-28", "author:Buck")],
        ),
    ]
    plan = plan_run(sources, seen=set(), backfill=5, first_run=False)
    assert [p.id for p in plan.to_push] == ["a1"]


def test_plan_dedupes_post_appearing_in_two_sources() -> None:
    shared = _p("dup", 50, "2026-06-28", "tag:AI Control")
    sources = [
        DiscoveredSource("tag:AI Control", 15, [shared]),
        DiscoveredSource(
            "author:Buck", None, [_p("dup", 50, "2026-06-28", "author:Buck")]
        ),
    ]
    plan = plan_run(sources, seen=set(), backfill=5, first_run=False)
    assert [p.id for p in plan.to_push] == ["dup"]


def test_plan_first_run_backfills_n_most_recent_and_suppresses_rest() -> None:
    posts = [
        _p("d1", 50, "2026-06-28", "author:Buck"),
        _p("d2", 50, "2026-06-27", "author:Buck"),
        _p("d3", 50, "2026-06-26", "author:Buck"),
        _p("d4", 50, "2026-06-25", "author:Buck"),
    ]
    sources = [DiscoveredSource("author:Buck", None, posts)]
    plan = plan_run(sources, seen=set(), backfill=2, first_run=True)
    # only the 2 most-recent are pushed
    assert [p.id for p in plan.to_push] == ["d1", "d2"]
    # but ALL eligible are marked seen so the backlog never floods later
    assert plan.new_seen == {"d1", "d2", "d3", "d4"}


def test_plan_orders_push_by_recency() -> None:
    sources = [
        DiscoveredSource(
            "author:Buck",
            None,
            [
                _p("old", 10, "2026-06-01", "author:Buck"),
                _p("new", 10, "2026-06-29", "author:Buck"),
            ],
        )
    ]
    plan = plan_run(sources, seen=set(), backfill=5, first_run=False)
    assert [p.id for p in plan.to_push] == ["new", "old"]


# --------------------------------------------------------------------------- #
# Filenames + HTML assembly
# --------------------------------------------------------------------------- #


def test_slugify_strips_path_and_reserved_chars() -> None:
    assert slugify('A/B: "C"? <D>') == "A B C D"


def test_slugify_falls_back_for_empty() -> None:
    assert slugify("///") == "untitled"


def test_epub_filename_prepends_iso_date() -> None:
    post = _p("abc", 9, "2026-06-28T13:20:54.692Z", "curated")
    post.title = "Hello World"
    assert epub_filename(post) == "2026-06-28 Hello World.epub"


def test_build_html_document_includes_title_byline_and_source() -> None:
    post = _p("abc", 9, "2026-06-28T13:20:54.692Z", "curated")
    post.title = "Hello World"
    post.author = "Buck"
    html = build_html_document(post, "<p>body text</p>", "https://lw/posts/abc")
    assert "<title>Hello World</title>" in html
    assert "Hello World" in html
    assert "Buck" in html
    assert "https://lw/posts/abc" in html
    assert "<p>body text</p>" in html


# --------------------------------------------------------------------------- #
# State round-trip
# --------------------------------------------------------------------------- #


def test_load_seen_missing_file_is_empty(tmp_path: Path) -> None:
    assert load_seen(tmp_path / "none.json") == set()


def test_save_then_load_seen_round_trips(tmp_path: Path) -> None:
    f = tmp_path / "seen.json"
    save_seen(f, {"a", "b", "c"})
    assert load_seen(f) == {"a", "b", "c"}


def test_saved_state_is_stable_json(tmp_path: Path) -> None:
    """Saved as a sorted list so the state file diffs cleanly."""
    f = tmp_path / "seen.json"
    save_seen(f, {"c", "a", "b"})
    data = json.loads(f.read_text())
    assert data["seen"] == ["a", "b", "c"]


# --------------------------------------------------------------------------- #
# EPUB rendering (real pandoc) + push (mocked client)
# --------------------------------------------------------------------------- #


def test_render_epub_produces_valid_epub(tmp_path: Path) -> None:
    out = tmp_path / "out.epub"
    html = "<html><head><title>T</title></head><body><h1>T</h1><p>hi</p></body></html>"
    render_epub(html, out)
    assert out.exists() and out.stat().st_size > 0
    # an EPUB is a zip archive containing mimetype == application/epub+zip
    with zipfile.ZipFile(out) as z:
        assert z.read("mimetype") == b"application/epub+zip"


def _epub_text(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        return "".join(
            z.read(n).decode("utf-8", "replace")
            for n in z.namelist()
            if n.endswith((".xhtml", ".html"))
        )


def test_render_epub_retains_byline_and_source_link(tmp_path: Path) -> None:
    """Regression: pandoc must not drop the byline/source when it builds the
    EPUB from a document whose first element is the title heading."""
    post = _p("abc", 9, "2026-06-28T00:00:00Z", "curated")
    post.title = "My Post"
    post.author = "Buck"
    document = build_html_document(
        post,
        "<p>body paragraph here</p>",
        "https://www.lesswrong.com/posts/abc/my-post",
    )
    out = tmp_path / "o.epub"
    render_epub(document, out)
    content = _epub_text(out)
    assert "Buck" in content  # byline author survives
    assert "lesswrong.com/posts/abc" in content  # source link survives
    assert "body paragraph here" in content


async def test_push_epub_creates_folder_then_uploads() -> None:
    sn = MagicMock()
    sn.device.create_folder = AsyncMock()
    sn.device.upload_content = AsyncMock()
    await push_epub(sn, "/INBOX/LessWrong", "2026-06-28 Hello.epub", b"epubbytes")
    sn.device.create_folder.assert_awaited_once()
    sn.device.upload_content.assert_awaited_once()
    args, kwargs = sn.device.upload_content.await_args
    # uploaded to the full path, as a non-device ("WEB") upload so it triggers sync
    assert "/INBOX/LessWrong/2026-06-28 Hello.epub" in (
        list(args) + list(kwargs.values())
    )
    assert kwargs.get("equipment_no") == "WEB" or "WEB" in args


# --------------------------------------------------------------------------- #
# Discovery + orchestration
# --------------------------------------------------------------------------- #


def test_parse_duration() -> None:
    assert parse_duration("6h") == 6 * 3600
    assert parse_duration("30m") == 30 * 60
    assert parse_duration("90s") == 90
    assert parse_duration("45") == 45


async def test_discover_builds_labelled_sources_with_floors() -> None:
    cfg = LessWrongConfig(
        curated=True,
        curated_url="https://AF/graphql",
        graphql_url="https://LW/graphql",
        authors=[AuthorSource("Buck", "b1")],
        tags=[TagSource("AI Control", "t1", karma_floor=15)],
        per_source_limit=10,
    )
    calls: list[tuple[str, str]] = []

    async def fake_graphql(session: object, url: str, query: str) -> dict:
        calls.append((url, query))
        return _POSTS_JSON

    with patch("supernote.integrations.lesswrong.graphql", side_effect=fake_graphql):
        sources = await discover(session=object(), config=cfg)

    assert [s.label for s in sources] == ["curated", "author:Buck", "tag:AI Control"]
    assert [s.karma_floor for s in sources] == [None, None, 15]
    assert all(len(s.posts) == 2 for s in sources)
    # curated is pulled from the AlignmentForum endpoint; authors + tags from LW
    assert [u for u, q in calls if "curated" in q] == ["https://AF/graphql"]
    assert [u for u, q in calls if "userPosts" in q and "b1" in q] == [
        "https://LW/graphql"
    ]
    assert [u for u, q in calls if "filterSettings" in q and "t1" in q] == [
        "https://LW/graphql"
    ]


def _fake_graphql_one_post() -> AsyncMock:
    async def _g(session: object, url: str, query: str) -> dict:
        if "post(input" in query:  # single-post detail query
            return {
                "data": {
                    "post": {
                        "result": {
                            "title": "Hello",
                            "htmlBody": "<p>body</p>",
                            "pageUrl": "https://lw/posts/abc",
                        }
                    }
                }
            }
        return {
            "data": {
                "posts": {
                    "results": [
                        {
                            "_id": "abc",
                            "title": "Hello",
                            "postedAt": "2026-06-28T00:00:00Z",
                            "baseScore": 5,
                            "user": {"displayName": "Buck"},
                        }
                    ]
                }
            }
        }

    return AsyncMock(side_effect=_g)


def _push_sn() -> MagicMock:
    sn = MagicMock()
    sn.device.create_folder = AsyncMock()
    sn.device.upload_content = AsyncMock()
    return sn


async def test_run_once_pushes_planned_posts_and_saves_state(tmp_path: Path) -> None:
    cfg = LessWrongConfig(
        curated=True, authors=[], tags=[], dest_folder="/INBOX/LessWrong"
    )
    sn = _push_sn()
    state = tmp_path / "seen.json"
    with patch("supernote.integrations.lesswrong.graphql", _fake_graphql_one_post()):
        plan = await run_once(
            cfg, state, session=object(), sn=sn, first_run=False, workdir=tmp_path
        )
    assert [p.id for p in plan.to_push] == ["abc"]
    sn.device.upload_content.assert_awaited_once()
    assert load_seen(state) == {"abc"}


async def test_run_once_dry_run_does_not_push_or_persist(tmp_path: Path) -> None:
    cfg = LessWrongConfig(curated=True, authors=[], tags=[])
    sn = _push_sn()
    state = tmp_path / "seen.json"
    with patch("supernote.integrations.lesswrong.graphql", _fake_graphql_one_post()):
        await run_once(
            cfg,
            state,
            session=object(),
            sn=sn,
            first_run=False,
            dry_run=True,
            workdir=tmp_path,
        )
    sn.device.upload_content.assert_not_awaited()
    assert not state.exists()


async def test_run_once_failed_render_is_not_marked_seen(tmp_path: Path) -> None:
    """A post whose EPUB render fails must be retried on the next run."""
    cfg = LessWrongConfig(curated=True, authors=[], tags=[])
    sn = _push_sn()
    state = tmp_path / "seen.json"
    with (
        patch("supernote.integrations.lesswrong.graphql", _fake_graphql_one_post()),
        patch(
            "supernote.integrations.lesswrong.render_epub",
            side_effect=RuntimeError("pandoc boom"),
        ),
    ):
        await run_once(
            cfg, state, session=object(), sn=sn, first_run=False, workdir=tmp_path
        )
    sn.device.upload_content.assert_not_awaited()
    assert load_seen(state) == set()  # not suppressed -> retried next run
