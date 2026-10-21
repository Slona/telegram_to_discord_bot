"""Make pictures and videos acceptable to a platform with strict media rules.

Uses ffmpeg/ffprobe when they are installed (apt install ffmpeg); without them
every function reports 'unavailable' and callers fall back to what they had.
The decisions are pure functions of ffprobe's JSON so they can be tested without
the binaries; only `_run` talks to the tools.
"""

import asyncio
import json
import logging
import os
import shutil

logger = logging.getLogger("tg_relay.media")

FFMPEG = os.environ.get("FFMPEG_BIN") or shutil.which("ffmpeg")
FFPROBE = os.environ.get("FFPROBE_BIN") or shutil.which("ffprobe")
TOOL_TIMEOUT = 900


def available():
    return bool(FFMPEG and FFPROBE)


async def _run(args, timeout=TOOL_TIMEOUT):
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"{os.path.basename(args[0])} timed out")
    if proc.returncode != 0:
        tail = err.decode(errors="replace").strip().splitlines()[-3:]
        raise RuntimeError(f"{os.path.basename(args[0])} failed: {' | '.join(tail)}")
    return out


async def probe(path, count_frames=False):
    args = [FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams"]
    if count_frames:
        args.append("-count_frames")
    return json.loads(await _run(args + [path], timeout=120))


def _fps(stream):
    for key in ("avg_frame_rate", "r_frame_rate"):
        try:
            num, den = stream.get(key, "0/0").split("/")
            if float(den) and float(num):
                return float(num) / float(den)
        except (ValueError, AttributeError):
            continue
    return 0.0


def _streams(info, kind):
    return [s for s in info.get("streams", []) if s.get("codec_type") == kind]


def _duration(info):
    for source in (info.get("format", {}), *info.get("streams", [])):
        try:
            value = float(source.get("duration"))
            if value > 0:
                return value
        except (TypeError, ValueError):
            continue
    return 0.0


# --- decisions (pure) --------------------------------------------------------------

def plan_video(info, max_seconds=300, max_width=1920):
    """('remux'|'transcode', None) or (None, reason) for Threads' video rules."""
    video = _streams(info, "video")
    if not video:
        return None, "no video stream"
    v = video[0]
    if _duration(info) > max_seconds + 0.5:
        return None, f"longer than {max_seconds // 60} minutes"
    names = info.get("format", {}).get("format_name", "")
    fps = _fps(v)
    ok = ("mp4" in names or "mov" in names) \
        and v.get("codec_name") in ("h264", "hevc") \
        and v.get("pix_fmt", "yuv420p") in ("yuv420p", "yuvj420p") \
        and 23 <= fps <= 60.1 \
        and int(v.get("width", 0)) <= max_width \
        and v.get("field_order", "progressive") in ("progressive", "unknown")
    for a in _streams(info, "audio"):
        ok = ok and a.get("codec_name") == "aac" \
            and int(a.get("sample_rate", 0) or 0) <= 48000 \
            and int(a.get("channels", 2)) <= 2
    return ("remux" if ok else "transcode"), None


def video_args(src, dst, info, mode, max_width=1920):
    v = _streams(info, "video")[0]
    args = [FFMPEG, "-y", "-v", "error", "-i", src, "-map", "0:v:0", "-map", "0:a:0?"]
    if mode == "remux":      # already fine: just put the index at the front
        return args + ["-c", "copy", "-movflags", "+faststart", dst]
    fps = _fps(v)
    args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
             "-x264-params", "open-gop=0",
             "-vf", f"scale='min({max_width},iw)':-2,setsar=1"]
    if fps < 23:
        args += ["-r", "24"]
    elif fps > 60.1:
        args += ["-r", "60"]
    return args + ["-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
                   "-movflags", "+faststart", dst]


def plan_image(info, size, ext, max_bytes=8 * 1024 * 1024, max_ratio=10):
    """What to do with a picture: (None, ...) as it is, or ('convert', filters, format)."""
    streams = _streams(info, "video")
    if not streams:
        return "reject", "no picture stream", None
    width, height = int(streams[0].get("width", 0)), int(streams[0].get("height", 0))
    if not width or not height:
        return "reject", "unknown size", None
    filters = []
    ratio = max(width, height) / min(width, height)
    if ratio > max_ratio:        # pad the short side with black up to 10:1
        if width > height:
            filters.append(f"pad=w=iw:h=ceil(iw/{max_ratio}/2)*2:x=0:y=(oh-ih)/2:color=black")
        else:
            filters.append(f"pad=w=ceil(ih/{max_ratio}/2)*2:h=ih:x=(ow-iw)/2:y=0:color=black")
    fmt = "png" if ext in (".webp", ".gif", ".png") else "jpg"
    if ext in (".jpg", ".jpeg", ".png") and size <= max_bytes and not filters:
        return None, None, None
    if size > max_bytes or width > 1440:
        filters.append("scale='min(1440,iw)':-2")
        if ext == ".png" and size > max_bytes:
            fmt = "jpg"          # a large PNG won't shrink enough as PNG
    return "convert", ",".join(filters) or "null", fmt


# --- doing it -----------------------------------------------------------------------

def _derived(path, suffix):
    stem, _ = os.path.splitext(path)
    return f"{stem}_th{suffix}"


async def threads_video(path):
    """(path of a Threads-ready mp4 | None, reason). The result is a new file when
    anything had to be done: the caller deletes it after use."""
    info = await probe(path)
    mode, reason = plan_video(info)
    if mode is None:
        return None, reason
    dst = _derived(path, ".mp4")
    await _run(video_args(path, dst, info, mode))
    if os.path.getsize(dst) > 1024 ** 3:
        os.remove(dst)
        return None, "over 1 GB even after re-encoding"
    return dst, None


async def threads_image(path):
    """(kind 'image'|'video', path, derived?, reason): a picture Threads accepts,
    converted when needed; an animated GIF becomes a video."""
    ext = os.path.splitext(path)[1].lower()
    info = await probe(path, count_frames=(ext == ".gif"))
    if ext == ".gif" and int((_streams(info, "video") or [{}])[0].get("nb_read_frames", 1) or 1) > 1:
        dst = _derived(path, ".mp4")
        await _run([FFMPEG, "-y", "-v", "error", "-i", path, "-movflags", "+faststart",
                    "-pix_fmt", "yuv420p", "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                    "-c:v", "libx264", "-x264-params", "open-gop=0", "-an", dst])
        return "video", dst, True, None
    action, filters, fmt = plan_image(info, os.path.getsize(path), ext)
    if action is None:
        return "image", path, False, None
    if action == "reject":
        return None, None, False, filters
    dst = _derived(path, ".png" if fmt == "png" else ".jpg")
    args = [FFMPEG, "-y", "-v", "error", "-i", path, "-frames:v", "1", "-vf", filters]
    args += ["-q:v", "3"] if fmt == "jpg" else []
    await _run(args + [dst])
    if os.path.getsize(dst) > 8 * 1024 * 1024 and fmt == "jpg":
        await _run([FFMPEG, "-y", "-v", "error", "-i", path, "-frames:v", "1", "-vf", filters,
                    "-q:v", "8", dst])
    return "image", dst, True, None
