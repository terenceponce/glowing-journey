"""Turn Hugo markdown (kubernetes/website style) into clean text chunks."""

import re
from dataclasses import dataclass

import yaml

FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)
HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
# {{< glossary_tooltip text="containers" term_id="container" >}} -> containers
GLOSSARY = re.compile(r"\{\{<\s*glossary_tooltip\s+[^>]*?text=\"([^\"]*)\"[^>]*?>\}\}")
# Any other shortcode tag, e.g. {{< note >}}, {{< /note >}}, {{% code_sample ... %}}. Inner text is kept.
SHORTCODE = re.compile(r"\{\{[<%].*?[%>]\}\}", re.S)
# [text](url) -> text; images are dropped.
IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*(\{#[^}]*\})?\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")


@dataclass
class Chunk:
    title: str
    section: str
    text: str


def parse_document(raw: str) -> tuple[str, str]:
    """Return (title, cleaned markdown body)."""
    title = ""
    m = FRONT_MATTER.match(raw)
    if m:
        try:
            meta = yaml.safe_load(m.group(1)) or {}
            title = str(meta.get("title") or meta.get("linkTitle") or "")
        except yaml.YAMLError:
            pass
        raw = raw[m.end():]
    body = HTML_COMMENT.sub("", raw)
    body = GLOSSARY.sub(r"\1", body)
    body = SHORTCODE.sub("", body)
    body = IMAGE.sub("", body)
    body = LINK.sub(r"\1", body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return title.strip(), body.strip()


def split_sections(body: str) -> list[tuple[str, str]]:
    """Split on markdown headings (ignoring '#' lines inside code fences). Returns (heading, text)."""
    sections: list[tuple[str, str]] = []
    heading, lines, in_code = "", [], False
    for line in body.splitlines():
        if FENCE.match(line):
            in_code = not in_code
        m = None if in_code else HEADING.match(line)
        if m:
            if "\n".join(lines).strip():
                sections.append((heading, "\n".join(lines).strip()))
            heading, lines = m.group(2).strip(), []
        else:
            lines.append(line)
    if "\n".join(lines).strip():
        sections.append((heading, "\n".join(lines).strip()))
    return sections


def split_text(text: str, size: int, overlap: int) -> list[str]:
    """Pack paragraphs into chunks of at most ~`size` characters, carrying `overlap` characters forward."""
    pieces: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= size:
            pieces.append(para)
            continue
        step = max(size - overlap, 1)
        pieces.extend(para[i:i + size] for i in range(0, len(para), step) if para[i:i + size].strip())

    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if not current:
            current = piece
        elif len(current) + 2 + len(piece) <= size:
            current = f"{current}\n\n{piece}"
        else:
            chunks.append(current)
            tail = current[-overlap:] if overlap else ""
            # Start the overlap at a word boundary.
            if tail and " " in tail:
                tail = tail[tail.index(" ") + 1:]
            current = f"{tail}\n\n{piece}" if tail and len(tail) + 2 + len(piece) <= size else piece
    if current:
        chunks.append(current)
    return chunks


def chunk_document(raw: str, size: int, overlap: int, min_chars: int = 50) -> list[Chunk]:
    title, body = parse_document(raw)
    chunks = []
    for heading, text in split_sections(body):
        for piece in split_text(text, size, overlap):
            if len(piece) >= min_chars:
                chunks.append(Chunk(title=title, section=heading, text=piece))
    return chunks
