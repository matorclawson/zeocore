import json, os, re, subprocess, sys, tomllib, urllib.error, urllib.request
from pathlib import Path

LEVELS = ("patch", "minor", "major")
# TAG_PREFIX: the tag namespace ("v" for a package, "engine/v" for the engine).
# INDEX: the index URL (PyPI's JSON API) or "none" for a repository with no
# package on any index, which then counts from its tags alone.
PREFIX = os.environ.get("TAG_PREFIX", "v")
if re.fullmatch(r"(?:[a-z][a-z0-9-]*/)?v", PREFIX) is None:
    sys.exit(
        f"::error::TAG_PREFIX {PREFIX!r} must be 'v' or '<name>/v' (lower-case name)"
    )
NO_INDEX = os.environ["INDEX"] == "none"
SEMVER = re.compile(re.escape(PREFIX) + r"(\d+)\.(\d+)\.(\d+)")


def git(*args):
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True
    ).stdout.split()


def fail(message):
    print(f"::error::{message}", file=sys.stderr)
    sys.exit(1)


def versions(tags):
    return sorted(tuple(map(int, m.groups())) for m in map(SEMVER.fullmatch, tags) if m)


def text(version):
    return ".".join(map(str, version))


def index(path):
    """(status, JSON) for INDEX/PROJECT/path; only 200 and 404 are answers."""
    url = f"{os.environ['INDEX']}/{os.environ['PROJECT']}/{path}"
    try:
        with urllib.request.urlopen(url, timeout=30) as answer:
            return 200, json.load(answer)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return 404, None
        fail(f"{url} answered {error.code}")
    except (urllib.error.URLError, OSError, ValueError) as error:
        fail(f"{url} could not be read: {error}")


# The release tag at HEAD, if this is a re-run, and every reachable one.
here = versions(git("tag", "--points-at", "HEAD", "--list", PREFIX + "*"))
if len(here) > 1:
    fail(f"HEAD carries more than one release tag: {here}")
earlier = [
    v
    for v in versions(git("tag", "--merged", "HEAD", "--list", PREFIX + "*"))
    if v not in here
]
if not earlier and not here:
    fail("no vX.Y.Z tag is reachable from HEAD to count from")

# Every fragment added since the last release tag, so a merge whose
# run was cancelled or failed still counts in the next release.
added = []
if earlier:
    since = PREFIX + text(earlier[-1])
    status = subprocess.run(
        [
            "git",
            "diff",
            "--name-status",
            "--no-renames",
            since,
            "HEAD",
            "--",
            "changelog.d/",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    for line in status:
        code, path = line.split("\t", 1)
        if path == "changelog.d/README.md":
            continue
        if code != "A":
            fail(
                f"{path} was changed or deleted since {since}; a fragment is added once and kept"
            )
        added.append(path)
level, notes = "patch", []
for path in sorted(added):
    first, _, body = Path(path).read_text(encoding="utf-8").partition("\n")
    found = re.fullmatch(r"bump:\s*(patch|minor|major)\s*", first)
    if found is None:
        fail(
            f"{path}'s first line must be 'bump: patch', 'bump: minor' or 'bump: major'"
        )
    level = max(level, found[1], key=LEVELS.index)
    notes.append(body.strip())

if here:
    version = here[0]
    print(f"HEAD already carries {PREFIX}{text(version)}: a re-run", file=sys.stderr)
else:
    # Count from the higher of the last tag and the newest version
    # on the index: a version can reach PyPI without a tag (0.15.1
    # did), and a file name is never accepted twice.
    last = earlier[-1]
    published = []
    if not NO_INDEX:
        status, project = index("json")
        if status != 200 or not project.get("releases"):
            fail(
                f"{os.environ['PROJECT']} isn't on the index yet; its first upload is a separate step"
            )
        published = versions(PREFIX + name for name in project["releases"])
        # A tag whose publish failed, with a merge landing before the
        # re-run, would otherwise never reach PyPI and lose its notes.
        if last not in published and index(f"{text(last)}/json")[0] == 404:
            fail(
                f"{PREFIX}{text(last)} is tagged but not on the index: re-run the Publish run "
                f"for {PREFIX}{text(last)}'s commit, which reuses its tag, before releasing again"
            )
    major, minor, patch = max(last, published[-1] if published else last)
    version = {
        "major": (major + 1, 0, 0),
        "minor": (major, minor + 1, 0),
        "patch": (major, minor, patch + 1),
    }[level]
    if git("tag", "--list", PREFIX + text(version)):
        fail(f"{PREFIX}{text(version)} already exists at another commit")
    # Only a 404 is free. The project answer above is the positive
    # control that the index is reachable and names this project.
    if not NO_INDEX and index(f"{text(version)}/json")[0] != 404:
        fail(f"{text(version)} is already on the index")
version_text = text(version)

floor = ""  # a Python package names its floor; a repository with no pyproject has none
if Path("pyproject.toml").exists():
    spec = tomllib.load(open("pyproject.toml", "rb"))["project"].get(
        "requires-python", ""
    )
    found = re.fullmatch(r">=(\d+\.\d+)(\.\d+)?", spec)
    if found is None:
        fail(f"requires-python {spec!r} has no >=X.Y floor")
    floor = found[1]

sha = git("rev-parse", "HEAD")[0]
body = "\n\n".join(notes) or f"A patch release of {sha}, with no changelog fragment."
Path(sys.argv[1]).write_text(f"{body}\n", encoding="utf-8")
print(f"{level} increment: {PREFIX}{version_text} at {sha}", file=sys.stderr)
print(f"version={version_text}")
print(f"floor={floor}")
print(f"tag={PREFIX}{version_text}")
