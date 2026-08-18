"""fetch 阶段驱动器：把论文源码落进工作目录的 `src/`。

fetch 是唯一写 `src/` 的阶段；`src/` 的含义是 e-print 内容的原样落盘。

论文参数按顺序识别为三种形态：本地源码目录（参数在文件系统里存在且是目录；空串与
单独的 `.`、`..`、`~` 是路径导航写法，不作为目录接受）；arXiv 链接（主机名是 arxiv.org
或其子域，路径前缀 `/abs/`、`/pdf/`、`/html/` 之一，前缀之后的整段剩余路径是编号，
编号可含斜杠，末尾的 `.pdf` 扩展名去掉，查询串与锚点丢弃）；其余输入按 arXiv 编号
处理，合法性由 `workdir.normalize_arxiv_id` 判定。

远程下载走 `https://export.arxiv.org/e-print/<编号>`。该端点不给文件名也不给可靠的
Content-Type，按下载体的头几个字节分流：`%PDF` → `src/main.pdf`；gzip 魔数 → 先按
tar.gz 解包，打不开则按单个 gzip 压缩文件解压（解压结果再判一次 `%PDF`）；裸 tar →
解包；其余原样写 `src/main.tex`。原始下载体先写 `build/e-print.bin`，供解包失败时
排查。不做自动重试，网络失败与空响应体一律记 `download_failed`。

解包安全：逐 tar 成员处理，只放行普通文件与目录，链接（软/硬）、设备文件、FIFO
一律拒绝；路径安全（绝对路径、`..` 穿越）逐成员交给标准库 `tarfile.data_filter`
判定。被拒成员名记入 manifest 的 `rejected`，不中断解包。

收尾判定对远程与本地两种入口共用：`src/` 里没有 `.tex` 但有 PDF → `pdf_only`；
两者都没有 → `empty`；有 `.tex` 时再做 PDF 套壳检测——`.tex` 字符总量低于
`MIN_TEX_CHARS` 视为无实质内容，出现 `\\includepdf` 且总量低于
`MIN_TEX_CHARS_WITH_INCLUDEPDF` 判定为 pdfpages 套壳，两种情形都记 `pdf_only`。

重跑语义：远程入口在已有 manifest 可解析且状态为 ok / pdf_only 时直接跳过，不访问
网络；状态是失败类或 manifest 解析不了则重新执行；`force` 无视已有结论。本地目录
入口不做跳过判定，每次都重新拷贝与判定。从头执行前把 `src/` 整目录删除重建。驱动
器不向调用方抛异常：失败一律转 manifest 状态，异常的类型名与信息记入 `message`。
"""

from __future__ import annotations

import gzip
import hashlib
import io
import shutil
import tarfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .. import __version__, manifests, workdir
from ..artifacts.fetch import FetchKind, FetchManifest, FetchStatus

#: 阶段名，也是 stage manifest 的文件名主干。
STAGE_NAME = "fetch"

#: e-print 下载端点；编号可含斜杠，原样进 URL 路径。
EPRINT_URL_TEMPLATE = "https://export.arxiv.org/e-print/{arxiv_id}"

#: 下载超时秒数。不做自动重试，超时即转 download_failed。
DOWNLOAD_TIMEOUT_SECONDS = 60.0

#: 下载请求的 User-Agent，标明工具版本与仓库地址。
DOWNLOAD_USER_AGENT = f"tongtu/{__version__} (+https://github.com/yhnocoder/tongtu)"

#: 原始下载体在 build/ 下的文件名，解包前写出。
EPRINT_PAYLOAD_FILENAME = "e-print.bin"

#: PDF 文件的头四个字节。
PDF_MAGIC = b"%PDF"

#: gzip 流的头两个字节。
GZIP_MAGIC = b"\x1f\x8b"

#: PDF 套壳检测阈值：全部 .tex 字符总量低于此值视为无实质内容。
MIN_TEX_CHARS = 1000

#: PDF 套壳检测阈值：.tex 里出现 \includepdf 时要求的更高字符总量，低于即判定为 pdfpages 套壳。
MIN_TEX_CHARS_WITH_INCLUDEPDF = 5000

#: 本地目录拷贝时跳过的目录与文件名（版本控制目录与系统缓存文件）。
COPY_SKIP_NAMES = frozenset({".git", ".svn", ".hg", "__pycache__", ".DS_Store"})

#: 不作为本地目录接受的字面参数：它们是路径导航写法，接受会把当前目录、上级目录
#: 或主目录整棵当成论文源码；这类参数落到编号判定后被拒绝。空串也在其中——
#: `Path("")` 归一成 `.`，不排除的话空参数与写 `.` 的后果相同。
PATH_NAVIGATION_LITERALS = frozenset({"", ".", "..", "~"})


class PaperArgumentError(ValueError):
    """arXiv 链接解析失败（主机不对、路径无编号前缀、前缀之后没有编号）。"""


@dataclass(frozen=True)
class PaperInput:
    """论文参数的识别结果：arXiv 编号（含从链接解析出的）或本地源码目录，两个字段恰有其一非 None。"""

    arxiv_id: str | None = None
    source_dir: Path | None = None


@dataclass(frozen=True)
class FetchResult:
    """驱动器的返回值：manifest、工作目录与是否命中跳过。"""

    manifest: FetchManifest
    workdir: workdir.Workdir
    skipped: bool


# ------------------------------------------------------------------ 输入识别


def parse_arxiv_url(url: str) -> str:
    """从 arXiv 链接解析出编号。

    主机名须是 arxiv.org 或其子域；路径前缀接受 `/abs/`、`/pdf/`、`/html/` 三种，
    取前缀之后的整段剩余路径作为编号（编号可含斜杠）；末尾的 `.pdf` 扩展名去掉；
    查询串与锚点丢弃。解析失败抛 PaperArgumentError。
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    if host != "arxiv.org" and not host.endswith(".arxiv.org"):
        raise PaperArgumentError(f"不是 arxiv.org 链接：{url}")
    for prefix in ("/abs/", "/pdf/", "/html/"):
        if parts.path.startswith(prefix):
            arxiv_id = parts.path[len(prefix) :].rstrip("/").removesuffix(".pdf")
            if not arxiv_id:
                raise PaperArgumentError(f"链接的路径前缀之后没有编号：{url}")
            return arxiv_id
    raise PaperArgumentError(f"链接路径不含 /abs/、/pdf/、/html/ 前缀：{url}")


def _as_source_directory(paper: str) -> Path | None:
    """论文参数指向的本地源码目录；不是目录、是路径导航写法、或用户名展不开时返回 None。

    `Path.expanduser()` 在 `~用户名` 里的用户不存在时抛 RuntimeError。那个参数不是目录，
    按编号继续判定，由 `normalize_arxiv_id` 拒绝并转成用法错误，不让异常穿出到调用方。
    """
    if paper in PATH_NAVIGATION_LITERALS:
        return None
    try:
        path = Path(paper).expanduser()
    except RuntimeError:
        return None
    return path if path.is_dir() else None


def parse_paper_argument(paper: str) -> PaperInput:
    """按顺序识别论文参数：本地源码目录 → arXiv 链接 → arXiv 编号。

    目录之外的两种形态都归结为编号：链接解析失败抛 PaperArgumentError，编号不合法
    由 `workdir.normalize_arxiv_id` 抛 WorkdirError。
    """
    source_dir = _as_source_directory(paper)
    if source_dir is not None:
        return PaperInput(source_dir=source_dir)
    if paper.startswith(("http://", "https://")):
        arxiv_id = parse_arxiv_url(paper)
    else:
        arxiv_id = paper
    workdir.normalize_arxiv_id(arxiv_id)  # 只做合法性判定，目录名转换在 workdir.resolve 里
    return PaperInput(arxiv_id=arxiv_id)


# ------------------------------------------------------------------ 阶段驱动器


def fetch_remote(
    arxiv_id: str,
    workdir_path: Path | None = None,
    *,
    force: bool = False,
    download: Callable[[str], bytes] | None = None,
) -> FetchResult:
    """远程入口：下载 e-print、按魔数分流解包、收尾判定，写出 manifest。

    已有 manifest 可解析且状态为 ok / pdf_only 时直接跳过，不访问网络，返回已存
    结论；`force` 无视已有结论。`download` 替换默认的 httpx 下载实现，供测试与
    离线复跑注入。
    """
    paper_workdir = workdir.Workdir(workdir.resolve(arxiv_id, workdir_path))
    if not force:
        existing = _load_reusable_manifest(paper_workdir.manifest_path(STAGE_NAME))
        if existing is not None:
            return FetchResult(manifest=existing, workdir=paper_workdir, skipped=True)

    url = EPRINT_URL_TEMPLATE.format(arxiv_id=arxiv_id)
    _reset_src(paper_workdir)
    payload_path = paper_workdir.build / EPRINT_PAYLOAD_FILENAME
    payload_path.unlink(missing_ok=True)  # 上次执行的下载体不残留，避免排查时读到旧字节

    try:
        payload = (download or _download_eprint)(url)
    except Exception as error:  # 网络失败类型多样，统一转状态
        manifest = FetchManifest(
            status=FetchStatus.DOWNLOAD_FAILED, source=arxiv_id, url=url, message=manifests.describe_error(error)
        )
        return _write_result(paper_workdir, manifest)
    if not payload:
        manifest = FetchManifest(
            status=FetchStatus.DOWNLOAD_FAILED, source=arxiv_id, url=url, message="下载成功但响应体为空。"
        )
        return _write_result(paper_workdir, manifest)

    payload_path.write_bytes(payload)
    payload_sha256 = hashlib.sha256(payload).hexdigest()
    try:
        kind, rejected, warnings = _unpack_payload(payload, paper_workdir.src)
        manifest = _manifest_from_src(
            paper_workdir,
            source=arxiv_id,
            kind=kind,
            url=url,
            payload_sha256=payload_sha256,
            payload_bytes=len(payload),
            rejected=rejected,
            warnings=warnings,
        )
    except Exception as error:  # 解包失败类型多样，统一转状态
        manifest = FetchManifest(
            status=FetchStatus.UNPACK_FAILED,
            source=arxiv_id,
            url=url,
            payload_sha256=payload_sha256,
            payload_bytes=len(payload),
            message=manifests.describe_error(error),
        )
    return _write_result(paper_workdir, manifest)


def fetch_local(source_dir: Path, workdir_path: Path | None = None) -> FetchResult:
    """本地目录入口：把源目录拷贝进 `src/` 后做收尾判定，写出 manifest。

    不做跳过判定，每次都重新拷贝与判定。工作目录名默认取源目录的 basename，
    `workdir_path` 覆盖。源目录恰好是本工作目录的 `src/` 自身时不清空不拷贝，
    直接收尾判定；源目录在 `src/` 内部更深层、或与工作目录是同一目录时拒绝执行。
    """
    source = Path(source_dir).expanduser().absolute()
    paper_workdir = workdir.Workdir(workdir.resolve(source.name, workdir_path))
    paper_workdir.create()
    if not source.is_dir():
        manifest = FetchManifest(
            status=FetchStatus.SOURCE_MISSING,
            source=str(source),
            kind="local",
            message=f"源目录不存在或不是目录：{source}",
        )
        return _write_result(paper_workdir, manifest)

    source_real = source.resolve()
    src_real = paper_workdir.src.resolve()
    if source_real == src_real:
        # 源目录就是本工作目录的 src/：内容已在位，不清空不拷贝。
        manifest = _manifest_from_src(paper_workdir, source=str(source), kind="local")
    elif source_real == paper_workdir.path.resolve():
        manifest = FetchManifest(
            status=FetchStatus.UNPACK_FAILED,
            source=str(source),
            kind="local",
            message="源目录与工作目录是同一目录，无法把它拷贝进自身的 src/。",
        )
    elif source_real.is_relative_to(src_real):
        manifest = FetchManifest(
            status=FetchStatus.UNPACK_FAILED,
            source=str(source),
            kind="local",
            message="源目录在本工作目录的 src/ 内部；执行前要整目录清空 src/，会把源目录一并删除，故拒绝执行。",
        )
    else:
        _reset_src(paper_workdir)
        try:
            shutil.copytree(
                source, paper_workdir.src, ignore=_copy_ignore(paper_workdir.path.resolve()), dirs_exist_ok=True
            )
            manifest = _manifest_from_src(paper_workdir, source=str(source), kind="local")
        except Exception as error:  # 拷贝失败类型多样，统一转状态
            manifest = FetchManifest(
                status=FetchStatus.UNPACK_FAILED,
                source=str(source),
                kind="local",
                message=manifests.describe_error(error),
            )
    return _write_result(paper_workdir, manifest)


# ------------------------------------------------------------------ 下载与解包


def _download_eprint(url: str) -> bytes:
    """默认下载实现：httpx，60 秒超时，跟随重定向，非 2xx 抛异常。"""
    response = httpx.get(
        url,
        headers={"User-Agent": DOWNLOAD_USER_AGENT},
        timeout=DOWNLOAD_TIMEOUT_SECONDS,
        follow_redirects=True,
    )
    response.raise_for_status()
    return response.content


def _unpack_payload(payload: bytes, src: Path) -> tuple[FetchKind, list[str], list[str]]:
    """按魔数分流下载体并写进 src/，返回（kind、被拒成员名、warnings）。

    分流顺序：`%PDF` → src/main.pdf；gzip 魔数 → 先按 tar.gz 解包，打不开则按单个
    gzip 压缩文件解压（解压结果再判一次 `%PDF`）；裸 tar → 解包；其余原样写
    src/main.tex。失败抛异常，由调用方转 unpack_failed。
    """
    if payload[:4] == PDF_MAGIC:
        (src / "main.pdf").write_bytes(payload)
        return "pdf", [], []
    if payload[:2] == GZIP_MAGIC:
        try:
            archive = tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz")
        except tarfile.ReadError:
            archive = None
        if archive is not None:
            with archive:
                rejected, warnings = _extract_tar_members(archive, src)
            return "tar.gz", rejected, warnings
        decompressed = gzip.decompress(payload)
        if decompressed[:4] == PDF_MAGIC:
            (src / "main.pdf").write_bytes(decompressed)
        else:
            (src / "main.tex").write_bytes(decompressed)
        return "gz", [], []
    try:
        archive = tarfile.open(fileobj=io.BytesIO(payload), mode="r:")
    except tarfile.ReadError:
        archive = None
    if archive is not None:
        with archive:
            rejected, warnings = _extract_tar_members(archive, src)
        return "tar", rejected, warnings
    (src / "main.tex").write_bytes(payload)
    return "tex", [], []


def _extract_tar_members(archive: tarfile.TarFile, dest: Path) -> tuple[list[str], list[str]]:
    """逐成员解包，返回（被拒成员名、warnings）。

    只放行普通文件与目录；路径安全逐成员交给标准库 `tarfile.data_filter` 判定
    （`extract` 的 filter="data"）。被拒成员记名后继续，不中断解包。
    """
    rejected: list[str] = []
    for member in archive:
        if not (member.isreg() or member.isdir()):
            rejected.append(member.name)
            continue
        try:
            archive.extract(member, path=dest, filter="data")
        except tarfile.FilterError:
            rejected.append(member.name)
    warnings: list[str] = []
    if rejected:
        warnings.append(
            f"解包拒绝了 {len(rejected)} 个 tar 成员（不是普通文件或目录，或路径不安全），名单见 rejected 字段"
        )
    return rejected, warnings


def _copy_ignore(workdir_real: Path) -> Callable[[str, list[str]], set[str]]:
    """生成 copytree 的 ignore 回调：跳过版本控制目录与系统缓存文件；工作目录嵌在
    源目录内时跳过工作目录自身，不把它拷进 src/。"""

    def ignore(parent: str, names: list[str]) -> set[str]:
        skipped = {name for name in names if name in COPY_SKIP_NAMES}
        for name in names:
            if name not in skipped and (Path(parent) / name).resolve() == workdir_real:
                skipped.add(name)
        return skipped

    return ignore


# ------------------------------------------------------------------ 收尾判定与落盘


def _manifest_from_src(
    paper_workdir: workdir.Workdir,
    *,
    source: str,
    kind: FetchKind,
    url: str = "",
    payload_sha256: str = "",
    payload_bytes: int = 0,
    rejected: list[str] | None = None,
    warnings: list[str] | None = None,
) -> FetchManifest:
    """收尾判定：遍历 src/ 全树（跳过符号链接），算逐文件 sha256 与 .tex 统计，得出状态。"""
    src = paper_workdir.src
    files: dict[str, str] = {}
    tex_files: list[str] = []
    tex_chars = 0
    has_includepdf = False
    paths = sorted(src.rglob("*"), key=lambda path: path.relative_to(src).as_posix())
    for path in paths:
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(src).as_posix()
        with path.open("rb") as stream:
            files[relative] = hashlib.file_digest(stream, "sha256").hexdigest()
        if path.suffix.lower() == ".tex":
            text = path.read_text(encoding="utf-8", errors="ignore")
            tex_files.append(relative)
            tex_chars += len(text)
            has_includepdf = has_includepdf or "\\includepdf" in text
    status, message = _verdict(files, tex_files, tex_chars, has_includepdf)
    return FetchManifest(
        status=status,
        source=source,
        kind=kind,
        url=url,
        payload_sha256=payload_sha256,
        payload_bytes=payload_bytes,
        files=files,
        tex_files=tex_files,
        tex_chars=tex_chars,
        rejected=list(rejected or []),
        warnings=list(warnings or []),
        message=message,
    )


def _verdict(
    files: dict[str, str], tex_files: list[str], tex_chars: int, has_includepdf: bool
) -> tuple[FetchStatus, str]:
    """按判定顺序得出状态：没有 .tex 先看有没有 PDF；有 .tex 再做 PDF 套壳检测。"""
    if not tex_files:
        if any(name.lower().endswith(".pdf") for name in files):
            return FetchStatus.PDF_ONLY, "src/ 里没有 .tex 文件、只有 PDF，走 degraded path。"
        return FetchStatus.EMPTY, "src/ 里既没有 .tex 文件也没有 PDF。"
    if tex_chars < MIN_TEX_CHARS:
        return FetchStatus.PDF_ONLY, (
            f".tex 字符总量 {tex_chars} 低于 {MIN_TEX_CHARS}，没有实质文本内容，按 PDF 套壳处理，走 degraded path。"
        )
    if has_includepdf and tex_chars < MIN_TEX_CHARS_WITH_INCLUDEPDF:
        return FetchStatus.PDF_ONLY, (
            f".tex 里出现 \\includepdf 且字符总量 {tex_chars} 低于 {MIN_TEX_CHARS_WITH_INCLUDEPDF}，"
            "判定为 pdfpages 套壳，走 degraded path。"
        )
    return FetchStatus.OK, ""


def _load_reusable_manifest(path: Path) -> FetchManifest | None:
    """读已有 manifest；可解析且状态为 ok / pdf_only（上次已有结论）时返回它，否则返回 None（重新执行）。"""
    manifest = manifests.load_manifest(path, FetchManifest)
    if manifest is None:
        return None
    if manifest.status in (FetchStatus.OK, FetchStatus.PDF_ONLY):
        return manifest
    return None


def _write_result(paper_workdir: workdir.Workdir, manifest: FetchManifest) -> FetchResult:
    """写出 manifest 并组装返回值；除跳过外的每次执行（含失败）都经此处落盘。"""
    manifests.write_manifest(paper_workdir.manifest_path(STAGE_NAME), manifest)
    return FetchResult(manifest=manifest, workdir=paper_workdir, skipped=False)


def _reset_src(paper_workdir: workdir.Workdir) -> None:
    """把 src/ 整目录删除后重建四区，避免与上次执行的残留混杂。"""
    shutil.rmtree(paper_workdir.src, ignore_errors=True)
    paper_workdir.create()
