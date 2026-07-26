import vapoursynth as vs

required = {"bs", "bwdif", "tivtc"}
installed = {plugin.namespace for plugin in vs.core.plugins()}
missing = sorted(required - installed)
if missing:
    raise SystemExit(f"missing VapourSynth plugins: {', '.join(missing)}")

import havsfunc  # noqa: F401

print("VapourSynth temporal plugin stack is ready")
