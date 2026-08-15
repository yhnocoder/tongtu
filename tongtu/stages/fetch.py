"""fetch 阶段：e-print 下载解包 + PDF-only / pdfpages 套壳检测（架构 §3 fetch 行）。

出口判据是机械的：源码树落 `src/`；**PDF-only 不抛栈**——它不是错误而是一条分支，返回
`FetchResult(status="pdf_only")` 由顶层标记降级路线（`fallback/`，零期只标记不实现，
见 PHASE0 §2.2 与 §5）。其余失败（下载失败、解包失败、源不可用）同样走结构化状态，
调用方按 `status` 分流，不需要 catch 一堆异常类型。

## 分流（迁自 v2 `scripts/fetch.py`，按魔数而非扩展名）

arXiv 的 `e-print` 端点不给文件名也不给可靠的 Content-Type，只能看头几个字节：

* `%PDF`            → PDF-only，直接进降级路线；
* `\\x1f\\x8b`（gzip） → 先当 tar.gz 解，`ReadError` 再退回「单个 .tex.gz」；
* 其余             → 裸 tar（少见）或裸 .tex，落 `src/main.tex`。

## 解包安全

tar 成员逐个手工落盘而非 `extractall`：Python 3.12 的 `filter="data"` 语义（拒绝绝对
路径、`..` 穿越、符号链接与设备文件）在 3.11 上不保证可用（`filter=` 参数是 3.11.4 才
回填的），这里自己实现同等约束，被拒的成员进 `FetchResult.rejected` 供 report 记录。

## PDF 套壳

有些「源码」只是 pdfpages 套壳（v2 遇到过 1412.6980），等同 PDF-only。v2 在展平后按
`flat.tex` 体量判定；这里前移到解包后按 `src/` 下全部 `.tex` 的**字符总量**判定——判据
等价（展平就是把这些文件拼起来），但能在 flatten 之前就分流，省一次 latexpand 调用。
"""

from __future__ import annotations

import gzip
import io
import shutil
import tarfile
import urllib.request
import zlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from .. import __version__
from ..workdir import Workdir

__all__ = [
    "EPRINT_URL",
    "USER_AGENT",
    "RAW_NAME",
    "STATUSES",
    "Fetcher",
    "FetchResult",
    "eprint_url",
    "urllib_fetcher",
    "fetch",
    "unpack",
    "ingest_local",
    "detect_pdf_shell",
]

#: e-print 端点。旧式 id（`hep-th/9901001`）带斜杠，原样进路径。
EPRINT_URL = "https://export.arxiv.org/e-print/{arxiv_id}"

USER_AGENT = f"tongtu/{__version__} (+https://github.com/yhnocoder/tongtu)"

#: 默认下载超时（秒）。
DEFAULT_TIMEOUT = 60.0

#: 原始下载体落在 `build/` 而非 `src/`——它是可丢弃的中间物，`src/` 只放源码树。
RAW_NAME = "e-print.bin"

#: 单文件形态（裸 .tex 或 .tex.gz）落盘时的文件名。
SINGLE_NAME = "main.tex"

# 状态常量。ok 之外都不是异常，调用方按状态分流。
OK = "ok"
PDF_ONLY = "pdf_only"  # → fallback/ 降级路线
EMPTY = "empty"  # 解包成功但树里没有 .tex 也没有 .pdf
DOWNLOAD_FAILED = "download_failed"
UNPACK_FAILED = "unpack_failed"
SOURCE_MISSING = "source_missing"  # 本地目录不存在 / arXiv id 非法

STATUSES: tuple[str, ...] = (
    OK,
    PDF_ONLY,
    EMPTY,
    DOWNLOAD_FAILED,
    UNPACK_FAILED,
    SOURCE_MISSING,
)

#: 套壳判据（迁自 v2）：全部 .tex 字符总量低于此值 = 没有实质内容。
SHELL_MIN_CHARS = 1000

#: 有 `\includepdf` 且字符总量低于此值 = pdfpages 套壳。
SHELL_INCLUDEPDF_CHARS = 5000

#: `tongtu run <dir>` 拷贝本地源码目录时跳过的名字。
IGNORED_LOCAL: frozenset[str] = frozenset({".git", ".svn", ".hg", "__pycache__", ".DS_Store"})

#: 下载原语：URL → 字节。默认实现走 urllib；测试注入假实现，不打网络。
Fetcher = Callable[[str], bytes]


@dataclass(frozen=True)
class FetchResult:
    """fetch 的结构化结果。`status` 是唯一的分流依据，异常一律转成状态。"""

    status: str
    src: Path
    kind: str = ""  # tar.gz / tar / gz / tex / pdf / local
    files: tuple[str, ...] = ()  # src/ 下的相对路径（posix 形式，已排序）
    tex_files: tuple[str, ...] = ()
    tex_chars: int = 0
    payload_bytes: int = 0
    raw_path: Path | None = None
    rejected: tuple[str, ...] = ()  # 被安全策略拦下的 tar 成员
    warnings: tuple[str, ...] = ()
    message: str = ""
    url: str = ""

    @property
    def ok(self) -> bool:
        return self.status == OK

    @property
    def fallback(self) -> bool:
        """真——顶层应把本篇标记到降级路线（`fallback/`）而不是当失败。"""
        return self.status == PDF_ONLY

    def to_json(self) -> dict:
        """给 manifest / report 用的扁平记录（路径一律相对工作目录无关的字符串）。"""
        data: dict = {
            "status": self.status,
            "kind": self.kind,
            "files": list(self.files),
            "tex_files": list(self.tex_files),
            "tex_chars": self.tex_chars,
            "payload_bytes": self.payload_bytes,
        }
        if self.url:
            data["url"] = self.url
        if self.rejected:
            data["rejected"] = list(self.rejected)
        if self.warnings:
            data["warnings"] = list(self.warnings)
        if self.message:
            data["message"] = self.message
        return data


# --------------------------------------------------------------------------- 下载


def eprint_url(arxiv_id: str) -> str:
    """arXiv id → e-print URL。id 非法时抛 `ValueError`（调用方一般先经 workdir 规范化）。"""
    raw = (arxiv_id or "").strip()
    if not raw or any(ch.isspace() for ch in raw) or ".." in raw or raw.startswith("/"):
        raise ValueError(f"非法 arXiv id：{arxiv_id!r}")
    return EPRINT_URL.format(arxiv_id=quote(raw, safe="/.-_"))


def urllib_fetcher(url: str, *, timeout: float = DEFAULT_TIMEOUT) -> bytes:
    """默认下载实现：零第三方依赖，带 UA（arXiv 对无 UA 请求不友好）。"""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return response.read()


def fetch(
    arxiv_id: str,
    workdir: Workdir,
    *,
    fetcher: Fetcher | None = None,
    url: str | None = None,
) -> FetchResult:
    """下载 e-print 并解包到 `workdir.src`。

    * `fetcher`：`Callable[[str], bytes]`，默认 `urllib_fetcher`。注入点在这里——
      测试与离线复跑不需要网络。
    * `url`：覆盖端点（镜像站 / 本地文件服务），默认由 `arxiv_id` 生成。

    任何失败都返回结构化状态，不抛栈；`status == "pdf_only"` 表示应走降级路线。
    """
    try:
        target = url or eprint_url(arxiv_id)
    except ValueError as exc:
        return FetchResult(status=SOURCE_MISSING, src=workdir.src, message=str(exc))

    workdir.create()
    get = fetcher or urllib_fetcher
    try:
        payload = get(target)
    except Exception as exc:  # 网络错误五花八门（URLError/OSError/超时…），统一转状态
        return FetchResult(
            status=DOWNLOAD_FAILED,
            src=workdir.src,
            url=target,
            message=f"下载失败（{type(exc).__name__}）：{exc}",
        )
    if not payload:
        return FetchResult(status=DOWNLOAD_FAILED, src=workdir.src, url=target, message="下载得到空响应")

    raw = workdir.build / RAW_NAME
    raw.write_bytes(payload)
    return unpack(payload, workdir, url=target, raw_path=raw)


# --------------------------------------------------------------------------- 解包


def unpack(
    payload: bytes,
    workdir: Workdir,
    *,
    url: str = "",
    raw_path: Path | None = None,
) -> FetchResult:
    """按魔数分流并解包到 `workdir.src`（下载与解包分离，便于离线复跑与测试）。"""
    src = workdir.src
    src.mkdir(parents=True, exist_ok=True)
    common = dict(payload_bytes=len(payload), raw_path=raw_path, url=url)

    if payload[:4] == b"%PDF":
        return _finalize(src, kind="pdf", forced_pdf=True, **common)

    rejected: tuple[str, ...] = ()
    if payload[:2] == b"\x1f\x8b":
        try:
            rejected = _extract_tar(payload, src)
            kind = "tar.gz"
        except tarfile.ReadError:
            # 不是 tar：那就是单个 .tex.gz（arXiv 对单文件投稿的常见形态）
            try:
                data = gzip.decompress(payload)
            except (OSError, EOFError, zlib.error) as exc:
                return FetchResult(status=UNPACK_FAILED, src=src, kind="gz", message=f"gzip 解压失败：{exc}", **common)
            if data[:4] == b"%PDF":
                return _finalize(src, kind="gz", forced_pdf=True, **common)
            (src / SINGLE_NAME).write_bytes(data)
            kind = "gz"
        except (tarfile.TarError, OSError) as exc:
            return FetchResult(status=UNPACK_FAILED, src=src, kind="tar.gz", message=f"解包失败：{exc}", **common)
    elif tarfile.is_tarfile(io.BytesIO(payload)):
        try:
            rejected = _extract_tar(payload, src)
        except (tarfile.TarError, OSError) as exc:
            return FetchResult(status=UNPACK_FAILED, src=src, kind="tar", message=f"解包失败：{exc}", **common)
        kind = "tar"
    else:
        (src / SINGLE_NAME).write_bytes(payload)
        kind = "tex"

    return _finalize(src, kind=kind, rejected=rejected, **common)


def _extract_tar(payload: bytes, dest: Path) -> tuple[str, ...]:
    """把 tar 成员逐个安全落盘，返回被拒成员名。

    只放行普通文件与目录；绝对路径、`..` 穿越、符号 / 硬链接、设备文件一律拒绝——
    链接是 `filter="data"` 也要拦的逃逸手段（软链指到 `/etc` 再往里写就出去了）。
    """
    rejected: list[str] = []
    root = dest.resolve()
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as archive:
        for member in archive:
            target = _safe_target(root, member.name)
            if target is None:
                rejected.append(member.name)
                continue
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                rejected.append(member.name)  # 链接 / 设备 / FIFO
                continue
            stream = archive.extractfile(member)
            if stream is None:
                rejected.append(member.name)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "wb") as handle:
                shutil.copyfileobj(stream, handle)
    return tuple(rejected)


def _safe_target(root: Path, name: str) -> Path | None:
    """成员名 → 落盘路径；越界返回 None。`root` 必须已 `resolve()`。"""
    if not name:
        return None
    pure = PurePosixPath(name.replace("\\", "/"))
    if pure.is_absolute():
        return None
    parts = [part for part in pure.parts if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return None
    target = root.joinpath(*parts)
    # parents 里可能已有别的东西（不会有符号链接——链接成员一律不落盘），仍做一次兜底
    try:
        resolved = Path(target).resolve()
    except OSError:
        return None
    if resolved != root and not resolved.is_relative_to(root):
        return None
    return target


# ----------------------------------------------------------------------- 本地目录


def ingest_local(
    directory: str | Path,
    workdir: Workdir,
    *,
    ignore: Iterable[str] = IGNORED_LOCAL,
) -> FetchResult:
    """`tongtu run <dir>`：把本地源码目录拷进 `src/`，其余判定与下载路径完全一致。

    拷贝而非软链或原地使用：`src/` 的契约是「e-print 原始解包，只读不改」，工作目录
    必须自足（可打包、可在别的机器续跑），也不能让流水线写回用户的目录。
    """
    source = Path(directory).expanduser()
    if not source.is_dir():
        return FetchResult(
            status=SOURCE_MISSING,
            src=workdir.src,
            kind="local",
            message=f"本地源码目录不存在：{source}",
        )

    workdir.create()
    src = workdir.src
    skipped = frozenset(ignore)
    if source.resolve() == src.resolve():
        # 已经就地在 src/ 里（重跑同一工作目录），不做无谓的自拷贝
        return _finalize(src, kind="local")

    workdir_root = workdir.path.resolve()

    def _ignore(dirpath: str, names: list[str]) -> set[str]:
        drop = {name for name in names if name in skipped}
        for name in names:
            # 工作目录嵌在源码目录里（`tongtu run .` + 默认 workdir）时不要自吞
            if Path(dirpath, name).resolve() == workdir_root:
                drop.add(name)
        return drop

    try:
        shutil.copytree(source, src, dirs_exist_ok=True, ignore=_ignore)
    except (shutil.Error, OSError) as exc:
        return FetchResult(status=UNPACK_FAILED, src=src, kind="local", message=f"拷贝本地源码失败：{exc}")
    return _finalize(src, kind="local")


# ----------------------------------------------------------------------- 结果判定


def detect_pdf_shell(src: Path, tex_files: Iterable[str]) -> tuple[str | None, int]:
    """套壳检测，返回 (原因或 None, .tex 字符总量)。判据迁自 v2（见模块文档）。"""
    total = 0
    includepdf = False
    for name in tex_files:
        text = (src / name).read_text(encoding="utf-8", errors="ignore")
        total += len(text)
        if "\\includepdf" in text:
            includepdf = True
    if total < SHELL_MIN_CHARS:
        return f"源码 .tex 总量仅 {total} 字符，无实质内容", total
    if includepdf and total < SHELL_INCLUDEPDF_CHARS:
        return f"源码是 pdfpages 套壳（\\includepdf，共 {total} 字符）", total
    return None, total


def _list_files(root: Path) -> tuple[str, ...]:
    return tuple(
        sorted(
            path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file() and not path.is_symlink()
        )
    )


def _finalize(
    src: Path,
    *,
    kind: str,
    forced_pdf: bool = False,
    rejected: tuple[str, ...] = (),
    payload_bytes: int = 0,
    raw_path: Path | None = None,
    url: str = "",
) -> FetchResult:
    """列文件、判 PDF-only / 套壳 / 空树，组装结果。"""
    files = _list_files(src)
    tex_files = tuple(name for name in files if name.lower().endswith(".tex"))
    warnings: list[str] = []
    if rejected:
        warnings.append(f"解包时拒绝了 {len(rejected)} 个不安全成员：{list(rejected[:5])}")

    common = dict(
        src=src,
        kind=kind,
        files=files,
        tex_files=tex_files,
        payload_bytes=payload_bytes,
        raw_path=raw_path,
        rejected=rejected,
        url=url,
    )

    if forced_pdf:
        return FetchResult(
            status=PDF_ONLY,
            message="e-print 是 PDF 而非 LaTeX 源码（PDF-only），走降级路线",
            warnings=tuple(warnings),
            **common,
        )

    if not tex_files:
        has_pdf = any(name.lower().endswith(".pdf") for name in files)
        if has_pdf:
            return FetchResult(
                status=PDF_ONLY,
                message="源码树里没有 .tex，只有 PDF，等同 PDF-only，走降级路线",
                warnings=tuple(warnings),
                **common,
            )
        return FetchResult(
            status=EMPTY,
            message="源码树里没有 .tex 文件" + ("（且解包全部成员被拒）" if rejected else ""),
            warnings=tuple(warnings),
            **common,
        )

    reason, tex_chars = detect_pdf_shell(src, tex_files)
    if reason is not None:
        return FetchResult(
            status=PDF_ONLY,
            tex_chars=tex_chars,
            message=f"{reason}，等同 PDF-only，走降级路线",
            warnings=tuple(warnings),
            **common,
        )

    return FetchResult(status=OK, tex_chars=tex_chars, warnings=tuple(warnings), **common)
