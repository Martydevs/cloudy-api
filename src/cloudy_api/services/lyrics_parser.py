"""Extract song lyrics from the HTML the frontend scraped from Letras.com.

Letras renders lyrics as ``<p>`` stanzas whose lines are separated by ``<br>``.
The frontend should send just the lyrics container, but a whole page, a bare
fragment or plain text all work: the parser looks for the known containers and
falls back to the whole document.
"""

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup, NavigableString, Tag

# Most specific first. `select_one` returns the first match in document order.
_CONTAINER_SELECTORS = (".lyric-original", ".cnt-letra", ".lyric", "[itemprop=text]")
_NOISE_TAGS = ("script", "style", "noscript", "template", "iframe", "svg")

MAX_LINES = 300
MAX_TITLE_CHARS = 200

Stanza = list[str]


class LyricsError(Exception):
    """The HTML doesn't contain usable lyrics (always the client's problem)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ParsedLyrics:
    title: str | None
    stanzas: list[Stanza]

    @property
    def line_count(self) -> int:
        return sum(len(stanza) for stanza in self.stanzas)


def _clean_line(line: str) -> str:
    return re.sub(r"[ \t\r\f\v]+", " ", line.replace("\xa0", " ")).strip()


def _find_title(soup: BeautifulSoup) -> str | None:
    heading = soup.find("h1")
    if heading is None:
        return None
    title = _clean_line(heading.get_text(" "))
    return title[:MAX_TITLE_CHARS] or None


def _find_container(soup: BeautifulSoup) -> Tag | BeautifulSoup:
    for selector in _CONTAINER_SELECTORS:
        found = soup.select_one(selector)
        if found is not None:
            return found
    return soup


def _text_blocks(container: Tag | BeautifulSoup) -> list[str]:
    """Return the text of each paragraph, with ``<br>`` turned into newlines."""
    for br in container.find_all("br"):
        br.replace_with("\n")
    paragraphs = container.find_all("p")
    if paragraphs:
        return [p.get_text() for p in paragraphs]
    return [container.get_text()]


def _split_stanzas(block: str) -> list[Stanza]:
    stanzas: list[Stanza] = []
    current: Stanza = []
    for raw in block.split("\n"):
        line = _clean_line(raw)
        if line:
            current.append(line)
        elif current:  # a blank line closes the stanza
            stanzas.append(current)
            current = []
    if current:
        stanzas.append(current)
    return stanzas


def parse_lyrics_html(html: str) -> ParsedLyrics:
    """Parse ``html`` into stanzas (lists of lines), keeping the original order."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(_NOISE_TAGS):
        tag.decompose()

    title = _find_title(soup)
    container = _find_container(soup)

    # In real HTML, newlines inside text nodes are insignificant whitespace;
    # only <br> and block boundaries break lines. Plain text keeps its newlines.
    if soup.find() is not None:
        for node in container.find_all(string=True):
            # Exact type check: comments, doctypes, etc. are subclasses and
            # must not be turned into visible text.
            if type(node) is NavigableString:
                node.replace_with(re.sub(r"\s+", " ", node))

    stanzas = [s for block in _text_blocks(container) for s in _split_stanzas(block)]
    if not stanzas:
        raise LyricsError("no_lyrics", "No lyrics found in the HTML")

    parsed = ParsedLyrics(title=title, stanzas=stanzas)
    if parsed.line_count > MAX_LINES:
        raise LyricsError(
            "lyrics_too_long", f"Lyrics have more than {MAX_LINES} lines; is this a song?"
        )
    return parsed
