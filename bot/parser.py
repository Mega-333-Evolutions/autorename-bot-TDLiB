"""Filename parsing, template rendering and file-name sanitising.

Everything here is pure (no I/O) so it is easy to test.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

DEFAULT_TEMPLATE = "[S{season}E{episode}] {title} [{quality}]"
# {1episode} ... {3episode}: the episode with zeros in front until it is 2 ... 4 digits wide (4 -> 04 / 004 / 0004)
PADDED_EPISODE_VARS = tuple(f"{zeros}episode" for zeros in range(1, 4))
TEMPLATE_VARS = ("title", "season", "episode", *PADDED_EPISODE_VARS, "quality", "year", "codec", "filename", "ext")
MAX_NAME_BYTES = 200

KNOWN_EXTS = {
    "mkv", "mp4", "m4v", "mov", "avi", "webm", "ts", "flv", "wmv", "mpg", "mpeg", "3gp",
    "mp3", "m4a", "flac", "ogg", "opus", "wav", "aac", "wma", "mka",
    "srt", "ass", "ssa", "vtt", "sub",
    "zip", "rar", "7z", "tar", "gz", "pdf", "epub", "cbz", "cbr", "apk", "iso",
    "jpg", "jpeg", "png", "webp", "gif", "txt",
}

_ILLEGAL_RE = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]')
_MULTISPACE_RE = re.compile(r"\s+")
# tokens that look like an extension but are really part of a release name
_NOT_EXT_RE = re.compile(r"^(?:\d{3,4}[pi]|[xh]26[45]|hevc|av1|s\d{1,2}e\d{1,4}|e\d{1,4})$", re.I)

# ---------------------------------------------------------------------------
# extension handling
# ---------------------------------------------------------------------------


def split_ext(name: str, known_only: bool = False) -> Tuple[str, str]:
    """Split ``name`` into ``(stem, ".ext")``.  ``ext`` is "" when there is none.

    ``known_only`` restricts extensions to a whitelist; it is used for names the
    user typed, where something like ``Season 1.5`` must not lose its ``.5``.
    """
    stem, ext = os.path.splitext(name)
    if not ext:
        return name, ""
    body = ext[1:]
    if body.lower() in KNOWN_EXTS:
        return stem, ext
    if (
        not known_only
        and 1 <= len(body) <= 5
        and body.isalnum()
        and re.search(r"[A-Za-z]", body)
        and not _NOT_EXT_RE.match(body)
    ):
        return stem, ext
    return name, ""


def finalize_name(stem: str, ext: str, max_bytes: int = MAX_NAME_BYTES) -> str:
    """Join stem and extension, truncating the stem so the result fits ``max_bytes``."""
    stem = stem.strip()
    if ext and stem.lower().endswith(ext.lower()):
        stem = stem[: -len(ext)].rstrip(" .")
    ext_bytes = len(ext.encode("utf-8"))
    while stem and len(stem.encode("utf-8")) + ext_bytes > max_bytes:
        stem = stem[:-1]
    stem = stem.rstrip(" .-_") or "file"
    return stem + ext


def sanitize(text: str) -> str:
    """Remove characters that are illegal in file names and tidy whitespace."""
    text = _ILLEGAL_RE.sub(" ", text)
    text = _MULTISPACE_RE.sub(" ", text).strip()
    return text.lstrip(".").strip()


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


@dataclass
class Info:
    title: str = ""
    season: str = ""
    episode: str = ""
    quality: str = ""
    year: str = ""
    codec: str = ""

    @property
    def structured(self) -> bool:
        return bool(self.season or self.episode or self.quality or self.year)


_Q = r"(?<![A-Za-z0-9])"
_QUALITY_RE = re.compile(_Q + r"(?:(2160|1440|1080|720|576|540|480|360|240)[pi]|(4k|uhd))(?![A-Za-z0-9])", re.I)
_RES_RE = re.compile(_Q + r"\d{3,4}x(\d{3,4})(?![A-Za-z0-9])", re.I)
_SE_RE = re.compile(_Q + r"S(\d{1,2})[ ._\-]?E(\d{1,4})(?!\d)", re.I)
_NXN_RE = re.compile(_Q + r"(\d{1,2})x(\d{2,3})(?![A-Za-z0-9])", re.I)
_SEASON_RE = re.compile(_Q + r"(?:Season|S)[ ._\-]?(\d{1,2})(?![A-Za-z0-9])", re.I)
_EP_WORD_RE = re.compile(_Q + r"(?:Episode|Ep)[ ._\-]?(\d{1,4})(?!\d)", re.I)
_E_RE = re.compile(_Q + r"E(\d{1,4})(?![A-Za-z0-9])", re.I)
_DASH_EP_RE = re.compile(r"\s-\s?(\d{1,4})(?:v\d+)?(?=[\s\[\(._\-]|$)")
_HASH_EP_RE = re.compile(r"#(\d{1,4})(?!\d)")
# episode written first: "1147 - One Piece" / "1147-One Piece". A 1-2 digit number needs a space beside
# the dash, so titles such as "3-gatsu no Lion" keep their number.
_LEAD_EP_RE = re.compile(
    r"(?:(?P<long>\d{3,4})(?:v\d+)?\s*[-–—]\s*|(?P<short>\d{1,2})(?:v\d+)?(?:\s+[-–—]\s*|\s*[-–—]\s+))(?=\S)"
)
_BRACKET_EP_RE = re.compile(r"\[(\d{1,4})(?:v\d+)?\]")  # "[1147]"
_NUM_TOKEN_RE = re.compile(_Q + r"(\d{3,4})(?:v\d+)?(?![A-Za-z0-9])")  # "One Piece 1147 720p"
_YEAR_PAREN_RE = re.compile(r"[\(\[]((?:19|20)\d{2})[\)\]]")
_YEAR_BARE_RE = re.compile(_Q + r"((?:19|20)\d{2})(?![A-Za-z0-9])")
_CODEC_RE = re.compile(_Q + r"(HEVC|[xh]\.?26[45]|AV1|AVC|VP9)(?![A-Za-z0-9])", re.I)
_SOURCE_RE = re.compile(_Q + r"(?:WEB[ .\-]?DL|WEB[ .\-]?Rip|Blu[ .\-]?Ray|BDRip|BRRip|HDTV|DVDRip|HDRip|REMUX)(?![A-Za-z0-9])", re.I)
_LEADING_GROUPS_RE = re.compile(r"^(?:\s*[\[\(][^\]\)]*[\]\)])+\s*")

_CODEC_NAMES = {
    "hevc": "HEVC", "x265": "x265", "x264": "x264", "h265": "H265",
    "h264": "H264", "av1": "AV1", "avc": "AVC", "vp9": "VP9",
}
_COMMON_HEIGHTS = {240, 360, 480, 540, 576, 720, 1080, 1440, 2160}


def _normalize(text: str) -> str:
    text = text.replace("_", " ")
    if " " not in text.strip():
        text = text.replace(".", " ")
    return _MULTISPACE_RE.sub(" ", text).strip()


def _strip_markup(text: str) -> str:
    return re.sub(r"[*`~​-‏⁠﻿]", "", text)


def _clean_title(text: str) -> str:
    text = re.sub(r"[\[\(]\s*$", "", text.strip())
    text = _MULTISPACE_RE.sub(" ", text.replace("_", " ")).strip(" -._[]()")
    return text


def _fmt_num(value: str) -> str:
    return f"{int(value):02d}"


def padded_episode(episode: str, zeros: int) -> str:
    """``episode`` with zeros in front until it is ``zeros + 1`` digits wide: with 2 zeros, ``4``, ``04`` and ``004`` all give
    ``004``.  A number that is already that wide stays exactly as it is (``1147`` with 2 zeros is ``1147``); no episode gives
    no text.  (``parse_info`` hands over the episode as at least two digits, without zeros it does not need: ``E004`` is ``04``.)"""
    return episode.zfill(zeros + 1) if episode.isdigit() else episode


def _is_year(value: str) -> bool:
    return 1900 <= int(value) <= 2099


def _leading_or_bracket_episode(norm: str) -> Optional[Tuple[str, Tuple[int, int]]]:
    """Episode numbers written first ("1147 - One Piece") or alone in brackets ("[1147]")."""
    start = len(norm) - len(_LEADING_GROUPS_RE.sub("", norm))  # skip "[Group] " prefixes
    m = _LEAD_EP_RE.match(norm, start)
    if m:
        number = m.group("long") or m.group("short")
        if not _is_year(number):
            return number, (start, m.end())
    for m in _BRACKET_EP_RE.finditer(norm):
        if not _is_year(m.group(1)):
            return m.group(1), m.span()
    return None


def _bare_episode(norm: str, spans: List[Tuple[int, int]]) -> Optional[Tuple[str, Tuple[int, int]]]:
    """A lone 3-4 digit number between the title and the release tags ("One Piece 1147 720p")."""
    marks = [a for a, _b in spans if a > 0]
    first = min(marks) if marks else len(norm)
    found = None
    for m in _NUM_TOKEN_RE.finditer(norm):
        if m.start() == 0 or m.start() >= first or _is_year(m.group(1)):
            continue
        if any(a <= m.start() < b for a, b in spans):
            continue
        found = m
    if found is None:
        return None
    if not marks:  # no release tags to anchor on: the number must end the title
        tail = norm[found.end():].strip()
        if tail and tail[0] not in "[(-–—":
            return None
    return found.group(1), found.span()


def parse_info(text: str) -> Info:
    """Extract title / season / episode / quality / year / codec from a release name."""
    raw = _strip_markup(text or "")
    # only strip a *known* extension: callers usually pass a stem already
    stem, _ext = split_ext(raw.strip().splitlines()[0] if raw.strip() else "", known_only=True)
    raw = stem
    norm = _normalize(raw)
    info = Info()
    spans: List[Tuple[int, int]] = []  # marker spans inside `norm`

    # --- season / episode ---------------------------------------------------
    season = episode = ""
    m = _SE_RE.search(norm)
    if m:
        season, episode = m.group(1), m.group(2)
        spans.append(m.span())
    else:
        m = _NXN_RE.search(norm)
        if m:
            season, episode = m.group(1), m.group(2)
            spans.append(m.span())
        else:
            ms = _SEASON_RE.search(norm)
            if ms:
                season = ms.group(1)
                spans.append(ms.span())
            for rx in (_EP_WORD_RE, _E_RE, _DASH_EP_RE, _HASH_EP_RE):
                me = rx.search(norm)
                if me:
                    episode = me.group(1)
                    spans.append(me.span())
                    break
            if not episode:
                found = _leading_or_bracket_episode(norm)
                if found:
                    episode = found[0]
                    spans.append(found[1])

    # --- quality --------------------------------------------------------------
    mq = _QUALITY_RE.search(norm)
    if mq:
        info.quality = "2160p" if mq.group(2) else f"{mq.group(1)}p"
        spans.append(mq.span())
    else:
        mr = _RES_RE.search(norm)
        if mr and int(mr.group(1)) in _COMMON_HEIGHTS:
            info.quality = f"{int(mr.group(1))}p"
            spans.append(mr.span())

    # --- year -------------------------------------------------------------------
    year_span: Optional[Tuple[int, int]] = None
    paren_years = list(_YEAR_PAREN_RE.finditer(norm))
    if paren_years:
        mp = paren_years[-1]
        info.year = mp.group(1)
        year_span = mp.span()
    else:
        candidates = []
        for my in _YEAR_BARE_RE.finditer(norm):
            if my.start() == 0:
                continue
            if any(a <= my.start() < b for a, b in spans):
                continue
            candidates.append(my)
        if mq:
            before = [c for c in candidates if c.start() < mq.start()]
            candidates = before or candidates
        if candidates:
            chosen = candidates[-1]
            info.year = chosen.group(1)
            year_span = chosen.span()
    if year_span:
        spans.append(year_span)

    # --- codec --------------------------------------------------------------------
    mc = _CODEC_RE.search(raw) or _CODEC_RE.search(norm)
    if mc:
        info.codec = _CODEC_NAMES.get(mc.group(1).lower().replace(".", ""), mc.group(1))
    mcn = _CODEC_RE.search(norm)
    if mcn:
        spans.append(mcn.span())
    ms2 = _SOURCE_RE.search(norm)
    if ms2:
        spans.append(ms2.span())

    # --- episode without any marker: "One Piece 1147 720p" (skipped for names with a year: movie titles) ---
    if not episode and not info.year:
        found = _bare_episode(norm, spans)
        if found:
            episode = found[0]
            spans.append(found[1])
    if episode and not season:
        season = "1"
    if episode:
        info.episode = _fmt_num(episode)
    if season:
        info.season = _fmt_num(season)

    # --- title ------------------------------------------------------------------------
    body = _LEADING_GROUPS_RE.sub("", norm)
    shift = len(norm) - len(body)
    local = [(a - shift, b - shift) for a, b in spans if b - shift > 0]
    # markers sitting at the very start ("S01E05 Some Title") are dropped first
    while True:
        trimmed = body.lstrip(" -._")
        lead = len(body) - len(trimmed)
        if lead:
            body = trimmed
            local = [(a - lead, b - lead) for a, b in local if b - lead > 0]
            continue
        hit = next(((a, b) for a, b in local if a <= 0 < b), None)
        if hit is None:
            break
        body = body[hit[1]:]
        local = [(a - hit[1], b - hit[1]) for a, b in local if b - hit[1] > 0]
    cut = len(body)
    for a, _b in local:
        if 0 < a < cut:
            cut = a
    # generic bracket groups after the title also end it
    mb = re.search(r"\s[\[\(]", body)
    if mb and mb.start() < cut:
        cut = mb.start()
    # so does a channel tag: "One Piece @Anime_X_Universe"
    mh = re.search(r"\s@\w", body)
    if mh and mh.start() < cut:
        cut = mh.start()
    info.title = _clean_title(body[:cut])
    return info


def parse_caption(caption: str) -> Info:
    """Parse a message caption: title from the first line, details from anywhere."""
    lines = [ln.strip() for ln in _strip_markup(caption or "").splitlines() if ln.strip()]
    if not lines:
        return Info()
    first = parse_info(lines[0])
    if len(lines) == 1:
        return first
    full = parse_info(" ".join(lines))
    for field in ("season", "episode", "quality", "year", "codec"):
        if not getattr(first, field) and getattr(full, field):
            setattr(first, field, getattr(full, field))
    return first


# ---------------------------------------------------------------------------
# templates
# ---------------------------------------------------------------------------

_TEMPLATE_RE = re.compile(r"\[[^\[\]]*\]|\([^()]*\)|\{(\w+)\}")
_TOKEN_RE = re.compile(r"\{(\w+)\}")


def render_template(template: str, values: Dict[str, str]) -> str:
    """Render ``template``.

    A bracketed ``[...]`` / ``(...)`` group is dropped completely when any known
    variable inside it is empty, so ``[S{season}E{episode}]`` vanishes instead of
    leaving ``[SE]`` behind.
    """

    def sub_tokens(text: str, group: bool) -> Optional[str]:
        empty = False

        def repl(m: "re.Match[str]") -> str:
            nonlocal empty
            name = m.group(1)
            if name not in values:
                return m.group(0)
            val = values.get(name, "")
            if not val:
                empty = True
            return val

        out = _TOKEN_RE.sub(repl, text)
        return None if (group and empty) else out

    def repl_all(m: "re.Match[str]") -> str:
        whole = m.group(0)
        if m.group(1) is not None:  # bare {token}
            return sub_tokens(whole, group=False) or ""
        out = sub_tokens(whole, group=True)
        return out if out is not None else ""

    rendered = _TEMPLATE_RE.sub(repl_all, template)
    rendered = re.sub(r"\[\s*\]|\(\s*\)", "", rendered)
    return _MULTISPACE_RE.sub(" ", rendered).strip(" -._")


_GROUP_RE = re.compile(r"\[[^\[\]]*\]|\([^()]*\)")


def missing_variables(template: str, info: Info) -> List[str]:
    """Variables the template uses *outside* [..]/(..) groups that are empty for this file.

    A bare ``{episode}`` that could not be found leaves a visible hole in the name ("One Piece - E.mkv"),
    unlike a bracket group, which is dropped on purpose; the caller warns the user about these.
    """
    out: List[str] = []
    for name in _TOKEN_RE.findall(_GROUP_RE.sub("", template or "")):
        field = "episode" if name in PADDED_EPISODE_VARS else name  # {2episode} is empty exactly when {episode} is
        if field in ("season", "episode", "quality", "year", "codec") and not getattr(info, field) and name not in out:
            out.append(name)
    return out


def apply_remove_words(text: str, words: Iterable[str]) -> str:
    for word in words:
        word = word.strip()
        if word:
            text = re.sub(re.escape(word), "", text, flags=re.I)
    text = re.sub(r"\[\s*\]|\(\s*\)", "", text)
    return _MULTISPACE_RE.sub(" ", text).strip(" -._")


def sample_values() -> Tuple[str, Dict[str, str]]:
    sample = "[SubsPlease] Tokyo Revengers - 05 (1080p) [ABCD1234].mkv"
    info = parse_info(sample)
    return sample, build_values(info, "[SubsPlease] Tokyo Revengers - 05 (1080p) [ABCD1234]", ".mkv")


def build_values(info: Info, original_stem: str, ext: str) -> Dict[str, str]:
    return {
        "title": info.title or _clean_title(original_stem),
        "season": info.season,
        "episode": info.episode,
        **{name: padded_episode(info.episode, zeros) for zeros, name in enumerate(PADDED_EPISODE_VARS, 1)},
        "quality": info.quality,
        "year": info.year,
        "codec": info.codec,
        "filename": original_stem,
        "ext": ext.lstrip("."),
    }


def compute_new_name(
    *,
    template: str,
    mode: str,
    original_name: str,
    original_ext: str,
    caption: str = "",
    remove_words: Iterable[str] = (),
) -> Tuple[str, Info]:
    """Return ``(new_file_name, info)`` for an incoming file."""
    stem, ext = split_ext(original_name)
    if not ext:
        ext = original_ext or ""
    info = parse_info(stem)
    if mode == "caption" and caption.strip():
        cinfo = parse_caption(caption)
        if cinfo.structured:
            for field in ("title", "season", "episode", "quality", "year", "codec"):
                if not getattr(cinfo, field) and getattr(info, field):
                    setattr(cinfo, field, getattr(info, field))
            info = cinfo
    values = build_values(info, stem, ext)
    rendered = render_template(template or DEFAULT_TEMPLATE, values)
    rendered = apply_remove_words(rendered, remove_words)
    out_stem = sanitize(rendered) or sanitize(stem)
    return finalize_name(out_stem, ext), info


# ---------------------------------------------------------------------------
# captions
# ---------------------------------------------------------------------------

CAPTION_VARS = (
    "filename", "original", "title", "season", "episode", *PADDED_EPISODE_VARS, "quality", "year", "codec", "ext", "size", "duration"
)


def render_caption(template: str, values: Dict[str, str]) -> str:
    """Fill ``{variables}`` in a caption template.

    The template is Telegram-HTML supplied by the user; the *values* are escaped.
    Newlines are preserved, unknown ``{tokens}`` are left alone.
    """
    import html

    def repl(m: "re.Match[str]") -> str:
        name = m.group(1)
        return html.escape(values[name], quote=False) if name in values else m.group(0)

    return _TOKEN_RE.sub(repl, template)
