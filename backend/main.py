"""CLI: load the packet, replay the shift log, chat, and write the morning brief."""

import argparse
import shutil

import config
import db
import memory
from agent import Agent

def _notes(where: str) -> list[tuple]:
    with db.connect() as con:
        return con.execute(
            f"SELECT note_id, written_at, author, text FROM notes WHERE {where} ORDER BY written_at"
        ).fetchall()

def cmd_load(args: argparse.Namespace) -> None:
    db.build()
    if args.reset_memory and config.MEMORY_DIR.exists():
        shutil.rmtree(config.MEMORY_DIR)
    memory.init()
    if changes := memory.sync_checks("initial load"):
        print(changes)
    print(f"loaded {config.DB_PATH}; memory at {config.MEMORY_DIR}")

def cmd_ingest_log(args: argparse.Namespace) -> None:
    """Replay the shift notebook in order, one note per chat turn, as if the GM sent each one."""
    for note_id, written_at, author, text in _notes("source = 'log'"):
        with Agent("chat", now=f"{written_at} America/New_York", notes_until=written_at) as agent:
            reply = agent.turn(f"[{note_id} | {written_at} | {author}] Shift note:\n{text}")
        print(f"--- {note_id}\n{reply}\n")

def cmd_chat(args: argparse.Namespace) -> None:
    print("Ask a question or tell me something new. Ctrl-D to exit.")
    with Agent("chat", snapshot=True) as agent:
        try:
            while True:
                if not (text := input("\n> ").strip()):
                    continue
                try:
                    note_id = db.add_chat_note(text, config.NOW_ISO)
                    print("\n" + agent.turn(f"[{note_id}] {text}"))
                except Exception as e:  # one bad turn shouldn't end the session
                    print(f"\n(error: {type(e).__name__}: {e}; ask again or try another question)")
        except (EOFError, KeyboardInterrupt):
            print()

def cmd_brief(args: argparse.Namespace) -> None:
    notes = "\n".join(f"[{n} | {w} | {a}] {t}" for n, w, a, t in _notes("written_at >= '2026-09-28'"))
    with Agent("brief", snapshot=True) as agent:
        brief = agent.turn(f"Write this morning's brief.\n\nNotes from the past week:\n{notes}")
    memory.write("briefs/2026-10-05.md",
                 f"# Brief 2026-10-05\nsummary: morning brief for {config.NOW}\n\n{brief}",
                 "morning brief 2026-10-05")
    print(brief)

def main() -> None:
    parser = argparse.ArgumentParser(description="Operations agent for Lantern Yard Kitchen")
    sub = parser.add_subparsers(dest="cmd", required=True)
    load = sub.add_parser("load", help="build the database from data/ and docs/")
    load.add_argument("--reset-memory", action="store_true")
    load.set_defaults(func=cmd_load)
    sub.add_parser("ingest-log", help="replay GM_OPS_LOG.md notes into memory").set_defaults(func=cmd_ingest_log)
    sub.add_parser("chat", help="interactive questions and updates").set_defaults(func=cmd_chat)
    sub.add_parser("brief", help="write the morning brief").set_defaults(func=cmd_brief)
    args = parser.parse_args()
    args.func(args)

if __name__ == "__main__":
    main()
