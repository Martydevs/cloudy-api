import pytest

from cloudy_api.services.lyrics_parser import MAX_LINES, LyricsError, parse_lyrics_html

# Shaped like a Letras.com song page: noise around a `.lyric-original` block of
# <p> stanzas whose lines are <br>-separated, with source indentation in between.
LETRAS_PAGE = """
<!DOCTYPE html>
<html><head><title>De Buenas Part - Josh Gamez</title>
<script>window.ads = "should not appear";</script><style>.x { color: red }</style></head>
<body>
  <h1 class="head-title">De Buenas Part</h1>
  <h2>Josh Gamez</h2>
  <div class="lyric-original">
    <p>Primera línea de la canción<br>
       segunda&nbsp;línea<br>
       tercera <a href="/x">línea</a> con enlace</p>
    <!-- an ad slot -->
    <p>Otra estrofa<br>con dos líneas</p>
  </div>
  <div class="other"><p>Related songs</p></div>
</body></html>
"""


def test_parses_letras_page_into_stanzas() -> None:
    parsed = parse_lyrics_html(LETRAS_PAGE)
    assert parsed.title == "De Buenas Part"
    assert parsed.stanzas == [
        ["Primera línea de la canción", "segunda línea", "tercera línea con enlace"],
        ["Otra estrofa", "con dos líneas"],
    ]
    assert parsed.line_count == 5


def test_ignores_scripts_styles_comments_and_other_blocks() -> None:
    text = " ".join(line for stanza in parse_lyrics_html(LETRAS_PAGE).stanzas for line in stanza)
    assert "ads" not in text
    assert "color" not in text
    assert "ad slot" not in text
    assert "Related songs" not in text


def test_fragment_without_container() -> None:
    parsed = parse_lyrics_html("<p>one<br>two</p><p>three</p>")
    assert parsed.title is None
    assert parsed.stanzas == [["one", "two"], ["three"]]


def test_double_br_splits_stanzas_inside_one_block() -> None:
    parsed = parse_lyrics_html('<div class="cnt-letra">one<br>two<br><br>three<br>four</div>')
    assert parsed.stanzas == [["one", "two"], ["three", "four"]]


def test_plain_text_keeps_its_newlines() -> None:
    parsed = parse_lyrics_html("one\ntwo\n\n\nthree\n")
    assert parsed.stanzas == [["one", "two"], ["three"]]


def test_empty_paragraphs_and_whitespace_lines_are_dropped() -> None:
    parsed = parse_lyrics_html("<p>  </p><p>a<br> <br>b</p><p><br></p>")
    assert parsed.stanzas == [["a"], ["b"]]


def test_title_from_h1_is_trimmed_and_capped() -> None:
    parsed = parse_lyrics_html(f"<h1>  {'x' * 500}  </h1><p>line</p>")
    assert parsed.title == "x" * 200
    assert parse_lyrics_html("<h1> </h1><p>line</p>").title is None


@pytest.mark.parametrize("html", ["", "   ", "<div></div>", "<script>1</script>", "<p><br></p>"])
def test_no_lyrics(html: str) -> None:
    with pytest.raises(LyricsError) as excinfo:
        parse_lyrics_html(html)
    assert excinfo.value.code == "no_lyrics"


def test_too_many_lines() -> None:
    parse_lyrics_html("<p>" + "<br>".join(["la"] * MAX_LINES) + "</p>")  # at the limit
    with pytest.raises(LyricsError) as excinfo:
        parse_lyrics_html("<p>" + "<br>".join(["la"] * (MAX_LINES + 1)) + "</p>")
    assert excinfo.value.code == "lyrics_too_long"
