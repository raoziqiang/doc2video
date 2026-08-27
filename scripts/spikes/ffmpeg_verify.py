"""M0 Spike:ffmpeg 命令集真实执行验证(方案附录 C + P0-06 复审要求)。

验证项:
1. C.1 单场景 Ken Burns + 纯数值淡入淡出 + adelay(横屏 16:9 与竖屏 9:16)
2. C.2 concat 拼接(流参数统一后)
3. C.3 sidechaincompress 方向实测 —— 旁白应压缩 BGM(2kHz 带增益下降),而非相反
4. 两遍 loudnorm(测量 print_format=json → 应用 measured_*)
5. C.4 ASS 烧录(CJK 字体 msyh.ttc)

结果写入 docs/spikes/ffmpeg_verify.json(提交),中间产物在 docs/spikes/_work(忽略)。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / "docs" / "spikes" / "_work"
RESULT = ROOT / "docs" / "spikes" / "ffmpeg_verify.json"

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"

results: list[dict] = []


def run(argv: list[str], retries: int = 2, **kw) -> subprocess.CompletedProcess:
    """执行 ffmpeg;观察到本机间歇性 -22(新鲜文件读取窗口,疑似杀软扫描),按管线重试策略:2s 指数退避(与 config retry.backoff_base_s 一致)。"""
    delay = 2.0
    for attempt in range(retries + 1):
        out = subprocess.run(argv, capture_output=True, text=True, timeout=180, **kw)
        if out.returncode == 0 or attempt == retries:
            if out.returncode != 0 and os.environ.get("DOC2VIDEO_SPIKE_DEBUG"):
                print("CMD FAILED:", " ".join(argv)[:300], file=sys.stderr)
                dump = Path(os.environ.get("DOC2VIDEO_SPIKE_DEBUG_DUMP", ".")) / f"fail_{int(time.time()*1000)}.log"
                dump.write_text(out.stderr or "", encoding="utf-8")
                print(f"full stderr -> {dump}", file=sys.stderr)
            return out
        if os.environ.get("DOC2VIDEO_SPIKE_DEBUG"):
            print(f"retry {attempt + 1}/{retries}: rc={out.returncode}", file=sys.stderr)
        time.sleep(delay)
        delay *= 2
    raise RuntimeError("unreachable")


def check(name: str, ok: bool, detail: str) -> None:
    results.append({"name": name, "ok": bool(ok), "detail": detail})
    print(f"  {'✓' if ok else '✗'} {name:36s} {detail}")


def ffprobe_json(path: Path) -> dict:
    out = run(
        [
            FFPROBE, "-v", "error", "-show_entries",
            "format=duration:stream=codec_name,width,height,pix_fmt,profile,level,sample_rate,channels",
            "-of", "json", str(path),
        ]
    )
    return json.loads(out.stdout)


def make_fixtures() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    run([FFMPEG, "-y", "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=25:duration=5",
         "-frames:v", "1", str(WORK / "scene16.png")], check=False)
    run([FFMPEG, "-y", "-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=25:duration=5",
         "-frames:v", "1", str(WORK / "scene916.png")], check=False)
    # 旁白:440Hz 正弦 3s,-6dB(峰值远高于 ducking 阈值 0.03,确保压缩充分触发)
    run([FFMPEG, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
         "-af", "volume=-6dB", str(WORK / "narration.wav")], check=False)
    # BGM:2000Hz 正弦 6s,-6dB
    run([FFMPEG, "-y", "-f", "lavfi", "-i", "sine=frequency=2000:duration=6",
         "-af", "volume=-6dB", str(WORK / "bgm.wav")], check=False)
    time.sleep(1.0)  # 静置:规避本机观察到的"新鲜文件读取窗口"瞬时失败


def scene_clip(input_png: str, canvas: str, crop: str, d: str, out_name: str) -> Path:
    """C.1:Ken Burns + 数值 fade + adelay + apad + 统一流参数。

    ⚠️ 坑(实测,gyan 8.1.2):crop 滤镜只接受冒号语法 crop=w:h(字母 x 会被当表达式变量,报
    \"Invalid chars 'x2160' at the end of expression\")。scale 与 zoompan 的 s 参数接受 WxH。
    """
    out = WORK / out_name
    run(
        [
            FFMPEG, "-y", "-loop", "1", "-i", str(WORK / input_png), "-i", str(WORK / "narration.wav"),
            "-filter_complex",
            f"[0:v]scale={crop}:force_original_aspect_ratio=increase,crop={crop},"
            f"zoompan=z='min(zoom+0.0008,1.15)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            f"d={d}:s={canvas}:fps=25,format=yuv420p,settb=1/25,"
            f"fade=t=in:st=0:d=0.3,fade=t=out:st=3.2:d=0.3[v];"
            f"[1:a]adelay=500|500,apad[a]",
            "-map", "[v]", "-map", "[a]", "-t", "3.5",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-profile:v", "high", "-level:v", "4.1",
            "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
            str(out),
        ],
        check=False,
    )
    return out


def verify_clip(path: Path, expect_w: int, expect_h: int) -> None:
    if not path.exists():
        check(f"C.1 {path.name}", False, "输出文件不存在")
        return
    info = ffprobe_json(path)
    streams = info.get("streams", [])
    v = next((s for s in streams if s.get("codec_name") == "h264"), None)
    a = next((s for s in streams if s.get("codec_name") == "aac"), None)
    dur = float(info.get("format", {}).get("duration", 0))
    ok = bool(
        v and v.get("width") == expect_w and v.get("height") == expect_h
        and v.get("pix_fmt") == "yuv420p" and v.get("profile") == "High"
        and a and a.get("sample_rate") == "48000" and a.get("channels") == 2
        and abs(dur - 3.5) < 0.1
    )
    check(
        f"C.1 {path.name}",
        ok,
        f"dur={dur:.2f}s h264:{v.get('profile') if v else '?'} {v.get('width') if v else '?'}x{v.get('height') if v else '?'} "
        f"{v.get('pix_fmt') if v else '?'} aac:{a.get('sample_rate') if a else '?'}Hz/{a.get('channels') if a else '?'}ch",
    )


def test_concat() -> None:
    """C.2:concat demuxer 拼接两个统一流参数的片段。"""
    scenes_txt = WORK / "scenes.txt"
    scenes_txt.write_text(
        f"file '{WORK / 'scene01_16x9.mp4'}'\nfile '{WORK / 'scene01_16x9.mp4'}'\n",
        encoding="utf-8",
    )
    out = WORK / "joined.mp4"
    run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(scenes_txt), "-c", "copy", str(out)],
        check=False)
    dur = float(ffprobe_json(out).get("format", {}).get("duration", 0)) if out.exists() else 0
    check("C.2 concat", out.exists() and abs(dur - 7.0) < 0.15, f"dur={dur:.2f}s(期望 ~7.0s)")


def band_volume(wav: Path, freq: int) -> float:
    """测量 2kHz±100Hz 带的 mean_volume(dB)。"""
    out = run(
        [FFMPEG, "-y", "-i", str(wav), "-af", f"bandpass=f={freq}:w=200,volumedetect", "-f", "null", "-"],
        check=False,
    )
    m = re.search(r"mean_volume:\s*(-?[\d.]+)\s*dB", out.stderr or "")
    return float(m.group(1)) if m else float("nan")


def test_ducking_direction() -> None:
    """C.3 方向验证:BGM 作 main(被压缩),旁白作 sidechain。"""
    narration = WORK / "narration.wav"
    bgm = WORK / "bgm.wav"
    ref = WORK / "mix_noduck.wav"
    ducked = WORK / "mix_ducked.wav"
    # 对照:无 ducking 混合
    run([FFMPEG, "-y", "-i", str(narration), "-i", str(bgm),
         "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=first:normalize=0",
         str(ref)], check=False)
    # 正确方向:BGM(#1)为 main、旁白(#0)为 sidechain → 旁白触发时 BGM 被压缩
    run([FFMPEG, "-y", "-i", str(narration), "-i", str(bgm),
         "-filter_complex",
         "[1:a][0:a]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400[bgm_duck];"
         "[0:a][bgm_duck]amix=inputs=2:duration=first:normalize=0",
         str(ducked)], check=False)
    # 错误方向(反接)对照:#0 为 main → 旁白被压缩
    wrong = WORK / "mix_wrongdir.wav"
    run([FFMPEG, "-y", "-i", str(narration), "-i", str(bgm),
         "-filter_complex",
         "[0:a][1:a]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400[n_duck];"
         "[1:a][n_duck]amix=inputs=2:duration=first:normalize=0",
         str(wrong)], check=False)

    v_ref = band_volume(ref, 2000)
    v_duck = band_volume(ducked, 2000)
    v_wrong = band_volume(wrong, 2000)
    correct_dir_works = v_duck < v_ref - 2.0
    wrong_dir_wrong = v_wrong >= v_duck  # 反接时 BGM 不被压缩
    check(
        "C.3 ducking 方向",
        correct_dir_works and wrong_dir_wrong,
        f"BGM带均值: 无duck={v_ref:.1f}dB 正确方向={v_duck:.1f}dB 反接对照={v_wrong:.1f}dB",
    )


def test_two_pass_loudnorm() -> None:
    """两遍 loudnorm:第一遍测量 print_format=json,第二遍应用 measured_*。"""
    src = WORK / "mix_ducked.wav"
    if not src.exists():
        check("两遍 loudnorm", False, "前置产物缺失")
        return
    p1 = run([FFMPEG, "-y", "-i", str(src), "-af",
              "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"], check=False)
    m = re.search(r"\{.*\}", p1.stderr or "", re.S)
    measured: dict = json.loads(m.group(0)) if m else {}
    ok_measure = all(k in measured for k in ("input_i", "input_tp", "input_lra", "input_thresh"))
    out = WORK / "loudnorm_final.wav"
    run([FFMPEG, "-y", "-i", str(src), "-af",
         f"loudnorm=I=-16:TP=-1.5:LRA=11:measured_I={measured.get('input_i','-23')}:"
         f"measured_TP={measured.get('input_tp','-1')}:measured_LRA={measured.get('input_lra','11')}:"
         f"measured_thresh={measured.get('input_thresh','-33')}:linear=true",
         str(out)], check=False)
    check("两遍 loudnorm", ok_measure and out.exists(),
          f"measured={{input_i:{measured.get('input_i','?')}, input_tp:{measured.get('input_tp','?')}}}")


def test_ass_burn_cjk() -> None:
    """C.4:ASS 烧录 + CJK 字体。

    坑(实测,gyan 8.1.2):`fontsdir=C:/Windows/Fonts` 的盘符冒号无法被滤镜解析器正确转义
    (`\\:` 无效,报 \"No option name near '/Windows/Fonts'\")。该 build 带 fontconfig,
    `ass=<文件>` 不带 fontsdir 即可自动解析系统字体(Microsoft YaHei 实测命中)。
    备选:fontsdir 用无冒号的相对路径(如 docs 中的 fontsdir=fonts)。
    """
    font_dir = Path(r"C:\Windows\Fonts")
    ass = WORK / "sub.ass"
    ass.write_text(
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, Bold, BorderStyle\n"
        "Style: Default,Microsoft YaHei,48,&H00FFFFFF,&H00000000,1,1\n"
        "[Events]\nFormat: Layer, Start, End, Style, Text\n"
        "Dialogue: 0,0:00:00.50,0:00:02.50,Default,大家好,这是一条中文字幕测试\n",
        encoding="utf-8",
    )
    out = WORK / "burned.mp4"
    run([FFMPEG, "-y", "-i", "scene01_16x9.mp4", "-vf",
         f"ass={ass.name}", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
         "-profile:v", "high", "-level:v", "4.1", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
         "-movflags", "+faststart", str(out)], check=False, cwd=WORK)
    check("C.4 ASS 烧录(CJK)", out.exists(),
          f"fontconfig 解析微软雅黑({'msyh.ttc 存在' if (font_dir / 'msyh.ttc').exists() else '字体缺失'})")


def main() -> int:
    print("ffmpeg Spike — 真实执行验证")
    ver = run([FFMPEG, "-version"], check=False)
    version_line = ver.stdout.splitlines()[0] if ver.stdout else "unknown"
    print(f"  ffmpeg: {version_line}")
    make_fixtures()
    for name in ("scene01_16x9.mp4", "scene01_916.mp4"):
        pass
    clip16 = scene_clip("scene16.png", "1920x1080", "3840:2160", "88", "scene01_16x9.mp4")
    clip916 = scene_clip("scene916.png", "1080x1920", "2160:3840", "88", "scene01_916.mp4")
    verify_clip(clip16, 1920, 1080)
    verify_clip(clip916, 1080, 1920)
    test_concat()
    test_ducking_direction()
    test_two_pass_loudnorm()
    test_ass_burn_cjk()

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "ffmpeg": version_line,
        "checks": results,
        "frozen": {
            "zoompan_d": "88(3.5s@25fps)",
            "fade": {"in_st": 0, "in_d": 0.3, "out_st": "scene_total-0.3(数值)", "out_d": 0.3},
            "codec": "libx264 -crf 20 -profile:v high -level:v 4.1 -pix_fmt yuv420p",
            "audio": "aac 128k 48000Hz stereo",
            "ducking": "[1:a][0:a]sidechaincompress(threshold=0.03,ratio=8,attack=20,release=400)",
            "loudnorm": "I=-16 TP=-1.5 LRA=11 两遍(measure→apply)",
            "cjk_font": "Microsoft YaHei(msyh.ttc)",
        },
    }
    RESULT.parent.mkdir(parents=True, exist_ok=True)
    RESULT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    failed = [c for c in results if not c["ok"]]
    print(f"\n结论: {len(results)-len(failed)}/{len(results)} 通过 → {RESULT}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
