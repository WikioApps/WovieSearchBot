"""Deterministic release metadata and bounded, literal search query parsing."""

from __future__ import annotations

import re
import shlex
import unicodedata
from decimal import Decimal

from anyascii import anyascii
from typing import Any

_YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_EPISODE = re.compile(
    r"\b(?:s(?:eason)?[ ._-]*(\d{1,3})[ ._-]*e(?:p(?:isode)?)?[ ._-]*(\d{1,4})|(\d{1,3})x(\d{1,4}))\b",
    re.I,
)
_SEASON = re.compile(r"\bs(?:eason)?[ ._-]*(\d{1,3})\b", re.I)
_QUALITY = re.compile(r"\b(2160p|1080p|720p|480p|360p|4k|uhd|fhd|hd)\b", re.I)
_RELEASE = re.compile(
    r"\b(?:web[ ._-]?(?:dl|rip)|bluray|blu[ ._-]?ray|brrip|bdrip|hdrip|hdtv|dvdrip|x26[45]|h[ ._-]?26[45]|hevc|avc|aac|ddp?|dts|hdr10?|dolby|webrip|remux|proper|repack)\b",
    re.I,
)
_LANGUAGES = {
    "english": ("english", "eng"),
    "hindi": ("hindi", "hin"),
    "tamil": ("tamil", "tam"),
    "telugu": ("telugu", "tel"),
    "malayalam": ("malayalam", "mal"),
    "kannada": ("kannada", "kan"),
    "bengali": ("bengali", "beng"),
    "marathi": ("marathi", "mar"),
    "punjabi": ("punjabi", "pun"),
    "gujarati": ("gujarati", "guj"),
    "japanese": ("japanese", "jpn"),
    "korean": ("korean", "kor"),
    "spanish": ("spanish", "spa"),
    "french": ("french", "fra"),
    "german": ("german", "deu"),
    "russian": ("russian", "rus"),
    "chinese": ("chinese", "zho"),
    "arabic": ("arabic", "ara"),
    "multi": ("multi", "multiaudio", "dual"),
}
_LANGUAGE_MAP = {
    alias: language for language, aliases in _LANGUAGES.items() for alias in aliases
}
_LANGUAGE_MAP.update(
    {
        "हिन्दी": "hindi",
        "हिंदी": "hindi",
        "தமிழ்": "tamil",
        "తెలుగు": "telugu",
        "മലയാളം": "malayalam",
        "ಕನ್ನಡ": "kannada",
        "বাংলা": "bengali",
        "मराठी": "marathi",
    }
)
_CODEC = re.compile(
    r"\b(x[ ._-]?26[45]|h[ ._-]?26[45]|hevc|avc|av1|vp9|vp8|mpeg[ ._-]?4)\b", re.I
)
_CODEC_ALIASES = {
    "x264": "h264",
    "avc": "h264",
    "h264": "h264",
    "x265": "h265",
    "hevc": "h265",
    "h265": "h265",
    "av1": "av1",
    "vp9": "vp9",
    "vp8": "vp8",
    "mpeg4": "mpeg4",
}
_EXTENSIONS = {
    "mkv",
    "mp4",
    "avi",
    "mov",
    "webm",
    "m4v",
    "ts",
    "mp3",
    "m4a",
    "flac",
    "wav",
    "pdf",
    "zip",
    "rar",
}


def normalize(text: str) -> str:
    """Keep Unicode scripts intact; normalize width, accents and punctuation."""
    text = unicodedata.normalize("NFKC", str(text)).casefold()
    # Strip Latin diacritics only: Indic vowel signs and other scripts are meaningful.
    text = "".join(
        unicodedata.normalize("NFKD", c)[0] if "LATIN" in unicodedata.name(c, "") else c
        for c in text
    )
    characters = [
        c if unicodedata.category(c)[0] in ("L", "N", "M") else " " for c in text
    ]
    return " ".join("".join(characters).split())


def ascii_normalize(text: str) -> str:
    return normalize(anyascii(unicodedata.normalize("NFKC", str(text))))


def tokenize(text: str, *, transliterate: bool = True) -> list[str]:
    original = normalize(text).split()
    words = original[:64] + (
        ascii_normalize(text).split()[:64] if transliterate else []
    )
    return list(dict.fromkeys(word[:64] for word in words if word))[:128]


def canonical_codec(value: str) -> str:
    cleaned = re.sub(r"[ ._-]+", "", value.casefold())
    if cleaned not in _CODEC_ALIASES:
        raise ValueError(
            "Codec must be h264/x264/avc, h265/x265/hevc, av1, vp9, vp8 or mpeg4."
        )
    return _CODEC_ALIASES[cleaned]


def parse_size(value: str) -> int:
    match = re.fullmatch(
        r"(\d+(?:\.\d{1,3})?)\s*(b|[kmgt]i?b)?", str(value).strip(), re.I
    )
    if not match:
        raise ValueError(
            "Use a size such as 750MB, 1.5GB, 512MiB or an integer byte count."
        )
    unit = (match[2] or "b").casefold()
    power = "kmgt".find(unit[0]) + 1 if unit != "b" else 0
    multiplier = (1024 if "i" in unit else 1000) ** power
    size = int(Decimal(match[1]) * multiplier)
    if not 0 <= size <= 2**63 - 1:
        raise ValueError("File size is outside the supported byte range.")
    return size


def canonical_language(value: str) -> str:
    value = normalize(value)
    return _LANGUAGE_MAP.get(value, value)


def canonical_quality(value: str) -> str:
    value = value.casefold().strip()
    return {"4k": "2160p", "uhd": "2160p", "fhd": "1080p", "hd": "720p"}.get(
        value, value
    )


def parse_release(filename: str, caption: str = "") -> dict[str, Any]:
    name = str(filename or "").strip()[:512]
    stem = re.sub(
        r"\.(?:mkv|mp4|avi|mov|webm|m4v|ts|mp3|m4a|flac|wav|pdf|zip|rar)$",
        "",
        name,
        flags=re.I,
    )
    text = re.sub(r"[._\[\](){}-]+", " ", stem)
    full = f"{text} {caption[:2048]}"
    year = _YEAR.search(text)
    episode = _EPISODE.search(text)
    season = _SEASON.search(text)
    quality = _QUALITY.search(full)
    codec = _CODEC.search(full)
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else None
    words = set(tokenize(full))
    language_hits = {_LANGUAGE_MAP[word] for word in words if word in _LANGUAGE_MAP}
    languages = [lang for lang in _LANGUAGES if lang in language_hits]
    boundaries = [
        m.start()
        for m in (
            year,
            episode,
            season,
            _QUALITY.search(text),
            _RELEASE.search(text),
            _CODEC.search(text),
        )
        if m
    ]
    title = text[: min(boundaries)] if boundaries else text
    title = (
        re.sub(r"\s+", " ", title).strip(" -_|")[:256]
        or text.strip()[:256]
        or "Untitled"
    )
    result: dict[str, Any] = {
        "title": title,
        "title_norm": normalize(title),
        "title_ascii": ascii_normalize(title),
        "year": int(year[1]) if year else None,
        "season": int(episode[1] or episode[3])
        if episode
        else int(season[1])
        if season
        else None,
        "episode": int(episode[2] or episode[4]) if episode else None,
        "quality": canonical_quality(quality[1]) if quality else None,
        "languages": languages,
        "tokens": tokenize(f"{title} {full}"),
        "parser_version": 2,
        "codec": canonical_codec(codec[1]) if codec else None,
        "extension": extension if extension in _EXTENSIONS else None,
    }
    return result


def parse_query(query: str) -> tuple[str, dict[str, Any]]:
    """Parse title plus release, language, codec, media type and byte-size filters.

    Malformed values raise ValueError rather than injecting arbitrary Mongo operators.
    No query input is evaluated as a regular expression or executable expression.
    """
    query = str(query).strip()
    if len(query) > 300:
        raise ValueError("Search queries must be at most 300 characters.")
    try:
        parts = shlex.split(query)
    except ValueError as exc:
        raise ValueError("Close the quotation marks in your search.") from exc
    filters: dict[str, Any] = {}
    plain: list[str] = []
    names = {
        "lang": "language",
        "language": "language",
        "q": "quality",
        "quality": "quality",
        "year": "year",
        "s": "season",
        "season": "season",
        "e": "episode",
        "episode": "episode",
        "codec": "codec",
        "type": "type",
        "minsize": "minsize",
        "maxsize": "maxsize",
        "phrase": "phrase",
    }
    for part in parts:
        key, sep, value = part.partition(":")
        if not sep or key.casefold() not in names:
            plain.append(part)
            continue
        key = names[key.casefold()]
        if not value:
            raise ValueError(f"{key}: needs a value.")
        if key in ("year", "season", "episode"):
            if not value.isdecimal():
                raise ValueError(f"{key} must be a positive number.")
            number = int(value)
            low, high = (
                (1888, 2100) if key == "year" else (0, 999 if key == "season" else 9999)
            )
            if not low <= number <= high:
                raise ValueError(f"{key} must be between {low} and {high}.")
            filters[key] = number
        elif key == "language":
            filters[key] = canonical_language(value)[:32]
        elif key == "codec":
            filters[key] = canonical_codec(value)
        elif key == "type":
            filters[key] = value.casefold().lstrip(".")
            if filters[key] not in _EXTENSIONS | {"document", "video", "audio"}:
                raise ValueError(
                    "Type must be document/video/audio or a supported extension such as mkv/mp4/mp3/pdf."
                )
        elif key in ("minsize", "maxsize"):
            filters[key] = parse_size(value)
        elif key == "phrase":
            phrase = normalize(value)
            if not phrase or len(phrase) > 120:
                raise ValueError("phrase: needs between one and 120 title characters.")
            filters[key] = phrase
        else:
            filters[key] = canonical_quality(value)
            if filters[key] not in ("2160p", "1080p", "720p", "480p", "360p"):
                raise ValueError("Quality must be 360p, 480p, 720p, 1080p or 2160p.")
    text = " ".join(plain)
    ep = _EPISODE.search(text)
    if ep:
        filters.setdefault("season", int(ep[1] or ep[3]))
        filters.setdefault("episode", int(ep[2] or ep[4]))
        text = text[: ep.start()] + " " + text[ep.end() :]
    year = _YEAR.search(text)
    quoted_title = any(" " in part and ":" not in part for part in parts)
    if quoted_title:
        filters["phrase_rank"] = normalize(
            next(part for part in parts if " " in part and ":" not in part)
        )[:120]
    if (
        year
        and year.end() == len(text.rstrip())
        and not quoted_title
        and len(normalize(text).split()) > 1
    ):
        filters.setdefault("year", int(year[1]))
        text = text[: year.start()] + " " + text[year.end() :]
    if filters.get("minsize", 0) > filters.get("maxsize", 2**63 - 1):
        raise ValueError("minsize must not exceed maxsize.")
    return " ".join(text.split()), filters
