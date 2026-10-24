"""Agent loop: assemble the prompt, run tool calls, check citations, log everything."""

import json
import re
import sys
import time

import litellm

import config
import db
import memory
import prompts
import workspace

ID_PATTERN = re.compile(
    r"\b(INV-\d{3}(?:-L\d{2})?|POS-\d{2}-M\d{2}|COUNT-(?:OPEN|CLOSE)-[A-Z]+|REC-M\d{2}-[A-Z]+"
    r"|CAT-[A-Z0-9]+|[NU]\d{2}|M\d{2}|V\d)\b"
)

# (pattern, problem message); {} is filled with the offending matches.
CITATION_CHECKS = [
    (re.compile(r"\[(?:sales|purchases|invoices|counts|recipes|menu|vendors|catalog|notes)(?:[ ,][^\[\]]*)?\]", re.I),
     "These cite a table, not a record: {}. Cite the row IDs (e.g. POS-21-M05, INV-011-L02, COUNT-CLOSE-EGG)."),
    (re.compile(r"\((?:[^()]*\b(?:query|queries|snapshot|table|data)\b[^()]*)\)", re.I),
     "These name a source vaguely instead of citing rows: {}. Replace them with row IDs in square brackets."),
]

DIM, CYAN, GREEN, RED, YELLOW, BOLD, RESET = "\033[2m", "\033[36m", "\033[32m", "\033[31m", "\033[33m", "\033[1m", "\033[0m"

def _trace(text: str, color: str = "", max_lines: int = 30) -> None:
    """Live view of the agent's work, on stderr so stdout stays the clean answer."""
    if not config.TRACE:
        return
    lines = text.rstrip().splitlines()
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"... ({len(lines) - max_lines} more lines)"]
    print(color + "\n".join(lines) + RESET, file=sys.stderr, flush=True)

def _trace_call(name: str, args: dict) -> None:
    if name == "python":
        _trace("▶ python\n" + "\n".join("    " + ln for ln in args.get("code", "").splitlines()), CYAN)
        return
    if name != "memory":
        _trace(f"▶ {name} {args}", CYAN)
        return
    target = args.get("path") or args.get("query") or ""
    reason = f"  — {r}" if (r := memory.reason(args)) else ""
    _trace(f"▶ memory {args.get('command')} {target}{reason}", CYAN)
    if args.get("command") == "edit":
        _trace(f"    - {args.get('old', '')}\n    + {args.get('new', '')}", CYAN, max_lines=10)
    elif args.get("command") == "write":
        _trace("    " + args.get("content", "").replace("\n", "\n    "), CYAN, max_lines=10)

def _is_error(out: str) -> bool:
    return out.startswith("ERROR") or "<error>" in out

def _citation_problems(answer: str) -> str:
    problems = []
    if unknown := sorted(set(ID_PATTERN.findall(answer)) - db.known_ids()):
        problems.append(f"These IDs don't exist in the records: {', '.join(unknown)}.")
    for pattern, message in CITATION_CHECKS:
        if hits := sorted(set(pattern.findall(answer))):
            problems.append(message.format(", ".join(hits)))
    return " ".join(problems)

def memory_block() -> str:
    open_items = (config.MEMORY_DIR / "open.md").read_text()
    data = (config.MEMORY_DIR / "data.md").read_text()
    return (f"MEMORY INDEX\n{memory.index()}\n\nOPEN (/memories/open.md)\n{open_items}\n"
            f"DATA (/memories/data.md)\n{data}")

class Agent:
    def __init__(self, mode: str, session: str | None = None, now: str = config.NOW,
                 notes_until: str | None = None) -> None:
        self.mode = mode
        self.now = now
        self.log_dir = config.RUNS_DIR / (session or time.strftime("%Y%m%d-%H%M%S"))
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.workspace = workspace.Workspace(notes_until)
        self.schema = workspace.manifest()
        self.messages: list[dict] = []
        self.memory_changes: list[str] = []

    def __enter__(self) -> "Agent":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.workspace.close()

    def _log(self, name: str, record: dict) -> None:
        with open(self.log_dir / f"{name}.jsonl", "a") as f:
            f.write(json.dumps(record) + "\n")

    def _append(self, message: dict) -> None:
        self.messages.append(message)
        self._log("transcript", message)

    def _python(self, args: dict) -> str:
        out = self.workspace.run(args.get("code", ""))
        self._log("python", {"code": args.get("code"), "output": out})
        return out

    def _memory(self, args: dict) -> str:
        out = memory.run(args)
        self._log("memory", {"args": args, "output": out})
        if out.startswith("OK "):
            self.memory_changes.append(f"{out.splitlines()[0]}: {memory.reason(args) or ''}")
        return out

    def _call_tool(self, name: str, args: dict) -> str:
        tools = {"python": self._python, "memory": self._memory}
        if name not in tools:
            return f"ERROR: unknown tool {name!r}.\nFix: use python or memory."
        return tools[name](args)

    def _run_call(self, call) -> str:
        try:
            args = json.loads(call.function.arguments or "{}")
        except json.JSONDecodeError as e:
            _trace(f"▶ {call.function.name} (bad arguments)", RED)
            return f"ERROR: tool arguments were not valid JSON ({e}).\nFix: send the arguments as a JSON object."
        _trace_call(call.function.name, args)
        return self._call_tool(call.function.name, args)

    def _complete(self) -> str:
        for step in range(1, config.MAX_STEPS + 1):
            _trace(f"\n── step {step} " + "─" * 50, BOLD)
            system = prompts.build(self.mode, now=self.now, schema=self.schema, memory=memory_block())
            msg = litellm.completion(
                model=config.LLM_MODEL,
                reasoning_effort=config.LLM_REASONING_EFFORT,
                messages=[{"role": "system", "content": system}, *self.messages],
                tools=[workspace.SCHEMA, memory.SCHEMA],
            ).choices[0].message
            calls = msg.tool_calls or []
            if reasoning := getattr(msg, "reasoning_content", None):
                _trace("thinking: " + reasoning, DIM, max_lines=15)
            if msg.content and calls:
                _trace(msg.content, YELLOW)
            assistant = {"role": "assistant", "content": msg.content}
            if calls:
                assistant["tool_calls"] = [
                    {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}}
                    for c in calls
                ]
            self._append(assistant)
            if not calls:
                return msg.content or ""
            for c in calls:
                out = self._run_call(c)
                _trace("◀ " + out, RED if _is_error(out) else GREEN, max_lines=20)
                self._append({"role": "tool", "tool_call_id": c.id, "content": out})
        _trace(f"stopped after {config.MAX_STEPS} steps without a final answer", RED)
        return "(stopped: step limit reached before a final answer)"

    def turn(self, user_text: str) -> str:
        """Answer one user message. Returns the reply plus the memory changes the tool confirmed."""
        self.memory_changes = []
        self._append({"role": "user", "content": user_text})
        answer = self._complete()
        if problems := _citation_problems(answer):
            _trace(f"citation check failed: {problems}", RED)
            self.messages.append({"role": "user", "content": (
                f"[citation check] {problems} Fix the citations and give the full answer again."
            )})
            answer = self._complete()
        changes = "\n".join(f"  - {c}" for c in self.memory_changes) or "  (none)"
        return f"{answer}\n\n[memory changes]\n{changes}"
