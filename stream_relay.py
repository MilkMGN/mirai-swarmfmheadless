"""
Headless helper to sniff the Swarm FM player stream URL and restream it to an
AES67-friendly RTP endpoint using ffmpeg.

Prereqs:
  - python -m pip install playwright
  - python -m playwright install chromium
  - ffmpeg available on PATH

Usage examples:
  python stream_relay.py sniff
  python stream_relay.py relay --target-rtp rtp://239.69.0.1:5004
  python stream_relay.py relay --stream-url https://example/stream.m3u8 --sdp-file aes67.sdp
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import shlex
import signal
import subprocess
import sys
from typing import Iterable, List, Optional, Sequence, Tuple

try:
    from playwright.async_api import async_playwright
except ImportError:  # pragma: no cover - runtime guard
    async_playwright = None  # type: ignore


DEFAULT_PLAYER_URL = os.getenv("SWARMFM_PLAYER_URL", "https://player.sw.arm.fm/")
DEFAULT_RTP_TARGET = os.getenv("AES67_RTP_TARGET", "rtp://239.69.0.1:5004")
DEFAULT_PAYLOAD_TYPE = os.getenv("AES67_PAYLOAD_TYPE", "96")
DEFAULT_SDP_PATH = os.getenv("AES67_SDP_FILE", "aes67.sdp")
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")

STREAM_HINTS = (
    ".m3u8",
    ".mp3",
    ".aac",
    ".ogg",
    ".opus",
    ".flac",
    ".ts",
    ".webm",
)


def is_stream_candidate(url: str, content_type: str | None) -> bool:
    lowered = url.lower()
    if any(hint in lowered for hint in STREAM_HINTS):
        return True
    if content_type and "audio" in content_type.lower():
        return True
    return False


_chromium_checked = False


def ensure_chromium_installed(auto_install: bool) -> None:
    """
    Make sure Playwright's Chromium is present. If auto_install is True, attempt
    to download it via `python -m playwright install chromium` (idempotent).
    """
    if not auto_install:
        return
    global _chromium_checked
    if _chromium_checked:
        return
    cmd = [sys.executable, "-m", "playwright", "install", "chromium"]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError as exc:  # pragma: no cover - runtime guard
        raise RuntimeError(
            "Playwright is installed but Chromium is missing; run `python -m playwright install chromium` manually."
        ) from exc
    _chromium_checked = True


async def sniff_stream_url(player_url: str, wait_ms: int = 12000, auto_install: bool = True) -> List[str]:
    if async_playwright is None:
        raise RuntimeError("playwright is not installed; run `pip install playwright && playwright install chromium`")

    ensure_chromium_installed(auto_install)

    candidates: List[str] = []

    async def record_request(route, request):
        await route.continue_()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--autoplay-policy=no-user-gesture-required",
                "--no-sandbox",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )
        context = await browser.new_context()
        await context.route("**/*", record_request)

        page = await context.new_page()

        async def on_response(resp):
            try:
                headers = resp.headers
            except Exception:
                headers = {}
            url = resp.url
            ct = headers.get("content-type", "")
            if is_stream_candidate(url, ct):
                candidates.append(url)

        page.on("response", lambda resp: asyncio.create_task(on_response(resp)))
        await page.goto(player_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(wait_ms)
        await browser.close()

    # Deduplicate while preserving order.
    seen = set()
    unique = []
    for url in candidates:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


def build_ffmpeg_cmd(
    stream_url: str,
    target_rtp: str,
    sdp_file: str,
    payload_type: str,
) -> Sequence[str]:
    return [
        FFMPEG_BIN,
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_delay_max",
        "2",
        "-i",
        stream_url,
        "-vn",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-sample_fmt",
        "s24",
        "-c:a",
        "pcm_s24be",
        "-payload_type",
        payload_type,
        "-f",
        "rtp",
        target_rtp,
        "-sdp_file",
        sdp_file,
    ]


def start_ffmpeg(cmd: Sequence[str]) -> subprocess.Popen:
    print(f"Spawning ffmpeg: {' '.join(shlex.quote(arg) for arg in cmd)}")
    try:
        return subprocess.Popen(cmd)
    except FileNotFoundError:
        raise RuntimeError("ffmpeg not found; install it or set FFMPEG_BIN") from None


async def run_relay(args):
    stream_url = args.stream_url
    if not stream_url:
        found = await sniff_stream_url(args.player_url, wait_ms=args.wait_ms, auto_install=args.auto_install)
        if not found:
            raise RuntimeError("No candidate stream URLs were detected; pass --stream-url manually as a fallback.")
        stream_url = found[0]
        print(f"Selected stream URL: {stream_url}")
        if len(found) > 1:
            print("Other candidates:\n  " + "\n  ".join(found[1:]))

    cmd = build_ffmpeg_cmd(stream_url, args.target_rtp, args.sdp_file, args.payload_type)
    proc = start_ffmpeg(cmd)

    def shutdown(signame: str):
        print(f"Received {signame}; stopping ffmpeg...")
        proc.terminate()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda signum, frame, signame=sig.name: shutdown(signame))

    exit_code = proc.wait()
    if exit_code:
        raise RuntimeError(f"ffmpeg exited with code {exit_code}")
    print("ffmpeg exited cleanly.")


async def run_sniff(args):
    urls = await sniff_stream_url(args.player_url, wait_ms=args.wait_ms, auto_install=args.auto_install)
    if not urls:
        print("No likely stream URLs detected.")
    else:
        print("Likely stream URLs (most likely first):")
        for url in urls:
            print(url)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Swarm FM headless relay helper")
    sub = parser.add_subparsers(dest="command", required=True)

    sniff = sub.add_parser("sniff", help="Log stream URLs discovered while the player loads.")
    sniff.add_argument("--player-url", default=DEFAULT_PLAYER_URL)
    sniff.add_argument("--wait-ms", type=int, default=12000, help="How long to observe network traffic.")
    sniff.add_argument(
        "--no-auto-install",
        dest="auto_install",
        action="store_false",
        help="Skip running `playwright install chromium` automatically.",
    )
    sniff.set_defaults(auto_install=True)
    sniff.set_defaults(func=run_sniff)

    relay = sub.add_parser("relay", help="Restream to AES67 RTP via ffmpeg.")
    relay.add_argument("--player-url", default=DEFAULT_PLAYER_URL)
    relay.add_argument("--stream-url", default=os.getenv("SWARMFM_STREAM_URL"))
    relay.add_argument("--target-rtp", default=DEFAULT_RTP_TARGET, help="rtp://host:port (can be multicast).")
    relay.add_argument("--payload-type", default=DEFAULT_PAYLOAD_TYPE, help="RTP payload type to announce in SDP.")
    relay.add_argument("--sdp-file", default=DEFAULT_SDP_PATH, help="Path to write SDP metadata for receivers.")
    relay.add_argument("--wait-ms", type=int, default=12000, help="How long to wait for discovery when sniffing.")
    relay.add_argument(
        "--no-auto-install",
        dest="auto_install",
        action="store_false",
        help="Skip running `playwright install chromium` automatically.",
    )
    relay.set_defaults(auto_install=True)
    relay.set_defaults(func=run_relay)

    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        asyncio.run(args.func(args))
    except KeyboardInterrupt:
        return 1
    except Exception as exc:  # pragma: no cover - runtime guard
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
