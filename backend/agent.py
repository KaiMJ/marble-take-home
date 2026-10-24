"""Agent loop: assemble the prompt, run tool calls, check citations, log everything."""

import json
import re
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
            if out.startswith(("written:", "edited:", "deleted:")):
                self.memory_changes.append(f"{out.splitlines()[0]}: {args.get('reason', '')}")
            return out
        return f"error: unknown tool {name}"

    def _complete(self) -> str:
        for _ in range(config.MAX_STEPS):
            resp = litellm.completion(
                model=config.LLM_MODEL,
                reasoning_effort=config.LLM_REASONING_EFFORT,
                messages=[self._system(), *self.messages],
                tools=[PYTHON_SCHEMA, memory.SCHEMA],
            )
            msg = resp.choices[0].message
            calls = msg.tool_calls or []
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
                    out = self._call_tool(c.function.name, args)
                except json.JSONDecodeError as e:
                    out = f"error: arguments were not valid JSON ({e})"
                self.messages.append({"role": "tool", "tool_call_id": c.id, "content": out})
                self._log("transcript", self.messages[-1])
        return "(stopped: step limit reached before a final answer)"

    def turn(self, user_text: str) -> str:
        """Answer one user message. Returns the reply plus the memory changes the tool confirmed."""
        self.memory_changes = []
        self.messages.append({"role": "user", "content": user_text})
        self._log("transcript", self.messages[-1])
        answer = self._complete()
        unknown = sorted(set(ID_PATTERN.findall(answer)) - db.known_ids())
        if unknown:
            self.messages.append({"role": "user", "content": (
                f"[citation check] These IDs don't exist in the records: {', '.join(unknown)}. "
                "Correct or remove them and give the full answer again."
            )})
            answer = self._complete()
        changes = "\n".join(f"  - {c}" for c in self.memory_changes) or "  (none)"
        return f"{answer}\n\n[memory changes]\n{changes}"

    def close(self) -> None:
        self.workspace.close()
