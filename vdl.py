#!/usr/bin/env python3
"""
vdl - video download engine.
Paste URL(s) -> MP4. Threaded queue, fragment-level concurrency, ffmpeg remux.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError
except ImportError:
    sys.exit("missing dep -> pip install -U yt-dlp")

PRESETS = {
    "best":  "bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b",
    "2160":  "bv*[height<=2160][ext=mp4]+ba[ext=m4a]/bv*[height<=2160]+ba/b[height<=2160]/b",
    "1080":  "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]/b",
    "720":   "bv*[height<=720][ext=mp4]+ba[ext=m4a]/bv*[height<=720]+ba/b[height<=720]/b",
    "480":   "bv*[height<=480][ext=mp4]+ba[ext=m4a]/bv*[height<=480]+ba/b[height<=480]/b",
    "audio": "ba[ext=m4a]/ba/b",
}


class Progress:
    """Compact, thread-safe, one-line-per-download progress on stderr."""

    def __init__(self, quiet: bool = False):
        self.lock = threading.Lock()
        self.quiet = quiet

    def hook(self, d):
        if self.quiet:
            return
        name = Path(d.get("filename") or "").name
        name = (name[:38] + "..") if len(name) > 40 else name
        with self.lock:
            if d["status"] == "downloading":
                sys.stderr.write(
                    f"\r  {name:40s} {d.get('_percent_str','').strip():>7s} "
                    f"{d.get('_speed_str','').strip():>11s} ETA {d.get('_eta_str','').strip():>7s}   "
                )
            elif d["status"] == "finished":
                sys.stderr.write(f"\r  {name:40s} 100.0%   finalizing (remux)...\n")
            sys.stderr.flush()


def build_opts(a, outdir: Path, prog: Progress) -> dict:
    fmt = PRESETS[a.quality]
    pps = [{"key": "FFmpegMetadata", "add_metadata": True}]
    if a.audio:
        pps.append({"key": "FFmpegExtractAudio", "preferredcodec": "m4a", "preferredquality": "0"})
    if a.thumbnail:
        pps.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})

    opts = {
        "outtmpl": str(outdir / "%(title).150B [%(id)s].%(ext)s"),
        "format": fmt,
        "merge_output_format": "mp4",
        "remuxvideo": "mp4",
        "noplaylist": not a.playlist,
        "concurrent_fragment_downloads": a.fragments,
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 3,
        "socket_timeout": 25,
        "continuedl": True,
        "overwrites": False,
        "windowsfilenames": True,
        "trim_file_name": 200,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [prog.hook],
        "postprocessors": pps,
        "download_archive": str(outdir / ".vdl-archive.txt"),
        "http_headers": {"User-Agent": "Mozilla/5.0"},
        "geo_bypass": True,
    }
    if a.thumbnail:
        opts["writethumbnail"] = True
    if a.cookies:
        opts["cookiefile"] = a.cookies
    if a.browser:
        opts["cookiesfrombrowser"] = (a.browser, None, None, None)
    if a.proxy:
        opts["proxy"] = a.proxy
    if a.rate_limit:
        opts["ratelimit"] = a.rate_limit
    if a.aria2 and shutil.which("aria2c"):
        opts["external_downloader"] = "aria2c"
        opts["external_downloader_args"] = {"aria2c": ["-x", "16", "-s", "16", "-k", "1M"]}
    return opts


def fetch_one(url: str, a, outdir: Path, prog: Progress) -> tuple[str, bool, str]:
    try:
        with YoutubeDL(build_opts(a, outdir, prog)) as ydl:
            info = ydl.extract_info(url, download=True)
            if info is None:
                return url, False, "extractor returned nothing"
            if a.playlist and "entries" in info:
                return url, True, f"{sum(1 for e in info['entries'] if e)} item(s)"
            title = info.get("title") or info.get("id") or "file"
            return url, True, title
    except DownloadError as e:
        return url, False, str(e).split("\n")[0][:160]
    except Exception as e:
        return url, False, f"{type(e).__name__}: {e}"


def probe(url: str, a, outdir: Path) -> None:
    with YoutubeDL(build_opts(a, outdir, Progress(quiet=True))) as ydl:
        info = ydl.extract_info(url, download=False)
    keep = ("id", "title", "uploader", "duration", "webpage_url", "ext")
    print(json.dumps({k: info.get(k) for k in keep}, indent=2, ensure_ascii=False))
    for f in info.get("formats") or []:
        print(f"  {f.get('format_id','?'):>10s}  {f.get('ext','?'):<5s} "
              f"{str(f.get('height') or ''):>5s}  {f.get('vcodec','?'):<12s} {f.get('acodec','?')}")


def main() -> int:
    p = argparse.ArgumentParser(prog="vdl", description="Paste a URL, get an MP4.")
    p.add_argument("urls", nargs="*", help="video URL(s)")
    p.add_argument("-c", "--clipboard", action="store_true", help="read URLs from clipboard")
    p.add_argument("-o", "--out", default="downloads", help="output dir (default: downloads)")
    p.add_argument("-q", "--quality", choices=PRESETS, default="best")
    p.add_argument("-j", "--jobs", type=int, default=4, help="parallel downloads (default 4)")
    p.add_argument("-f", "--fragments", type=int, default=16, help="parallel fragments per file")
    p.add_argument("-a", "--audio", action="store_true", help="audio only (m4a)")
    p.add_argument("-t", "--thumbnail", action="store_true", help="embed thumbnail")
    p.add_argument("--playlist", action="store_true", help="allow whole playlists")
    p.add_argument("--cookies", help="cookies.txt path")
    p.add_argument("--browser", help="pull cookies from browser: chrome|firefox|edge|safari")
    p.add_argument("--proxy", help="http://host:port")
    p.add_argument("--rate-limit", type=lambda s: int(float(s.rstrip('KkMm')) * (1024 if s[-1:].lower()=='k' else 1024**2 if s[-1:].lower()=='m' else 1)), help="e.g. 4M")
    p.add_argument("--aria2", action="store_true", help="use aria2c if installed")
    p.add_argument("--probe", action="store_true", help="print metadata + formats, no download")
    p.add_argument("--quiet", action="store_true")
    a = p.parse_args()

    if not shutil.which("ffmpeg"):
        print("warn: ffmpeg not on PATH - merging/remux will fail", file=sys.stderr)

    urls = list(a.urls)
    if a.clipboard:
        try:
            import pyperclip
            urls.append(pyperclip.paste().strip())
        except Exception:
            for cmd in (["pbpaste"], ["xclip", "-o", "-selection", "clipboard"], ["powershell", "-c", "Get-Clipboard"]):
                try:
                    urls.append(subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout.strip())
                    break
                except Exception:
                    continue
    urls = [u.strip() for u in urls if u and u.strip().startswith("http")]
    if not urls:
        p.error("no URL given (pass one or use -c)")

    outdir = Path(a.out).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    prog = Progress(a.quiet)

    if a.probe:
        for u in urls:
            probe(u, a, outdir)
        return 0

    ok = bad = 0
    with ThreadPoolExecutor(max_workers=max(1, a.jobs)) as pool:
        futures = [pool.submit(fetch_one, u, a, outdir, prog) for u in urls]
        for fut in as_completed(futures):
            url, good, msg = fut.result()
            if good:
                ok += 1
                if not a.quiet:
                    print(f"  OK   {msg}  <-  {url[:70]}")
            else:
                bad += 1
                print(f"  FAIL {msg}  <-  {url[:70]}", file=sys.stderr)

    print(f"\n{ok} done, {bad} failed -> {outdir}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        sys.exit(130)
