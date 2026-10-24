"""Agent loop: assemble the prompt, run tool calls, check citations, log everything."""

import json
import re
import sys
import time
from pathlib import Path

import litellm

import config
import db
import memory
import prompts
from workspace import SCHEMA as PYTHON_SCHEMA
from workspace import Workspace, manifest

ID_PATTERN = re.compile(
    r"\b(INV-\d{3}(?:-L\d{2})?|POS-\d{2}-M\d{2}|COUNT-(?:OPEN|CLOSE)-[A-Z]+|REC-M\d{2}-[A-Z]+"
    r"|CAT-[A-Z0-9]+|[NU]\d{2}|M\d{2}|V\d)\b"
)


DIM, CYAN, GREEN, RED, YELLOW, BOLD, RESET = "\033[2m", "\033[36m", "\033[32m", "\033[31m", "\033[33m", "\033[1m", "\033[0m"


def _trace(text: str, color: str = "", max_lines: int = 30) -> None:
    """Live view of the agent's work, on stderr so stdout stays the clean answer."""
    if not config.TRACE:
        return
    lines = text.rstrip().splitlines()
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"... ({len(lines) - max_lines} more lines)"]
    print(color + "\n".join(lines) + RESET, file=sys.stderr, flush=True)


def _is_error(out: str) -> bool:
    return out.startswith("ERROR") or "<error>" in out


def memory_block() -> str:
    open_items = (config.MEMORY_DIR / "open.md").read_text()
    return f"MEMORY INDEX\n{memory.index()}\n\nOPEN (/memories/open.md)\n{open_items}"


class Agent:
    def __init__(self, mode: str, session: str | None = None, now: str = config.NOW,
                 notes_until: str | None = None) -> None:
        self.mode = mode
        self.now = now
        self.session = session or time.strftime("%Y%m%d-%H%M%S")
        self.log_dir: Path = config.RUNS_DIR / self.session
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.workspace = Workspace(notes_until)
        self.schema = manifest()
        self.messages: list[dict] = []
        self.memory_changes: list[str] = []

    def _system(self) -> dict:
        # Rebuilt every turn so the memory index reflects the latest writes.
        content = prompts.build(self.mode, now=self.now, schema=self.schema, memory=memory_block())
        return {"role": "system", "content": content}

    def _log(self, name: str, record: dict) -> None:
        with open(self.log_dir / f"{name}.jsonl", "a") as f:
            f.write(json.dumps(record) + "\n")

    def _call_tool(self, name: str, args: dict) -> str:
        if name == "python":
            out = self.workspace.run(args.get("code", ""))
            self._log("python", {"code": args.get("code"), "output": out})
            return out
        if name == "memory":
            out = memory.run(args)
            self._log("memory", {"args": args, "output": out})
            if out.startswith("OK "):
                self.memory_changes.append(f"{out.splitlines()[0]}: {args.get('reason', '')}")
            return out
        return f"ERROR: unknown tool {name!r}.\nFix: use python or memory."

    def _complete(self) -> str:
        for step in range(1, config.MAX_STEPS + 1):
            _trace(f"\n── step {step} " + "─" * 50, BOLD)
            resp = litellm.completion(
                model=config.LLM_MODEL,
                reasoning_effort=config.LLM_REASONING_EFFORT,
                messages=[self._system(), *self.messages],
                tools=[PYTHON_SCHEMA, memory.SCHEMA],
            )
            msg = resp.choices[0].message
            calls = msg.tool_calls or []
            reasoning = getattr(msg, "reasoning_content", None)
            if reasoning:
                _trace("thinking: " + reasoning, DIM, max_lines=15)
            if msg.content and calls:
                _trace(msg.content, YELLOW)
            self.messages.append({
                "role": "assistant",
                "content": msg.content,
                **({"tool_calls": [
                    {"id": c.id, "type": "function",
                     "function": {"name": c.function.name, "arguments": c.function.arguments}}
                    for c in calls
                ]} if calls else {}),
            })
            self._log("transcript", self.messages[-1])
            if not calls:
                return msg.content or ""
            for c in calls:
                try:
                    args = json.loads(c.function.arguments or "{}")
                except json.JSONDecodeError as e:
                    out = f"ERROR: tool arguments were not valid JSON ({e}).\nFix: send the arguments as a JSON object."
                    _trace(f"▶ {c.function.name} (bad arguments)", RED)
                else:
                    _trace_call(c.function.name, args)
                    out = self._call_tool(c.function.name, args)
                _trace("◀ " + out, RED if _is_error(out) else GREEN, max_lines=20)
                self.messages.append({"role": "tool", "tool_call_id": c.id, "content": out})
                self._log("transcript", self.messages[-1])
        _trace(f"stopped after {config.MAX_STEPS} steps without a final answer", RED)
        return "(stopped: step limit reached before a final answer)"

    def turn(self, user_text: str) -> str:
        """Answer one user message. Returns the reply plus the memory changes the tool confirmed."""
        self.memory_changes = []
        self.messages.append({"role": "user", "content": user_text})
        self._log("transcript", self.messages[-1])
        answer = self._complete()
        unknown = sorted(set(ID_PATTERN.findall(answer)) - db.known_ids())
        if unknown:
            _trace(f"citation check failed: {', '.join(unknown)}", RED)
            self.messages.append({"role": "user", "content": (
                f"[citation check] These IDs don't exist in the records: {', '.join(unknown)}. "
                "Correct or remove them and give the full answer again."
            )})
            answer = self._complete()
        changes = "\n".join(f"  - {c}" for c in self.memory_changes) or "  (none)"
        return f"{answer}\n\n[memory changes]\n{changes}"

    def close(self) -> None:
        self.workspace.close()


def _trace_call(name: str, args: dict) -> None:
    if name == "python":
        _trace("▶ python\n" + "\n".join("    " + ln for ln in args.get("code", "").splitlines()), CYAN)
    elif name == "memory":
        target = args.get("path") or args.get("query") or ""
        reason = f"  — {args['reason']}" if args.get("reason") else ""
        _trace(f"▶ memory {args.get('command')} {target}{reason}", CYAN)
        if args.get("command") == "edit":
            _trace(f"    - {args.get('old', '')}\n    + {args.get('new', '')}", CYAN, max_lines=10)
        elif args.get("command") == "write":
            _trace("    " + args.get("content", "").replace("\n", "\n    "), CYAN, max_lines=10)
    else:
        _trace(f"▶ {name} {args}", CYAN)
