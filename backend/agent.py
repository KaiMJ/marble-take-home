"""Agent loop: assemble the prompt, run tool calls, check citations, log everything."""

import json
import sys
import time

import litellm

import checks
import config
import memory
import prompts
import snapshot
import workspace

CITATION_RETRIES = 2

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

def _cost(response) -> float | None:
    """Dollar cost of one completion, or None when litellm has no price for the model."""
    try:
        return litellm.completion_cost(completion_response=response)
    except Exception:
        return None

class Usage:
    """Running token and dollar totals; cost stays None once any call can't be priced."""

    def __init__(self) -> None:
        self.calls = self.prompt_tokens = self.completion_tokens = 0
        self.cost: float | None = 0.0

    def add(self, prompt_tokens: int, completion_tokens: int, cost: float | None) -> None:
        self.calls += 1
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        self.cost = None if cost is None or self.cost is None else self.cost + cost

    def __str__(self) -> str:
        cost = f"${self.cost:.4f}" if self.cost is not None else "cost unknown"
        return f"{self.calls} calls, {self.prompt_tokens:,} in / {self.completion_tokens:,} out tokens, {cost}"

def memory_block() -> str:
    return f"MEMORY (memory.md)\n{(config.MEMORY_DIR / memory.MAIN).read_text()}"

class Agent:
    def __init__(self, mode: str, session: str | None = None, now: str = config.NOW,
                 notes_until: str | None = None, snapshot: bool = False) -> None:
        self.mode = mode
        self.now = now
        self.log_dir = config.RUNS_DIR / (session or time.strftime("%Y%m%d-%H%M%S"))
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.workspace = workspace.Workspace(notes_until)
        self.schema = workspace.manifest()
        # Log replay runs in the past, so it must not see the snapshot (closing counts, later invoices).
        self.with_snapshot = snapshot
        self.messages: list[dict] = []
        self.memory_changes: list[str] = []
        self.usage = Usage()  # whole session
        self.turn_usage = Usage()

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

    def _citation_problems(self, answer: str) -> list[str]:
        return checks.citation_problems(answer, set(snapshot.TABLES) if self.with_snapshot else set())

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

    def _record_usage(self, response) -> None:
        u = response.usage
        prompt_tokens, completion_tokens = u.prompt_tokens or 0, u.completion_tokens or 0
        cost = _cost(response)
        for usage in (self.usage, self.turn_usage):
            usage.add(prompt_tokens, completion_tokens, cost)
        self._log("usage", {"model": config.LLM_MODEL, "prompt_tokens": prompt_tokens,
                            "completion_tokens": completion_tokens, "cost": cost})

    def _complete(self) -> str:
        for step in range(1, config.MAX_STEPS + 1):
            _trace(f"\n── step {step} " + "─" * 50, BOLD)
            # Rebuilt each step: memory and its data overrides may have changed during the turn.
            system = prompts.build(self.mode, now=self.now, schema=self.schema, memory=memory_block(),
                                   snapshot=snapshot.build() if self.with_snapshot else "")
            response = litellm.completion(
                model=config.LLM_MODEL,
                reasoning_effort=config.LLM_REASONING_EFFORT,
                messages=[{"role": "system", "content": system}, *self.messages],
                tools=[workspace.SCHEMA, memory.SCHEMA],
            )
            self._record_usage(response)
            msg = response.choices[0].message
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
        self.turn_usage = Usage()
        self._append({"role": "user", "content": user_text})
        answer = self._complete()
        for _ in range(CITATION_RETRIES):
            if not (problems := self._citation_problems(answer)):
                break
            _trace("citation check failed:\n" + "\n".join(problems), RED)
            self._append({"role": "user", "content": (
                "[automatic check, not from the user] These citations don't resolve:\n- " + "\n- ".join(problems)
                + "\nFix them, then repeat your previous answer in full with only those citations changed."
            )})
            answer = self._complete()
        if problems := self._citation_problems(answer):
            answer += "\n\n[warning] Unverified citations:\n- " + "\n- ".join(problems)
        print(f"{DIM}[llm usage] this turn: {self.turn_usage}; session: {self.usage}{RESET}", file=sys.stderr, flush=True)
        changes = "\n".join(f"  - {c}" for c in self.memory_changes) or "  (none)"
        return f"{answer}\n\n[memory changes]\n{changes}"
