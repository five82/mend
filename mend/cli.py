import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path

import vapoursynth as vs

FPS_NUM = 60_000
FILM_FPS_NUM = 24_000
FPS_DEN = 1_001
METHODS = ("fieldmatch", "bwdif", "qtgmc")
SQUARE_PIXEL_METHODS = ("upscale", "finishing", "ai-cugan-1", "ai-cugan0", "ai-cugan3")
# Bump when the locked full-disc output changes; this versions its cache identity.
HANDOFF_PROFILE = "simpsons-dvd-v1"
SPINDLE_CACHE_VERSION = 1
SPINDLE_METADATA_NAME = "spindle.cache.json"
TITLE_FILE_PATTERN = re.compile(r"(?i)(?:^|[^a-z0-9])(?:title_)?t(\d{2,3})")


def spindle_cache_dir() -> Path:
    return (
        Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        / "spindle"
        / "rips"
    )


def resolve_source(value: str) -> Path:
    path = Path(value).expanduser()
    if path.exists():
        return path.resolve()

    root = spindle_cache_dir()
    matches = sorted(path for path in root.glob(f"{value}*") if path.is_dir())
    if not matches:
        raise ValueError(f"source not found: {value}")
    if len(matches) > 1:
        raise ValueError(f"fingerprint prefix is ambiguous: {value}")
    return matches[0]


def source_files(source: Path) -> list[Path]:
    if source.is_file():
        if source.suffix.lower() != ".mkv":
            raise ValueError(f"source is not an MKV: {source}")
        return [source]
    files = sorted(source.glob("*.mkv"))
    if not files:
        raise ValueError(f"no MKV files in {source}")
    return files


def select_title(source: Path, title: str | None) -> Path:
    files = source_files(source)
    if title is None:
        if len(files) != 1:
            names = ", ".join(path.name for path in files)
            raise ValueError(f"--title is required; choices: {names}")
        return files[0]
    matches = [path for path in files if path.name == title or path.stem == title]
    if len(matches) != 1:
        raise ValueError(f"title not found: {title}")
    return matches[0]


def probe(path: Path) -> dict:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name,width,height,sample_aspect_ratio,display_aspect_ratio,field_order,r_frame_rate,avg_frame_rate,pix_fmt,color_range,color_space,color_transfer,color_primaries,chroma_location:format=duration,size",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def frame_counts(path: Path) -> tuple[int, int]:
    coded = vs.core.bs.VideoSource(
        str(path), rff=False, cachemode=1, showprogress=False
    )
    displayed = vs.core.bs.VideoSource(
        str(path), rff=True, cachemode=1, showprogress=False
    )
    return coded.num_frames, displayed.num_frames


def analyze_file(path: Path) -> dict:
    data = probe(path)
    stream = data["streams"][0]
    coded, displayed = frame_counts(path)
    duration = float(data["format"]["duration"])
    expected_film = duration * 24_000 / 1_001
    return {
        "name": path.name,
        "path": str(path),
        "duration_seconds": duration,
        "size_bytes": int(data["format"]["size"]),
        "codec": stream.get("codec_name"),
        "width": stream.get("width"),
        "height": stream.get("height"),
        "sample_aspect_ratio": stream.get("sample_aspect_ratio"),
        "display_aspect_ratio": stream.get("display_aspect_ratio"),
        "field_order": stream.get("field_order"),
        "pixel_format": stream.get("pix_fmt"),
        "color": {
            key: stream.get(key)
            for key in (
                "color_range",
                "color_space",
                "color_transfer",
                "color_primaries",
            )
        },
        "coded_frames": coded,
        "rff_display_frames": displayed,
        "rff_duration_seconds": displayed * 1_001 / 30_000,
        "expected_23_976_frames": expected_film,
        "coded_excess_percent": (coded / expected_film - 1) * 100,
    }


def read_cache_metadata(source: Path) -> dict | None:
    if not source.is_dir():
        return None
    path = source / SPINDLE_METADATA_NAME
    if not path.exists():
        return None
    return json.loads(path.read_text())


def probe_container(path: Path) -> dict:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "stream=index,codec_name,codec_type,width,height,sample_aspect_ratio,field_order,r_frame_rate,avg_frame_rate,pix_fmt,color_range,color_space,color_transfer,color_primaries:format=duration,size",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def identify_matroska(path: Path) -> dict:
    result = subprocess.run(
        ["mkvmerge", "--identification-format", "json", "--identify", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def parse_title_id(path: Path) -> int | None:
    match = TITLE_FILE_PATTERN.search(path.name)
    return int(match.group(1)) if match else None


def handoff_fingerprint(source_fingerprint: str) -> str:
    value = f"mend\0{HANDOFF_PROFILE}\0{source_fingerprint}"
    return hashlib.sha256(value.encode()).hexdigest()


def handoff_work_dir(derived_fingerprint: str) -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "mend" / "handoffs" / derived_fingerprint


def handoff_source(source: Path) -> tuple[dict, dict, list[Path]]:
    root = spindle_cache_dir().resolve()
    if not source.is_dir() or source.parent != root:
        raise ValueError("handoff source must be a Spindle rip-cache entry")

    metadata = read_cache_metadata(source)
    if metadata is None:
        raise ValueError(f"cache metadata not found: {source / SPINDLE_METADATA_NAME}")
    if metadata.get("version") != SPINDLE_CACHE_VERSION:
        raise ValueError(
            f"unsupported Spindle cache version: {metadata.get('version')}"
        )
    if metadata.get("mend_profile"):
        raise ValueError("handoff source is already a Mend derivative")

    fingerprint = metadata.get("fingerprint")
    if fingerprint != source.name:
        raise ValueError("cache directory and metadata fingerprints differ")
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint or ""):
        raise ValueError(f"invalid cache fingerprint: {fingerprint}")

    try:
        envelope = json.loads(metadata.get("ripspec_data", ""))
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid cached RipSpec: {error}") from error
    if envelope.get("version") != SPINDLE_CACHE_VERSION:
        raise ValueError(f"unsupported RipSpec version: {envelope.get('version')}")

    content = envelope.get("metadata") or {}
    title = content.get("show_title") or content.get("title")
    if str(title).casefold() != "the simpsons":
        raise ValueError("handoff profile only supports The Simpsons")
    if str(content.get("media_type", "")).casefold() != "tv":
        raise ValueError("handoff profile requires TV content")
    if str(content.get("disc_source", "")).casefold() != "dvd":
        raise ValueError("handoff profile requires a DVD source")
    season = content.get("season_number")
    if not isinstance(season, int) or not 1 <= season <= 10:
        raise ValueError("handoff profile only supports seasons 1 through 10")

    episodes = envelope.get("episodes") or []
    if not episodes:
        raise ValueError("cached RipSpec has no episodes")
    title_ids = [episode.get("title_id") for episode in episodes]
    if any(not isinstance(title_id, int) or title_id < 0 for title_id in title_ids):
        raise ValueError("cached RipSpec has an invalid episode title ID")
    if len(set(title_ids)) != len(title_ids):
        raise ValueError("cached RipSpec maps multiple episodes to one title")

    files_by_id: dict[int, Path] = {}
    for path in source_files(source):
        title_id = parse_title_id(path)
        if title_id is None:
            continue
        if title_id in files_by_id:
            raise ValueError(f"multiple MKVs map to title {title_id}")
        files_by_id[title_id] = path
    missing = [title_id for title_id in title_ids if title_id not in files_by_id]
    if missing:
        raise ValueError(f"cache is missing episode title files: {missing}")

    files = [files_by_id[title_id] for title_id in title_ids]
    if metadata.get("title_count") != len(files):
        raise ValueError("cache title count does not match the episode files")
    for path in files:
        validate_handoff_source_file(path)
    return metadata, envelope, files


def validate_handoff_source_file(path: Path) -> None:
    data = probe_container(path)
    videos = [
        stream
        for stream in data.get("streams", [])
        if stream.get("codec_type") == "video"
    ]
    audios = [
        stream
        for stream in data.get("streams", [])
        if stream.get("codec_type") == "audio"
    ]
    if len(videos) != 1 or not audios:
        raise ValueError(
            f"source must have one video and at least one audio track: {path.name}"
        )
    video = videos[0]
    if (
        video.get("codec_name") != "mpeg2video"
        or video.get("width") != 720
        or video.get("height") != 480
        or video.get("sample_aspect_ratio") != "8:9"
    ):
        raise ValueError(f"source is not the supported NTSC DVD format: {path.name}")


def clean_handoff_metadata(
    metadata: dict, envelope: dict, derived_fingerprint: str, total_bytes: int
) -> dict:
    clean_envelope = json.loads(json.dumps(envelope))
    clean_envelope["fingerprint"] = derived_fingerprint
    clean_envelope["assets"] = {}
    clean_envelope["attributes"] = {}
    return {
        "version": SPINDLE_CACHE_VERSION,
        "fingerprint": derived_fingerprint,
        "disc_title": metadata["disc_title"],
        "cached_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "title_count": len(clean_envelope.get("episodes") or []),
        "total_bytes": total_bytes,
        "ripspec_data": json.dumps(clean_envelope, separators=(",", ":")),
        "metadata_json": metadata.get("metadata_json", ""),
        "mend_profile": HANDOFF_PROFILE,
        "mend_source_fingerprint": metadata["fingerprint"],
    }


def analyze(args: argparse.Namespace) -> int:
    source = resolve_source(args.source)
    result = {
        "source": str(source),
        "cache_metadata": read_cache_metadata(source),
        "files": [analyze_file(path) for path in source_files(source)],
    }
    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    metadata = result["cache_metadata"] or {}
    print(metadata.get("disc_title", source.name))
    print(f"Source: {source}")
    print()
    print(
        f"{'Title':<14} {'Duration':>9} {'Coded':>8} {'RFF':>8} {'Film exp.':>10} {'Excess':>8} {'Field':>8}"
    )
    for item in result["files"]:
        minutes, seconds = divmod(round(item["duration_seconds"]), 60)
        print(
            f"{item['name']:<14} {minutes:02d}:{seconds:02d}"
            f" {item['coded_frames']:>8} {item['rff_display_frames']:>8}"
            f" {item['expected_23_976_frames']:>10.1f}"
            f" {item['coded_excess_percent']:>7.2f}% {item['field_order']:>8}"
        )
    return 0


def rank_scan_metadata(
    lines: Iterable[str], duration: float, window: float, count: int
) -> list[dict]:
    bins = defaultdict(lambda: {"frames": 0, "interlaced": 0, "repeated": 0, "cuts": 0})
    current = None
    for line in lines:
        if line.startswith("frame:"):
            match = re.search(r"\bpts_time:([^ ]+)", line)
            current = int(float(match.group(1)) // window) if match else None
            if current is not None:
                bins[current]["frames"] += 1
            continue
        if current is None or "=" not in line:
            continue
        key, value = line.strip().split("=", 1)
        if key == "lavfi.idet.multiple.current_frame" and value in ("tff", "bff"):
            bins[current]["interlaced"] += 1
        elif key == "lavfi.idet.repeated.current_frame" and value != "neither":
            bins[current]["repeated"] += 1
        elif key == "lavfi.scd.score" and float(value) >= 10:
            bins[current]["cuts"] += 1

    expected_frames = window * 24_000 / 1_001
    ranked = []
    for index, stats in bins.items():
        start = index * window
        if start + window > duration or stats["frames"] < expected_frames * 0.8:
            continue
        cadence = max(0.0, stats["frames"] / expected_frames - 1) * 100
        interlaced = stats["interlaced"] / stats["frames"] * 100
        repeated = stats["repeated"] / stats["frames"] * 100
        ranked.append(
            {
                "start_seconds": start,
                "risk_score": cadence + interlaced + repeated * 0.25,
                "cadence_excess_percent": cadence,
                "interlaced_percent": interlaced,
                "repeated_percent": repeated,
                "scene_changes": stats["cuts"],
                "coded_frames": stats["frames"],
            }
        )

    selected = []
    for candidate in sorted(ranked, key=lambda item: item["risk_score"], reverse=True):
        if all(
            abs(candidate["start_seconds"] - item["start_seconds"]) >= window * 3
            for item in selected
        ):
            selected.append(candidate)
            if len(selected) == count:
                break
    return selected


def scan_file(path: Path, window: float, count: int) -> dict:
    source_data = probe(path)
    duration = float(source_data["format"]["duration"])
    with tempfile.NamedTemporaryFile(
        prefix="mend-scan-", suffix=".txt", delete=False
    ) as file:
        metadata_path = Path(file.name)
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-an",
        "-sn",
        "-dn",
        "-vf",
        f"idet,scdet,metadata=mode=print:file={metadata_path}",
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(command, capture_output=True, check=False)
        if result.returncode:
            detail = result.stderr.decode(errors="replace").strip()
            raise RuntimeError(f"scan failed for {path.name}: {detail}")
        with metadata_path.open() as metadata:
            candidates = rank_scan_metadata(metadata, duration, window, count)
    finally:
        metadata_path.unlink(missing_ok=True)
    return {
        "name": path.name,
        "path": str(path),
        "duration_seconds": duration,
        "window_seconds": window,
        "candidates": candidates,
    }


def scan(args: argparse.Namespace) -> int:
    if args.window <= 0 or args.count <= 0:
        raise ValueError("--window and --count must be positive")
    source = resolve_source(args.source)
    files = [select_title(source, args.title)] if args.title else source_files(source)
    result = {"source": str(source), "files": []}
    for path in files:
        print(f"Scanning {path.name}...", file=sys.stderr, flush=True)
        result["files"].append(scan_file(path, args.window, args.count))
    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    for item in result["files"]:
        print(item["name"])
        print(
            f"{'Start':>9} {'Score':>7} {'Cadence':>9} {'Interlace':>10}"
            f" {'Repeated':>9} {'Cuts':>5}"
        )
        for candidate in item["candidates"]:
            print(
                f"{candidate['start_seconds']:>9.3f}"
                f" {candidate['risk_score']:>7.1f}"
                f" {candidate['cadence_excess_percent']:>8.1f}%"
                f" {candidate['interlaced_percent']:>9.1f}%"
                f" {candidate['repeated_percent']:>8.1f}%"
                f" {candidate['scene_changes']:>5}"
            )
        print()
    return 0


def sample_output_dir(source: Path, title: Path, start: float) -> Path:
    fingerprint = source.name if source.is_dir() else "files"
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "mend" / "samples" / fingerprint / title.stem / f"{start:09.3f}"


def matroska_color_properties(metadata: dict) -> list[str]:
    properties = []
    for key, name, values in (
        ("color_space", "color-matrix-coefficients", {"smpte170m": "6"}),
        ("color_range", "color-range", {"tv": "1"}),
        ("color_transfer", "color-transfer-characteristics", {"smpte170m": "6"}),
        ("color_primaries", "color-primaries", {"smpte170m": "6"}),
    ):
        value = metadata.get(key)
        if not value or value == "unknown":
            continue
        if value not in values:
            raise ValueError(f"unsupported {key}: {value}")
        properties.extend(("--set", f"{name}={values[value]}"))
    return properties


def render_video(
    title: Path,
    method: str,
    output: Path,
    field_order: str,
    sample_aspect_ratio: str,
    color_metadata: dict,
    frame_range: tuple[int, int] | None,
    metadata: dict[str, object],
) -> None:
    script = Path(__file__).with_name("temporal.vpy")
    vspipe = Path(sys.executable).with_name("vspipe")
    environment = os.environ.copy()
    nvidia_icd = Path("/usr/share/vulkan/icd.d/nvidia_icd.json")
    if nvidia_icd.is_file():
        environment.setdefault("VK_ICD_FILENAMES", str(nvidia_icd))
    pipe_command = [
        str(vspipe),
        "--arg",
        f"source={title}",
        "--arg",
        f"method={method}",
        "--arg",
        f"field_order={field_order}",
    ]
    if frame_range is not None:
        pipe_command.extend(
            ("--start", str(frame_range[0]), "--end", str(frame_range[1]))
        )
    pipe_command.extend(("--container", "y4m", str(script), "-"))

    properties = matroska_color_properties(color_metadata)
    color_options = []
    for key, option in (
        ("color_range", "-color_range"),
        ("color_space", "-colorspace"),
        ("color_transfer", "-color_trc"),
        ("color_primaries", "-color_primaries"),
        ("chroma_location", "-chroma_sample_location"),
    ):
        value = color_metadata.get(key)
        if value and value != "unknown":
            color_options.extend((option, value))
    metadata_options = []
    for key, value in metadata.items():
        metadata_options.extend(("-metadata", f"{key}={value}"))
    ffmpeg_command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "yuv4mpegpipe",
        "-i",
        "pipe:0",
        "-map",
        "0:v:0",
        "-vf",
        f"setsar={sample_aspect_ratio.replace(':', '/')}",
        *color_options,
        "-c:v",
        "ffv1",
        "-level",
        "3",
        "-g",
        "1",
        *metadata_options,
        str(output),
    ]

    first = subprocess.Popen(
        pipe_command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )
    assert first.stdout is not None
    second = subprocess.run(
        ffmpeg_command, stdin=first.stdout, capture_output=True, check=False
    )
    first.stdout.close()
    _, first_stderr = first.communicate()
    if first.returncode or second.returncode:
        output.unlink(missing_ok=True)
        detail = (first_stderr + second.stderr).decode(errors="replace").strip()
        raise RuntimeError(f"render failed for {method}: {detail}")

    if properties:
        metadata_result = subprocess.run(
            ["mkvpropedit", str(output), "--edit", "track:v1", *properties],
            capture_output=True,
            check=False,
        )
        if metadata_result.returncode:
            output.unlink(missing_ok=True)
            detail = metadata_result.stderr.decode(errors="replace").strip()
            raise RuntimeError(f"metadata update failed for {method}: {detail}")


def render_sample(
    title: Path,
    method: str,
    start: float,
    duration: float,
    output: Path,
    field_order: str,
    sample_aspect_ratio: str,
    color_metadata: dict,
) -> None:
    fps_num = FILM_FPS_NUM if method == "upscale" else FPS_NUM
    start_frame = round(start * fps_num / FPS_DEN)
    frame_count = round(duration * fps_num / FPS_DEN)
    render_video(
        title,
        method,
        output,
        field_order,
        sample_aspect_ratio,
        color_metadata,
        (start_frame, start_frame + frame_count - 1),
        {
            "mend_temporal_method": method,
            "mend_source": title,
            "mend_start_seconds": start,
        },
    )


def matroska_duration_seconds(value: str) -> float:
    hours, minutes, seconds = value.split(":", 2)
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def comparable_tracks(data: dict) -> list[tuple]:
    property_names = (
        "language",
        "track_name",
        "default_track",
        "forced_track",
        "hearing_impaired",
        "visual_impaired",
        "text_descriptions",
        "original",
        "commentary",
        "audio_channels",
        "audio_sampling_frequency",
    )
    tracks = []
    for track in data.get("tracks", []):
        if track.get("type") == "video":
            continue
        properties = track.get("properties") or {}
        tracks.append(
            (
                track.get("type"),
                track.get("codec"),
                *(properties.get(name) for name in property_names),
            )
        )
    return tracks


def comparable_attachments(data: dict) -> list[tuple]:
    return [
        (
            item.get("file_name"),
            item.get("content_type"),
            item.get("description"),
        )
        for item in data.get("attachments", [])
    ]


def validate_handoff_title(source: Path, output: Path) -> None:
    data = probe_container(output)
    videos = [
        stream
        for stream in data.get("streams", [])
        if stream.get("codec_type") == "video"
    ]
    audios = [
        stream
        for stream in data.get("streams", [])
        if stream.get("codec_type") == "audio"
    ]
    if len(videos) != 1 or not audios:
        raise RuntimeError(f"handoff output has invalid streams: {output.name}")
    video = videos[0]
    if (
        video.get("codec_name") != "ffv1"
        or video.get("width") != 1440
        or video.get("height") != 1080
        or video.get("sample_aspect_ratio") != "1:1"
        or video.get("pix_fmt") != "yuv420p10le"
        or video.get("field_order") not in (None, "progressive")
    ):
        raise RuntimeError(f"handoff output has invalid video format: {output.name}")
    for key, expected in (
        ("color_range", "tv"),
        ("color_space", "smpte170m"),
        ("color_transfer", "smpte170m"),
        ("color_primaries", "smpte170m"),
    ):
        if video.get(key) != expected:
            raise RuntimeError(f"handoff output has invalid {key}: {output.name}")

    rate = video.get("avg_frame_rate") or video.get("r_frame_rate")
    if not rate or abs(float(Fraction(rate)) - FILM_FPS_NUM / FPS_DEN) > 0.001:
        raise RuntimeError(f"handoff output has invalid frame rate: {output.name}")
    if int(data["format"]["size"]) < 10 * 1024 * 1024:
        raise RuntimeError(f"handoff output is too small: {output.name}")

    source_mkv = identify_matroska(source)
    output_mkv = identify_matroska(output)
    if comparable_tracks(source_mkv) != comparable_tracks(output_mkv):
        raise RuntimeError(f"handoff output changed non-video tracks: {output.name}")
    if comparable_attachments(source_mkv) != comparable_attachments(output_mkv):
        raise RuntimeError(f"handoff output changed attachments: {output.name}")
    source_chapters = [
        item.get("num_entries") for item in source_mkv.get("chapters", [])
    ]
    output_chapters = [
        item.get("num_entries") for item in output_mkv.get("chapters", [])
    ]
    if source_chapters != output_chapters:
        raise RuntimeError(f"handoff output changed chapters: {output.name}")

    source_video = next(
        track for track in source_mkv["tracks"] if track.get("type") == "video"
    )
    output_video = next(
        track for track in output_mkv["tracks"] if track.get("type") == "video"
    )
    source_duration = source_video.get("properties", {}).get("tag_duration")
    output_duration = output_video.get("properties", {}).get("tag_duration")
    if not source_duration or not output_duration:
        raise RuntimeError(f"handoff output has no video duration: {output.name}")
    difference = abs(
        matroska_duration_seconds(source_duration)
        - matroska_duration_seconds(output_duration)
    )
    if difference > 0.1:
        raise RuntimeError(
            f"handoff output duration differs by {difference:.3f}s: {output.name}"
        )


def render_handoff_title(source: Path, output: Path, source_fingerprint: str) -> None:
    source_data = probe(source)
    stream = source_data["streams"][0]
    video_temp = output.parent / f".{output.stem}.video.mkv"
    mux_temp = output.parent / f".{output.stem}.mux.mkv"
    video_temp.unlink(missing_ok=True)
    mux_temp.unlink(missing_ok=True)
    try:
        render_video(
            source,
            "upscale",
            video_temp,
            "tff",
            "1:1",
            stream,
            None,
            {
                "mend_temporal_method": "upscale",
                "mend_profile": HANDOFF_PROFILE,
                "mend_source_fingerprint": source_fingerprint,
            },
        )
        result = subprocess.run(
            [
                "mkvmerge",
                "--output",
                str(mux_temp),
                str(video_temp),
                "--no-video",
                str(source),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(f"handoff mux failed for {source.name}: {detail}")
        validate_handoff_title(source, mux_temp)
        os.replace(mux_temp, output)
    finally:
        video_temp.unlink(missing_ok=True)
        mux_temp.unlink(missing_ok=True)


def validate_published_handoff(
    destination: Path,
    source_metadata: dict,
    source_paths: list[Path],
    derived_fingerprint: str,
) -> None:
    metadata = read_cache_metadata(destination)
    if metadata is None:
        raise RuntimeError(f"derived cache entry has no metadata: {destination}")
    if (
        metadata.get("fingerprint") != derived_fingerprint
        or metadata.get("mend_profile") != HANDOFF_PROFILE
        or metadata.get("mend_source_fingerprint") != source_metadata.get("fingerprint")
    ):
        raise RuntimeError(f"derived cache metadata does not match: {destination}")
    outputs = [destination / source.name for source in source_paths]
    if any(not output.is_file() for output in outputs):
        raise RuntimeError(f"derived cache entry is incomplete: {destination}")
    if {path.name for path in source_paths} != {
        path.name for path in source_files(destination)
    }:
        raise RuntimeError(f"derived cache entry has unexpected MKVs: {destination}")
    for source, output in zip(source_paths, outputs, strict=True):
        validate_handoff_title(source, output)
    total_bytes = sum(output.stat().st_size for output in outputs)
    if (
        metadata.get("title_count") != len(outputs)
        or metadata.get("total_bytes") != total_bytes
    ):
        raise RuntimeError(f"derived cache size metadata does not match: {destination}")
    envelope = json.loads(metadata.get("ripspec_data", ""))
    if (
        envelope.get("fingerprint") != derived_fingerprint
        or envelope.get("assets") != {}
        or envelope.get("attributes") != {}
    ):
        raise RuntimeError(
            f"derived cache contains stale pipeline results: {destination}"
        )


def publish_handoff(source: Path) -> tuple[str, Path]:
    metadata, envelope, source_paths = handoff_source(source)
    derived_fingerprint = handoff_fingerprint(metadata["fingerprint"])
    root = spindle_cache_dir().resolve()
    destination = root / derived_fingerprint
    if destination.exists():
        validate_published_handoff(
            destination, metadata, source_paths, derived_fingerprint
        )
        print(f"Using existing Mend cache entry: {destination}", flush=True)
        return derived_fingerprint, destination

    work = handoff_work_dir(derived_fingerprint)
    work.mkdir(parents=True, exist_ok=True)
    for index, source_file in enumerate(source_paths, 1):
        output = work / source_file.name
        (work / f".{source_file.stem}.video.mkv").unlink(missing_ok=True)
        (work / f".{source_file.stem}.mux.mkv").unlink(missing_ok=True)
        if output.exists():
            validate_handoff_title(source_file, output)
            print(
                f"Phase {index}/{len(source_paths)} - Reusing {output.name}",
                flush=True,
            )
            continue
        print(
            f"Phase {index}/{len(source_paths)} - Restoring {source_file.name}",
            flush=True,
        )
        render_handoff_title(source_file, output, metadata["fingerprint"])

    outputs = [work / source_file.name for source_file in source_paths]
    if {path.name for path in outputs} != {path.name for path in source_files(work)}:
        raise RuntimeError(f"handoff work directory has unexpected MKVs: {work}")
    total_bytes = sum(output.stat().st_size for output in outputs)
    handoff_metadata = clean_handoff_metadata(
        metadata, envelope, derived_fingerprint, total_bytes
    )
    metadata_temp = work / f".{SPINDLE_METADATA_NAME}.tmp"
    metadata_temp.write_text(json.dumps(handoff_metadata, indent=2) + "\n")
    os.replace(metadata_temp, work / SPINDLE_METADATA_NAME)
    try:
        work.rename(destination)
    except OSError as error:
        raise RuntimeError(f"publish derived cache entry: {error}") from error
    print(f"Published Mend cache entry: {destination}", flush=True)
    return derived_fingerprint, destination


def handoff(args: argparse.Namespace) -> int:
    source = resolve_source(args.source)
    derived_fingerprint, destination = publish_handoff(source)
    spindle = shutil.which("spindle")
    if spindle is None:
        raise RuntimeError(
            f"Spindle is not installed; process the published entry manually: "
            f"spindle cache process {derived_fingerprint}"
        )
    print(f"Handing off to Spindle: {derived_fingerprint}", flush=True)
    result = subprocess.run(
        [spindle, "cache", "process", derived_fingerprint], check=False
    )
    if result.returncode:
        raise RuntimeError(
            f"Spindle did not queue {derived_fingerprint}; the derivative remains at "
            f"{destination}"
        )
    return 0


def sample(args: argparse.Namespace) -> int:
    source = resolve_source(args.source)
    title = select_title(source, args.title)
    if args.start < 0 or args.duration <= 0:
        raise ValueError("--start must be non-negative and --duration must be positive")
    source_data = probe(title)
    source_duration = float(source_data["format"]["duration"])
    stream = source_data["streams"][0]
    sample_aspect_ratio = stream.get("sample_aspect_ratio")
    if not sample_aspect_ratio or sample_aspect_ratio == "N/A":
        sample_aspect_ratio = "1:1"
    if args.start + args.duration > source_duration:
        raise ValueError("sample extends past the source duration")

    method = getattr(args, "method", None)
    if method is None:
        model = getattr(args, "model", "denoise")
        method = {
            "denoise": "ai-denoise",
            "denoise-long": "ai-denoise45",
            "compress1": "ai-compress4",
            "compress1-long": "ai-compress4-45",
            "compress2": "ai-compress5",
            "compress2-long": "ai-compress5-45",
            "compress3": "ai-compress6",
            "compress3-long": "ai-compress6-45",
            "cugan-conservative": "ai-cugan-1",
            "cugan-no-denoise": "ai-cugan0",
            "cugan-denoise3x": "ai-cugan3",
        }[model]
    methods = METHODS if method == "all" else (method,)
    output_dir = (
        Path(args.output).expanduser()
        if args.output
        else sample_output_dir(source, title, args.start)
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    for method in methods:
        output = output_dir / f"{method}.mkv"
        print(f"Rendering {method}: {output}", flush=True)
        render_sample(
            title,
            method,
            args.start,
            args.duration,
            output,
            args.field_order,
            "1:1" if method in SQUARE_PIXEL_METHODS else sample_aspect_ratio,
            stream,
        )
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="mend", description="Video restoration research tools"
    )
    commands = result.add_subparsers(dest="command", required=True)

    analyze_parser = commands.add_parser(
        "analyze", help="inspect a file or Spindle rip-cache entry"
    )
    analyze_parser.add_argument(
        "source", help="MKV path, directory, or Spindle fingerprint prefix"
    )
    analyze_parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON"
    )
    analyze_parser.set_defaults(run=analyze)

    handoff_parser = commands.add_parser(
        "handoff",
        help="restore a Simpsons DVD cache entry and queue it in Spindle",
    )
    handoff_parser.add_argument(
        "source", help="Spindle rip-cache directory or fingerprint prefix"
    )
    handoff_parser.set_defaults(run=handoff)

    scan_parser = commands.add_parser(
        "scan", help="rank temporal-problem windows for comparison"
    )
    scan_parser.add_argument(
        "source", help="MKV path, directory, or Spindle fingerprint prefix"
    )
    scan_parser.add_argument("--title", help="scan only this MKV filename")
    scan_parser.add_argument(
        "--window", type=float, default=10.0, help="window duration in seconds"
    )
    scan_parser.add_argument(
        "--count", type=int, default=6, help="candidate windows per title"
    )
    scan_parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON"
    )
    scan_parser.set_defaults(run=scan)

    sample_options = argparse.ArgumentParser(add_help=False)
    sample_options.add_argument(
        "source", help="MKV path, directory, or Spindle fingerprint prefix"
    )
    sample_options.add_argument(
        "--title", help="MKV filename when source is a directory"
    )
    sample_options.add_argument(
        "--start", type=float, required=True, help="start time in seconds"
    )
    sample_options.add_argument(
        "--duration", type=float, default=10.0, help="duration in seconds (default: 10)"
    )
    sample_options.add_argument("--field-order", choices=("tff", "bff"), default="tff")
    sample_options.add_argument("--output", help="output directory")

    sample_parser = commands.add_parser(
        "sample",
        parents=[sample_options],
        help="render separate lossless temporal-restoration samples",
    )
    sample_parser.add_argument("--method", choices=("all",) + METHODS, default="all")
    sample_parser.set_defaults(run=sample)

    compare_parser = commands.add_parser(
        "compare",
        parents=[sample_options],
        help="render one synchronized, labeled temporal comparison",
    )
    compare_parser.set_defaults(method="comparison", run=sample)

    cleanup_parser = commands.add_parser(
        "cleanup",
        parents=[sample_options],
        help="render one synchronized, labeled cleanup comparison",
    )
    cleanup_parser.set_defaults(method="cleanup", run=sample)

    restore_parser = commands.add_parser(
        "restore",
        parents=[sample_options],
        help="render the locked native-resolution restoration",
    )
    restore_parser.set_defaults(method="restore", run=sample)

    upscale_parser = commands.add_parser(
        "upscale",
        parents=[sample_options],
        help="render the locked 1440x1080 upscale",
    )
    upscale_parser.set_defaults(method="upscale", run=sample)

    finishing_parser = commands.add_parser(
        "finishing",
        parents=[sample_options],
        help="compare line repair, line finishing, and mild debanding",
    )
    finishing_parser.set_defaults(method="finishing", run=sample)

    ai_parser = commands.add_parser(
        "ai",
        parents=[sample_options],
        help="render a BasicVSR++ temporal restoration experiment",
    )
    ai_parser.add_argument(
        "--model",
        choices=(
            "denoise",
            "denoise-long",
            "compress1",
            "compress1-long",
            "compress2",
            "compress2-long",
            "compress3",
            "compress3-long",
            "cugan-conservative",
            "cugan-no-denoise",
            "cugan-denoise3x",
        ),
        default="denoise",
        help="temporal restoration model (default: denoise)",
    )
    ai_parser.set_defaults(run=sample)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.run(args)
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"mend: {error}", file=sys.stderr)
        return 1
