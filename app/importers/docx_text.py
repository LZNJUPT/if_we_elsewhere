# -*- coding: utf-8 -*-
"""Word (.docx) 导入器（v0.3 · 数据规范 v2）—— **零新增依赖**。

.docx 就是一个 zip 包，正文在 `word/document.xml`。这里用标准库 zipfile +
xml.etree 直接抠文本，不引入 python-docx：

- `w:p`   段落 → 一行
- `w:tbl` 表格 → 每行按单元格用 TAB 拼接（很多「时间 | 发送者 | 内容」的表格导出）
- `w:tab` / `w:br` → 制表/换行
- `w:drawing` / `w:pict` → 只计数（图片不属于聊天记录导入通道，见媒体导入）

拍平成的行流交给 plaintext_lines.parse_lines 复用同一套时间戳/发送者启发式，
因此两个适配器的解析口径完全一致。

不支持旧版二进制 `.doc`（detect 返回 False，doctor 会给出转存引导）。
"""
from __future__ import annotations

import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _para_text(p: ET.Element, counter: dict) -> str:
    """一个 w:p → 字符串（tab/br 转成制表与换行；图片只计数）。"""
    parts: list[str] = []
    for node in p.iter():
        tag = node.tag
        if tag == f"{W}t":
            parts.append(node.text or "")
        elif tag == f"{W}tab":
            parts.append("\t")
        elif tag == f"{W}br":
            parts.append("\n")
        elif tag in (f"{W}drawing", f"{W}pict"):
            counter["images"] = counter.get("images", 0) + 1
    return "".join(parts)


def _cell_text(tc: ET.Element, counter: dict) -> str:
    return " ".join(x for x in (_para_text(p, counter)
                                for p in tc.findall(f"{W}p")) if x).strip()


class DocxTextImporter:
    source_name = "docx"

    def __init__(self) -> None:
        self.stats: dict = {}

    def detect(self, path: Path) -> bool:
        p = Path(path)
        if p.suffix.lower() != ".docx" or not zipfile.is_zipfile(p):
            return False
        try:
            with zipfile.ZipFile(p) as z:
                return "word/document.xml" in z.namelist()
        except Exception:
            return False

    def read_lines(self, path: Path) -> tuple[list[str], dict]:
        """docx → 行流（段落为行，表格行按 TAB 拼单元格）。"""
        counter: dict = {"tables": 0, "images": 0}
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml")
        root = ET.fromstring(xml)
        body = root.find(f"{W}body")
        lines: list[str] = []
        if body is None:
            return lines, counter
        for child in body:
            if child.tag == f"{W}p":
                lines.extend(_para_text(child, counter).split("\n"))
            elif child.tag == f"{W}tbl":
                counter["tables"] += 1
                for tr in child.findall(f"{W}tr"):
                    cells = [_cell_text(tc, counter) for tc in tr.findall(f"{W}tc")]
                    if any(cells):
                        lines.append("\t".join(cells))
        return lines, counter

    def parse(self, path: Path) -> list[dict]:
        # 延迟导入：让注册顺序与 registry._BUILTIN 的声明顺序一致（plaintext 收尾兜底）
        from importers.plaintext_lines import parse_lines
        lines, extra = self.read_lines(Path(path))
        rows, stats = parse_lines(lines)
        stats.update(extra)
        stats["source_name"] = self.source_name
        stats["encoding"] = "docx(zip+xml)"
        self.stats = stats
        return rows


# 模块导入即登记进格式注册表
from importers import registry as _registry            # noqa: E402
_registry.register(DocxTextImporter())
