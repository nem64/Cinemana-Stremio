"""Stremio stream add-on: find Cinemana sources for a requested IMDb ID."""

import base64
import os
import re
import time
from functools import lru_cache
from urllib.parse import quote

import requests
from flask import Flask, Response, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

CINEMANA_BASE = "https://cinemana.shabakaty.com/api/android"
IMDB_SUGGEST = "https://v3.sg.media-imdb.com/suggestion/a"
ANIME_MAPPING = "https://arm.haglund.dev/api/v2/imdb"
ANILIST_API = "https://graphql.anilist.co"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

MAX_SEARCH_PAGES = 5
SEARCH_PAGE_SIZE = 12
MEDIA_KINDS = {"movie": "1", "series": "2"}
SUBTITLE_CACHE_TTL = 6 * 60 * 60

MAL_RE = re.compile(r"myanimelist\.net/anime/(\d+)")
_subtitle_cache = {}

MANIFEST = {
    "id": "org.cinemana.stremio",
    "version": "1.1.0",
    "name": "Cinemana",
    "description": "Direct MP4 streams and subtitles from Shabakaty's Cinemana",
    "logo": "https://cinemana.shabakaty.com/favicon.ico",
    "resources": ["stream"],
    "types": ["movie", "series"],
    "idPrefixes": ["tt"],
    "catalogs": [],
}


def cinemana_get(path, params=None):
    """Return parsed JSON, or None when Cinemana rejects a request."""
    try:
        response = requests.get(
            f"{CINEMANA_BASE}/{path}", headers=HEADERS, params=params, timeout=30
        )
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError):
        return None


def imdb_lookup(imdb_id):
    """Return (title, year) for an IMDb ID, or None."""
    try:
        response = requests.get(
            f"{IMDB_SUGGEST}/{imdb_id}.json", headers=HEADERS, timeout=15
        )
        response.raise_for_status()
        for item in response.json().get("d", []):
            if item.get("id") == imdb_id and item.get("l"):
                return item["l"], item.get("y")
    except Exception:
        pass
    return None


def extract_imdb_id(url):
    match = re.search(r"tt\d+", str(url or ""))
    return match.group(0) if match else None


def extract_mal_id(url):
    match = MAL_RE.search(str(url or ""))
    return match.group(1) if match else None


@lru_cache(maxsize=1024)
def imdb_to_mal(imdb_id):
    """Map an IMDb ID to its MyAnimeList ID (many Cinemana anime only carry a MAL link)."""
    try:
        response = requests.get(
            ANIME_MAPPING, headers=HEADERS, params={"id": imdb_id}, timeout=10
        )
        response.raise_for_status()
        data = response.json()
        entries = data if isinstance(data, list) else [data]
        for entry in entries:
            if entry.get("myanimelist"):
                return str(entry["myanimelist"])
    except Exception:
        pass
    return None


@lru_cache(maxsize=1024)
def anime_alt_titles(imdb_id):
    """Resolve alternative anime titles through ARM (IMDb -> MAL) and AniList."""
    mal_id = imdb_to_mal(imdb_id)
    if not mal_id:
        return ()

    query = (
        "query($idMal:Int){Media(idMal:$idMal,type:ANIME)"
        "{title{romaji english native} synonyms}}"
    )
    try:
        response = requests.post(
            ANILIST_API,
            json={"query": query, "variables": {"idMal": int(mal_id)}},
            headers={**HEADERS, "Content-Type": "application/json"},
            timeout=10,
        )
        response.raise_for_status()
        media = (response.json().get("data") or {}).get("Media") or {}
    except (requests.RequestException, ValueError):
        return ()

    values = list((media.get("title") or {}).values())
    values.extend(media.get("synonyms") or [])
    return tuple(dict.fromkeys(value for value in values if value))


def normalize_title(title):
    return re.sub(r"[^a-z0-9]+", " ", str(title or "").lower()).strip()


def has_matching_title(info, titles):
    requested = {normalize_title(title) for title in titles if title}
    available = {
        normalize_title(info.get(key))
        for key in ("en_title", "other_title", "ar_title")
    }
    requested.discard("")
    available.discard("")
    return bool(requested & available)


def info_matches_imdb(info, imdb_id, mal_id, titles, media_type, year=None):
    ref = info.get("imdbUrlRef") or ""

    ref_imdb = extract_imdb_id(ref)
    if ref_imdb:
        return ref_imdb == imdb_id

    ref_mal = extract_mal_id(ref)
    if ref_mal and mal_id:
        return ref_mal == mal_id

    # Don't trust title-only matches for entries linked to an unrelated site.
    if ref and not ref_mal:
        return False

    if not has_matching_title(info, titles):
        return False

    # Series often bundle several seasons under the first season's year, so
    # only use the year as a sanity check for movies.
    info_year = info.get("year")
    if media_type == "movie" and year and info_year:
        try:
            return abs(int(str(year)[:4]) - int(str(info_year)[:4])) <= 1
        except ValueError:
            return True
    return True


def is_media_type(item, media_type):
    return str(item.get("kind")) == MEDIA_KINDS[media_type]


def cinemana_search(title, media_type, year=None):
    """Search via AdvancedSearch, newest first."""
    results = []
    for page in range(MAX_SEARCH_PAGES):
        params = {
            "level": 0,
            "videoTitle": title,
            "sort": "desc",
            "page": page,
            "type": media_type,
        }
        if year:
            params["year"] = f"{year},{year}"

        page_items = cinemana_get("AdvancedSearch", params=params)
        if not isinstance(page_items, list):
            break
        results.extend(item for item in page_items if is_media_type(item, media_type))
        if len(page_items) < SEARCH_PAGE_SIZE:
            break
    return results


def cinemana_simple_search(title, media_type):
    """Title search endpoint also indexes `other_title`, unlike AdvancedSearch."""
    keyword = quote(base64.b64encode(title.encode()).decode(), safe="")
    results = []
    for page in range(MAX_SEARCH_PAGES):
        path = (
            f"video/V/2/itemsPerPage/20/video_title_search/{keyword}"
            f"/itemsPerPage/{SEARCH_PAGE_SIZE}/pageNumber/{page}/level/0"
        )
        page_items = cinemana_get(path)
        if not isinstance(page_items, list):
            break
        results.extend(item for item in page_items if is_media_type(item, media_type))
        if len(page_items) < SEARCH_PAGE_SIZE:
            break
    return results


def unique_videos(results, seen):
    for item in results:
        video_id = item.get("nb")
        if video_id and video_id not in seen:
            seen.add(video_id)
            yield item


def search_candidates(titles, media_type, year=None):
    """Yield candidate videos: AdvancedSearch first, then the simple search fallback."""
    seen = set()

    for title in titles:
        results = cinemana_search(title, media_type, year)
        if not results and year:
            results = cinemana_search(title, media_type)
        yield from unique_videos(results, seen)

    # AdvancedSearch can miss entries whose English title only lives in `other_title`.
    for title in titles:
        yield from unique_videos(cinemana_simple_search(title, media_type), seen)


def cinemana_info(video_id):
    return cinemana_get(f"allVideoInfo/id/{video_id}")


def cinemana_episodes(series_id):
    data = cinemana_get(f"videoSeason/id/{series_id}")
    return data if isinstance(data, list) else []


def cinemana_streams(video_id):
    data = cinemana_get(f"transcoddedFiles/id/{video_id}")
    return data if isinstance(data, list) else []


def addon_base_url():
    proto = request.headers.get("X-Forwarded-Proto", request.scheme)
    host = request.headers.get("X-Forwarded-Host", request.host)
    prefix = request.headers.get("X-Forwarded-Prefix", "").rstrip("/")
    return f"{proto}://{host}{prefix}"


def subtitle_sources(info):
    """Pick the best Cinemana subtitle file per language (VTT > SRT > ASS)."""
    preferred = {"ar": (9, None, None), "en": (9, None, None)}
    special = None
    for translation in (info or {}).get("translations") or []:
        url = translation.get("file")
        if not url:
            continue
        language = (translation.get("type") or "").lower()
        extension = (translation.get("extention") or "").lower()
        rank = {"vtt": 0, "srt": 1, "ass": 2}.get(extension, 9)
        if language in preferred and rank < preferred[language][0]:
            preferred[language] = (rank, url, extension)
        elif language == "sp" and (special is None or rank < special[0]):
            special = (rank, url, extension)

    # "sp" is the styled/special track; it is normally Arabic.
    if preferred["ar"][1] is None and special:
        preferred["ar"] = special

    return {language: value for language, value in preferred.items() if value[1]}


def srt_to_vtt(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    text = re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", text)
    text = re.sub(r"(?m)^\d+\n(?=\d{2}:\d{2}:\d{2}\.\d{3} -->)", "", text)
    if not text.lstrip().startswith("WEBVTT"):
        text = "WEBVTT\n\n" + text.lstrip("\n")
    return text


def ass_timestamp(value):
    match = re.match(r"(\d+):(\d{2}):(\d{2})[.,](\d{1,3})", value.strip())
    if not match:
        return None
    hours, minutes, seconds, fraction = match.groups()
    millis = int(fraction.ljust(3, "0")[:3])
    return f"{int(hours):02d}:{minutes}:{seconds}.{millis:03d}"


def ass_to_vtt(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    in_events = False
    fields = None
    cues = []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_events = stripped.lower() == "[events]"
            continue
        if not in_events:
            continue
        if stripped.lower().startswith("format:"):
            fields = [field.strip().lower() for field in stripped.split(":", 1)[1].split(",")]
            continue
        if not stripped.lower().startswith("dialogue:") or not fields:
            continue
        values = stripped.split(":", 1)[1].split(",", len(fields) - 1)
        if len(values) < len(fields):
            continue
        row = dict(zip(fields, values))
        start = ass_timestamp(row.get("start", ""))
        end = ass_timestamp(row.get("end", ""))
        body = row.get("text", "")
        body = re.sub(r"\{[^}]*\}", "", body)
        body = body.replace("\\N", "\n").replace("\\n", "\n").replace("\\h", " ")
        body = body.strip()
        if start and end and body:
            cues.append(f"{start} --> {end}\n{body}")
    return "WEBVTT\n\n" + "\n\n".join(cues) + ("\n" if cues else "")


def convert_to_vtt(text, extension):
    if extension == "vtt":
        text = text.lstrip("\ufeff")
        return text if text.lstrip().startswith("WEBVTT") else "WEBVTT\n\n" + text
    if extension in ("ass", "ssa"):
        return ass_to_vtt(text)
    return srt_to_vtt(text)


def build_subtitle_tracks(video_id, info):
    base = addon_base_url()
    sources = subtitle_sources(info)
    tracks = []
    for language in ("ar", "en"):
        if language not in sources:
            continue
        tracks.append({
            "id": f"cinemana-{language}-{video_id}",
            "url": f"{base}/subtitles/{video_id}/{language}.vtt",
            "lang": language,
        })
    return tracks


def build_streams(video_id, subtitles=None):
    streams = []
    for item in cinemana_streams(video_id):
        url = item.get("videoUrl")
        if not url:
            continue
        quality = item.get("resolution") or item.get("name") or "unknown"
        streams.append({
            "url": url,
            "title": f"{quality} - Cinemana",
            "name": f"Cinemana {quality}",
            "behaviorHints": {
                "notWebReady": False,
                "bingeGroup": "cinemana",
            },
        })

    if subtitles:
        for stream in streams:
            stream["subtitles"] = subtitles

    return streams


def find_episode_id(series_id, season, episode):
    for item in cinemana_episodes(series_id):
        if (
            str(item.get("season")) == season
            and str(item.get("episodeNummer")) == episode
        ):
            return item.get("nb")
    return None


def find_match(imdb_id, title, media_type, year=None):
    titles = []
    seen_titles = set()

    def add_titles(values):
        for value in values:
            for candidate in (value, re.split(r"[:\-–]", value or "", maxsplit=1)[0]):
                candidate = (candidate or "").strip()
                key = normalize_title(candidate)
                if key and key not in seen_titles:
                    seen_titles.add(key)
                    titles.append(candidate)

    mal_id = None
    mal_checked = False

    def matching_info(results):
        nonlocal mal_id, mal_checked
        for result in results:
            # Search results already carry the fields needed to match, so only
            # fetch full metadata for the candidate that actually matches.
            if not mal_checked and extract_mal_id(result.get("imdbUrlRef")):
                mal_id = imdb_to_mal(imdb_id)
                mal_checked = True
            if not info_matches_imdb(
                result, imdb_id, mal_id, titles, media_type, year
            ):
                continue
            info = cinemana_info(result["nb"])
            if info:
                return info
        return None

    add_titles([title])
    info = matching_info(search_candidates(titles, media_type, year))
    if info:
        return info

    # Anime is often listed under its romaji/native title, while the IMDb entry
    # (and therefore Stremio) uses the English one. Try AniList aliases.
    start = len(titles)
    add_titles(anime_alt_titles(imdb_id))
    if len(titles) > start:
        info = matching_info(search_candidates(titles[start:], media_type, year))
        if info:
            return info

    return None


def get_streams(media_type, item_id):
    if media_type not in MEDIA_KINDS:
        return []

    parts = item_id.split(":")
    imdb_id = parts[0]

    lookup = imdb_lookup(imdb_id)
    if not lookup:
        return []
    title, year = lookup

    info = find_match(imdb_id, title, media_type, year)
    if not info:
        return []

    if media_type == "movie":
        subtitles = build_subtitle_tracks(info.get("nb"), info)
        return build_streams(info.get("nb"), subtitles)

    season = parts[1] if len(parts) > 1 else "1"
    episode = parts[2] if len(parts) > 2 else "1"
    episode_id = find_episode_id(info.get("nb"), season, episode)
    if not episode_id:
        return []
    episode_info = cinemana_info(episode_id) or {}
    subtitles = build_subtitle_tracks(episode_id, episode_info)
    return build_streams(episode_id, subtitles)


@app.route("/manifest.json")
@app.route("/<config>/manifest.json")
def manifest(config=None):
    return jsonify(MANIFEST)


@app.route("/stream/<media_type>/<item_id>.json")
@app.route("/<config>/stream/<media_type>/<item_id>.json")
def stream(media_type, item_id, config=None):
    return jsonify({"streams": get_streams(media_type, item_id)})


@app.route("/subtitles/<video_id>/<lang>.vtt")
def subtitle_file(video_id, lang):
    lang = lang.lower()
    empty = Response("WEBVTT\n\n", content_type="text/vtt; charset=utf-8")

    if lang not in ("ar", "en"):
        return empty

    key = (video_id, lang)
    now = time.time()
    cached = _subtitle_cache.get(key)
    if cached and now - cached[0] < SUBTITLE_CACHE_TTL:
        return Response(
            cached[1],
            content_type="text/vtt; charset=utf-8",
            headers={"Cache-Control": f"max-age={SUBTITLE_CACHE_TTL}"},
        )

    info = cinemana_info(video_id) or {}
    source = subtitle_sources(info).get(lang)
    if not source:
        return empty

    _, url, extension = source
    try:
        response = requests.get(url, headers=HEADERS, timeout=30)
        response.raise_for_status()
        # Cinemana serves subtitles as text/plain without a charset.
        text = response.content.decode("utf-8-sig", errors="replace")
        text = convert_to_vtt(text, extension)
    except (requests.RequestException, ValueError):
        return empty

    _subtitle_cache[key] = (now, text)
    return Response(
        text,
        content_type="text/vtt; charset=utf-8",
        headers={"Cache-Control": f"max-age={SUBTITLE_CACHE_TTL}"},
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5069))
    app.run(host="0.0.0.0", port=port, threaded=True)
