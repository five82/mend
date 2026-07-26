import vapoursynth as vs

core = vs.core
source = globals()["source"]
method = globals()["method"]
field_order = globals().get("field_order", "tff")
if field_order not in ("tff", "bff"):
    raise ValueError("field_order must be tff or bff")

tff = field_order == "tff"
clip = core.bs.VideoSource(source, rff=True, cachemode=1, showprogress=False)
clip = core.std.SetFieldBased(clip, value=2 if tff else 1)

if method == "bwdif":
    output = core.bwdif.Bwdif(clip, field=3 if tff else 2)
elif method == "fieldmatch":
    bob = core.bwdif.Bwdif(clip, field=3 if tff else 2)
    first_fallback = core.std.SelectEvery(bob, cycle=2, offsets=0)
    second_fallback = core.std.SelectEvery(bob, cycle=2, offsets=1)
    first_field = 1 if tff else 0
    second_field = 0 if tff else 1
    first = core.tivtc.TFM(
        clip,
        order=1 if tff else 0,
        field=first_field,
        mode=0,
        PP=6,
        mChroma=0,
        cthresh=12,
        chroma=1,
        micmatching=0,
        clip2=first_fallback,
    )
    second = core.tivtc.TFM(
        clip,
        order=1 if tff else 0,
        field=second_field,
        mode=4,
        PP=6,
        mChroma=0,
        cthresh=12,
        chroma=1,
        micmatching=0,
        clip2=second_fallback,
    )
    output = core.std.Interleave([first, second])
elif method == "qtgmc":
    from havsfunc import QTGMC

    output = QTGMC(
        clip,
        Preset="Fast",
        TFF=tff,
        SourceMatch=3,
        Lossless=2,
        Sharpness=0.0,
    )
else:
    raise ValueError(f"unknown temporal method: {method}")

output.set_output()
