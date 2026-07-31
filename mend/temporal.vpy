import vapoursynth as vs

core = vs.core
source = globals()["source"]
clip = core.bs.VideoSource(source, rff=True, showprogress=False)
clip = core.std.SetFieldBased(clip, value=2)
bob = core.bwdif.Bwdif(clip, field=3)
fallback = core.std.SelectEvery(bob, cycle=2, offsets=0)
matched = core.tivtc.TFM(
    clip,
    order=1,
    field=1,
    mode=0,
    PP=6,
    mChroma=0,
    cthresh=12,
    chroma=1,
    micmatching=0,
    clip2=fallback,
)
restored = core.tivtc.TDecimate(matched, mode=1)
restored = core.std.CropRel(restored, left=10, right=10)
rgb = core.resize.Bicubic(
    restored,
    format=vs.RGBS,
    matrix_in_s="470bg",
    range_s="limited",
    range=1,
    dither_type="error_diffusion",
)
upscaled = core.rcnv.RealCUGAN(
    rgb,
    scale=2,
    noise=3,
    model=2,
    tta=0,
    syncgap=2,
    gpu_id=0,
    gpu_thread=2,
)
output = core.resize.Bicubic(
    upscaled,
    width=1440,
    height=1080,
    format=vs.YUV420P10,
    matrix_s="470bg",
    range_s="full",
    range=0,
    dither_type="error_diffusion",
)
output.set_output()
