"""animex — animex.one provider over its pp.animex.one JSON engine.

No scraping anywhere: the engine behind animex.one is a REST API.
  animex.one/anime/x-{anilist_id}                  HTML page whose hydration
                                                   data carries the internal
                                                   slug id (e.g. "yani-neko-d7724")
  pp.animex.one/rest/api/episodes?id={iid}         episode list with hasDub/hasSub
  pp.animex.one/rest/api/servers?id&epNum          sub/dub provider lists per episode
  pp.animex.one/rest/api/sources?id&epNum&providerId
                                                   direct m3u8 urls + subtitle tracks
                                                   + intro/outro chapters

Playback embeds are vidcloud.se iframes behind an anti-bot runtime, but the
sources endpoint bypasses all of that with plain JSON. The language split
lives in `servers` (subProviders vs dubProviders) — the sources call itself
ignores language, so we tag each provider's streams by the list we picked
it from.
"""

import asyncio
import re

from src.providers._http import fetch_json, fetch_text
from src.providers._match import episode_meta, expected_count
from src.providers._media import build_ctx

NAME = "animex"
SITE = "https://animex.one"
PP = "https://pp.animex.one/rest/api"
REFERER = "https://animex.one/"

# anilist_id → internal slug id (one page fetch per show, kept for the process)
_slug_cache: dict[int, str] = {}


async def _internal_id(anilist_id: int) -> str:
    if anilist_id in _slug_cache:
        return _slug_cache[anilist_id]
    html = await fetch_text(f"{SITE}/anime/x-{anilist_id}", {"Referer": SITE + "/"})
    # The hydration object literal names the main entity several ways; the
    # first two patterns anchor on the main object, the third on its season
    # entry (some pages only carry that one).
    m = (re.search(r'slug:"([^"]{2,64})",idMal:', html)
         or re.search(r'id:"([^"]{2,64})",romajiTitle:', html)
         or re.search(r'animeId:"([^"]{2,64})",anilistId:null', html))
    if not m:
        raise RuntimeError(f"animex: no internal id on page for {anilist_id}")
    iid = m.group(1)
    _slug_cache[anilist_id] = iid
    return iid


def _title(media: dict) -> str:
    t = (media or {}).get("title") or {}
    return t.get("english") or t.get("romaji") or ""


async def get_episodes(anilist_id: int, ctx: dict | None = None) -> dict:
    ctx = ctx or await build_ctx(int(anilist_id))
    media = ctx["media"]
    iid = await _internal_id(int(anilist_id))
    raw = await fetch_json(f"{PP}/episodes?id={iid}")
    if not isinstance(raw, list):
        raise RuntimeError(f"animex: bad episodes payload for {iid}")
    expected = expected_count(media, ctx.get("anizip"))
    sub, dub = [], []
    for src in raw:
        try:
            number = int(src.get("number") or 0)
        except (TypeError, ValueError):
            continue
        if number < 1 or (expected and number > expected):
            continue
        meta = episode_meta(number, ctx)
        base = {
            "number": number,
            "title": meta["title"] or src.get("titles", {}).get("en") or f"Episode {number}",
            "duration": meta["duration"] or src.get("length"),
            "filler": meta["filler"] or bool(src.get("isFiller")),
            "uncensored": meta["uncensored"],
            "description": meta["description"] or src.get("description"),
            "image": meta["image"] or src.get("img"),
            "airDate": meta["airDate"] or src.get("airDateUtc"),
            "sourceNumber": number,
        }
        if src.get("hasSub"):
            sub.append({"id": f"watch/animex/{anilist_id}/sub/animex-{number}", **base, "audio": "sub"})
        if src.get("hasDub"):
            dub.append({"id": f"watch/animex/{anilist_id}/dub/animex-{number}", **base, "audio": "dub"})
    return {
        "meta": {
            "id": iid,
            "title": _title(media),
            "source": NAME,
            "matchScore": 1.0,
            "numbering": "plain",
            "episodeOffset": 0,
        },
        "episodes": {"sub": sub, "dub": dub},
    }


def _map_track(t: dict) -> dict | None:
    """pp track → kuhi subtitle shape; drops the thumbnail storyboards (no lang)."""
    lang = (t.get("lang") or "").strip()
    url = t.get("url") or ""
    if not url or not lang or t.get("kind") not in ("captions", "subtitles"):
        return None
    return {
        "url": url,
        "label": t.get("label") or lang,
        "lang": lang[:2].lower(),
        "default": bool(t.get("default")),
    }


async def _provider_sources(iid: str, ep: int, provider_id: str, audio: str) -> list:
    data = await fetch_json(f"{PP}/sources?id={iid}&epNum={ep}&providerId={provider_id}")
    if not isinstance(data, dict) or data.get("error"):
        return []
    chapters = {}
    for c in data.get("chapters") or []:
        if isinstance(c, dict) and c.get("title"):
            chapters[c["title"].strip().lower()] = c
    subs, seen = [], set()
    for t in data.get("tracks") or []:
        mapped = _map_track(t) if isinstance(t, dict) else None
        if mapped and mapped["url"] not in seen:
            seen.add(mapped["url"])
            subs.append(mapped)
    out = []
    for s in data.get("sources") or []:
        url = (s or {}).get("url") or ""
        mime = (s.get("type") or "").lower()
        # providers disguise playlists (…/s/deeznuts.txt) — trust the mime too
        if not url or (".m3u8" not in url and "mpegurl" not in mime):
            continue
        obj = {
            "url": url,
            "type": "hls",
            "server": provider_id.capitalize(),
            "audio": audio,
            "referer": REFERER,
            "subtitles": subs,
            "priority": 5,
            "isActive": False,
        }
        for key in ("intro", "outro"):
            ch = chapters.get(key)
            if ch and (ch.get("start") or ch.get("end")):
                obj[key] = {"start": int(ch.get("start") or 0), "end": int(ch.get("end") or 0)}
        out.append(obj)
    return out


async def watch(anilist_id: int, audio: str, ep: int, ctx: dict | None = None) -> list:
    if audio not in ("sub", "dub", "all"):
        raise ValueError("audio must be sub, dub or all")
    ctx = ctx or await build_ctx(int(anilist_id))
    iid = await _internal_id(int(anilist_id))
    servers = await fetch_json(f"{PP}/servers?id={iid}&epNum={int(ep)}")
    if not isinstance(servers, dict) or servers.get("error"):
        return []
    subs_p = [p.get("id") for p in servers.get("subProviders") or [] if p.get("id")]
    dubs_p = [p.get("id") for p in servers.get("dubProviders") or [] if p.get("id")]
    if not subs_p and not dubs_p:
        return []
    audios = ["sub", "dub"] if audio == "all" else [audio]
    streams = []
    for aud in audios:
        pool = dubs_p if aud == "dub" else subs_p
        if not pool:  # this episode has no entry for the requested language —
            pool = subs_p or dubs_p  # race the other one rather than nothing
        results = await asyncio.gather(
            *[_provider_sources(iid, int(ep), pid, aud) for pid in dict.fromkeys(pool)],
            return_exceptions=True)
        for r in results:
            if isinstance(r, list):
                streams.extend(r)
    for i, s in enumerate(streams):
        s["priority"] = 5 if i == 0 else 4
        s["isActive"] = i == 0
    return streams
