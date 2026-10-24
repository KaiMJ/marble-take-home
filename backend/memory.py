"""`memory` tool: markdown files under /memories, versioned with git (one commit per write)."""

import difflib
import subprocess
from pathlib import Path

import config

ROOT = config.MEMORY_DIR
PREFIX = "/memories"

SCHEMA = {
    "type": "function",
    "function": {
        "name": "memory",
        "description": (
            "Your persistent notebook: markdown files under /memories, versioned (every write is kept). "
            "Use it for facts, corrections, open questions, todos and hypotheses that should carry across sessions.\n"
            "Commands: view (file or directory; as_of reads a past version), search (words or an ID), "
            "write (create/replace a file; empty content deletes), edit (replace one exact snippet).\n"
            "Conventions: one file per topic (e.g. /memories/vendors/<name>.md, /memories/ingredients/<name>.md). "
            "Line 1 '# Title', line 2 'summary: <one line>' (shown in the index). "
            "Entries: '- <statement> from <date> [<source IDs>] (reported|observed|inferred)'. "
            "Changed beliefs: '- ~~<old>~~ -> <new> [<source IDs>]'. "
            "Hypotheses: '- Hypothesis (low|med|high): <claim> [<IDs>]. Confirm by: <what would settle it>'. "
            "Open questions and todos live in /memories/open.md as '- [ ] ...', closed as '- [x] ... [<source>]'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "enum": ["view", "search", "write", "edit"]},
                "path": {"type": "string", "description": "e.g. /memories/vendors/alder.md"},
                "content": {"type": "string", "description": "write: full file content; empty string deletes"},
                "old": {"type": "string", "description": "edit: exact text to replace (must occur once)"},
                "new": {"type": "string", "description": "edit: replacement text"},
                "query": {"type": "string", "description": "search: words or an ID"},
                "as_of": {"type": "string", "description": "view: ISO datetime; read the version at that time"},
                "reason": {"type": "string", "description": "write/edit: why, with source IDs"},
            },
            "required": ["command"],
        },
    },
}


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def init() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    if not (ROOT / ".git").exists():
        _git("init", "-q")
        _git("config", "user.name", "ops-agent")
        _git("config", "user.email", "ops-agent@localhost")
        (ROOT / "open.md").write_text("# Open questions and todos\nsummary: unresolved questions and follow-ups\n")
        _git("add", "-A")
        _git("commit", "-qm", "init memory")


def _resolve(path: str) -> Path:
    if not path or not (path == PREFIX or path.startswith(PREFIX + "/")):
        raise ValueError(f"path must be under {PREFIX}")
    p = (ROOT / path[len(PREFIX):].lstrip("/")).resolve()
    if p != ROOT.resolve() and ROOT.resolve() not in p.parents:
        raise ValueError(f"path must be under {PREFIX}")
    return p


def _rel(p: Path) -> str:
    return str(p.relative_to(ROOT))


def _commit(p: Path, reason: str) -> str:
    _git("add", "-A", "--", _rel(p))
    _git("commit", "-qm", reason, "--allow-empty")
    return _git("rev-parse", "--short", "HEAD").strip()


def index() -> str:
    lines = []
    for f in sorted(ROOT.rglob("*.md")):
        summary = next((ln[8:].strip() for ln in f.read_text().splitlines() if ln.startswith("summary:")), "")
        changed = _git("log", "-1", "--format=%cs", "--", _rel(f)).strip()
        lines.append(f"{PREFIX}/{_rel(f)} ({changed}): {summary}")
    return "\n".join(lines) or "(empty)"


def view(path: str, as_of: str | None = None) -> str:
    p = _resolve(path)
    if as_of:
        rev = _git("rev-list", "-1", f"--before={as_of}", "HEAD").strip()
        if not rev:
            return f"error: no memory existed before {as_of}"
        try:
            text = _git("show", f"{rev}:{_rel(p)}")
        except subprocess.CalledProcessError:
            return f"error: {path} did not exist at {as_of}"
    elif p.is_dir():
        return index() if p == ROOT.resolve() else "\n".join(
            ln for ln in index().splitlines() if ln.startswith(path.rstrip("/") + "/"))
    elif p.exists():
        text = p.read_text()
    else:
        return f"error: {path} not found"
    return "\n".join(f"{i:4} {ln}" for i, ln in enumerate(text.splitlines(), 1))


def search(query: str) -> str:
    terms = query.lower().split()
    files = sorted(ROOT.rglob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)
    hits = []
    for f in files:
        lines = f.read_text().splitlines()
        for i, ln in enumerate(lines):
            if all(t in ln.lower() for t in terms):
                ctx = lines[max(0, i - 1): i + 2]
                hits.append(f"{PREFIX}/{_rel(f)}:{i + 1}\n  " + "\n  ".join(ctx))
                if len(hits) == 20:
                    return "\n".join(hits)
    return "\n".join(hits) or "no matches"


def write(path: str, content: str, reason: str) -> str:
    p = _resolve(path)
    if p.suffix != ".md":
        return "error: memory files must end in .md"
    if not content:
        if not p.exists():
            return f"error: {path} not found"
        p.unlink()
        return f"deleted: {path} (commit {_commit(p, reason)})"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content if content.endswith("\n") else content + "\n")
    return f"written: {path} (commit {_commit(p, reason)})"


def edit(path: str, old: str, new: str, reason: str) -> str:
    p = _resolve(path)
    if not p.exists():
        return f"error: {path} not found"
    text = p.read_text()
    n = text.count(old)
    if n != 1:
        close = difflib.get_close_matches(old, text.splitlines(), n=1, cutoff=0.4)
        hint = f" Closest line: {close[0]!r}" if close else ""
        return f"error: 'old' occurs {n} times in {path}; it must occur exactly once.{hint}"
    p.write_text(text.replace(old, new))
    sha = _commit(p, reason)
    diff = "\n".join(difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0))
    return f"edited: {path} (commit {sha})\n{diff}"


def run(args: dict) -> str:
    cmd = args.get("command")
    try:
        if cmd == "view":
            return view(args.get("path", PREFIX), args.get("as_of"))
        if cmd == "search":
            return search(args["query"])
        # A missing reason shouldn't lose the write; commit it and nudge instead.
        reason = args.get("reason") or "(no reason given)"
        note = "" if args.get("reason") else "\nwarning: no 'reason' given; include one with source IDs next time"
        if cmd == "write":
            return write(args["path"], args.get("content", ""), reason) + note
        if cmd == "edit":
            return edit(args["path"], args["old"], args["new"], reason) + note
        return f"error: unknown command {cmd!r}"
    except KeyError as e:
        return f"error: missing argument {e}"
    except ValueError as e:
        return f"error: {e}"
