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


def deblock(clip):
    return core.deblock.Deblock(clip, quant=18)


def remove_gibbs(clip):
    luma = core.std.ShufflePlanes(clip, 0, vs.GRAY)
    edge = core.std.Expr(core.std.Prewitt(luma), "x 20 - 20 / 255 *")
    edge = core.rgvs.RemoveGrain(edge, mode=17)

    outer = edge
    for _ in range(4):
        outer = core.std.Maximum(outer)
    inner = core.std.Minimum(core.std.Inflate(edge))
    mask = core.std.Expr([outer, inner], "x y -")
    mask = core.std.BoxBlur(mask, hradius=1, vradius=1)

    smooth = core.dfttest.DFTTest(clip, tbsize=1, planes=[0])
    return core.std.MaskedMerge(
        clip, smooth, mask, planes=[0], first_plane=True
    )


def clean():
    from havsfunc import SMDegrain

    output = core.dotkill.DotKillS(restore("fieldmatch"), iterations=1)
    output = remove_gibbs(deblock(output))
    return SMDegrain(
        output,
        tr=2,
        thSAD=400,
        RefineMotion=True,
        contrasharp=True,
        plane=0,
        prefilter=2,
        chroma=False,
    )


def upscale():
    from nnedi3_resample import nnedi3_resample

    return nnedi3_resample(
        clean(),
        target_width=1440,
        target_height=1080,
        mats="601",
        matd="601",
        cplaces="mpeg2",
        cplaced="mpeg2",
        fulls=False,
        fulld=False,
        nns=4,
        mode="znedi3",
    )


if method == "comparison":
    branches = [
        core.text.Text(restore(name), name.upper(), alignment=7, scale=2)
        for name in ("fieldmatch", "bwdif", "qtgmc")
    ]
    output = core.std.StackHorizontal(branches)
elif method == "cleanup":
    branches = [
        core.text.Text(branch, label, alignment=7, scale=2)
        for branch, label in (
            (restore("fieldmatch"), "BASELINE"),
            (clean(), "RESTORATION V2"),
        )
    ]
    output = core.std.StackHorizontal(branches)
elif method == "restore":
    output = clean()
elif method == "upscale":
    output = upscale()
else:
    output = restore(method)

output.set_output()
