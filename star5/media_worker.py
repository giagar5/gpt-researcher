"""Star5 media worker: decodes photos, videos and audio, run in its own venv (/opt/star5-media) as a
short-lived subprocess, so Whisper's ~1 GB of memory is used only while a file is processed.

    python media_worker.py image IN OUTDIR
    python media_worker.py video IN OUTDIR [--frames 8] [--max-seconds 3600]
    python media_worker.py audio IN OUTDIR [--max-seconds 3600]

Prints one JSON object on stdout. Needs av, pillow, pillow-heif and faster-whisper.
"""

import argparse
import json
import os
import sys
import warnings
from pathlib import Path

IMAGE_SIDE = 1568      # longest side sent to the vision model
FRAME_SIDE = 1024
THUMB_SIDE = 320
MAX_PIXELS = 80_000_000
SEEK_DECODE_LIMIT = 600   # frames decoded after a seek before taking what we have (~20 s at 30 fps)


def pil():
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    warnings.simplefilter("error", Image.DecompressionBombWarning)
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except ImportError:
        pass
    return Image, ImageOps


def save_jpeg(image, path, side, quality=85):
    image = image.convert("RGB")
    image.thumbnail((side, side))
    image.save(path, "JPEG", quality=quality, optimize=True)
    return image.size


def taken_at(image):
    try:
        exif = image.getexif()
        value = exif.get_ifd(0x8769).get(0x9003) or exif.get(0x0132)
        return str(value)[:19] if value else None
    except Exception:
        return None


def do_image(source, out):
    Image, ImageOps = pil()
    with Image.open(source) as image:
        image.load()
        when = taken_at(image)
        image = ImageOps.exif_transpose(image)
        width, height = image.size
        save_jpeg(image.copy(), out / "image.jpg", IMAGE_SIDE)
        save_jpeg(image, out / "thumb.jpg", THUMB_SIDE, 80)
    return {"images": ["image.jpg"], "thumb": "thumb.jpg", "width": width, "height": height, "taken": when}


def transcribe(source, max_seconds, threads):
    from faster_whisper import WhisperModel
    options = dict(device="cpu", compute_type="int8", cpu_threads=threads,
                   download_root=os.environ.get("STAR5_WHISPER_DIR", "/var/cache/gpt-researcher/whisper"))
    name = os.environ.get("STAR5_WHISPER_MODEL", "small")
    try:
        # install.sh downloads the model: do not ask Hugging Face for updates on every file.
        model = WhisperModel(name, local_files_only=True, **options)
    except Exception:
        model = WhisperModel(name, **options)
    segments, info = model.transcribe(str(source), vad_filter=True, beam_size=1,
                                      clip_timestamps=f"0,{max_seconds}")
    lines = []
    for segment in segments:
        minutes, seconds = divmod(int(segment.start), 60)
        text = segment.text.strip()
        if text:
            lines.append(f"[{minutes:02d}:{seconds:02d}] {text}")
    return {"transcript": "\n".join(lines), "language": info.language, "duration": round(info.duration),
            "truncated": info.duration > max_seconds}


def has_audio(container):
    return any(stream.type == "audio" for stream in container.streams)


def do_video(source, out, frames, max_seconds, threads):
    import av
    Image, _ = pil()
    result = {"images": [], "thumb": None}
    with av.open(str(source)) as container:
        stream = next((s for s in container.streams if s.type == "video"), None)
        if stream is None:
            raise ValueError("no video stream")
        duration = float(container.duration / av.time_base) if container.duration else 0.0
        result["duration"] = round(duration)
        audio = has_audio(container)
        stream.thread_type = "AUTO"
        span = duration if duration > 1 else 1
        marks = [span * (index + 0.5) / frames for index in range(frames)]
        start = float(stream.start_time * stream.time_base) if stream.start_time else 0.0
        for index, mark in enumerate(marks):
            try:
                container.seek(int((start + mark) / stream.time_base), stream=stream, any_frame=False, backward=True)
                frame = None
                # The seek lands on the previous keyframe: decode forward to the wanted instant
                # (bounded, so videos with very sparse keyframes stay cheap).
                for decoded, candidate in enumerate(container.decode(stream)):
                    frame = candidate
                    if candidate.time is None or candidate.time >= start + mark or decoded >= SEEK_DECODE_LIMIT:
                        break
                if frame is None:
                    continue
            except av.error.FFmpegError:
                continue
            name = f"frame{index:02d}.jpg"
            image = frame.to_image()
            save_jpeg(image, out / name, FRAME_SIDE, 80)
            result["images"].append(name)
            if result["thumb"] is None and index >= frames // 2:
                save_jpeg(image, out / "thumb.jpg", THUMB_SIDE, 80)
                result["thumb"] = "thumb.jpg"
    if result["images"] and result["thumb"] is None:
        with Image.open(out / result["images"][0]) as first:
            save_jpeg(first, out / "thumb.jpg", THUMB_SIDE, 80)
        result["thumb"] = "thumb.jpg"
    if audio:
        try:
            result.update(transcribe(source, max_seconds, threads))
        except Exception as error:
            result["transcript_error"] = f"{type(error).__name__}: {error}"[:300]
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("image", "video", "audio"))
    parser.add_argument("source")
    parser.add_argument("outdir")
    parser.add_argument("--frames", type=int, default=8)
    parser.add_argument("--max-seconds", type=int, default=3600)
    parser.add_argument("--threads", type=int, default=2)
    args = parser.parse_args()
    source, out = Path(args.source), Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    try:
        if args.command == "image":
            result = do_image(source, out)
        elif args.command == "video":
            result = do_video(source, out, max(1, min(args.frames, 16)), args.max_seconds, args.threads)
        else:
            result = transcribe(source, args.max_seconds, args.threads)
    except Exception as error:
        print(json.dumps({"error": f"{type(error).__name__}: {error}"[:500]}))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
