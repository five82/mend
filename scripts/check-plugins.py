import vapoursynth as vs

required = {
    "bs",
    "bwdif",
    "dfttest",
    "dotkill",
    "fmtc",
    "mv",
    "neo_f3kdb",
    "rgvs",
    "tivtc",
    "warp",
    "znedi3",
}
installed = {plugin.namespace for plugin in vs.core.plugins()}
missing = sorted(required - installed)
if missing:
    raise SystemExit(f"missing VapourSynth plugins: {', '.join(missing)}")

import havsfunc  # noqa: F401
import nnedi3_resample  # noqa: F401

print("VapourSynth restoration and upscale plugin stack is ready")
