# Release notes

Every merge to `main` is a release. The notes for each version are on its
[GitHub Release](https://github.com/zeroemployeeorg/zeocore/releases); the
history up to 0.15.1 is in [CHANGELOG.md](CHANGELOG.md), and
[`changelog.d/`](changelog.d/README.md) holds the notes of changes not yet
released.

## Recent milestones

- **0.35.x: briefing sources.** Keyless public sources for a live briefing:
  MET Norway weather, ADSB.lol flight position and ECB daily reference rates.
  A traveller-stated destination fills in when the public route lookup returns
  nothing, and the card says so. 0.35.1 accepts the route lookup's `201`
  answer. See [Briefing cards](docs/how-to/briefing-cards.md).
- **0.34.0.** A market card's change is labelled `COMPUTED` with the two days
  it came from, not provider data.
- **0.33.0: the HTML renderer.** `zeo_core.adapters.widget_html` turns a
  `WidgetBoard` into self-contained HTML: inline style only, no JavaScript, no
  remote resource, every field escaped.
- **0.32.x.** Real-source fields in the widget contract are nullable (no
  schedule, no rain probability, no previous rate) and a daily reference rate
  is its own freshness basis.
- **0.31.0: managed Agents API wrapper.** `zeo_core.integrations.agents` runs
  one message on an OpenAI managed agent session and answers its tool calls
  with your functions. See [the guide](docs/integrations/agents-api.md).
- **0.30.0: widget contracts.** `zeo_core.contracts.ui` adds `DataRef`,
  `WidgetUpsert` and `WidgetBoard` with the honesty rules checked in code.
- **0.29.0: Python 3.12 or newer.** The floor moved from 3.14 to 3.12, the
  interpreter Google Colab provides.

## Upgrading

`pip install --upgrade zeocore`. The package follows semantic versioning but is
a beta library until 1.0: read the release notes before moving across minor
versions.
