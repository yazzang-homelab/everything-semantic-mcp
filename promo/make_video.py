"""Render the 1:1 Korean promo video (1080x1080, 30 fps, ~22 s) -> promo/everything-semantic-mcp.mp4

  pip install pillow imageio-ffmpeg
  python promo/make_video.py

Frames are drawn with Pillow and piped to ffmpeg (H.264, yuv420p: plays on Threads/Instagram/
phones). All numbers on screen are measured values from benchmarks/bench.py on the README's
test machine; the file paths in the demo are illustrative.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
OUT = HERE / "everything-semantic-mcp.mp4"
POSTER = HERE / "poster.png"
W = H = 1080
FPS = 30
DUR = 22.0

BG = (11, 15, 23)
PANEL = (18, 24, 38)
LINE = (31, 42, 61)
INK = (231, 237, 247)
MUTE = (139, 152, 173)
ACC = (255, 176, 32)
GREEN = (61, 220, 151)
BLUE = (106, 168, 255)
RED = (255, 107, 107)

FONTS = Path("C:/Windows/Fonts")


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    # Noto Sans KR: Malgun Gothic draws "\\" as the won sign, which would mangle Windows paths.
    f = ImageFont.truetype(str(FONTS / "NotoSansKR-VF.ttf"), size)
    f.set_variation_by_name(b"Bold" if bold else b"Regular")
    return f


def mono(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / "CascadiaMono.ttf"), size)


F = {
    k: font(*v)
    for k, v in {
        "h1": (104, True),
        "h2": (68, True),
        "h3": (46, True),
        "body": (36, False),
        "bodyb": (36, True),
        "small": (28, False),
        "term": (34, False),
        "termb": (34, True),
        "big": (120, True),
        "tag": (28, True),
    }.items()
}
M = {"tag": mono(26), "url": mono(34)}


# ── easing / timing helpers ───────────────────────────────────────────────


def clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def ease(x: float) -> float:
    x = clamp(x)
    return 1 - (1 - x) ** 3


def span(t: float, start: float, dur: float) -> float:
    return ease((t - start) / dur)


def mix(c1, c2, a):
    return tuple(int(c1[i] + (c2[i] - c1[i]) * a) for i in range(3))


# ── drawing primitives ────────────────────────────────────────────────────


def background(img: Image.Image, t: float) -> None:
    d = ImageDraw.Draw(img)
    off = int((t * 12) % 54)
    for x in range(-54 + off, W, 54):
        d.line([(x, 0), (x, H)], fill=(17, 23, 34))
    for y in range(-54 + off, H, 54):
        d.line([(0, y), (W, y)], fill=(17, 23, 34))


def text(d, xy, s, f, fill, a=1.0, anchor="la"):
    d.text(xy, s, font=f, fill=mix(BG, fill, a), anchor=anchor)


def panel(d, box, a=1.0, border=LINE, fill=PANEL, r=26):
    d.rounded_rectangle(box, radius=r, fill=mix(BG, fill, a), outline=mix(BG, border, a), width=3)


def tag(d, x, y, s, color=ACC, a=1.0):
    w = d.textlength(s, font=F["tag"]) + 64
    d.rounded_rectangle(
        (x, y, x + w, y + 56),
        radius=28,
        fill=mix(BG, (26, 20, 8), a),
        outline=mix(BG, (58, 46, 16), a),
        width=2,
    )
    d.ellipse((x + 22, y + 22, x + 34, y + 34), fill=mix(BG, color, a))
    text(d, (x + 46, y + 28), s, F["tag"], color, a, anchor="lm")


def typed(s: str, t: float, start: float, cps: float = 9.0) -> str:
    n = int(max(0.0, t - start) * cps)
    return s[:n]


# ── scenes ────────────────────────────────────────────────────────────────


def scene_hook(d, t):  # 0 - 4.5
    a = span(t, 0.2, 0.6)
    tag(d, 88, 120, "MCP 서버 · Windows · 오픈소스", a=a)
    for i, (s, c) in enumerate([("파일 이름을", INK), ("몰라도", INK), ("AI가 찾는다", ACC)]):
        k = span(t, 0.5 + i * 0.45, 0.6)
        text(d, (88, 260 + i * 130 + int((1 - k) * 40)), s, F["h1"], c, k)
    k = span(t, 2.2, 0.7)
    text(d, (88, 720), "voidtools Everything 위에", F["body"], MUTE, k)
    text(d, (88, 775), "로컬 의미 검색을 얹은 MCP 서버", F["bodyb"], INK, k)


def scene_demo(d, t):  # 4.5 - 11
    t0 = 4.5
    a = span(t, t0, 0.5)
    text(d, (88, 110), "이름이 정확하지 않으면?", F["h2"], INK, a)

    # left: name search
    panel(d, (60, 230, 1020, 520), a)
    text(d, (100, 268), "기존 이름 검색", F["small"], RED, a)
    q = typed("신입 교육 자료", t, t0 + 0.6)
    text(d, (100, 330), "› " + q + ("▏" if int(t * 2) % 2 and len(q) < 8 else ""), F["termb"], INK, a)
    k = span(t, t0 + 2.0, 0.4)
    text(d, (100, 400), "결과 0건", F["termb"], RED, k)
    text(d, (100, 452), "실제 폴더명: 신입사원 온보딩 가이드", F["small"], MUTE, k)

    # right: semantic
    k2 = span(t, t0 + 2.8, 0.5)
    panel(d, (60, 560, 1020, 1000), k2, border=mix(LINE, GREEN, 0.5))
    text(d, (100, 598), "everything_semantic_search", F["small"], GREEN, k2)
    q2 = typed("신입 교육 자료", t, t0 + 3.2)
    text(d, (100, 660), "› " + q2, F["termb"], INK, k2)
    rows = [
        ("0.72", "D:\\HR\\신입사원 온보딩 가이드\\"),
        ("0.66", "D:\\HR\\…\\환영 안내서.pdf"),
        ("0.58", "\\\\fileserver\\공유\\교육자료\\"),
    ]
    for i, (sc, p) in enumerate(rows):
        r = span(t, t0 + 4.6 + i * 0.3, 0.35)
        y = 730 + i * 62
        text(d, (100, y), sc, F["termb"], ACC, r)
        text(d, (200, y), p, F["term"], INK, r)
    r = span(t, t0 + 5.7, 0.4)
    text(d, (100, 930), "3건 · 약 50ms · 118만 개 이름", F["termb"], GREEN, r)
    text(d, (1000, 1040), "* 경로는 예시, 시간은 실측", F["small"], MUTE, r, anchor="rs")


def scene_features(d, t):  # 11 - 16
    t0 = 11.0
    a = span(t, t0, 0.5)
    text(d, (88, 110), "무엇이 다른가", F["h2"], INK, a)
    items = [
        (ACC, "01  의미 검색", "뜻으로 찾기 · GPU·API 키 불필요", "모든 처리는 내 PC 안에서"),
        (GREEN, "02  자가 진단", "Everything이 멈추는 설정을 찾고", "해결법까지 알려줌"),
        (BLUE, "03  순수 Python", "es.exe·DLL 없이 공식 IPC 직결", "한글 · NAS 경로 지원"),
    ]
    for i, (c, h, l1, l2) in enumerate(items):
        k = span(t, t0 + 0.5 + i * 0.6, 0.6)
        x = 60 + int((1 - k) * 80)
        y = 240 + i * 260
        panel(d, (x, y, x + 960, y + 220), k)
        d.rectangle((x, y + 20, x + 10, y + 200), fill=mix(BG, c, k))
        text(d, (x + 50, y + 34), h, F["h3"], c, k)
        text(d, (x + 50, y + 108), l1, F["body"], INK, k)
        text(d, (x + 50, y + 158), l2, F["body"], MUTE, k)


def countup(v: float, k: float) -> float:
    return v * k


def scene_numbers(d, t):  # 16 - 19.5
    t0 = 16.0
    a = span(t, t0, 0.5)
    text(d, (88, 110), "직접 측정한 숫자", F["h2"], INK, a)
    k = span(t, t0 + 0.4, 1.4)
    cells = [
        (GREEN, f"{int(countup(50, k))}ms", "의미 검색 1회", "118만 개 이름"),
        (ACC, f"{countup(612, k):.0f}만", "색인 항목", "로컬 + NAS"),
        (BLUE, f"{countup(0.98, k):.2f}", "2단계 재정렬", "recall@20"),
        (RED, f"{int(countup(264, k))}초", "의미 색인 생성", "노트북 CPU"),
    ]
    for i, (c, big, l1, l2) in enumerate(cells):
        x = 60 + (i % 2) * 490
        y = 230 + (i // 2) * 360
        kk = span(t, t0 + 0.2 + i * 0.15, 0.5)
        panel(d, (x, y, x + 470, y + 330), kk)
        text(d, (x + 40, y + 50), big, F["big"], c, kk)
        text(d, (x + 40, y + 210), l1, F["body"], INK, kk)
        text(d, (x + 40, y + 260), l2, F["small"], MUTE, kk)
    text(d, (540, 1000), "Windows 11 · i7-1255U · Everything 1.4 x64", F["small"], MUTE, a, anchor="mm")


def scene_cta(d, t):  # 19.5 - 22
    t0 = 19.5
    a = span(t, t0, 0.6)
    text(d, (540, 330), "everything-semantic-mcp", F["h2"], INK, a, anchor="mm")
    text(d, (540, 430), "Claude Code · Claude Desktop · Codex · Cursor", F["body"], MUTE, a, anchor="mm")
    k = span(t, t0 + 0.5, 0.6)
    panel(d, (70, 520, 1010, 700), k, border=mix(LINE, ACC, 0.6))
    d.text((540, 578), "github.com/yazzang-homelab/", font=M["url"], fill=mix(BG, ACC, k), anchor="mm")
    d.text((540, 640), "everything-semantic-mcp", font=M["url"], fill=mix(BG, ACC, k), anchor="mm")
    text(d, (540, 790), "오픈소스 · MIT · 무료", F["h3"], GREEN, k, anchor="mm")


SCENES = [
    (0.0, 4.5, scene_hook),
    (4.5, 11.0, scene_demo),
    (11.0, 16.0, scene_features),
    (16.0, 19.5, scene_numbers),
    (19.5, DUR, scene_cta),
]


def frame(t: float) -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    background(img, t)
    d = ImageDraw.Draw(img)
    for start, end, fn in SCENES:
        if start <= t < end:
            fn(d, t)
            fade = (
                min(clamp((t - start) / 0.25), clamp((end - t) / 0.3))
                if fn is not scene_cta
                else clamp((t - start) / 0.25)
            )
            if fade < 1:
                img = Image.blend(Image.new("RGB", (W, H), BG), img, fade)
            break
    # progress bar
    d = ImageDraw.Draw(img)
    d.rectangle((0, H - 8, int(W * t / DUR), H), fill=ACC)
    return img


def main() -> None:
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [
        ffmpeg,
        "-y",
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{W}x{H}",
        "-r",
        str(FPS),
        "-i",
        "-",
        "-c:v",
        "libx264",
        "-preset",
        "slow",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(OUT),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    frames = int(DUR * FPS)
    for i in range(frames):
        t = i / FPS
        proc.stdin.write(frame(t).tobytes())
        if abs(t - 10.2) < 1 / FPS / 2:
            frame(t).save(POSTER)
    proc.stdin.close()
    if proc.wait() != 0:
        raise SystemExit("ffmpeg failed")
    print(OUT, OUT.stat().st_size // 1024, "KB,", frames, "frames")


if __name__ == "__main__":
    main()
