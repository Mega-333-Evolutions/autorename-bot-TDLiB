"""``bot.tg.html`` against Telethon's HTML parser, which the bot used before it moved to TDLib: the same text and the same entities
for any HTML, so a caption or a format a person saved before the move reads back exactly as it did.

Telethon is not something the bot needs any more, so this runs only where it happens to be installed (``pip install telethon``)."""
import random

import pytest

telethon_html = pytest.importorskip("telethon.extensions.html")

from bot.tg import html as ours  # noqa: E402

FIXED = [
    "", "plain", "<b>bold</b>", "<b>a</b> and <i>b</i>", "<b><i>both</i></b>", "<b>a<b>b</b>c</b>", "  <b> padded </b>  ",
    "<a href='https://x.io/a?b=1&amp;c=2'>link</a>", '<a href="https://x.io">https://x.io</a>', "<a href='mailto:a@b.co'>mail</a>",
    "<a>no href</a>", "<pre>code\nblock</pre>", '<pre><code class="language-python">print(1)</code></pre>', "<code>inline</code>",
    "<blockquote>q</blockquote>", "<blockquote expandable>q</blockquote>", "<u>u</u><s>s</s><del>d</del><strong>S</strong><em>E</em>",
    "<tg-emoji emoji-id='5368324170671202286'>😀</tg-emoji>", "<tg-emoji>bad</tg-emoji>", "<span>unknown</span> tag", "a &lt; b &amp; c &gt; d",
    "😀 <b>emoji 😀 bold</b> 😀", "<b>😀</b>", "𝐛𝐨𝐥𝐝 <i>𝐢</i>", "<b>unclosed", "</b>stray close", "<b></b>empty", "<b> </b>", "\n<b>x</b>\n",
    "<i>a</i><i>b</i>", "<code>a</code> <code>b</code>", "{filename} <b>{quality}</b> [{episode}]",
]
TAGS = ["b", "i", "u", "s", "code", "strong", "em", "del", "pre", "blockquote", "a href='https://e.io/%d'", "span", "tg-emoji emoji-id='%d'"]
WORDS = ["a", "bc", "😀", "x y", " ", "&amp;", "&lt;", "é", "𝐱", "\n", "{n}"]


def normal(entities):
    """What matters about the entities, whatever classes they are (Telethon leaves ``collapsed`` as ``None`` when it is not set)."""
    out = []
    for e in entities:
        extra = {k: getattr(e, k) for k in ("language", "url", "user_id", "collapsed", "document_id") if hasattr(e, k)}
        if "collapsed" in extra:
            extra["collapsed"] = bool(extra["collapsed"])
        out.append((type(e).__name__, e.offset, e.length, tuple(sorted(extra.items()))))
    return out


def outcome(parse, html):
    try:
        text, entities = parse(html)
        return text, normal(entities)
    except Exception as exc:  # noqa: BLE001 - a refusal must be the same kind of refusal
        return "raised", type(exc).__name__


def random_html(rng):
    out, stack = [], []
    for _ in range(rng.randint(1, 12)):
        r = rng.random()
        if r < 0.35:
            tag = rng.choice(TAGS)
            if "%d" in tag:
                tag = tag % rng.randint(1, 99999)
            stack.append(tag.split()[0])
            out.append(f"<{tag}>")
        elif r < 0.6 and stack:
            out.append(f"</{stack.pop()}>")
        else:
            out.append(rng.choice(WORDS))
    if rng.random() < 0.6:
        while stack:
            out.append(f"</{stack.pop()}>")
    return "".join(out)


@pytest.mark.parametrize("html", FIXED)
def test_handwritten_html_reads_the_same(html):
    assert outcome(ours.parse, html) == outcome(telethon_html.parse, html)


def test_random_html_reads_the_same():
    rng = random.Random(7)
    for _ in range(2000):
        html = random_html(rng)
        assert outcome(ours.parse, html) == outcome(telethon_html.parse, html), html
