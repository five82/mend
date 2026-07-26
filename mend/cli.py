import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

import vapoursynth as vs

FPS_NUM = 60_000
FPS_DEN = 1_001
METHODS = ("fieldmatch", "bwdif", "qtgmc")


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
    path = source / "spindle.cache.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


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
    start_frame = round(start * FPS_NUM / FPS_DEN)
    frame_count = round(duration * FPS_NUM / FPS_DEN)
    end_frame = start_frame + frame_count - 1
    script = Path(__file__).with_name("temporal.vpy")
    vspipe = Path(sys.executable).with_name("vspipe")
    pipe_command = [
        str(vspipe),
        "--arg",
        f"source={title}",
        "--arg",
        f"method={method}",
        "--arg",
        f"field_order={field_order}",
        "--start",
        str(start_frame),
        "--end",
        str(end_frame),
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
        f"setsar={sample_aspect_ratio.replace(':', '/')}",
        *color_options,
        "-c:v",
        "ffv1",
        "-level",
        "3",
        "-g",
        "1",
        "-metadata",
        f"mend_temporal_method={method}",
        "-metadata",
        f"mend_source={title}",
        "-metadata",
        f"mend_start_seconds={start}",
        str(output),
    ]

    first = subprocess.Popen(
        pipe_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
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
        raise RuntimeError(f"sample render failed for {method}: {detail}")

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

    methods = METHODS if args.method == "all" else (args.method,)
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
            sample_aspect_ratio,
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
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.run(args)
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"mend: {error}", file=sys.stderr)
        return 1
