import vapoursynth as vs


def main() -> int:
    required = {"bs", "bwdif", "rcnv", "tivtc"}
    installed = {plugin.namespace for plugin in vs.core.plugins()}
    missing = sorted(required - installed)
    if missing:
        raise RuntimeError(f"missing VapourSynth plugins: {', '.join(missing)}")
    print("Mend's VapourSynth plugin stack is ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
