"""DOCX 包的安全校验。

为什么必须在打开文档之前做
--------------------------
`.docx` 是一个 ZIP 包，里面的 XML 会被 lxml 解析。如果直接把不可信文件交给
解析器，攻击者能做的事情远超"解析失败"：

- **zip bomb**：几 KB 的包解压出几十 GB，进程被内存打爆；
- **DTD / 实体扩展**："billion laughs" 让一次解析消耗指数级内存；
- **路径穿越**：条目名带 `..` 或绝对路径，任何"先解压到磁盘"的写法都会写到目录外；
- **伪装格式**：`.docm` 带宏、`.dotx` 是模板、旧 `.doc` 是 OLE 复合文档——
  三者都不是本模块要支持的文本 DOCX；
- **扫描目标 ≠ 解析目标**：python-docx 按 content-type / 关系图找主文档，
  只看固定文件名会漏掉真正的解析对象；
- **外部关系**：`TargetMode="External"` 指向远端，一旦跟随就是一次 SSRF/隐私外泄。

本模块**只在内存里读包，不落盘解压**。校验分四层：

1. 容器层：文件头、EOCD 多卷位、条目数、大小、压缩比、路径、加密位、
   重名、符号链接/设备条目；
2. 部件层：必须有 `[Content_Types].xml` 与 `word/document.xml`，根关系
   （`_rels/.rels`）的 officeDocument 目标必须就是 `word/document.xml`，
   主部件不能是宏/模板变体；声明为 XML 内容类型的部件必须落在扫描集合内；
3. XML 编码层：**只接受 UTF-8**（允许 UTF-8 BOM）。UTF-16/32（含无 BOM
   自动识别形态）、含 NUL、无法按 UTF-8 解码的 XML 一律拒绝——字节级
   `<!DOCTYPE` 扫描在 UTF-16 下会瞎，而解析器仍然会解析；
4. XML 内容层：解码后扫描任何 XML 部件的 `<!DOCTYPE>` / `<!ENTITY>`；
   随后用**安全解析器**（`resolve_entities=False, load_dtd=False,
   no_network=True, huge_tree=False`）解析元数据部件；外部关系只计数、不请求。

所有失败一律抛 `InputError`（CLI 退出码 3），消息必须说清"是什么、为什么、
用户该怎么做"——只写"解析失败"等于让使用者无从下手。
"""

from __future__ import annotations

import codecs
import io
import posixpath
import re
import stat
import struct
import zipfile
import zlib
from dataclasses import dataclass
from typing import Any

from lxml import etree

from docqa.errors import InputError

# ---------------------------------------------------------------- 文件头

ZIP_MAGIC = b"PK\x03\x04"
EMPTY_ZIP_MAGIC = b"PK\x05\x06"
EOCD_MAGIC = b"PK\x05\x06"
# 旧版 Word（.doc）与 OOXML 加密包都用 OLE 复合文档格式。
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
# UTF-16/32 的 BOM。XML 只接受 UTF-8：其它编码会让字节级 DTD 扫描失效，
# 而 lxml 仍会按声明解析，等于绕过第 4 层。
UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")
UTF32_BOMS = (b"\x00\x00\xfe\xff", b"\xff\xfe\x00\x00")
UTF8_BOM = b"\xef\xbb\xbf"

# ---------------------------------------------------------------- 上限

# 条目数。正常 DOCX 几十个条目，5000 已经非常宽松。
MAX_ENTRIES = 5_000
# 单条目解压后上限：`document.xml` 的硬上限是 100 MB，其余部件只可能更小。
MAX_ENTRY_UNCOMPRESSED = 100 * 1024 * 1024
# 整包总解压上限。
MAX_TOTAL_UNCOMPRESSED = 500 * 1024 * 1024
# 元数据部件（[Content_Types].xml / *.rels）的上限。真实文件都远小于 1 MB；
# 8 MB 足够容纳极端但合法的关系表，同时把"用巨大 rels 拖死解析器"挡在外面。
MAX_METADATA_XML_BYTES = 8 * 1024 * 1024
# 压缩比阈值：超过这个比例且解压后够大，按 zip bomb 处理。
# 阈值用 `>=`：恰好等于 1 MiB 的条目同样要查，否则攻击者只要把体积
# 控制在阈值下方 2 字节就能绕过。
SUSPICIOUS_RATIO = 200
SUSPICIOUS_RATIO_MIN_BYTES = 1024 * 1024
# `word/document.xml` 单独再限一次：它是必读的正文部件。
MAX_DOCUMENT_XML_BYTES = 100 * 1024 * 1024
# 解析后的规模上限（由 parser 在遍历后调用 `check_parse_limits`）。
MAX_PARAGRAPHS = 100_000
MAX_TOTAL_CHARS = 20_000_000

# ---------------------------------------------------------------- 部件与类型

CONTENT_TYPES_PART = "[Content_Types].xml"
ROOT_RELS_PART = "_rels/.rels"
DOCUMENT_PART = "word/document.xml"
MAIN_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
)
MACRO_CONTENT_TYPES = frozenset(
    {
        "application/vnd.ms-word.document.macroEnabled.main+xml",
        "application/vnd.ms-word.template.macroEnabledTemplate.main+xml",
    }
)
TEMPLATE_CONTENT_TYPES = frozenset(
    {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml",
    }
)
MACRO_PART = "word/vbaProject.bin"

MACRO_SUFFIXES = (".docm", ".dotm")
TEMPLATE_SUFFIXES = (".dotx", ".dotm")
LEGACY_SUFFIXES = (".doc",)

_CT_NS = "{http://schemas.openxmlformats.org/package/2006/content-types}"
_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_OFFICE_DOCUMENT_TYPE_SUFFIX = "/officeDocument"
_XML_DECL_ENCODING_RE = re.compile(
    r"<\?xml[^>]*?encoding\s*=\s*[\"']([^\"']+)", re.IGNORECASE
)
_CHUNK = 1024 * 1024
_MARKER_OVERLAP = 16

# 读取 ZIP 时可能冒出的所有底层异常。任何一条漏出去都会变成 API 500 / CLI 堆栈，
# 违反"校验失败一律 InputError"的契约（SEC-DOCX-05）。
_ZIP_ERRORS = (
    zipfile.BadZipFile,
    OSError,
    ValueError,
    RuntimeError,
    NotImplementedError,
    EOFError,
    zlib.error,
    struct.error,
    OverflowError,
)


@dataclass(frozen=True)
class DocxSecurity:
    """通过校验的包的基本事实。

    这些数字进 manifest：一次 ingest 到底读进来多大的包、里面有多少外部关系，
    出问题时是排查的第一手证据。
    """

    entries: int
    compressed_bytes: int
    uncompressed_bytes: int
    document_xml_bytes: int
    external_relationships: int
    content_type: str


def is_docx(data: bytes) -> bool:
    """快速判断字节流是不是 DOCX（仅用于上传分派，不替代 `validate_docx`）。

    返回 False 不代表文件一定安全，只是"看起来不是 DOCX"。
    真正的校验必须走 `validate_docx`。这里也设条目上限：不能因为
    "上传分派"就先把一个超大中央目录全部物化成 `ZipInfo`。
    """
    if not data or not data.startswith(ZIP_MAGIC):
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            return len(infos) <= MAX_ENTRIES and any(
                info.filename == DOCUMENT_PART for info in infos
            )
    except _ZIP_ERRORS:
        return False


def validate_docx(data: bytes, *, filename: str | None = None) -> DocxSecurity:
    """校验一个 DOCX 包并返回它的事实描述。

    Args:
        data: 文件字节。**不落盘**，只在内存里读。
        filename: 原始文件名（可选）。用于按扩展名拦截 `.docm` / `.dotx` / 旧 `.doc`——
            内容与扩展名不一致时，拒绝比"猜用户想干什么"更安全。

    Raises:
        InputError: 任何一项校验失败。消息说明原因与可操作的处理方式。
    """
    _check_suffix(filename)
    _check_header(data)
    _check_archive_layout(data)
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except _ZIP_ERRORS as exc:
        raise InputError(
            "DOCX 的 ZIP 结构损坏，无法读取条目表。\n"
            "常见原因是上传被截断或文件被当作文本编辑过；请重新导出后重试。"
        ) from exc
    try:
        with archive:
            return _inspect_archive(archive)
    except InputError:
        raise
    except _ZIP_ERRORS as exc:
        # 兜底：任何底层读取异常（损坏 DEFLATE、坏 CRC、不支持的方法）都必须是
        # 可操作的输入错误，而不是 500/堆栈。
        raise InputError(
            f"读取 DOCX 失败：{type(exc).__name__}: {exc}。\n"
            "文件可能已损坏；请用 Word/WPS 重新另存为 .docx 后重试。"
        ) from exc


# ---------------------------------------------------------------- 容器层


def _check_suffix(filename: str | None) -> None:
    if not filename:
        return
    lowered = filename.lower()
    if lowered.endswith(MACRO_SUFFIXES):
        raise InputError(
            f"不支持的 DOCX 变体：{filename} 是启用宏的文档（.docm/.dotm）。\n"
            "本工具只处理纯文本 .docx，宏不会被执行也不会被保留。\n"
            "请用 Word/WPS 打开后「另存为 → .docx」再上传。"
        )
    if lowered.endswith(TEMPLATE_SUFFIXES):
        raise InputError(
            f"不支持的 DOCX 变体：{filename} 是 Word 模板（.dotx/.dotm）。\n"
            "请用 Word/WPS 打开后「另存为 → .docx」再上传。"
        )
    if lowered.endswith(LEGACY_SUFFIXES):
        raise InputError(
            f"不支持的格式：{filename} 是旧版 Word 文档（.doc）。\n"
            "旧版 .doc 是 OLE 复合文档，不是 ZIP 包，本工具无法解析。\n"
            "请用 Word/WPS「另存为 → .docx」后重试。"
        )


def _check_header(data: bytes) -> None:
    if not data:
        raise InputError("DOCX 文件为空，无法解析。请确认不是上传中断产生的 0 字节文件。")
    if data.startswith(OLE_MAGIC):
        raise InputError(
            "这不是 .docx，而是 OLE 复合文档（旧版 .doc 或加密的 Word 文件）。\n"
            "请用 Word/WPS「另存为 → .docx」，并确认文件未加密。"
        )
    if data.startswith(EMPTY_ZIP_MAGIC):
        raise InputError("这是一个空 ZIP 归档，不是 DOCX。请重新导出文件后重试。")
    if not data.startswith(ZIP_MAGIC):
        raise InputError(
            "不是有效的 DOCX：文件头不是 ZIP（PK\\x03\\x04）。\n"
            "DOCX 本质是 ZIP 包；扩展名被改成 .docx 的其它文件常在这里被发现。"
        )


def _check_archive_layout(data: bytes) -> None:
    """拒绝多卷 ZIP。Python `zipfile` 会忽略 disk 字段，别的工具不会。"""
    offset = _find_eocd(data)
    if offset < 0 or offset + 8 > len(data):
        return
    disk = int.from_bytes(data[offset + 4 : offset + 6], "little")
    central_disk = int.from_bytes(data[offset + 6 : offset + 8], "little")
    # 0xFFFF 是 Zip64 的占位值，不代表多卷；1..0xFFFE 才是真的卷号。
    if 0 < disk < 0xFFFF or 0 < central_disk < 0xFFFF:
        raise InputError(
            "DOCX 是多卷（multi-disk）ZIP，本工具不支持。\n"
            "请用 Word/WPS 重新另存为单个 .docx 文件。"
        )


def _find_eocd(data: bytes) -> int:
    """找到真正的 EOCD（注释长度必须与文件末尾吻合），避免误判注释里的签名。"""
    start = max(0, len(data) - (22 + 0xFFFF))
    position = data.rfind(EOCD_MAGIC, start)
    while position >= 0:
        if position + 22 <= len(data):
            comment_len = int.from_bytes(data[position + 20 : position + 22], "little")
            if position + 22 + comment_len == len(data):
                return position
        position = data.rfind(EOCD_MAGIC, start, position)
    return -1


def _inspect_archive(archive: zipfile.ZipFile) -> DocxSecurity:
    try:
        infos = archive.infolist()
    except _ZIP_ERRORS as exc:
        raise InputError(f"DOCX 的 ZIP 结构损坏，无法读取条目表：{exc}") from exc

    if not infos:
        raise InputError("这是一个空 ZIP 归档，不是 DOCX。请重新导出文件后重试。")
    if len(infos) > MAX_ENTRIES:
        raise InputError(
            f"DOCX 条目数 {len(infos)} 超过上限 {MAX_ENTRIES}，疑似打包异常或恶意构造。\n"
            "请重新导出文件；如果文件确实很大，请联系维护者评估上限。"
        )

    _check_names(infos)
    _check_entries(infos)

    names = {info.filename for info in infos if not info.is_dir()}
    for required in (CONTENT_TYPES_PART, DOCUMENT_PART):
        if required not in names:
            raise InputError(
                f"DOCX 缺少必要部件：{required}。\n"
                "这不是完整的 Word 文档，或包已损坏。请用 Word/WPS 重新另存为 .docx。"
            )
    if MACRO_PART in names:
        raise InputError(
            "DOCX 中包含 VBA 宏部件（word/vbaProject.bin），本工具不处理带宏文档。\n"
            "请另存为纯文本 .docx，或先移除宏。"
        )

    # 顺序是安全属性：**先扫描所有 XML 部件，再解析任何 XML**。
    document_size = _scan_xml_parts(archive, infos)
    overrides, defaults = _content_type_maps(archive)
    _reject_unscanned_xml_parts(infos, overrides, defaults)
    content_type = _main_content_type(overrides)
    _require_office_document_target(archive)
    external = _count_external_relationships(archive, infos)

    return DocxSecurity(
        entries=len(infos),
        compressed_bytes=sum(i.compress_size for i in infos),
        uncompressed_bytes=sum(i.file_size for i in infos),
        document_xml_bytes=document_size,
        external_relationships=external,
        content_type=content_type,
    )


def _check_names(infos: list[zipfile.ZipInfo]) -> None:
    seen: set[str] = set()
    for info in infos:
        name = info.filename
        # `zipfile` 会在读取时把 `filename` 在 NUL 处截断、在 Windows 上把
        # 反斜杠换成 `/`。只看 `filename` 会漏掉原始名里的攻击特征，
        # 所以原始名（`orig_filename`）与归一后的名字都要查。
        original = getattr(info, "orig_filename", "") or ""
        if original and original != name:
            check_entry_path(original)
        if name in seen:
            raise InputError(
                f"DOCX 内存在重复条目：{name}。\n"
                "重复条目会让不同解析器读到不同内容（zip confusion），因此拒绝解析。"
            )
        seen.add(name)
        check_entry_path(name)


def check_entry_path(name: str) -> None:
    """拒绝 NUL/控制字符、绝对路径、`..` 与反斜杠条目名。

    反斜杠这一条在 Windows 上会被 `zipfile` 自己先归一成 `/`，
    所以它实际防的是 POSIX 部署（那里 `os.sep` 是 `/`，反斜杠会原样保留）。
    两个平台都要有明确的拒绝理由，而不是依赖运行时的偶然行为。

    NUL 尤其重要：不同 ZIP 实现会在 NUL 处截断文件名，同一份包可能在
    校验器与解压器眼里是两个不同的文件（zip confusion）。
    """
    prefix = f"DOCX 内条目路径不安全：{name!r}。\n"
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in name):
        raise InputError(prefix + "条目名含 NUL 或控制字符，可能造成文件名截断/混淆。")
    if "\\" in name:
        raise InputError(prefix + "条目名含反斜杠，会绕过基于 '/' 的路径检查。请在 Windows 上用 Word 重新另存。")
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        raise InputError(prefix + "条目名是绝对路径。合法 DOCX 的条目全部是包内相对路径。")
    parts = name.split("/")
    if ".." in parts:
        raise InputError(prefix + "条目名含 '..'，可能写出包外目录（路径穿越）。")


def _check_entries(infos: list[zipfile.ZipInfo]) -> None:
    total = 0
    for info in infos:
        if info.flag_bits & 0x1:
            raise InputError(
                f"DOCX 含加密条目：{info.filename}。\n"
                "加密的 DOCX 无法在不提供口令的情况下读取；本工具不支持口令输入。\n"
                "请在 Word 中移除密码后另存为 .docx。"
            )
        _check_entry_type(info)
        size = info.file_size
        if size > MAX_ENTRY_UNCOMPRESSED:
            raise InputError(
                f"DOCX 条目解压后过大：{info.filename} 声明解压后 {_mb(size)}，"
                f"超过单条目上限 {_mb(MAX_ENTRY_UNCOMPRESSED)}。\n"
                "这通常是 zip bomb 的特征。请确认文件来源后重新导出。"
            )
        if (
            size >= SUSPICIOUS_RATIO_MIN_BYTES
            and info.compress_size > 0
            and size / info.compress_size > SUSPICIOUS_RATIO
        ):
            ratio = size / info.compress_size
            raise InputError(
                f"DOCX 条目压缩比异常：{info.filename} 压缩后 {info.compress_size} 字节，"
                f"解压后 {_mb(size)}（约 {ratio:.0f}:1，阈值 {SUSPICIOUS_RATIO}:1）。\n"
                "这通常是 zip bomb。请重新导出文件或确认来源可信。"
            )
        total += size
        if total > MAX_TOTAL_UNCOMPRESSED:
            raise InputError(
                f"DOCX 总解压体积超过上限 {_mb(MAX_TOTAL_UNCOMPRESSED)}。\n"
                "这通常是 zip bomb。请重新导出文件或确认来源可信。"
            )


def _check_entry_type(info: zipfile.ZipInfo) -> None:
    mode = info.external_attr >> 16
    if not mode:
        return
    if any(
        check(mode)
        for check in (
            stat.S_ISLNK,
            stat.S_ISCHR,
            stat.S_ISBLK,
            stat.S_ISFIFO,
            stat.S_ISSOCK,
        )
    ):
        raise InputError(
            f"DOCX 含符号链接/设备/特殊条目：{info.filename}。\n"
            "合法 DOCX 只有普通文件与目录条目；这类条目在不同解压器下行为不一致。\n"
            "请用 Word/WPS 重新另存为 .docx。"
        )


def _mb(num: int) -> str:
    return f"{num / (1024 * 1024):.1f} MB"


# ---------------------------------------------------------------- XML 编码与扫描


def _scan_xml_parts(archive: zipfile.ZipFile, infos: list[zipfile.ZipInfo]) -> int:
    """扫描所有 XML 部件（按扩展名），返回 `word/document.xml` 的解压大小。

    扫描集合是 `.xml` / `.rels`，包括**没有被任何部件引用**的孤立 rels：
    扫描是廉价的流式检查，不该因为"解析器不读它"就放过。

    扫描上限按类别区分：元数据部件（`[Content_Types].xml` 与所有 `.rels`）
    只有 8 MiB——它们的合法体积在几十 KB 量级，用 100 MiB 上限去扫等于
    允许攻击者先让我们读完 100 MiB 再拒绝。
    """
    document_size = 0
    for info in infos:
        name = info.filename
        if info.is_dir() or not name.lower().endswith((".xml", ".rels")):
            continue
        is_metadata = name == CONTENT_TYPES_PART or name.lower().endswith(".rels")
        if is_metadata:
            limit = MAX_METADATA_XML_BYTES
            if info.file_size > limit:
                raise InputError(
                    f"DOCX 的元数据部件 {name} 解压后 {_mb(info.file_size)}，"
                    f"超过上限 {_mb(limit)}。\n"
                    "Content_Types / 关系表正常只有几十 KB；这通常是构造的放大载荷。\n"
                    "请用 Word/WPS 重新另存为 .docx。"
                )
        elif name == DOCUMENT_PART:
            limit = MAX_DOCUMENT_XML_BYTES
            if info.file_size > limit:
                raise InputError(
                    f"{DOCUMENT_PART} 解压后 {_mb(info.file_size)}，"
                    f"超过上限 {_mb(limit)}。请拆分文档后重试。"
                )
        else:
            limit = MAX_ENTRY_UNCOMPRESSED

        seen = _scan_xml_part(archive, info, limit=limit)
        if name == DOCUMENT_PART:
            document_size = seen
    return document_size


def _scan_xml_part(archive: zipfile.ZipFile, info: zipfile.ZipInfo, *, limit: int) -> int:
    """流式解码并扫描一个 XML 部件，返回实际读取的字节数。

    只接受 UTF-8：UTF-16/32 下 `<!DOCTYPE` 的字节形态完全不同，字节级扫描会
    漏掉，而 lxml 仍会照常解析。与其"尽力识别编码"，不如只允许唯一一种
    合法编码——Word 产出的 DOCX 本来就是 UTF-8。
    """
    name = info.filename
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    seen = 0
    tail = ""
    first = True
    declaration_checked = False
    try:
        with archive.open(info) as handle:
            while True:
                raw = handle.read(_CHUNK)
                if not raw:
                    break
                if first:
                    raw, stripped = _strip_or_reject_encoding(raw, name)
                    first = False
                    seen += stripped
                seen += len(raw)
                if seen > limit:
                    raise InputError(
                        f"DOCX 条目实际解压后超过上限 {_mb(limit)}：{name}。\n"
                        "这通常是 zip bomb 的特征，已中止解析。"
                    )
                try:
                    text = decoder.decode(raw)
                except UnicodeDecodeError as exc:
                    raise InputError(
                        f"DOCX 的 XML 部件不是 UTF-8：{name}。\n"
                        "本工具只接受 UTF-8 编码的 XML（Word 默认导出即如此）；\n"
                        "其它编码会让 DTD/ENTITY 扫描失效。请用 Word/WPS 重新另存为 .docx。"
                    ) from exc
                if "\x00" in text:
                    raise InputError(
                        f"DOCX 的 XML 部件含 NUL 字节：{name}。\n"
                        "这通常是 UTF-16/32 或二进制文件伪装成 XML；已拒绝解析。"
                    )
                if not declaration_checked:
                    declaration_checked = True
                    _check_xml_declaration(text, name)
                window = (tail + text).lower()
                if "<!doctype" in window or "<!entity" in window:
                    raise InputError(
                        f"DOCX 的 XML 部件含 DOCTYPE/ENTITY 声明：{name}。\n"
                        "这会让 XML 解析器展开外部/内部实体（XXE、billion laughs）。\n"
                        "合法 DOCX 不需要 DTD；请用 Word/WPS 重新另存为 .docx。"
                    )
                tail = (tail + text)[-_MARKER_OVERLAP:]
            try:
                decoder.decode(b"", final=True)
            except UnicodeDecodeError as exc:
                raise InputError(
                    f"DOCX 的 XML 部件不是完整的 UTF-8：{name}。请重新导出文件。"
                ) from exc
    except InputError:
        raise
    except _ZIP_ERRORS as exc:
        raise InputError(
            f"读取 DOCX 部件失败：{name}（{type(exc).__name__}: {exc}）。包可能已损坏，请重新导出。"
        ) from exc
    return seen


def _strip_or_reject_encoding(raw: bytes, name: str) -> tuple[bytes, int]:
    """检查开头编码特征；UTF-8 BOM 剥掉，其余返回 (可解码字节, 已剥离字节数)。"""
    if raw.startswith(UTF32_BOMS) or raw.startswith(UTF16_BOMS):
        raise InputError(
            f"DOCX 的 XML 部件使用 UTF-16/UTF-32 编码：{name}。\n"
            "合法 Word 文档使用 UTF-8；其它编码会让 DTD/ENTITY 扫描失效。\n"
            "请用 Word/WPS「另存为 → .docx」重新导出为 UTF-8。"
        )
    if raw.startswith(UTF8_BOM):
        return raw[len(UTF8_BOM) :], len(UTF8_BOM)
    # 无 BOM 的 UTF-16/32：XML 以 `<?xml` 开头，编码后会出现 `<\x00` / `\x00<`。
    if raw[:2] in (b"<\x00", b"\x00<"):
        raise InputError(
            f"DOCX 的 XML 部件看起来是 UTF-16/UTF-32（无 BOM）：{name}。\n"
            "本工具只接受 UTF-8；请用 Word/WPS 重新另存为 .docx。"
        )
    return raw, 0


def _check_xml_declaration(text: str, name: str) -> None:
    match = _XML_DECL_ENCODING_RE.search(text[:512])
    if match is None:
        return
    declared = match.group(1).strip().lower()
    if declared not in {"utf-8", "utf8"}:
        raise InputError(
            f"DOCX 的 XML 声明要求 {declared} 编码：{name}。\n"
            "本工具只接受 UTF-8；请用 Word/WPS 重新另存为 .docx。"
        )


# ---------------------------------------------------------------- 安全解析


def _parse_xml(raw: bytes, name: str) -> Any:
    """用**禁止实体展开**的 lxml 解析器解析不可信 XML。

    不能用 `xml.etree.ElementTree`：expat 会展开内部实体，一个嵌套
    `<!ENTITY>` 就能在解析阶段吃掉几百 MB。即使前面已扫描过 DTD，这里仍用
    安全解析器是纵深防御——扫描与解析的目标必须用同一套规则守住。
    """
    parser = etree.XMLParser(
        resolve_entities=False,
        load_dtd=False,
        no_network=True,
        huge_tree=False,
        recover=False,
    )
    try:
        return etree.fromstring(raw, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise InputError(
            f"DOCX 的 XML 部件损坏：{name}（{exc}）。\n"
            "请用 Word/WPS 重新另存为 .docx。"
        ) from exc


# ---------------------------------------------------------------- 部件层


def _content_type_maps(
    archive: zipfile.ZipFile,
) -> tuple[dict[str, str], dict[str, str]]:
    raw = _read_member(archive, CONTENT_TYPES_PART, limit=MAX_METADATA_XML_BYTES)
    root = _parse_xml(raw, CONTENT_TYPES_PART)
    overrides: dict[str, str] = {}
    defaults: dict[str, str] = {}
    for override in root.findall(f"{_CT_NS}Override"):
        part_name = override.get("PartName") or ""
        content_type = override.get("ContentType") or ""
        if part_name and content_type:
            overrides[_normalise_part_name(part_name).lower()] = content_type
    for default in root.findall(f"{_CT_NS}Default"):
        extension = (default.get("Extension") or "").lower()
        content_type = default.get("ContentType") or ""
        if extension and content_type:
            defaults[extension] = content_type
    return overrides, defaults


def _reject_unscanned_xml_parts(
    infos: list[zipfile.ZipInfo],
    overrides: dict[str, str],
    defaults: dict[str, str],
) -> None:
    """禁止"内容是 XML、文件名却不是 .xml/.rels"的部件。

    python-docx 按 Content-Type 决定用 XML 解析器打开哪个 part，而不是按扩展名。
    只按扩展名扫描，等于允许攻击者把真正的解析目标命名为 `.bin`。
    """
    for info in infos:
        if info.is_dir():
            continue
        name = info.filename
        if name.lower().endswith((".xml", ".rels")):
            continue
        content_type = overrides.get(_normalise_part_name(name).lower())
        if content_type is None:
            extension = posixpath.splitext(name)[1].lstrip(".").lower()
            content_type = defaults.get(extension)
        if content_type and _is_xml_content_type(content_type):
            raise InputError(
                f"DOCX 部件 {name} 声明为 XML 内容（{content_type}），"
                "但扩展名不是 .xml/.rels。\n"
                "这类部件会绕过 DTD/ENTITY 扫描却被解析器打开，因此拒绝解析。\n"
                "请用 Word/WPS 重新另存为 .docx。"
            )


def _is_xml_content_type(content_type: str) -> bool:
    lowered = content_type.strip().lower()
    return (
        lowered.endswith("+xml")
        or lowered in {"application/xml", "text/xml"}
        or lowered.endswith("/xml")
    )


def _normalise_part_name(part_name: str) -> str:
    return part_name.strip().lstrip("/")


def _main_content_type(overrides: dict[str, str]) -> str:
    content_type = overrides.get(DOCUMENT_PART.lower(), "")
    if not content_type:
        raise InputError(
            f"{CONTENT_TYPES_PART} 没有声明 /{DOCUMENT_PART} 的内容类型。\n"
            "这不是结构完整的 Word 文档。请用 Word/WPS 重新另存为 .docx。"
        )
    if content_type in MACRO_CONTENT_TYPES:
        raise InputError(
            "DOCX 的主文档类型是启用宏的文档（macroEnabled）。\n"
            "本工具只处理纯文本 .docx。请移除宏后另存为 .docx。"
        )
    if content_type in TEMPLATE_CONTENT_TYPES:
        raise InputError(
            "DOCX 的主文档类型是 Word 模板（template）。\n"
            "请用 Word/WPS 打开后「另存为 → .docx」再重试。"
        )
    if content_type != MAIN_CONTENT_TYPE:
        raise InputError(
            f"不支持的主文档类型：{content_type}。\n"
            "本工具只处理标准 WordprocessingML 文档（.docx）。"
        )
    return content_type


def _require_office_document_target(archive: zipfile.ZipFile) -> None:
    """根关系必须把 officeDocument 指向 `word/document.xml`。

    python-docx 通过 `_rels/.rels` 找主文档；只检查"包里存在 word/document.xml"
    不能保证被解析的就是它。
    """
    raw = _read_member(archive, ROOT_RELS_PART, limit=MAX_METADATA_XML_BYTES)
    root = _parse_xml(raw, ROOT_RELS_PART)
    target = ""
    for relation in root.findall(f"{_REL_NS}Relationship"):
        relation_type = (relation.get("Type") or "").rstrip("/")
        if relation_type.endswith(_OFFICE_DOCUMENT_TYPE_SUFFIX):
            target = relation.get("Target") or ""
            break
    if not target:
        raise InputError(
            f"DOCX 的根关系缺少 officeDocument（{ROOT_RELS_PART}）。\n"
            "这不是结构完整的 Word 文档。请用 Word/WPS 重新另存为 .docx。"
        )
    normalised = posixpath.normpath(target.lstrip("/"))
    if normalised.lower() != DOCUMENT_PART.lower():
        raise InputError(
            f"DOCX 的主文档目标不是 {DOCUMENT_PART}，而是 {target!r}。\n"
            "本工具只支持标准结构的 .docx；请用 Word/WPS 重新另存为 .docx。"
        )


def _count_external_relationships(
    archive: zipfile.ZipFile, infos: list[zipfile.ZipInfo]
) -> int:
    """统计外部关系数量。**只数，不请求**——任何外部目标都不在解析阶段被访问。"""
    total = 0
    for info in infos:
        if info.is_dir() or not info.filename.lower().endswith(".rels"):
            continue
        raw = _read_member(
            archive, info.filename, info=info, limit=MAX_METADATA_XML_BYTES
        )
        root = _parse_xml(raw, info.filename)
        for relation in root.findall(f"{_REL_NS}Relationship"):
            if (relation.get("TargetMode") or "").lower() == "external":
                total += 1
    return total


def _read_member(
    archive: zipfile.ZipFile,
    name: str,
    *,
    info: zipfile.ZipInfo | None = None,
    limit: int = MAX_ENTRY_UNCOMPRESSED,
) -> bytes:
    target = info
    if target is None:
        try:
            target = archive.getinfo(name)
        except KeyError as exc:
            raise InputError(f"DOCX 缺少部件：{name}。请用 Word/WPS 重新另存为 .docx。") from exc
    try:
        with archive.open(target) as handle:
            data = handle.read(limit + 1)
    except _ZIP_ERRORS as exc:
        raise InputError(
            f"读取 DOCX 部件失败：{name}（{type(exc).__name__}: {exc}）。包可能已损坏。"
        ) from exc
    if len(data) > limit:
        raise InputError(f"DOCX 部件解压后超过上限：{name}。这通常是 zip bomb 的特征。")
    return data


# ---------------------------------------------------------------- 解析后上限


def check_parse_limits(paragraphs: int, total_chars: int) -> None:
    """校验解析后的规模。在遍历完文档后调用——**先限制再建模**。

    这一层防的不是 zip bomb，而是"结构合法但规模异常"的文档：几十万段落、
    上千万字符会让切分与索引把内存吃光，而 ZIP 层看起来完全正常。
    """
    if paragraphs > MAX_PARAGRAPHS:
        raise InputError(
            f"DOCX 段落数 {paragraphs} 超过上限 {MAX_PARAGRAPHS}。\n"
            "请拆分文档后分批处理；如果这是正常业务文档，请联系维护者评估上限。"
        )
    if total_chars > MAX_TOTAL_CHARS:
        raise InputError(
            f"DOCX 正文字符数 {total_chars} 超过上限 {MAX_TOTAL_CHARS}。\n"
            "请拆分文档后分批处理；如果这是正常业务文档，请联系维护者评估上限。"
        )
