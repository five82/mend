# Mend

Mend restores supported NTSC animation DVDs before Spindle's AV1 encode. Its current profile is locked to the first ten seasons of a prime time long-running, traditionally animated 1989 TV series.

Raw MakeMKV rips remain the source of truth. Mend writes each restoration to a separate Spindle rip-cache entry and returns that derivative to Spindle's normal processing pipeline. It never modifies the original rip.

## Requirements

Mend currently targets Debian 13 with an NVIDIA Vulkan device. It requires:

- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- Spindle with fingerprint-based `cache process` selection
- The existing custom FFmpeg and ffprobe build
- mkvtoolnix
- `7z`, CMake, curl, Git, a C++ compiler, `glslangValidator`, and pkg-config
- Vulkan development files

The Debian build dependencies include `p7zip-full`, `cmake`, `curl`, `git`, `g++`, `glslang-tools`, `pkg-config`, and `libvulkan-dev`.

## Install

From a Mend checkout:

```bash
uv tool install .
mend setup
```

`uv tool install` creates an isolated Python environment and exposes the `mend` command. `mend setup` registers that environment as the active VapourSynth runtime, installs BestSource, Bwdif, and TIVTC into it, then builds and installs the pinned Real-CUGAN Vulkan plugin and models.

To reinstall after updating the checkout:

```bash
uv tool install --force .
mend setup
```

## Restore and hand off a disc

Pass one or more unique prefixes of source entries' Spindle cache fingerprints:

```bash
mend handoff FINGERPRINT [FINGERPRINT ...]
```

When multiple fingerprints are supplied, Mend restores and hands off each entry in order before starting the next one.

Mend:

1. Validates the cached RipSpec, episode-to-title mapping, and NTSC DVD source format.
2. Restores every episode to lossless 10-bit FFV1 at 1440x1080, square-pixel, progressive 23.976 fps.
3. Preserves the source audio, subtitle tracks, chapters, attachments, and relevant track metadata.
4. Validates every completed title before publishing anything to Spindle's rip cache.
5. Publishes a deterministic derivative with a fresh RipSpec and queues it with `spindle cache process`.

The locked profile restores film cadence, removes the 10-pixel ragged mastering edge from each horizontal side, and reconstructs the image with Real-CUGAN Pro denoise3x through Vulkan. The vertical frame is retained.

Each title reports timestamped start and completion messages with elapsed time. In an interactive terminal, VSPipe also displays live frame progress while the restoration is running.

Interrupted work is kept under `~/.cache/mend/handoffs/`. Run the same command again to validate and reuse completed titles. Published derivatives remain ordinary Spindle rip-cache entries and are managed by Spindle's normal cache size and pruning policy.

## Development

Create the project environment and install the native plugins:

```bash
uv sync
uv run mend setup
```

Run the checks:

```bash
uvx ruff format --check mend tests
uvx ruff check mend tests
uv run python -m unittest discover -s tests
uv run python -m mend.check_plugins
```
