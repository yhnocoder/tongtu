#!/usr/bin/env python3
"""fixture 论文的图片资产生成器（零第三方依赖，纯 zlib/struct 手写）。

三篇 fixture 论文（`tests/fixtures/papers/`）里的 `\\includegraphics` 需要真图文件：
figures 阶段要拿它们练预渲染，编译层 e2e 要拿它们练 xelatex 的图片路径。真实论文的
图不入库（license，见 `README.md`），所以这里现场造两种**最小的合法文件**：

* **PNG**：8-bit truecolor（color type 2），逐行 filter 0，IDAT 走 `zlib.compress`。
  三个 chunk（IHDR / IDAT / IEND）各自带 CRC32——这是 PNG 规范要求的全部。
* **PDF 1.4**：单页、一个 `re f` 填充矩形，手写 xref 表与 trailer。字节偏移由组装
  过程记账得到，因此文件是**自洽**的（xelatex 与 xdvipdfmx 都要读 xref 与 MediaBox）。

生成物**提交进仓库**（CI 不跑本脚本），但随时可复跑再生：

    uv run python tests/fixtures/gen_assets.py          # 写回各论文的 figures/
    uv run python tests/fixtures/gen_assets.py --check  # 只比对，不写盘

PNG 的字节流含 zlib 压缩结果，理论上随 zlib 版本可变；`--check` 与
`tests/test_fixtures.py` 因此对 PNG 比对**结构等价**（IHDR + 解压后的像素流），
对 PDF 比对逐字节。
"""

from __future__ import annotations

import argparse
import struct
import sys
import zlib
from pathlib import Path

PAPERS = Path(__file__).resolve().parent / "papers"

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

#: 生成清单：相对 `papers/` 的路径 → (类型, 参数)。
#: 尺寸刻意小（图只是占位，视觉内容不承载任何信息），但都是合法可解码的文件。
ASSETS: dict[str, tuple[str, dict]] = {
    "article/figures/pipeline.pdf": (
        "pdf",
        {
            "width": 144,
            "height": 108,
            "rects": [
                (8, 8, 128, 92, (0.94, 0.95, 0.97)),
                (16, 40, 32, 28, (0.20, 0.40, 0.65)),
                (56, 40, 32, 28, (0.35, 0.58, 0.78)),
                (96, 40, 32, 28, (0.55, 0.72, 0.86)),
            ],
        },
    ),
    "article/figures/residuals.png": ("png", {"width": 48, "height": 32, "pattern": "bars"}),
    "revtex/figures/spectrum.pdf": (
        "pdf",
        {
            "width": 120,
            "height": 96,
            "rects": [
                (6, 6, 108, 84, (0.97, 0.96, 0.93)),
                (18, 16, 14, 54, (0.65, 0.25, 0.20)),
                (42, 16, 14, 38, (0.75, 0.45, 0.25)),
                (66, 16, 14, 62, (0.55, 0.30, 0.45)),
                (90, 16, 14, 26, (0.30, 0.35, 0.55)),
            ],
        },
    ),
    "conference/figures/layout.png": ("png", {"width": 64, "height": 24, "pattern": "lanes"}),
}


# --------------------------------------------------------------------------- #
# PNG
# --------------------------------------------------------------------------- #


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    """一个 PNG chunk：长度（大端 4 字节）+ 类型 + 数据 + CRC32(类型 + 数据)。"""
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def _pixel(pattern: str, x: int, y: int, width: int, height: int) -> tuple[int, int, int]:
    """确定性的假图案——没有随机数，重跑必然字节一致（模 zlib 版本）。"""
    if x < 2 or y < 2 or x >= width - 2 or y >= height - 2:
        return (40, 48, 64)  # 边框
    if pattern == "bars":
        # 「残差随迭代下降」的柱状暗示：柱高随 x 递减。
        bar = (x - 2) // 6
        top = height - 4 - (height - 8) // (bar + 1)
        if (x - 2) % 6 < 4 and y >= top:
            return (32 + 24 * bar, 96 + 12 * bar, 176 - 8 * bar)
        return (246, 247, 250)
    if pattern == "lanes":
        # 「调度泳道」：三条横带。
        lane = (y - 2) * 3 // max(1, height - 4)
        return [(70, 110, 180), (120, 170, 210), (200, 220, 235)][min(lane, 2)]
    return (200, 200, 200)


def build_png(*, width: int, height: int, pattern: str) -> bytes:
    """最小合法 PNG：signature + IHDR + IDAT + IEND。"""
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # 每行的 filter type，0 = None
        for x in range(width):
            raw.extend(_pixel(pattern, x, y, width, height))
    ihdr = struct.pack(
        ">IIBBBBB",
        width,
        height,
        8,  # bit depth
        2,  # color type 2 = truecolor RGB
        0,  # compression = deflate
        0,  # filter method 0
        0,  # 非隔行
    )
    return (
        PNG_MAGIC
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _png_chunk(b"IEND", b"")
    )


def png_fingerprint(data: bytes) -> tuple[bytes, bytes]:
    """PNG 的**结构指纹** = (IHDR 数据, 解压后的像素流)。

    用来做「重跑是否再生出同一张图」的比对：绕开 zlib 版本对压缩字节流的影响。
    """
    if not data.startswith(PNG_MAGIC):
        raise ValueError("不是 PNG：signature 不对")
    pos = len(PNG_MAGIC)
    ihdr = b""
    idat = bytearray()
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        tag = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        (crc,) = struct.unpack(">I", data[pos + 8 + length : pos + 12 + length])
        if crc != (zlib.crc32(tag + body) & 0xFFFFFFFF):
            raise ValueError(f"PNG chunk {tag!r} 的 CRC 不对")
        if tag == b"IHDR":
            ihdr = body
        elif tag == b"IDAT":
            idat.extend(body)
        pos += 12 + length
    if not ihdr:
        raise ValueError("PNG 缺 IHDR")
    return ihdr, zlib.decompress(bytes(idat))


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #


def build_pdf(*, width: int, height: int, rects: list[tuple]) -> bytes:
    """最小合法单页 PDF 1.4：Catalog / Pages / Page / Contents + xref + trailer。

    `rects` 是 `(x, y, w, h, (r, g, b))` 列表，按序画成填充矩形（PDF 的 `re f`）。
    """
    ops: list[bytes] = []
    for x, y, w, h, (r, g, b) in rects:
        ops.append(f"{r:.3f} {g:.3f} {b:.3f} rg".encode("ascii"))
        ops.append(f"{x} {y} {w} {h} re f".encode("ascii"))
    stream = b"\n".join(ops) + b"\n"

    bodies: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}]"
            f" /Resources << >> /Contents 4 0 R >>"
        ).encode("ascii"),
        b"<< /Length "
        + str(len(stream)).encode("ascii")
        + b" >>\nstream\n"
        + stream
        + b"endstream",
    ]

    # 第二行的高位字节注释是 PDF 规范的建议写法：告诉工具链这是二进制文件。
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for number, body in enumerate(bodies, start=1):
        offsets.append(len(out))
        out += str(number).encode("ascii") + b" 0 obj\n" + body + b"\nendobj\n"

    xref_offset = len(out)
    size = len(bodies) + 1
    out += b"xref\n0 " + str(size).encode("ascii") + b"\n"
    out += b"0000000000 65535 f \n"  # 每条 xref 记录恰好 20 字节
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += b"trailer\n<< /Size " + str(size).encode("ascii") + b" /Root 1 0 R >>\n"
    out += b"startxref\n" + str(xref_offset).encode("ascii") + b"\n%%EOF\n"
    return bytes(out)


# --------------------------------------------------------------------------- #
# 驱动
# --------------------------------------------------------------------------- #


def render(kind: str, params: dict) -> bytes:
    if kind == "png":
        return build_png(**params)
    if kind == "pdf":
        return build_pdf(**params)
    raise ValueError(f"未知资产类型：{kind}")


def equivalent(kind: str, produced: bytes, committed: bytes) -> bool:
    """入库文件与重新生成的结果是否等价（PNG 比结构，PDF 比字节）。"""
    if kind == "png":
        return png_fingerprint(produced) == png_fingerprint(committed)
    return produced == committed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="只比对入库文件，不写盘")
    parser.add_argument("--root", type=Path, default=PAPERS, help="papers/ 目录（默认同级）")
    args = parser.parse_args(argv)

    failures = 0
    for relative, (kind, params) in sorted(ASSETS.items()):
        target = args.root / relative
        data = render(kind, params)
        if args.check:
            if not target.exists():
                print(f"缺文件：{relative}")
                failures += 1
            elif not equivalent(kind, data, target.read_bytes()):
                print(f"与重新生成的结果不符：{relative}")
                failures += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        print(f"写出 {relative}（{len(data)} 字节）")
    if args.check and failures == 0:
        print(f"{len(ASSETS)} 个资产与生成器一致")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
