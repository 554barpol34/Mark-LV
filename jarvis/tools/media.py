"""YouTube: find a video and play it in JARVIS's Opera window."""
from __future__ import annotations

import time
from urllib.parse import quote_plus

from jarvis.tools import tool
from jarvis.tools.browser import BROWSER


def _consent(page):
    if "consent." in page.url:
        for label in ("Tümünü kabul et", "Accept all", "Kabul et", "Reject all", "Tümünü reddet"):
            try:
                page.get_by_role("button", name=label).first.click(timeout=2000)
                page.wait_for_load_state("domcontentloaded", timeout=8000)
                return
            except Exception:
                continue


@tool("youtube_play", """Play a YouTube video. Give a link, or what to search for (song name,
"channel - title", topic). The first matching video (not a Short) starts playing.""",
      {"query": {"type": "string"}}, ["query"], label=lambda a: f"YouTube: {a.get('query')}")
def youtube_play(query: str):
    page = BROWSER._connect()
    q = query.strip()
    if "youtube.com/" in q or "youtu.be/" in q:
        page.goto(q if q.startswith("http") else "https://" + q, wait_until="domcontentloaded")
    else:
        page.goto("https://www.youtube.com/results?search_query=" + quote_plus(q),
                  wait_until="domcontentloaded")
        _consent(page)
        page.wait_for_selector("ytd-video-renderer a#video-title", timeout=15000)
        links = page.eval_on_selector_all(
            "ytd-video-renderer a#video-title",
            "els => els.map(e => [e.href, e.title || e.innerText])")
        links = [l for l in links if "/shorts/" not in l[0]]
        if not links:
            return f"No video found for {q!r}."
        page.goto(links[0][0], wait_until="domcontentloaded")
    _consent(page)
    try:
        page.wait_for_selector("video", timeout=15000)
        time.sleep(1.5)
        playing = page.evaluate("""() => { const v = document.querySelector('video');
            if (v && v.paused) { v.play().catch(() => {}); } return v && !v.paused; }""")
    except Exception:
        playing = None
    title = page.title().replace(" - YouTube", "")
    state = "is playing" if playing else "is open (press play if it did not start)"
    return f"\"{title}\" {state}: {page.url}"


@tool("media_control", "Pause/resume, skip ads, mute or go fullscreen on the YouTube video in the Opera window.",
      {"action": {"type": "string", "enum": ["pause", "play", "toggle", "skip_ad", "mute", "fullscreen"]}},
      ["action"], label=lambda a: f"YouTube: {a.get('action')}")
def media_control(action: str):
    page = BROWSER._connect()
    if action == "skip_ad":
        try:
            page.locator(".ytp-skip-ad-button, .ytp-ad-skip-button, .ytp-ad-skip-button-modern").first.click(timeout=4000)
            return "Skipped the ad."
        except Exception:
            return "No skippable ad right now."
    js = {"pause": "v.pause()", "play": "v.play()", "toggle": "v.paused ? v.play() : v.pause()",
          "mute": "v.muted = !v.muted"}.get(action)
    if js:
        page.evaluate(f"() => {{ const v = document.querySelector('video'); if (v) {{ {js}; }} }}")
        return "Done."
    page.keyboard.press("f")
    return "Toggled fullscreen."
