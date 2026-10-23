"""Helpers for the executed-docs tests: find the site's pages and read their code blocks."""

import re
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DOCS = REPO / "docs"
_NOT_SITE_PAGES = ("SPEC.md", "PROGRESS.md", "adr/", "rfc/", "milestones/", "scripts/")
_FENCE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<fence>```+)(?P<info>[^\n`]*)\n"
    r"(?P<body>.*?)^(?P=indent)(?P=fence)[ \t]*$",
    re.MULTILINE | re.DOTALL,
)


@dataclass(frozen=True)
class CodeBlock:
    """One fenced code block of a Markdown page."""

    page: Path
    language: str
    title: str | None
    body: str
    marker: str | None
    """The ``<!-- docs-test: NAME -->`` comment right before the block, if any."""

    def where(self) -> str:
        """``page:language`` for assertion messages."""
        return f"{self.page.relative_to(REPO)} ({self.language} {self.title or ''})"


def site_pages() -> list[Path]:
    """Every Markdown page of the docs site, plus the README."""
    pages = [
        page
        for page in sorted(DOCS.rglob("*.md"))
        if not str(page.relative_to(DOCS)).startswith(_NOT_SITE_PAGES)
    ]
    return [REPO / "README.md", *pages]


def code_blocks(page: Path) -> list[CodeBlock]:
    """The fenced code blocks of ``page``, with their language, title and test marker."""
    text = page.read_text(encoding="utf-8")
    blocks = []
    for match in _FENCE.finditer(text):
        info = match.group("info").strip()
        language = info.split(" ", 1)[0]
        title = re.search(r'title="([^"]+)"', info)
        before = text[: match.start()].rstrip().rsplit("\n", 1)[-1]
        marker = re.fullmatch(r"<!-- docs-test: ([\w-]+) -->", before.strip())
        body = "\n".join(
            line.removeprefix(match.group("indent")) for line in match.group("body").splitlines()
        )
        blocks.append(
            CodeBlock(
                page,
                language,
                title.group(1) if title else None,
                body,
                marker.group(1) if marker else None,
            )
        )
    return blocks


def marked_block(page: Path, marker: str) -> CodeBlock:
    """The one block of ``page`` preceded by ``<!-- docs-test: marker -->``."""
    found = [block for block in code_blocks(page) if block.marker == marker]
    assert len(found) == 1, f"{page}: expected one block marked {marker!r}, found {len(found)}"
    return found[0]


def commands(block: CodeBlock) -> list[str]:
    """A bash block's commands: comments and blank lines dropped, continuations joined."""
    joined = block.body.replace("\\\n", " ")
    lines = (line.split(" #", 1)[0].strip() for line in joined.splitlines())
    return [line for line in lines if line and not line.startswith("#")]
