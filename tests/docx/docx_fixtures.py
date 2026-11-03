"""DOCX 测试夹具：现场生成文件，不依赖仓库里的样例。

**为什么不是 `tests/docx/conftest.py`**：顶层 `tests/conftest.py` 与子目录
conftest 会以同一个模块名 `conftest` 被 pytest 导入，而 `tests/test_chunker.py`
等文件里有 `from conftest import ...`，两者相撞会让收集阶段直接 ImportError。
这里只放工厂类；每个测试模块自己写一行 `docx_factory` fixture，
避免"导入的夹具名与参数名同名"触发 F811。

两条纪律：

1. **全部现场生成**。合法 DOCX 用 python-docx 写进 `tmp_path`；非法包（zip bomb、
   DTD、路径穿越、加密条目）真实生成器根本产不出来，只能手工拼 ZIP 字节。
2. **不触网、不读 `.env`、不打印密钥**。DOCX 解析完全离线，测试也不该有网络依赖。

`DocxFactory` 一个对象集齐所有构造工具，避免测试文件之间互相 import 工具函数。
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

from docx.oxml.ns import nsdecls

from docx import Document

MAIN_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
)
MACRO_CONTENT_TYPE = "application/vnd.ms-word.document.macroEnabled.main+xml"
TEMPLATE_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml"
)
RELS_CONTENT_TYPE = "application/vnd.openxmlformats-package.relationships+xml"
XML_CONTENT_TYPE = "application/xml"

Builder = Callable[[Document], None]

_DEFAULT_BODY = "<w:p><w:r><w:t>示例正文</w:t></w:r></w:p>"


class DocxFactory:
    """在 `tmp_path` 下造 DOCX 或任意 ZIP 包的工厂。"""

    main_content_type = MAIN_CONTENT_TYPE
    macro_content_type = MACRO_CONTENT_TYPE
    template_content_type = TEMPLATE_CONTENT_TYPE
    rels_content_type = RELS_CONTENT_TYPE

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self._counter = 0

    # ------------------------------------------------------------ 合法 DOCX

    def build(self, builder: Builder | None = None) -> Document:
        doc = Document()
        if builder is not None:
            builder(doc)
        return doc

    def bytes(self, builder: Builder | None = None) -> bytes:
        buf = io.BytesIO()
        self.build(builder).save(buf)
        return buf.getvalue()

    def path(self, builder: Builder | None = None, *, name: str = "sample.docx") -> Path:
        out = self.tmp_path / name
        out.write_bytes(self.bytes(builder))
        return out

    # ------------------------------------------------------------ 手工 ZIP

    def package(self, parts: Mapping[str, bytes], *, compress: bool = True) -> bytes:
        """按给定条目名/内容拼一个 ZIP（条目名原样保留）。"""
        return self.package_named(parts.items(), compress=compress)

    def package_named(
        self, entries: Iterable[tuple[str, bytes]], *, compress: bool = True
    ) -> bytes:
        """条目名**不经任何规范化**的版本。

        `zipfile.ZipInfo.__init__` 会把反斜杠换成斜杠，而路径穿越正是要保留
        原始形态才能测到，所以构造完再改 `filename`。
        """
        method = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", method) as zf:
            for name, data in entries:
                info = zipfile.ZipInfo("placeholder.xml")
                info.filename = name
                info.compress_type = method
                zf.writestr(info, data)
        return buf.getvalue()

    def rewrite(self, data: bytes, changes: Mapping[str, bytes | None]) -> bytes:
        """替换/新增/删除 ZIP 条目，其余原样复制。`None` 表示删除。"""
        with zipfile.ZipFile(io.BytesIO(data)) as src:
            existing = [i.filename for i in src.infolist()]
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
                for info in src.infolist():
                    if info.filename in changes:
                        new = changes[info.filename]
                        if new is not None:
                            out.writestr(info.filename, new)
                    else:
                        out.writestr(info.filename, src.read(info.filename))
                for name, new in changes.items():
                    if new is not None and name not in existing:
                        out.writestr(name, new)
        return buf.getvalue()

    def set_zip_flags(self, data: bytes, flag: int = 0x1) -> bytes:
        """把所有条目的通用标志位或上 `flag`（0x1 = 加密）。

        直接改字节，因为 `zipfile` 会重置调用方设置的 `flag_bits`。
        压缩数据里可能碰巧出现签名字节，本函数只用于"必须被拒绝"的用例，
        不追求生成的包可被正常读取。
        """
        buf = bytearray(data)
        for sig, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
            idx = 0
            while True:
                idx = buf.find(sig, idx)
                if idx < 0:
                    break
                end = idx + offset + 2
                current = int.from_bytes(buf[idx + offset : end], "little")
                buf[idx + offset : end] = (current | flag).to_bytes(2, "little")
                idx += 4
        return bytes(buf)

    def corrupt_member(self, data: bytes, name: str, *, offset: int = 4) -> bytes:
        """翻转某条目 DEFLATE 数据里的一个字节（中央目录/CRC 不变）。

        `zipfile` 在读取时才会发现 DEFLATE 损坏并抛 `zlib.error`——这是
        "异常绝不能逃出校验层"的回归载荷。
        """
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            info = archive.getinfo(name)
        start = (
            info.header_offset
            + 30
            + len(info.filename.encode("utf-8"))
            + len(info.extra)
        )
        buf = bytearray(data)
        position = start + offset
        if position >= len(buf):
            raise ValueError("压缩数据偏移越界")
        buf[position] ^= 0xFF
        return bytes(buf)

    def set_eocd_disks(self, data: bytes, *, disk: int = 1, central_disk: int = 1) -> bytes:
        """把 EOCD 的卷号字段改成非零，伪造多卷 ZIP。"""
        buf = bytearray(data)
        start = max(0, len(buf) - (22 + 0xFFFF))
        position = buf.rfind(b"PK\x05\x06", start)
        if position < 0:
            raise ValueError("找不到 EOCD")
        comment_len = int.from_bytes(buf[position + 20 : position + 22], "little")
        if position + 22 + comment_len != len(buf):
            raise ValueError("EOCD 不是最后一条记录")
        buf[position + 4 : position + 6] = disk.to_bytes(2, "little")
        buf[position + 6 : position + 8] = central_disk.to_bytes(2, "little")
        return bytes(buf)

    def symlink_entry(self, data: bytes, name: str = "word/link") -> bytes:
        """追加一个 external_attr 为 Unix symlink 的条目。"""
        buf = io.BytesIO(data)
        with zipfile.ZipFile(buf, "a") as archive:
            info = zipfile.ZipInfo(name)
            info.external_attr = 0o120777 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, b"/etc/passwd")
        return buf.getvalue()

    # ------------------------------------------------------------ 最小合法包

    def content_types(
        self,
        *,
        main_content_type: str = MAIN_CONTENT_TYPE,
        overrides: Mapping[str, str] | None = None,
        defaults: Mapping[str, str] | None = None,
    ) -> bytes:
        default_map = {
            "rels": RELS_CONTENT_TYPE,
            "xml": XML_CONTENT_TYPE,
        }
        default_map.update(defaults or {})
        override_map = {"word/document.xml": main_content_type}
        override_map.update(overrides or {})
        lines = [
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
        ]
        lines.extend(
            f'<Default Extension="{ext}" ContentType="{ctype}"/>'
            for ext, ctype in default_map.items()
        )
        lines.extend(
            f'<Override PartName="/{name.lstrip("/")}" ContentType="{ctype}"/>'
            for name, ctype in override_map.items()
        )
        lines.append("</Types>")
        return "".join(lines).encode("utf-8")

    def document_xml(self, body_inner: str | None = None) -> bytes:
        inner = _DEFAULT_BODY if body_inner is None else body_inner
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f"<w:document {nsdecls('w')}><w:body>{inner}<w:sectPr/></w:body></w:document>"
        ).encode("utf-8")

    def minimal_parts(
        self,
        *,
        main_content_type: str = MAIN_CONTENT_TYPE,
        body: str | None = None,
    ) -> dict[str, bytes]:
        return {
            "[Content_Types].xml": self.content_types(main_content_type=main_content_type),
            "_rels/.rels": (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
                'Target="word/document.xml"/>'
                "</Relationships>"
            ).encode("utf-8"),
            "word/document.xml": self.document_xml(body),
        }

    def minimal(
        self, *, main_content_type: str = MAIN_CONTENT_TYPE, body: str | None = None
    ) -> bytes:
        return self.package(self.minimal_parts(main_content_type=main_content_type, body=body))

    def rels_xml(self, entries: Iterable[tuple[str, str, str, str]]) -> bytes:
        """构造 `.rels`：每个 entry = (id, type, target, target_mode|"")。"""
        lines = [
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">',
        ]
        for rid, rtype, target, mode in entries:
            mode_attr = f' TargetMode="{mode}"' if mode else ""
            lines.append(
                f'<Relationship Id="{rid}" Type="{rtype}" Target="{target}"{mode_attr}/>'
            )
        lines.append("</Relationships>")
        return "".join(lines).encode("utf-8")

