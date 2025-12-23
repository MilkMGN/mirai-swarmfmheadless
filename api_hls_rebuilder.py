"""
Rebuild a pullable HLS audio stream from the Swarm FM player API.

How it works:
- Polls https://swarmfm.boopdev.com/v2/player (configurable) for current track id and position.
- Builds the public MP3 URL (https://swarmfm.boopdev.com/assets/music/<id>.mp3).
- Starts ffmpeg to transcode/segment into HLS, seeking to the reported position.
- Serves the HLS playlist/segments via a lightweight HTTP server (built-in http.server).

Prereqs:
- ffmpeg on PATH.
- No extra Python deps (uses urllib/json).

Usage:
  python api_hls_rebuilder.py --http-port 8080
  # HLS URL: http://<host>:8080/live.m3u8
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

API_URL_DEFAULT = os.getenv("SWARMFM_API_URL", "https://swarmfm.boopdev.com/v2/player")
MEDIA_URL_TEMPLATE = os.getenv("SWARMFM_MEDIA_TEMPLATE", "https://swarmfm.boopdev.com/assets/music/{id}.mp3")
OUT_DIR_DEFAULT = os.getenv("SWARMFM_HLS_DIR", "hls_out")
PLAYLIST_DEFAULT = os.getenv("SWARMFM_HLS_PLAYLIST", "live.m3u8")
SEGMENT_SECONDS_DEFAULT = float(os.getenv("SWARMFM_HLS_SEGMENT_SECONDS", "6"))
HTTP_PORT_DEFAULT = int(os.getenv("SWARMFM_HLS_HTTP_PORT", "8080"))
POLL_SECONDS_DEFAULT = float(os.getenv("SWARMFM_API_POLL_SECONDS", "1"))
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")


def fetch_state(api_url: str) -> Optional[dict]:
    try:
        with urllib.request.urlopen(api_url, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError):
        return None


def extract_track(state: dict) -> Optional[tuple[str, float]]:
    # Expect shape: {"track": {"id": <id>, ...}, "position": <seconds>, ...}
    try:
        track_id = str(state["track"]["id"])
        position = float(state.get("position", 0))
        return track_id, max(position, 0.0)
    except Exception:
        return None


def sanitize_state_for_log(state: Optional[dict]) -> Optional[dict]:
    if not state or not isinstance(state, dict):
        return state
    cleaned = dict(state)
    track = cleaned.get("track")
    if isinstance(track, dict) and "lyrics" in track:
        track = dict(track)
        track.pop("lyrics", None)
        cleaned["track"] = track
    return cleaned


class HLSRebuilder:
    def __init__(
        self,
        api_url: str,
        out_dir: Path,
        playlist_name: str,
        segment_seconds: float,
        poll_seconds: float,
        verbose: bool,
    ):
        self.api_url = api_url
        self.out_dir = out_dir
        self.playlist_name = playlist_name
        self.segment_seconds = segment_seconds
        self.poll_seconds = poll_seconds
        self._ffmpeg_proc: Optional[subprocess.Popen] = None
        self._stop = threading.Event()
        self._current_track: Optional[str] = None
        self.verbose = verbose

    def stop(self):
        self._stop.set()
        self._kill_ffmpeg()

    def _kill_ffmpeg(self):
        if self._ffmpeg_proc and self._ffmpeg_proc.poll() is None:
            try:
                self._ffmpeg_proc.terminate()
            except Exception:
                pass
            try:
                self._ffmpeg_proc.wait(timeout=5)
            except Exception:
                self._ffmpeg_proc.kill()
        self._ffmpeg_proc = None

    def _clean_out_dir(self):
        if self.out_dir.exists():
            for item in self.out_dir.iterdir():
                if item.is_file() or item.is_symlink():
                    item.unlink(missing_ok=True)
                elif item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
        else:
            self.out_dir.mkdir(parents=True, exist_ok=True)

    def _start_ffmpeg(self, media_url: str, start_at: float):
        self._kill_ffmpeg()
        self._clean_out_dir()
        playlist_path = self.out_dir / self.playlist_name
        cmd = [
            FFMPEG_BIN,
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_delay_max",
            "2",
            "-ss",
            str(start_at),
            "-i",
            media_url,
            "-vn",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-f",
            "hls",
            "-hls_time",
            str(self.segment_seconds),
            "-hls_list_size",
            "8",
            "-hls_flags",
            "delete_segments+omit_endlist",
            str(playlist_path),
        ]
        print(f"Starting ffmpeg: {' '.join(cmd)}")
        try:
            self._ffmpeg_proc = subprocess.Popen(cmd)
        except FileNotFoundError:
            raise RuntimeError("ffmpeg not found; install it or set FFMPEG_BIN") from None

    def loop(self):
        if self.verbose:
            print(f"Polling {self.api_url} every {self.poll_seconds}s")
        while not self._stop.is_set():
            state = fetch_state(self.api_url)
            if self.verbose:
                print(f"Fetched state: {sanitize_state_for_log(state)!r}")
            track = extract_track(state) if state else None
            if track:
                track_id, position = track
                if track_id != self._current_track:
                    media_url = MEDIA_URL_TEMPLATE.format(id=track_id)
                    print(f"Switching to track {track_id} @ {position:.2f}s -> {media_url}")
                    self._current_track = track_id
                    self._start_ffmpeg(media_url, position)
            else:
                if self.verbose:
                    print("No valid track info found in API response.")
            time.sleep(self.poll_seconds)


class QuietHTTPRequestHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        return  # silence access logs


def run_http_server(directory: Path, port: int):
    handler = lambda *args, **kwargs: QuietHTTPRequestHandler(*args, directory=str(directory), **kwargs)
    httpd = ThreadingHTTPServer(("", port), handler)
    print(f"Serving HLS from {directory} on port {port} (playlist: http://<host>:{port}/live.m3u8)")
    httpd.serve_forever()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Rebuild HLS from Swarm FM API")
    parser.add_argument("--api-url", default=API_URL_DEFAULT)
    parser.add_argument("--out-dir", default=OUT_DIR_DEFAULT)
    parser.add_argument("--playlist", default=PLAYLIST_DEFAULT)
    parser.add_argument("--segment-seconds", type=float, default=SEGMENT_SECONDS_DEFAULT)
    parser.add_argument("--poll-seconds", type=float, default=POLL_SECONDS_DEFAULT)
    parser.add_argument("--http-port", type=int, default=HTTP_PORT_DEFAULT)
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir)
    rebuilder = HLSRebuilder(
        api_url=args.api_url,
        out_dir=out_dir,
        playlist_name=args.playlist,
        segment_seconds=args.segment_seconds,
        poll_seconds=args.poll_seconds,
        verbose=args.verbose,
    )

    http_thread = threading.Thread(target=run_http_server, args=(out_dir, args.http_port), daemon=True)
    http_thread.start()

    def handle_signal(signum, frame):
        print(f"Received signal {signum}; shutting down.")
        rebuilder.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    rebuilder.loop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
