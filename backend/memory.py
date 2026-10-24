"""`memory` tool: markdown files under /memories, versioned with git (one commit per write)."""

import difflib
import re
import subprocess
from pathlib import Path

import checks
import config

ROOT = config.MEMORY_DIR
PREFIX = "/memories"

# Starting layout. The agent fills these in and may add files, but the base topics stay stable
# so related facts land in the same place across sessions.
SEED_FILES = {
    "open.md": (
        "# Open items\n"
        "summary: unresolved questions, todos, and hypotheses to revisit\n\n"
        "## Questions\n\n## Todos\n\n## Hypotheses\n"
    ),
    "vendors.md": (
        "# Vendors\n"
        "summary: delivery days, order cutoffs, account details, vendor-reported changes\n"
    ),
    "ingredients.md": (
        "# Ingredients\n"
        "summary: prices, pack sizes, supply status, per-ingredient observations\n"
    ),
    "menu.md": (
        "# Menu\n"
        "summary: items, recipes and portions, launches and changes, demand patterns\n"
    ),
    "operations.md": (
        "# Operations\n"
        "summary: equipment, staff, waste, receiving, upcoming events\n"
    ),
    "data.md": (
        "# Data\n"
        "summary: how to read the records: SKU-to-ingredient links, corrections, known record problems\n\n"
        "## Mappings\n\n## Corrections\n\n## Known issues\n"
    ),
}

SCHEMA = {
    "type": "function",
    "function": {
        "name": "memory",
        "description": (
            "Your persistent notebook: markdown files under /memories, versioned (every change is kept). "
            "Commands: view (a file, or a directory to list files with summaries; as_of reads a past version), "
            "search (words or an ID), write (create or replace a whole file; empty content deletes), "
            "edit (replace one exact snippet; view the file first to copy text exactly). "
            "write and edit need a reason; if omitted, the source IDs cited in brackets in the new text are used.\n"
            "Layout: open.md (## Questions, ## Todos, ## Hypotheses), vendors.md, ingredients.md, menu.md, "
            "operations.md, data.md (## Mappings, ## Corrections, ## Known issues). Add entries under the "
            "matching file and heading; create a new file only for a topic none of these fits. "
            "Keep line 2 as 'summary: <one line>'.\n"
            "Record what is true now, not a diary: notes are already stored and searchable, so don't copy them; "
            "record what a note changes. Give each fact one home and update it in place when it changes. "
            "Any follow-up ('need to', 'keep checking', 'not yet seen') becomes a todo or question in open.md; "
            "a possible explanation becomes a hypothesis.\n"
            "Entry format: '- <statement> from <date> [<source IDs>] (reported|observed|inferred)'. "
            "Changed belief: '- ~~<old>~~ -> <new> [<source IDs>]'. "
            "Hypothesis: '- (low|med|high) <claim> [<IDs>]. Confirm by: <what would settle it>'. "
            "Todo/question: '- [ ] ...', closed as '- [x] ... [<source>]'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "enum": ["view", "search", "write", "edit"]},
                "path": {"type": "string", "description": "e.g. /memories/ingredients.md"},
                "content": {"type": "string", "description": "write: full file content; empty string deletes"},
                "old": {"type": "string", "description": "edit: exact text to replace (must occur once)"},
                "new": {"type": "string", "description": "edit: replacement text"},
                "query": {"type": "string", "description": "search: words or an ID"},
                "as_of": {"type": "string", "description": "view: ISO datetime; read the version at that time"},
                "reason": {"type": "string", "description": "write/edit: why, with source IDs (defaults to the IDs cited in the new text)"},
            },
            "required": ["command"],
        },
    },
}


class MemoryError(Exception):
    """A failed command. The message tells the model what happened and how to fix it."""


def _fail(what: str, fix: str) -> MemoryError:
    return MemoryError(f"ERROR: {what}\nNothing was saved.\nFix: {fix}")


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def init() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    if not (ROOT / ".git").exists():
        _git("init", "-q")
        _git("config", "user.name", "ops-agent")
        _git("config", "user.email", "ops-agent@localhost")
        for name, text in SEED_FILES.items():
            (ROOT / name).write_text(text)
        _git("add", "-A")
        _git("commit", "-qm", "init memory layout")


def _files() -> list[Path]:
    return sorted(f for f in ROOT.rglob("*.md") if ".git" not in f.parts)


def _resolve(path: str) -> Path:
    if not path or not (path == PREFIX or path.startswith(PREFIX + "/")):
        raise _fail(f"path {path!r} is outside {PREFIX}.", f"use a path like {PREFIX}/ingredients.md.")
    p = (ROOT / path[len(PREFIX):].lstrip("/")).resolve()
    if p != ROOT.resolve() and ROOT.resolve() not in p.parents:
        raise _fail(f"path {path!r} escapes {PREFIX}.", f"use a path like {PREFIX}/ingredients.md.")
    return p


def _rel(p: Path) -> str:
    return str(p.relative_to(ROOT))


def _existing() -> str:
    return ", ".join(f"{PREFIX}/{_rel(f)}" for f in _files())


def _commit(p: Path, reason: str) -> str:
    _git("add", "-A", "--", _rel(p))
    _git("commit", "-qm", reason, "--allow-empty")
    return _git("rev-parse", "--short", "HEAD").strip()


def index() -> str:
    lines = []
    for f in _files():
        summary = next((ln[8:].strip() for ln in f.read_text().splitlines() if ln.startswith("summary:")), "")
        changed = _git("log", "-1", "--format=%cs", "--", _rel(f)).strip()
        lines.append(f"{PREFIX}/{_rel(f)} ({changed}): {summary}")
    return "\n".join(lines) or "(empty)"


def view(path: str, as_of: str | None = None) -> str:
    p = _resolve(path)
    if as_of:
        rev = _git("rev-list", "-1", f"--before={as_of}", "HEAD").strip()
        if not rev:
            raise MemoryError(f"ERROR: no memory existed before {as_of}.\nFix: use a later as_of, or omit it.")
        try:
            text = _git("show", f"{rev}:{_rel(p)}")
        except subprocess.CalledProcessError:
            raise MemoryError(f"ERROR: {path} did not exist at {as_of}.\nFix: omit as_of to read the current version.")
    elif p.is_dir():
        if p == ROOT.resolve():
            return index()
        return "\n".join(ln for ln in index().splitlines() if ln.startswith(path.rstrip("/") + "/")) or "(empty)"
    elif p.exists():
        text = p.read_text()
    else:
        raise MemoryError(f"ERROR: {path} does not exist.\nExisting files: {_existing()}")
    return "\n".join(f"{i:4} {ln}" for i, ln in enumerate(text.splitlines(), 1))


def search(query: str) -> str:
    terms = query.lower().split()
    hits = []
    for f in sorted(_files(), key=lambda f: f.stat().st_mtime, reverse=True):
        lines = f.read_text().splitlines()
        for i, ln in enumerate(lines):
            if all(t in ln.lower() for t in terms):
                ctx = lines[max(0, i - 1): i + 2]
                hits.append(f"{PREFIX}/{_rel(f)}:{i + 1}\n  " + "\n  ".join(ctx))
                if len(hits) == 20:
                    return "\n".join(hits)
    return "\n".join(hits) or f"No matches for {query!r}. Try fewer or different words."


def write(path: str, content: str, reason: str) -> str:
    p = _resolve(path)
    if p.suffix != ".md":
        raise _fail(f"{path} is not a .md file.", "end the path with .md.")
    if not content:
        if not p.exists():
            raise _fail(f"cannot delete {path}: it does not exist.", f"existing files: {_existing()}")
        p.unlink()
        return f"OK deleted {path} (commit {_commit(p, reason)})"
    existed = p.exists()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content if content.endswith("\n") else content + "\n")
    verb = "replaced" if existed else "created"
    return f"OK {verb} {path} (commit {_commit(p, reason)})"


def edit(path: str, old: str, new: str, reason: str) -> str:
    p = _resolve(path)
    if not p.exists():
        raise _fail(f"{path} does not exist.", f"use write to create it, or pick one of: {_existing()}")
    text = p.read_text()
    n = text.count(old)
    if n == 0:
        close = difflib.get_close_matches(old.strip(), text.splitlines(), n=1, cutoff=0.4)
        hint = f"closest line is {close[0]!r}; " if close else ""
        raise _fail(f"'old' text was not found in {path}.",
                    f"{hint}view the file and copy the text exactly (without line numbers).")
    if n > 1:
        raise _fail(f"'old' text occurs {n} times in {path}.", "include more surrounding text so it matches once.")
    p.write_text(text.replace(old, new))
    sha = _commit(p, reason)
    diff = "\n".join(difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0))
    return f"OK edited {path} (commit {sha})\n{diff}"


REQUIRED = {"view": ["path"], "search": ["query"], "write": ["path", "content"], "edit": ["path", "old", "new"]}
CITATION = re.compile(r"\[([^\[\]]+)\]")


def _reason(args: dict) -> str | None:
    """The given reason, or else the sources cited in the new text (e.g. '[N03, INV-013-L01]')."""
    if args.get("reason"):
        return args["reason"]
    cited = CITATION.findall(args.get("new") or args.get("content") or "")
    return f"sources: {', '.join(cited)}" if cited else None


def sync_checks(reason: str) -> str:
    """Apply SKU mappings from data.md and refresh data-check questions in open.md."""
    problems = checks.apply_mappings()
    changes = checks.sync_open_questions()
    if changes:
        _commit(ROOT / "open.md", f"data checks: {', '.join(changes)} ({reason})")
    notes = [f"data check: {c}" for c in changes] + [f"WARNING: {p}" for p in problems]
    return "\n".join(notes)


def run(args: dict) -> str:
    cmd = args.get("command")
    if cmd not in REQUIRED:
        return f"ERROR: unknown command {cmd!r}.\nFix: use one of view, search, write, edit."
    missing = [k for k in REQUIRED[cmd] if args.get(k) is None]
    if missing:
        saving = "\nNothing was saved." if cmd in ("write", "edit") else ""
        return f"ERROR: {cmd} is missing {', '.join(missing)}.{saving}\nFix: call again with {', '.join(missing)}."
    if cmd in ("write", "edit"):
        args = {**args, "reason": _reason(args)}
        if not args["reason"]:
            return (f"ERROR: {cmd} has no reason and the new text cites no sources.\nNothing was saved.\n"
                    "Fix: add a reason, or cite source IDs in brackets in the text, e.g. [N03, INV-013-L01].")
    try:
        if cmd == "view":
            return view(args["path"], args.get("as_of"))
        if cmd == "search":
            return search(args["query"])
        if cmd == "write":
            out = write(args["path"], args["content"], args["reason"])
        else:
            out = edit(args["path"], args["old"], args["new"], args["reason"])
        if args["path"].rstrip("/").endswith("data.md"):
            extra = sync_checks(args["reason"])
            out = f"{out}\n{extra}" if extra else out
        return out
    except MemoryError as e:
        return str(e)
