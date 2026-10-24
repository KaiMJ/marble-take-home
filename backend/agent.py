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

DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
BOLD = "\033[1m"
RESET = "\033[0m"


def _trace(text: str, color: str = "", max_lines: int = 30) -> None:
    """Live view of the agent's work, on stderr so stdout stays the clean answer."""
    if not config.TRACE:
        return
    lines = text.rstrip().splitlines()
    if len(lines) > max_lines:
        hidden = len(lines) - max_lines
        lines = lines[:max_lines] + [f"... ({hidden} more lines)"]
    print(color + "\n".join(lines) + RESET, file=sys.stderr, flush=True)


def _trace_call(name: str, args: dict) -> None:
    if name == "python":
        code = "\n".join("    " + line for line in args.get("code", "").splitlines())
        _trace(f"▶ python\n{code}", CYAN)
        return
    if name == "memory":
        _trace_memory_call(args)
        return
    _trace(f"▶ {name} {args}", CYAN)


def _trace_memory_call(args: dict) -> None:
    target = args.get("path") or args.get("query") or ""
    why = memory.reason(args)
    reason = f"  — {why}" if why else ""
    command = args.get("command")
    _trace(f"▶ memory {command} {target}{reason}", CYAN)
    if command == "edit":
        _trace(f"    - {args.get('old', '')}\n    + {args.get('new', '')}", CYAN, max_lines=10)
    elif command == "write":
        content = args.get("content", "").replace("\n", "\n    ")
        _trace("    " + content, CYAN, max_lines=10)


def _is_error(output: str) -> bool:
    return output.startswith("ERROR") or "<error>" in output


def _completion_cost(response) -> float | None:
    """Dollar cost of one completion, or None when litellm has no price for the model."""
    try:
        return litellm.completion_cost(completion_response=response)
    except Exception:
        return None


class Usage:
    """Running token and dollar totals. Cost stays None once any call can't be priced."""

    def __init__(self) -> None:
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cost: float | None = 0.0

    def add(self, prompt_tokens: int, completion_tokens: int, cost: float | None) -> None:
        self.calls += 1
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        if cost is None or self.cost is None:
            self.cost = None
        else:
            self.cost += cost

    def __str__(self) -> str:
        cost = f"${self.cost:.4f}" if self.cost is not None else "cost unknown"
        return f"{self.calls} calls, {self.prompt_tokens:,} in / {self.completion_tokens:,} out tokens, {cost}"


def _memory_block() -> str:
    text = (config.MEMORY_DIR / memory.MAIN).read_text()
    return f"MEMORY (memory.md)\n{text}"


def _citation_fix_request(problems: list[str]) -> str:
    listed = "\n- ".join(problems)
    return (
        "[automatic check, not from the user] These citations don't resolve:\n- "
        f"{listed}\n"
        "Fix them, then repeat your previous answer in full with only those citations changed."
    )


def _assistant_record(message) -> dict:
    record = {"role": "assistant", "content": message.content}
    if not message.tool_calls:
        return record
    record["tool_calls"] = [
        {
            "id": call.id,
            "type": "function",
            "function": {"name": call.function.name, "arguments": call.function.arguments},
        }
        for call in message.tool_calls
    ]
    return record


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
        self.include_snapshot = snapshot
        self.messages: list[dict] = []
        self.memory_changes: list[str] = []
        self.session_usage = Usage()
        self.turn_usage = Usage()

    def __enter__(self) -> "Agent":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.workspace.close()

    def turn(self, user_text: str) -> str:
        """Answer one user message. Returns the reply plus the memory changes the tool confirmed."""
        self.memory_changes = []
        self.turn_usage = Usage()
        self._append({"role": "user", "content": user_text})

        answer = self._repair_citations(self._complete())
        self._print_usage()
        changes = "\n".join(f"  - {change}" for change in self.memory_changes) or "  (none)"
        return f"{answer}\n\n[memory changes]\n{changes}"

    def _repair_citations(self, answer: str) -> str:
        for _ in range(CITATION_RETRIES):
            problems = self._citation_problems(answer)
            if not problems:
                return answer
            _trace("citation check failed:\n" + "\n".join(problems), RED)
            self._append({"role": "user", "content": _citation_fix_request(problems)})
            answer = self._complete()

        problems = self._citation_problems(answer)
        if not problems:
            return answer
        return answer + "\n\n[warning] Unverified citations:\n- " + "\n- ".join(problems)

    def _complete(self) -> str:
        for step in range(1, config.MAX_STEPS + 1):
            _trace(f"\n── step {step} " + "─" * 50, BOLD)
            response = self._completion()
            self._record_usage(response)
            message = response.choices[0].message
            self._trace_model_message(message)
            self._append(_assistant_record(message))
            if not message.tool_calls:
                return message.content or ""
            self._run_tool_calls(message.tool_calls)

        _trace(f"stopped after {config.MAX_STEPS} steps without a final answer", RED)
        return "(stopped: step limit reached before a final answer)"

    def _completion(self):
        # Rebuilt each step: memory and its data overrides may have changed during the turn.
        return litellm.completion(
            model=config.LLM_MODEL,
            reasoning_effort=config.LLM_REASONING_EFFORT,
            messages=[{"role": "system", "content": self._system_prompt()}, *self.messages],
            tools=[workspace.SCHEMA, memory.SCHEMA],
        )

    def _system_prompt(self) -> str:
        current = snapshot.build() if self.include_snapshot else ""
        return prompts.build(self.mode, now=self.now, schema=self.schema,
                             memory=_memory_block(), snapshot=current)

    def _trace_model_message(self, message) -> None:
        reasoning = getattr(message, "reasoning_content", None)
        if reasoning:
            _trace("thinking: " + reasoning, DIM, max_lines=15)
        if message.content and message.tool_calls:
            _trace(message.content, YELLOW)

    def _run_tool_calls(self, calls) -> None:
        for call in calls:
            output = self._run_call(call)
            color = RED if _is_error(output) else GREEN
            _trace("◀ " + output, color, max_lines=20)
            self._append({"role": "tool", "tool_call_id": call.id, "content": output})

    def _citation_problems(self, answer: str) -> list[str]:
        tables = set(snapshot.TABLES) if self.include_snapshot else set()
        return checks.citation_problems(answer, tables)

    def _run_call(self, call) -> str:
        try:
            args = json.loads(call.function.arguments or "{}")
        except json.JSONDecodeError as exc:
            _trace(f"▶ {call.function.name} (bad arguments)", RED)
            return f"ERROR: tool arguments were not valid JSON ({exc}).\nFix: send the arguments as a JSON object."
        _trace_call(call.function.name, args)
        return self._call_tool(call.function.name, args)

    def _call_tool(self, name: str, args: dict) -> str:
        if name == "python":
            return self._python(args)
        if name == "memory":
            return self._memory(args)
        return f"ERROR: unknown tool {name!r}.\nFix: use python or memory."

    def _python(self, args: dict) -> str:
        output = self.workspace.run(args.get("code", ""))
        self._log("python", {"code": args.get("code"), "output": output})
        return output

    def _memory(self, args: dict) -> str:
        output = memory.run(args)
        self._log("memory", {"args": args, "output": output})
        if output.startswith("OK "):
            self.memory_changes.append(f"{output.splitlines()[0]}: {memory.reason(args) or ''}")
        return output

    def _record_usage(self, response) -> None:
        usage = response.usage
        prompt_tokens = usage.prompt_tokens or 0
        completion_tokens = usage.completion_tokens or 0
        cost = _completion_cost(response)
        self.session_usage.add(prompt_tokens, completion_tokens, cost)
        self.turn_usage.add(prompt_tokens, completion_tokens, cost)
        self._log("usage", {
            "model": config.LLM_MODEL,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cost": cost,
        })

    def _print_usage(self) -> None:
        print(
            f"{DIM}[llm usage] this turn: {self.turn_usage}; session: {self.session_usage}{RESET}",
            file=sys.stderr,
            flush=True,
        )

    def _append(self, message: dict) -> None:
        self.messages.append(message)
        self._log("transcript", message)

    def _log(self, name: str, record: dict) -> None:
        with open(self.log_dir / f"{name}.jsonl", "a") as handle:
            handle.write(json.dumps(record) + "\n")
