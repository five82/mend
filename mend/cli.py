import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path

FILM_FPS_NUM = 24_000
FPS_DEN = 1_001
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
    files = sorted(source.glob("*.mkv"))
    if not files:
        raise ValueError(f"no MKV files in {source}")
    return files


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
        "stream=index,codec_name,codec_type,width,height,sample_aspect_ratio,field_order,r_frame_rate,avg_frame_rate,pix_fmt,color_range,color_space,color_transfer,color_primaries,chroma_location:format=duration,size",
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


def render_handoff_video(title: Path, output: Path, color_metadata: dict) -> None:
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
        "--container",
        "y4m",
        str(script),
        "-",
    ]

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
        "setsar=1/1",
        *color_options,
        "-c:v",
        "ffv1",
        "-level",
        "3",
        "-g",
        "1",
        "-metadata",
        "mend_temporal_method=upscale",
        "-metadata",
        f"mend_profile={HANDOFF_PROFILE}",
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
        raise RuntimeError(f"render failed: {detail}")

    if properties:
        metadata_result = subprocess.run(
            ["mkvpropedit", str(output), "--edit", "track:v1", *properties],
            capture_output=True,
            check=False,
        )
        if metadata_result.returncode:
            output.unlink(missing_ok=True)
            detail = metadata_result.stderr.decode(errors="replace").strip()
            raise RuntimeError(f"metadata update failed: {detail}")


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


def render_handoff_title(source: Path, output: Path) -> None:
    source_data = probe_container(source)
    stream = next(
        item for item in source_data["streams"] if item.get("codec_type") == "video"
    )
    video_temp = output.parent / f".{output.stem}.video.mkv"
    mux_temp = output.parent / f".{output.stem}.mux.mkv"
    video_temp.unlink(missing_ok=True)
    mux_temp.unlink(missing_ok=True)
    try:
        render_handoff_video(source, video_temp, stream)
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
        render_handoff_title(source_file, output)

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


def setup(_args: argparse.Namespace) -> int:
    environment = os.environ.copy()
    environment["MEND_ENV"] = sys.prefix
    script = Path(__file__).with_name("bootstrap-plugins")
    subprocess.run(["sh", str(script)], check=True, env=environment)
    return 0


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


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="mend", description="Restore supported NTSC animation DVDs for Spindle"
    )
    commands = result.add_subparsers(dest="command", required=True)

    setup_parser = commands.add_parser(
        "setup", help="install and verify Mend's native VapourSynth plugins"
    )
    setup_parser.set_defaults(run=setup)

    handoff_parser = commands.add_parser(
        "handoff",
        help="restore a supported DVD cache entry and queue it in Spindle",
    )
    handoff_parser.add_argument(
        "source", help="Spindle rip-cache directory or fingerprint prefix"
    )
    handoff_parser.set_defaults(run=handoff)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.run(args)
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"mend: {error}", file=sys.stderr)
        return 1
