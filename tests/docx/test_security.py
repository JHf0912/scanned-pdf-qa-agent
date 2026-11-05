"""DOCX 包安全校验的测试。

这里每一个"拒绝"都是一条真实的攻击面：zip bomb、DTD 实体扩展、路径穿越、
加密条目、宏/模板变体、伪装成 .docx 的旧格式。断言分两层：

1. 异常类型必须是 `InputError`（CLI 退出码 3，而不是 1）；
2. 消息必须**可操作**——说清是什么、为什么拒绝、用户能做什么。
   只断言"抛了异常"会放过一条"解析失败"的废消息。
"""

from __future__ import annotations

import tracemalloc

import pytest
from docx_fixtures import DocxFactory

from docqa.docx import security
from docqa.docx.security import DocxSecurity, check_parse_limits, is_docx, validate_docx
from docqa.errors import InputError

OFFICE_DOCUMENT_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
)


@pytest.fixture
def docx_factory(tmp_path):
    """现场生成 DOCX 的工厂（见 `docx_fixtures.py`）。"""
    return DocxFactory(tmp_path)


# ---------------------------------------------------------------- 合法输入


def test_valid_docx_reports_package_stats(docx_factory):
    data = docx_factory.bytes()
    report = validate_docx(data)
    assert isinstance(report, DocxSecurity)
    assert report.entries > 0
    assert report.uncompressed_bytes > report.compressed_bytes
    assert report.document_xml_bytes > 0
    assert report.external_relationships == 0
    assert report.content_type == docx_factory.main_content_type


def test_minimal_package_is_accepted(docx_factory):
    data = docx_factory.minimal()
    report = validate_docx(data)
    assert report.entries == 3
    assert report.document_xml_bytes > 0


def test_is_docx_accepts_real_docx(docx_factory):
    assert is_docx(docx_factory.bytes()) is True


def test_is_docx_accepts_minimal_package(docx_factory):
    assert is_docx(docx_factory.minimal()) is True


def test_is_docx_rejects_random_bytes():
    assert is_docx(b"not a zip at all") is False


def test_is_docx_rejects_plain_zip_without_document_part(docx_factory):
    data = docx_factory.package({"hello.txt": b"hi"})
    assert is_docx(data) is False


# ---------------------------------------------------------------- 文件头与容器


def test_rejects_old_doc_ole_header():
    data = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512
    with pytest.raises(InputError) as exc:
        validate_docx(data, filename="合同.doc")
    message = str(exc.value)
    assert "旧版" in message
    assert ".docx" in message  # 给出可操作路径：另存为


def test_rejects_non_zip_bytes():
    with pytest.raises(InputError) as exc:
        validate_docx(b"plain text pretending to be docx")
    assert "ZIP" in str(exc.value)


def test_rejects_empty_file():
    with pytest.raises(InputError) as exc:
        validate_docx(b"")
    assert "空" in str(exc.value)


def test_rejects_empty_zip(docx_factory):
    data = docx_factory.package({})
    with pytest.raises(InputError) as exc:
        validate_docx(data)
    assert "空" in str(exc.value)


def test_rejects_corrupt_zip_with_magic():
    with pytest.raises(InputError) as exc:
        validate_docx(b"PK\x03\x04" + b"\x00" * 64)
    assert "ZIP" in str(exc.value)


def test_rejects_package_missing_content_types(docx_factory):
    data = docx_factory.package({"word/document.xml": docx_factory.document_xml()})
    with pytest.raises(InputError) as exc:
        validate_docx(data)
    assert "[Content_Types].xml" in str(exc.value)


def test_rejects_package_missing_document_part(docx_factory):
    data = docx_factory.package({"[Content_Types].xml": docx_factory.content_types()})
    with pytest.raises(InputError) as exc:
        validate_docx(data)
    assert "word/document.xml" in str(exc.value)


def test_rejects_duplicate_entries(docx_factory):
    document = docx_factory.document_xml()
    data = docx_factory.package_named(
        [
            ("[Content_Types].xml", docx_factory.content_types()),
            ("word/document.xml", document),
            ("word/document.xml", document),
        ]
    )
    with pytest.raises(InputError) as exc:
        validate_docx(data)
    assert "重复" in str(exc.value)


# ---------------------------------------------------------------- 变体拒绝


def test_rejects_macro_enabled_content_type(docx_factory):
    data = docx_factory.minimal(main_content_type=docx_factory.macro_content_type)
    with pytest.raises(InputError) as exc:
        validate_docx(data)
    assert "宏" in str(exc.value)


def test_rejects_template_content_type(docx_factory):
    data = docx_factory.minimal(main_content_type=docx_factory.template_content_type)
    with pytest.raises(InputError) as exc:
        validate_docx(data)
    assert "模板" in str(exc.value)


def test_rejects_macro_part_even_with_docx_content_type(docx_factory):
    parts = docx_factory.minimal_parts()
    parts["word/vbaProject.bin"] = b"\x00" * 32
    data = docx_factory.package(parts)
    with pytest.raises(InputError) as exc:
        validate_docx(data)
    assert "宏" in str(exc.value)


@pytest.mark.parametrize("name", ["合同.docm", "合同.DOTM"])
def test_rejects_macro_extension(docx_factory, name):
    with pytest.raises(InputError) as exc:
        validate_docx(docx_factory.bytes(), filename=name)
    assert "宏" in str(exc.value)


@pytest.mark.parametrize("name", ["模板.dotx", "模板.DOTX"])
def test_rejects_template_extension(docx_factory, name):
    with pytest.raises(InputError) as exc:
        validate_docx(docx_factory.bytes(), filename=name)
    assert "模板" in str(exc.value)


@pytest.mark.parametrize("name", ["旧合同.doc", "旧合同.DOC"])
def test_rejects_legacy_extension(docx_factory, name):
    with pytest.raises(InputError) as exc:
        validate_docx(docx_factory.bytes(), filename=name)
    assert "旧版" in str(exc.value)
    assert ".docx" in str(exc.value)


# ---------------------------------------------------------------- 资源上限


def test_rejects_too_many_entries(docx_factory, monkeypatch):
    monkeypatch.setattr(security, "MAX_ENTRIES", 2)
    with pytest.raises(InputError) as exc:
        validate_docx(docx_factory.minimal())
    assert "条目" in str(exc.value)


def test_rejects_oversized_entry(docx_factory, monkeypatch):
    monkeypatch.setattr(security, "MAX_ENTRY_UNCOMPRESSED", 64)
    with pytest.raises(InputError) as exc:
        validate_docx(docx_factory.minimal())
    assert "解压" in str(exc.value)


def test_rejects_oversized_total(docx_factory, monkeypatch):
    monkeypatch.setattr(security, "MAX_TOTAL_UNCOMPRESSED", 128)
    monkeypatch.setattr(security, "MAX_ENTRY_UNCOMPRESSED", 1024 * 1024)
    with pytest.raises(InputError) as exc:
        validate_docx(docx_factory.minimal())
    assert "解压" in str(exc.value)


def test_rejects_compression_ratio_bomb(docx_factory):
    parts = docx_factory.minimal_parts()
    # 11 MB 的零字节：单条目与总量都远低于上限，只有压缩比暴露它是 bomb。
    parts["word/media/bomb.bin"] = b"\x00" * (11 * 1024 * 1024)
    data = docx_factory.package(parts)
    with pytest.raises(InputError) as exc:
        validate_docx(data)
    assert "压缩比" in str(exc.value)


# ---------------------------------------------------------------- 路径与加密


def test_rejects_absolute_entry_path(docx_factory):
    data = docx_factory.package_named(
        [
            ("[Content_Types].xml", docx_factory.content_types()),
            ("word/document.xml", docx_factory.document_xml()),
            ("/etc/passwd", b"x"),
        ]
    )
    with pytest.raises(InputError) as exc:
        validate_docx(data)
    assert "路径" in str(exc.value)


def test_rejects_parent_traversal_entry(docx_factory):
    data = docx_factory.package_named(
        [
            ("[Content_Types].xml", docx_factory.content_types()),
            ("word/document.xml", docx_factory.document_xml()),
            ("word/../../evil.xml", b"x"),
        ]
    )
    with pytest.raises(InputError) as exc:
        validate_docx(data)
    assert "路径" in str(exc.value)


def test_rejects_backslash_entry():
    # `zipfile` 在 Windows 上会把条目名里的反斜杠归一成 `/`，所以这条检查
    # 用函数级用例守住（POSIX 部署下条目名里的反斜杠会原样保留）。
    from docqa.docx.security import check_entry_path

    with pytest.raises(InputError) as exc:
        check_entry_path("word\\evil.xml")
    assert "路径" in str(exc.value)


def test_rejects_encrypted_entry(docx_factory):
    data = docx_factory.set_zip_flags(docx_factory.minimal())
    with pytest.raises(InputError) as exc:
        validate_docx(data)
    assert "加密" in str(exc.value)


# ---------------------------------------------------------------- DTD / 实体


@pytest.mark.parametrize(
    "doctype",
    [
        '<!DOCTYPE w:document [<!ENTITY xxe "boom">]>',
        '<!DOCTYPE w:document SYSTEM "http://example.invalid/evil.dtd">',
    ],
)
def test_rejects_dtd_and_entity_in_document(docx_factory, doctype):
    data = docx_factory.rewrite(
        docx_factory.minimal(),
        {"word/document.xml": (doctype + docx_factory.document_xml().decode("utf-8")).encode("utf-8")},
    )
    with pytest.raises(InputError) as exc:
        validate_docx(data)
    message = str(exc.value)
    assert "DOCTYPE" in message or "ENTITY" in message or "DTD" in message


def test_rejects_dtd_in_any_xml_part(docx_factory):
    parts = docx_factory.minimal_parts()
    parts["word/styles.xml"] = (
        b'<?xml version="1.0"?>'
        b'<!DOCTYPE x [<!ENTITY a "b">]>'
        b'<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>'
    )
    data = docx_factory.package(parts)
    with pytest.raises(InputError) as exc:
        validate_docx(data)
    message = str(exc.value)
    assert "DOCTYPE" in message or "ENTITY" in message or "DTD" in message


def test_content_types_dtd_is_rejected_before_parsing(docx_factory):
    # 回归：`[Content_Types].xml` 自己也是 XML。若先解析它再扫描，
    # DTD/ENTITY 已经进了解析器，扫描只是事后检查。
    body = docx_factory.document_xml()
    parts = {
        "[Content_Types].xml": (
            b'<?xml version="1.0"?>'
            b'<!DOCTYPE Types [<!ENTITY x "boom">]>'
            b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            b'<Override PartName="/word/document.xml" ContentType="'
            + docx_factory.main_content_type.encode()
            + b'"/></Types>'
        ),
        "word/document.xml": body,
    }
    with pytest.raises(InputError) as exc:
        validate_docx(docx_factory.package(parts))
    message = str(exc.value)
    assert "DOCTYPE" in message or "ENTITY" in message or "DTD" in message
    assert "[Content_Types].xml" in message


def test_utf16_document_xml_is_rejected(docx_factory):
    # 字节级扫描认不出 UTF-16 里的 `<!DOCTYPE`，但 lxml 会照常解析。
    xml = (
        '<?xml version="1.0" encoding="UTF-16"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>正文</w:t></w:r></w:p></w:body></w:document>"
    )
    data = docx_factory.rewrite(
        docx_factory.minimal(), {"word/document.xml": xml.encode("utf-16")}
    )
    with pytest.raises(InputError) as exc:
        validate_docx(data)
    assert "UTF-16" in str(exc.value)


def test_rejects_oversized_document_xml(docx_factory, monkeypatch):
    monkeypatch.setattr(security, "MAX_DOCUMENT_XML_BYTES", 64)
    with pytest.raises(InputError) as exc:
        validate_docx(docx_factory.minimal())
    assert "document.xml" in str(exc.value)


# ---------------------------------------------------------------- 外部关系


def test_external_relationships_are_counted_not_fetched(docx_factory):
    rels_type = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    rels = docx_factory.rels_xml(
        [
            ("rId1", f"{rels_type}/styles", "styles.xml", ""),
            ("rId2", f"{rels_type}/hyperlink", "https://example.invalid/page", "External"),
        ]
    )
    data = docx_factory.rewrite(docx_factory.bytes(), {"word/_rels/document.xml.rels": rels})
    report = validate_docx(data)
    assert report.external_relationships == 1


# ---------------------------------------------------------------- 解析后上限


def test_parse_limits_accept_values_at_the_boundary():
    check_parse_limits(security.MAX_PARAGRAPHS, security.MAX_TOTAL_CHARS)


def test_parse_limits_reject_too_many_paragraphs():
    with pytest.raises(InputError) as exc:
        check_parse_limits(security.MAX_PARAGRAPHS + 1, 0)
    assert "段落" in str(exc.value)


def test_parse_limits_reject_too_many_chars():
    with pytest.raises(InputError) as exc:
        check_parse_limits(0, security.MAX_TOTAL_CHARS + 1)
    assert "字符" in str(exc.value)


def test_rejects_directory_entries_with_traversal(docx_factory):
    data = docx_factory.package_named(
        [
            ("[Content_Types].xml", docx_factory.content_types()),
            ("word/document.xml", docx_factory.document_xml()),
            ("word/../", b""),
        ]
    )
    with pytest.raises(InputError) as exc:
        validate_docx(data)
    assert "路径" in str(exc.value)


def test_bad_zip_is_input_error_not_crash(docx_factory):
    data = docx_factory.package({"word/document.xml": b"<x/>"})
    truncated = data[:-40]
    with pytest.raises(InputError):
        validate_docx(truncated)


def test_zip_with_only_directories_is_rejected(docx_factory):
    # 只有目录条目的 ZIP 不是 DOCX：缺少内容类型与正文部件。
    payload = docx_factory.package_named([("word/", b""), ("docs/", b"")])
    with pytest.raises(InputError):
        validate_docx(payload)


def test_zipfile_errors_never_escape(docx_factory):
    # 断言异常类型边界：`zipfile.BadZipFile` 绝不能直接冒到 CLI 层。
    with pytest.raises(InputError):
        validate_docx(b"PK\x03\x04broken")


# ================================================================ 安全审计回归
#
# 对应 docs/reviews/2026-10-29-docx-security-audit.md（SEC-DOCX-01/02/05/07/11）。
# 每个用例都构造真实载荷；只断言异常类型会放过"拒绝理由错误"的实现。


# ---------------------------------------------------------------- SEC-DOCX-01


def _utf16_bomb(payload_size: int = 32, entities: int = 4) -> bytes:
    """无 BOM 的 UTF-16LE XML，含 DOCTYPE + 嵌套 ENTITY。"""
    literal = "A" * payload_size
    entity_lines = f'<!ENTITY a0 "{literal}">'
    for index in range(1, entities):
        entity_lines += f'<!ENTITY a{index} "{("&a" + str(index - 1) + ";") * 100}">'
    xml = (
        '<?xml version="1.0" encoding="UTF-16"?>'
        f"<!DOCTYPE Relationships [{entity_lines}]>"
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"<unused>&a{entities - 1};</unused>"
        "</Relationships>"
    )
    return xml.encode("utf-16-le")  # 关键：没有 BOM


def test_utf16_without_bom_orphan_rels_is_rejected_before_parsing(
    docx_factory, monkeypatch
):
    parts = docx_factory.minimal_parts()
    parts["word/_rels/orphan.xml.rels"] = _utf16_bomb()
    data = docx_factory.package(parts, compress=False)

    parsed: list[object] = []

    def _explode(*args, **kwargs):
        parsed.append(args)
        raise AssertionError("XML 在扫描前被送进了解析器")

    monkeypatch.setattr(security, "_parse_xml", _explode)
    with pytest.raises(InputError) as exc:
        validate_docx(data)
    assert "UTF-16" in str(exc.value)
    assert parsed == []


def test_utf16_without_bom_content_types_is_rejected(docx_factory):
    parts = docx_factory.minimal_parts()
    parts["[Content_Types].xml"] = _utf16_bomb()
    data = docx_factory.package(parts, compress=False)
    with pytest.raises(InputError) as exc:
        validate_docx(data)
    assert "UTF-16" in str(exc.value)


def test_utf16_bomb_does_not_expand_entities(docx_factory):
    # 若实体被解析器展开，1,000,000 字符的字面量 × 嵌套链会到 100 MB 量级。
    parts = docx_factory.minimal_parts()
    parts["word/_rels/orphan.xml.rels"] = _utf16_bomb(payload_size=1_000_000, entities=3)
    data = docx_factory.package(parts, compress=False)
    tracemalloc.start()
    try:
        with pytest.raises(InputError):
            validate_docx(data)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 64 * 1024 * 1024, f"峰值 {peak / 1024 / 1024:.1f} MB，疑似实体展开"


def test_utf8_dtd_in_orphan_rels_is_rejected_before_parsing(docx_factory, monkeypatch):
    parts = docx_factory.minimal_parts()
    parts["word/_rels/orphan.xml.rels"] = (
        b'<?xml version="1.0"?>'
        b'<!DOCTYPE Relationships [<!ENTITY a "b">]>'
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'
    )
    data = docx_factory.package(parts, compress=False)
    parsed: list[object] = []

    def _explode(*args, **kwargs):
        parsed.append(args)
        raise AssertionError("XML 在扫描前被送进了解析器")

    monkeypatch.setattr(security, "_parse_xml", _explode)
    with pytest.raises(InputError):
        validate_docx(data)
    assert parsed == []


def test_non_utf8_xml_is_rejected(docx_factory):
    parts = docx_factory.minimal_parts()
    parts["word/document.xml"] = (
        b'<?xml version="1.0" encoding="GBK"?>'
        b'<w:document xmlns:w="x"><w:body/></w:document>'
        + "中文".encode("gbk")
    )
    data = docx_factory.package(parts, compress=False)
    with pytest.raises(InputError) as exc:
        validate_docx(data)
    message = str(exc.value)
    assert "UTF-8" in message or "GBK" in message


# ---------------------------------------------------------------- SEC-DOCX-02


def test_xml_content_type_with_non_xml_extension_is_rejected(docx_factory):
    parts = docx_factory.minimal_parts()
    parts["word/document.bin"] = b"<w:document/>"
    parts["[Content_Types].xml"] = docx_factory.content_types(
        overrides={"word/document.bin": docx_factory.main_content_type}
    )
    with pytest.raises(InputError) as exc:
        validate_docx(docx_factory.package(parts))
    message = str(exc.value)
    assert "document.bin" in message
    assert "XML" in message or "扩展名" in message


def test_styles_bin_with_xml_content_type_is_rejected(docx_factory):
    parts = docx_factory.minimal_parts()
    parts["word/styles.bin"] = b"<w:styles/>"
    parts["[Content_Types].xml"] = docx_factory.content_types(
        overrides={"word/styles.bin": "application/xml"}
    )
    with pytest.raises(InputError) as exc:
        validate_docx(docx_factory.package(parts))
    assert "styles.bin" in str(exc.value)


def test_office_document_target_must_be_document_xml(docx_factory):
    parts = docx_factory.minimal_parts()
    parts["word/document.bin"] = b"BEFORE"
    parts["_rels/.rels"] = docx_factory.rels_xml(
        [("rId1", OFFICE_DOCUMENT_REL, "word/document.bin", "")]
    )
    with pytest.raises(InputError) as exc:
        validate_docx(docx_factory.package(parts))
    assert "主文档目标" in str(exc.value)


# ---------------------------------------------------------------- SEC-DOCX-05


def test_corrupt_deflate_is_input_error_not_crash(docx_factory):
    data = docx_factory.corrupt_member(docx_factory.bytes(), "word/document.xml")
    # 中央目录完好，所以 `is_docx` 仍会返回 True——真正读取时才炸。
    assert is_docx(data) is True
    with pytest.raises(InputError) as exc:
        validate_docx(data)
    assert "DOCX" in str(exc.value)


def test_corrupt_deflate_never_leaks_low_level_errors(docx_factory):
    data = docx_factory.corrupt_member(docx_factory.bytes(), "word/document.xml")
    try:
        validate_docx(data)
    except InputError:
        pass
    except Exception as exc:  # noqa: BLE001 - 测试就是要抓住任何非 InputError
        pytest.fail(f"底层异常逃出校验层：{type(exc).__name__}: {exc}")


def test_truncated_archive_is_input_error(docx_factory):
    data = docx_factory.bytes()
    with pytest.raises(InputError):
        validate_docx(data[: len(data) // 2])


# ---------------------------------------------------------------- SEC-DOCX-07


def test_ratio_threshold_at_exactly_one_mib_is_rejected(docx_factory):
    parts = docx_factory.minimal_parts()
    parts["word/media/bomb.bin"] = b"\x00" * (1024 * 1024)
    with pytest.raises(InputError) as exc:
        validate_docx(docx_factory.package(parts))
    assert "压缩比" in str(exc.value)


def test_small_entry_below_the_ratio_floor_is_allowed(docx_factory):
    # 比例检查有最小体积门限：小文件的高压缩比是正常现象。
    parts = docx_factory.minimal_parts()
    parts["word/media/small.bin"] = b"\x00" * (1024 * 1024 - 1)
    report = validate_docx(docx_factory.package(parts))
    assert report.entries >= 4


def test_multidisk_zip_is_rejected(docx_factory):
    data = docx_factory.set_eocd_disks(docx_factory.minimal())
    with pytest.raises(InputError) as exc:
        validate_docx(data)
    assert "多卷" in str(exc.value)


def test_is_docx_enforces_entry_limit(docx_factory, monkeypatch):
    monkeypatch.setattr(security, "MAX_ENTRIES", 2)
    assert is_docx(docx_factory.minimal()) is False


# ---------------------------------------------------------------- SEC-DOCX-11


def test_symlink_entry_is_rejected(docx_factory):
    data = docx_factory.symlink_entry(docx_factory.minimal())
    with pytest.raises(InputError) as exc:
        validate_docx(data)
    assert "符号链接" in str(exc.value)


def test_nul_entry_path_is_rejected():
    # `zipfile` 读取时会在 NUL 处截断文件名，端到端用例看不到原始字节；
    # 这里直接守住校验函数，防止换实现/换平台后截断差异变成 zip confusion。
    from docqa.docx.security import check_entry_path

    with pytest.raises(InputError) as exc:
        check_entry_path("evil\x00.xml")
    message = str(exc.value)
    assert "NUL" in message or "控制字符" in message


def test_nul_entry_name_is_rejected_end_to_end(docx_factory):
    # RECHECK-03：`ZipInfo.filename` 被截断成 'evil'，但 orig_filename
    # 仍是 'evil\x00.xml'；只看 filename 会放行。
    parts = docx_factory.minimal_parts()
    data = docx_factory.package_named(
        list(parts.items()) + [("evil" + chr(0) + ".xml", b"x")]
    )
    with pytest.raises(InputError) as exc:
        validate_docx(data)
    message = str(exc.value)
    assert "NUL" in message or "控制字符" in message


def test_backslash_entry_is_rejected_end_to_end(docx_factory):
    # Windows 上 `filename` 会被归一成 '../evil.xml'，`orig_filename` 仍保留反斜杠。
    parts = docx_factory.minimal_parts()
    data = docx_factory.package_named(
        list(parts.items()) + [(".." + chr(92) + "evil.xml", b"x")]
    )
    with pytest.raises(InputError) as exc:
        validate_docx(data)
    assert "路径" in str(exc.value)


def test_oversized_metadata_is_rejected_before_scanning(docx_factory, monkeypatch):
    # RECHECK-04：元数据部件必须先按 8 MiB 预检，不能先完整扫描再拒绝。
    parts = docx_factory.minimal_parts()
    parts["word/_rels/orphan.xml.rels"] = b"x" * (9 * 1024 * 1024)
    data = docx_factory.package(parts, compress=False)

    scanned: list[str] = []
    real_scan = security._scan_xml_part

    def _spy(archive, info, *, limit):
        scanned.append(info.filename)
        return real_scan(archive, info, limit=limit)

    monkeypatch.setattr(security, "_scan_xml_part", _spy)
    with pytest.raises(InputError) as exc:
        validate_docx(data)
    assert "orphan.xml.rels" not in scanned
    assert "元数据" in str(exc.value)


def test_large_but_allowed_metadata_is_scanned_within_budget(docx_factory):
    # 7 MiB 合法 rels（大注释撑大）应通过，且峰值受 8 MiB 上限约束。
    padding = b"x" * (7 * 1024 * 1024)
    rels = (
        b'<?xml version="1.0"?>'
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b"<!--" + padding + b"-->"
        b"</Relationships>"
    )
    parts = docx_factory.minimal_parts()
    parts["word/_rels/orphan.xml.rels"] = rels
    data = docx_factory.package(parts, compress=False)

    tracemalloc.start()
    try:
        report = validate_docx(data)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert report.entries >= 4
    assert peak < 96 * 1024 * 1024, f"峰值 {peak / 1024 / 1024:.1f} MB"
