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
bob = core.bwdif.Bwdif(clip, field=3 if tff else 2)


def restore(name):
    if name == "bwdif":
        return bob
    if name == "fieldmatch":
        first_fallback = core.std.SelectEvery(bob, cycle=2, offsets=0)
        second_fallback = core.std.SelectEvery(bob, cycle=2, offsets=1)
        first = core.tivtc.TFM(
            clip,
            order=1 if tff else 0,
            field=1 if tff else 0,
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
            field=0 if tff else 1,
            mode=4,
            PP=6,
            mChroma=0,
            cthresh=12,
            chroma=1,
            micmatching=0,
            clip2=second_fallback,
        )
        return core.std.Interleave([first, second])
    if name == "qtgmc":
        from havsfunc import QTGMC

        return QTGMC(
            clip,
            Preset="Fast",
            TFF=tff,
            SourceMatch=3,
            Lossless=2,
            Sharpness=0.0,
        )
    raise ValueError(f"unknown temporal method: {name}")


def clean():
    # Keep cleanup spatial: temporal smoothing risks trails across field-time motion.
    return core.deblock.Deblock(restore("fieldmatch"), quant=18)


if method == "comparison":
    branches = [
        core.text.Text(restore(name), name.upper(), alignment=7, scale=2)
        for name in ("fieldmatch", "bwdif", "qtgmc")
    ]
    output = core.std.StackHorizontal(branches)
elif method == "cleanup":
    baseline = restore("fieldmatch")
    branches = [
        core.text.Text(branch, label, alignment=7, scale=2)
        for branch, label in (
            (baseline, "BASELINE"),
            (clean(), "DEBLOCK Q18"),
        )
    ]
    output = core.std.StackHorizontal(branches)
elif method == "restore":
    output = clean()
else:
    output = restore(method)

output.set_output()
