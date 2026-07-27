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


def repair_line_doubling(clip):
    field_based = core.std.SetFieldBased(clip, value=2 if tff else 1)
    bobbed = core.znedi3.nnedi3(
        field_based,
        field=3 if tff else 2,
        nns=4,
        qual=2,
    )
    first = core.std.SelectEvery(bobbed, cycle=2, offsets=0)
    second = core.std.SelectEvery(bobbed, cycle=2, offsets=1)
    return core.std.SetFieldBased(core.std.Merge(first, second), value=0)


def clean(input_clip=None):
    from havsfunc import SMDegrain

    if input_clip is None:
        input_clip = restore("fieldmatch")
    output = core.dotkill.DotKillS(input_clip, iterations=1)
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


def nnedi3_upscale(input_clip):
    from nnedi3_resample import nnedi3_resample

    return nnedi3_resample(
        input_clip,
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


def ai_temporal(input_clip, model, length):
    from vsbasicvsrpp import basicvsrpp

    rgb = core.resize.Bicubic(
        input_clip,
        format=vs.RGBH,
        matrix_in_s="470bg",
        range_s="limited",
        range=1,
        dither_type="error_diffusion",
    )
    restored = basicvsrpp(
        rgb,
        model=model,
        auto_download=True,
        length=length,
        cpu_cache=True,
    )
    return core.resize.Bicubic(
        restored,
        format=vs.YUV420P10,
        matrix_s="470bg",
        range_s="full",
        range=0,
        dither_type="error_diffusion",
    )


def finish_lines(clip):
    from havsfunc import FastLineDarkenMOD

    darkened = FastLineDarkenMOD(clip, strength=20, protection=6)
    return core.warp.AWarpSharp2(
        darkened,
        blur=4,
        type=1,
        depth=3,
        chroma=0,
        cplace="mpeg2",
    )


def to_bits(clip, bits):
    return core.fmtc.bitdepth(clip, bits=bits, dmode=1)


def upscale():
    repaired = clean(repair_line_doubling(restore("fieldmatch")))
    output = finish_lines(nnedi3_upscale(to_bits(repaired, 16)))
    return core.neo_f3kdb.Deband(
        output,
        preset="low",
        dynamic_grain=False,
        keep_tv_range=True,
        output_depth=10,
    )


def finishing_comparison():
    previous = nnedi3_upscale(clean())
    repaired_native = clean(repair_line_doubling(restore("fieldmatch")))
    repaired = nnedi3_upscale(repaired_native)
    line_finished = finish_lines(previous)

    branches = [
        core.text.Text(to_bits(branch, 10), label, alignment=7, scale=2)
        for branch, label in (
            (previous, "PREVIOUS V2"),
            (repaired, "+ LINE-DOUBLE REPAIR"),
            (line_finished, "+ LINE FINISH"),
            (upscale(), "LOCKED: ALL + MILD DEBAND"),
        )
    ]
    return core.std.StackVertical(
        [
            core.std.StackHorizontal(branches[:2]),
            core.std.StackHorizontal(branches[2:]),
        ]
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
elif method == "finishing":
    output = finishing_comparison()
elif method.startswith("ai-denoise"):
    output = ai_temporal(restore("fieldmatch"), 9, int(method.removeprefix("ai-denoise") or 15))
elif method.startswith("ai-compress"):
    parts = method.removeprefix("ai-compress").split("-", 1)
    output = ai_temporal(
        restore("fieldmatch"), int(parts[0]), int(parts[1]) if len(parts) == 2 else 15
    )
else:
    output = restore(method)

output.set_output()
