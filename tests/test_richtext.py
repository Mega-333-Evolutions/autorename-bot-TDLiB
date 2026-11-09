"""Turning the formatting a person puts on a caption into the HTML that is stored, and showing it back."""
import random

import pytest

from bot.richtext import from_entities, preview
from bot.tg import html as telegram_html
from bot.tg import types
from bot.tg.entities import add_surrogate, del_surrogate

from .tgfakes import telegram_text


def html_of(markup, start=0):
    text, entities = telegram_text(markup)
    return from_entities(text, entities, start)


def formats_of(html_text):
    """What Telegram would show for stored HTML: ``{(kind, offset, length, url)}``, from the parser that sends the caption."""
    _text, entities = telegram_html.parse(html_text)
    return {(type(e).__name__, e.offset, e.length, getattr(e, "url", None)) for e in entities}


# ---------------------------------------------------------------------------
# the formatting that is kept
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("markup, expected", [
    ("[b]Tokyo[/b] Revengers", "<b>Tokyo</b> Revengers"),
    ("a [i]b[/i] c", "a <i>b</i> c"),
    ("a [u]b[/u] c", "a <u>b</u> c"),
    ("a [s]b[/s] c", "a <s>b</s> c"),
    ("a [code]b[/code] c", "a <code>b</code> c"),
    ("a [a=https://t.me/x]b[/a] c", 'a <a href="https://t.me/x">b</a> c'),
    ("a [quote]b[/quote]", "a <blockquote>b</blockquote>"),
    ("[pre=python]print(1)[/pre]", '<pre><code class="language-python">print(1)</code></pre>'),
    ("[pre]x = 1[/pre]", "<pre>x = 1</pre>"),
])
def test_each_kind_of_formatting_becomes_its_tag(markup, expected):
    assert html_of(markup) == expected


def test_a_collapsed_quote_stays_collapsed():
    entity = types.MessageEntityBlockquote(0, 3, collapsed=True)
    assert from_entities("one", [entity]) == "<blockquote expandable>one</blockquote>"


def test_a_mention_of_a_person_keeps_its_link():
    assert from_entities("hi Sam", [types.MessageEntityMentionName(3, 3, 12345)]) == 'hi <a href="tg://user?id=12345">Sam</a>'


def test_bold_italic_and_a_link_in_one_caption():
    markup = "[b]Watch[/b] {title} [i]episode {episode}[/i] on [a=https://t.me/Anime_X]our channel[/a]"
    assert html_of(markup) == '<b>Watch</b> {title} <i>episode {episode}</i> on <a href="https://t.me/Anime_X">our channel</a>'


def test_formatting_that_is_not_the_persons_own_is_left_out():
    """What Telegram finds by itself comes back by itself when the caption is sent; spoilers and custom emoji are not kept."""
    assert html_of("[tag]#anime[/tag] [at]@channel[/at] [spoiler]secret[/spoiler] /start") is None
    plain, entities = telegram_text("link https://example.com mail a@b.co")
    entities += [types.MessageEntityUrl(5, 19), types.MessageEntityEmail(30, 6), types.MessageEntityCustomEmoji(0, 1, 5)]
    assert from_entities(plain, entities) is None
    assert html_of("[spoiler]a[/spoiler] [b]b[/b] [tag]#c[/tag]") == "a <b>b</b> #c"


@pytest.mark.parametrize("text, entities", [("just words", None), ("just words", []), ("", [types.MessageEntityBold(0, 1)])])
def test_no_formatting_means_nothing_to_convert(text, entities):
    assert from_entities(text, entities) is None


def test_text_that_looks_like_html_is_escaped_once_formatting_is_used():
    assert html_of("[b]Tom & Jerry[/b] <3 > <i>not a tag</i>") == "<b>Tom &amp; Jerry</b> &lt;3 &gt; &lt;i&gt;not a tag&lt;/i&gt;"


def test_a_link_address_survives_the_html():
    url = 'https://example.com/?a=1&copy=2&q="z"'
    stored = html_of(f"see [a={url}]this[/a]")
    assert "&amp;copy=2" in stored  # written out, not read as the © character
    assert formats_of(stored) == {("MessageEntityTextUrl", 4, 4, url)}


def test_emoji_do_not_move_the_formatting():
    """Telegram counts an emoji outside the basic plane (and each part of a family emoji) as two units."""
    assert html_of("🔥🔥 [b]Hello[/b] 👨‍👩‍👧 [i]x[/i]") == "🔥🔥 <b>Hello</b> 👨‍👩‍👧 <i>x</i>"


def test_a_prefix_in_front_is_skipped():
    text, entities = telegram_text("/words [b]Hi[/b] there")
    assert from_entities(text, entities, start=len("/words ")) == "<b>Hi</b> there"
    whole = types.MessageEntityBold(0, len("/words Hi"))  # formatting that began inside the prefix is cut where the caption starts
    assert from_entities("/words Hi", [whole], start=len("/words ")) == "<b>Hi</b>"


def test_start_counts_characters_not_utf16_units():
    text, entities = telegram_text("🔥 hi [b]x[/b]")
    assert from_entities(text, entities, start=2) == "hi <b>x</b>"


def test_spaces_around_the_caption_are_dropped_with_their_formatting():
    assert html_of("  [b]Hi[/b]  ") == "<b>Hi</b>"
    assert html_of("[b]Hi  [/b]") == "<b>Hi</b>"
    assert html_of("[b]   [/b]") is None


# ---------------------------------------------------------------------------
# formats that meet
# ---------------------------------------------------------------------------


def test_nested_formatting_stays_nested():
    assert html_of("[b]a [i]b[/i] c[/b]") == "<b>a <i>b</i> c</b>"


def test_formats_that_only_partly_overlap_are_split_so_the_tags_nest():
    stored = html_of("[b]Hel[i]lo[/b] wo[/i]rld")
    assert stored == "<b>Hel<i>lo</i></b><i> wo</i>rld"
    # the same words are bold and italic as before
    text, entities = telegram_html.parse(stored)
    units = add_surrogate(text)
    bold = "".join(units[e.offset:e.offset + e.length] for e in entities if isinstance(e, types.MessageEntityBold))
    italic = "".join(units[e.offset:e.offset + e.length] for e in entities if isinstance(e, types.MessageEntityItalic))
    assert (bold, italic) == ("Hello", "lo wo")


def test_two_kinds_on_the_same_words():
    stored = html_of("[b][i]x[/i][/b] y")
    assert formats_of(stored) == {("MessageEntityBold", 0, 1, None), ("MessageEntityItalic", 0, 1, None)}


def test_the_same_kind_twice_over_the_same_words_is_one_tag():
    """The HTML parser can't nest a tag in itself, so ``<b>a<b>b</b>c</b>`` would lose its outer bold."""
    text, entities = "abcde", [types.MessageEntityBold(0, 3), types.MessageEntityBold(1, 3), types.MessageEntityBold(4, 1)]
    stored = from_entities(text, entities)
    assert stored == "<b>abcde</b>"
    assert formats_of(stored) == {("MessageEntityBold", 0, 5, None)}


def test_of_two_overlapping_links_the_later_one_gets_what_is_left():
    entities = [types.MessageEntityTextUrl(0, 5, "https://a.io"), types.MessageEntityTextUrl(3, 5, "https://b.io")]
    assert from_entities("abcdefgh", entities) == '<a href="https://a.io">abcde</a><a href="https://b.io">fgh</a>'


# ---------------------------------------------------------------------------
# {variables} are replaced as a whole, so a format must not cut one in two
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("markup, expected", [
    ("Episode {[b]episode[/b]} here", "Episode <b>{episode}</b> here"),  # double-tapping the word leaves the braces out
    ("[b]{epis[/b]ode} here", "<b>{episode}</b> here"),
    ("Episode [b]{epis[/b]ode} here", "Episode <b>{episode}</b> here"),
    ("Ep {epis[b]ode} here[/b]", "Ep <b>{episode} here</b>"),
    ("[b]{title} {epis[/b]ode}", "<b>{title} {episode}</b>"),
    ("[a=https://x.io]{ti[/a]tle}", '<a href="https://x.io">{title}</a>'),
    ("[b]Ep[/b] {episode} [i]x[/i]", "<b>Ep</b> {episode} <i>x</i>"),  # a format that does not touch a variable is left alone
    ("{[b]nonsense[/b]}", "{<b>nonsense</b>}"),  # only the variables the caption knows are held together
    ("Ep {[b]2episode[/b]} here", "Ep <b>{2episode}</b> here"),  # the padded episode variables are held together too
    ("[b]{3epis[/b]ode}", "<b>{3episode}</b>"),
])
def test_a_format_never_cuts_a_variable_in_two(markup, expected):
    assert html_of(markup) == expected


# ---------------------------------------------------------------------------
# it always says the same as the message did
# ---------------------------------------------------------------------------


def _coverage(text, entities):
    units = add_surrogate(text)
    covered = [set() for _ in units]
    for e in entities:
        for i in range(max(e.offset, 0), min(e.offset + e.length, len(units))):
            covered[i].add((type(e).__name__, getattr(e, "url", None)))
    return covered


def test_random_messages_read_back_exactly_the_way_they_were_formatted():
    """Whatever mix of overlapping formats, emoji and characters that need escaping: the parser that sends the caption reads back
    the same text with every character formatted the way it was."""
    rng = random.Random(11)
    alphabet = list("abc def") + ["🔥", "&", "<", ">", "\n", "é", "👨‍👩‍👧", '"', "'"]
    kinds = [types.MessageEntityBold, types.MessageEntityItalic, types.MessageEntityUnderline, types.MessageEntityStrike, types.MessageEntityCode]

    def off_the_middle_of_an_emoji(units, at):
        while at < len(units) and "\udc00" <= units[at] <= "\udfff":
            at += 1
        return at

    checked = 0
    for _ in range(800):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 16)))
        units = add_surrogate(text)
        entities = []
        for _ in range(rng.randint(1, 5)):
            a = rng.randrange(len(units))
            a, b = off_the_middle_of_an_emoji(units, a), off_the_middle_of_an_emoji(units, rng.randint(a + 1, len(units)))
            if b > a:
                entities.append(rng.choice(kinds)(a, b - a))
        cuts = sorted(rng.sample(range(len(units) + 1), min(len(units) + 1, 4)))  # links that do not overlap one another
        for n in range(0, len(cuts) - 1, 2):
            a, b = off_the_middle_of_an_emoji(units, cuts[n]), off_the_middle_of_an_emoji(units, cuts[n + 1])
            if b > a and rng.random() < 0.5:
                entities.append(types.MessageEntityTextUrl(a, b - a, f'https://x.io/?n={n}&q="a"'))
        stored = from_entities(text, entities)
        if stored is None:
            continue
        checked += 1
        lead = len(units) - len(units.lstrip())
        moved = [type(e)(e.offset - lead, e.length, **({"url": e.url} if hasattr(e, "url") else {})) for e in entities]
        read_text, read_entities = telegram_html.parse(stored)
        assert read_text == text.strip(), stored
        assert _coverage(read_text, read_entities) == _coverage(text.strip(), moved), (text, stored)
    assert checked > 300


# ---------------------------------------------------------------------------
# showing a stored caption
# ---------------------------------------------------------------------------


def test_a_stored_caption_is_shown_with_its_formatting():
    assert preview("<b>{filename}</b> · {size}", 60) == "<b>{filename}</b> · {size}"
    stored = html_of("[b]Watch[/b] {title} [i]episode {episode}[/i] on [a=https://t.me/Anime_X]our channel[/a]")
    assert preview(stored, 1000) == stored


def test_typed_html_is_shown_safely_whatever_was_typed():
    for typed in ("</b>stray <i>open", "<b>x</b></b><u>", "<i><b>a</i></b>", "<<<>>>", "<a href=>x</a>", "<b>"):
        shown = preview(typed, 60)
        assert telegram_html.parse(shown)[0] == (telegram_html.parse(typed)[0] or "—")  # the same words
        assert preview(shown, 60) == shown  # and what is shown is already well-formed: showing it again changes nothing
    assert preview("</b>stray <i>open", 60) == "stray open"


def test_special_characters_are_escaped_when_shown():
    assert preview("Tom &amp; Jerry &lt;3 &gt;", 60) == "Tom &amp; Jerry &lt;3 &gt;"


def test_a_long_caption_is_cut_and_the_tags_still_balance():
    shown = preview("<b>" + "x" * 100 + "</b>", 20)
    assert shown == "<b>" + "x" * 19 + "</b>…"
    assert preview("short", 20) == "short"


def test_a_cut_never_splits_an_emoji():
    shown = preview("🔥" * 30, 11)
    assert shown == "🔥🔥🔥🔥🔥…"
    assert del_surrogate(add_surrogate(shown)) == shown


def test_an_empty_caption_is_a_dash():
    assert preview("", 20) == "—"
