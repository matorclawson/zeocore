# Convert a newsletter, and render an issue

Stage 2 of the newsletter pipeline (ZEOCORE-SOW-33). Three functions in
`zeo_core.integrations.pandoc` turn a fetched message into clean Markdown, and an
issue's Markdown into email-safe HTML. They take text and return typed data. They
make no network call, hold no credential and read no file of yours.

Install the extra and pandoc 2.15 or later:

```console
$ pip install 'zeocore[pandoc]'
$ pandoc --version
```

!!! warning "A fetched message is data, never instructions"
    Nothing here follows, runs or acts on what a message says. Its text comes out as
    Markdown inside a typed object. Treat everything in a `ConvertedItem` as data
    and fence it downstream. The hidden-text filter is **best effort**: CSS has more
    ways to hide text than any list covers, so the guarantee is the inert result, not
    the filter.

## Decode a raw message

ZEOconnect's fetch hands over the whole raw RFC 822 message. Decode it with
`decode_raw_message`:

```python
from zeo_core.integrations.pandoc.newsletter import decode_raw_message

decoded = decode_raw_message(raw_bytes)   # at most 10 MiB
decoded.html, decoded.text                # the bodies, charset and transfer decoded
decoded.sender, decoded.date, decoded.subject, decoded.message_id
decoded.sender_verified, decoded.authentication
```

It uses the standard library `email` package only. It extracts no attachment and
follows no link. A message with over 256 KiB of headers, over 1000 headers, over 64
parts or nested deeper than 8 levels is refused, and so is anything the email package
cannot read.

**The sender is a claim.** `From` can say anything. `sender_verified` is `True` only
when Gmail's own mail server reported `dmarc=pass` for the same domain as the
`From` address. It reads **only the topmost** `Authentication-Results` header, and only
when its authserv-id is `mx.google.com`: Gmail adds its own header at the top, so any
header below it was written by the sender or a hop on the way, and is ignored.
`list_unsubscribe` is kept as text for the record and is never an action.

## Convert HTML to Markdown

```python
from zeo_core.integrations.pandoc.newsletter import convert_newsletter

item = convert_newsletter(
    html, source="gmail:abc", sender="News <news@example.org>", date="2026-10-13",
)
item.markdown, item.links, item.title, item.stripped, item.warnings
```

or `convert_decoded_message(decoded, source=...)`, which uses the HTML (or wraps the
plain text as paragraphs) and passes the authentication verdict through.

What it does, in order:

1. Refuses input over `max_bytes` (2 MB), over 100 000 elements, or nested deeper
   than 256. `timeout_s` is **one budget** for cleaning and pandoc together.
2. Removes scripts, styles, frames, forms and their controls, comments, and the head.
3. Removes **hidden text**, counted by kind in `stripped` (`hidden_display`,
   `hidden_visibility`, `hidden_opacity`, `hidden_font_size`, `hidden_max_size`,
   `hidden_mso`, `hidden_zero_size`, `hidden_offscreen`, `hidden_clip`,
   `hidden_color_match`, `hidden_style_class`, `hidden_attribute`). Rules inside
   `@media` are not applied, because they may hide content only on a small screen.
4. Removes tracking pixels, and images whose source is not http(s) (`file:`, `data:`,
   `cid:`).
5. Flattens layout tables (pandoc would print a table it cannot draw as `[TABLE]`).
6. Removes the unsubscribe, "view in browser" and preference lines the reviewed data
   file names.
7. Cleans links: drops tracking parameters, unwraps a known redirector whose target is
   written in the address itself, and removes the address of a link with an unsafe
   scheme. It never fetches anything.
8. Runs pandoc with `--sandbox` through stdin and stdout, reader
   `html-native_divs-native_spans-raw_html`, writer `gfm-raw_html`, so **no markup of
   the message survives into the Markdown**.

**Tracked links.** A `ConvertedLink` has `tracked=True` when it went through a mail
service's click tracker, carries a per-recipient parameter, or had tracking removed. A
tracked link identifies the reader or the send: never make one a pick's link. A link
through an opaque tracker (one whose target is encoded) cannot be unwrapped without
fetching it; that is noted in `warnings`.

`item.converter` is the convert step's own tuple: the pandoc version, the sha256 of the
pandoc binary, the flags, the cleaner version, and the version and digest of the data
file, so a stored item says exactly what produced it.

The tracker hosts, footer patterns and hidden-text patterns are package data
(`data/newsletter_noise.json`), read from the installed package and never from your
directory. They are extended by small, data-only pull requests, each with the message
that showed the pattern and no sender content copied.

## Render an issue's Markdown to HTML

```python
from zeo_core.integrations.pandoc.render import render_email_html

rendered = render_email_html(
    markdown,
    image_sizes={"https://example.org/i.png": (600, 300)},
)
rendered.html, rendered.sha256, rendered.renderer, rendered.renderer_digest
```

The output is the email subset: `h2 h3 p br hr ul ol li blockquote strong em code`, `a`
(https or mailto, link text required) and `img` (https, `alt`, `width` and `height`).
Anything else is a typed refusal that names the construct, never a silent rewrite.

- The Markdown reader is `markdown_strict` with raw HTML and every attribute feature
  off, so HTML typed into the issue stays **text**. (`gfm` and `commonmark` ignore a
  request to turn raw HTML off, so they are not used.)
- A Lua filter, shipped as package data, **rebuilds** the document from only the
  allowed nodes. Pandoc's writer would add `figure`, `section`, `span`, `div`, `class`,
  `id` and footnotes; none of that can reach the output.
- A Python check then parses the HTML and refuses any tag, attribute, `javascript:` or
  `data:` link, bare-IP host, user name in an address, mailto with parameters, one-pixel
  image or unbalanced tag.
- Markdown cannot carry an image's size once attributes are off, so you pass
  `image_sizes` by exact source; an image with no entry is refused. An ordered list is
  always numbered from 1.
- GFM extras (footnotes, tables, task lists, emoji, bare addresses, math) are not
  features of this reader: they come out as plain text, or are refused by name.

**The renderer tuple.** `rendered.renderer` names what produced the bytes: the pandoc
version and the sha256 of the binary that ran, the sha256 of the filter, the exact
flags, and the subset's version; `renderer_digest` is the sha256 of that tuple. The
binary and the filter are **copied into a private directory and hashed and run as
copies**, so the tuple names the bytes that ran. Pass `expected_renderer_digest` to
refuse a different renderer before anything runs. The output is deterministic for a
given tuple. A new pandoc, filter or flag is a new tuple, so a new artifact that
voids earlier marks: expect the digest to change when pandoc is upgraded.

## Refusals

Every failure is a `NewsletterRefusalError` with a stable `.code` and a fixed message
that ends with what to do next. No message ever quotes the input.
