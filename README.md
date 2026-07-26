# mend

Mend is a tool for restoring badly mastered NTSC animation DVDs before Spindle's AV1 encode. The current scope is the first ten seasons of a 1989 prime time long-running, traditionally animated TV series.

Raw MakeMKV rips remain the source of truth. Mend currently analyzes them and renders short, lossless temporal-restoration comparisons; it does not publish files into Spindle's cache yet.

## Setup

Requirements: Debian 13, `uv`, `7zip`, the existing custom FFmpeg/ffprobe build, and mkvtoolnix. Mend does not require an FFmpeg rebuild.

```bash
./scripts/bootstrap-plugins
```

The script creates a uv environment and installs the VapourSynth source, field-matching, and deinterlacing plugins.

## Analyze a Spindle rip

Pass a cache fingerprint prefix, cache directory, or MKV path:

```bash
uv run python -m mend analyze FINGERPRINT
uv run python -m mend analyze FINGERPRINT --json
```

Analysis reports both coded MPEG-2 frames and the 29.97 fps display stream reconstructed from repeat-field flags. This distinction is required for MakeMKV's variable-frame-rate MKVs.

## Find temporal problem areas

Rank windows containing video-rate cadence, interlace evidence, or repeated fields:

```bash
uv run python -m mend scan FINGERPRINT --title TITLE.mkv
```

Omit `--title` to scan every MKV in the cache entry. Use the reported start times with `mend compare`; `--window`, `--count`, and `--json` control the scan output. Ranking is a fixture-selection heuristic, not an automatic restoration decision.

## Render temporal samples

```bash
uv run python -m mend sample FINGERPRINT \
  --title TITLE.mkv \
  --start 60 \
  --duration 10
```

This writes three 59.94p, video-only FFV1 samples under `~/.local/share/mend/samples/`:

- `fieldmatch.mkv`: two-parity field matching, with BWDIF only for unmatched combed frames
- `bwdif.mkv`: full BWDIF bob
- `qtgmc.mkv`: QTGMC Fast with source matching

Select one method with `--method`. For frame-synchronized inspection, render all three methods side by side with embedded labels:

```bash
uv run python -m mend compare FINGERPRINT \
  --title TITLE.mkv \
  --start 60 \
  --duration 10
```

The comparison is written as `comparison.mkv` in the same sample directory. These files are research fixtures, not library outputs.
