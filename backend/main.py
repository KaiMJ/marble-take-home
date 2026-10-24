"""CLI: load the packet, replay the shift log, chat, and write the morning brief."""

import argparse
import shutil
import sqlite3

import config
import db
import memory
from agent import Agent

NOW_ISO = "2026-10-05 06:00"


def cmd_load(reset_memory: bool) -> None:
    db.build()
    if reset_memory and config.MEMORY_DIR.exists():
        shutil.rmtree(config.MEMORY_DIR)
    memory.init()
    for line in memory.sync_checks("initial load").splitlines():
        print(line)
    print(f"loaded {config.DB_PATH}; memory at {config.MEMORY_DIR}")


def cmd_ingest_log() -> None:
    """Replay the shift notebook in order, one note per chat turn, as if the GM sent each one."""
    with sqlite3.connect(config.DB_PATH) as con:
        notes = con.execute(
            "SELECT note_id, written_at, author, text FROM notes WHERE source = 'log' ORDER BY written_at"
        ).fetchall()
    for note_id, written_at, author, text in notes:
        # Replay as of the note's time: later notes are hidden so the agent can't see the future.
        agent = Agent("chat", now=f"{written_at} America/New_York", notes_until=written_at)
        reply = agent.turn(f"[{note_id} | {written_at} | {author}] Shift note:\n{text}")
        agent.close()
        print(f"--- {note_id}\n{reply}\n")


def cmd_chat() -> None:
    agent = Agent("chat")
    print("Ask a question or tell me something new. Ctrl-D to exit.")
    try:
        while True:
            text = input("\n> ").strip()
            if not text:
                continue
            note_id = db.add_chat_note(text, NOW_ISO)
            print("\n" + agent.turn(f"[{note_id}] {text}"))
    except (EOFError, KeyboardInterrupt):
        print()
    finally:
        agent.close()


def cmd_brief() -> None:
    with sqlite3.connect(config.DB_PATH) as con:
        recent = con.execute(
            "SELECT note_id, written_at, author, text FROM notes WHERE written_at >= '2026-09-28' ORDER BY written_at"
        ).fetchall()
    notes = "\n".join(f"[{n} | {w} | {a}] {t}" for n, w, a, t in recent)
    agent = Agent("brief")
    brief = agent.turn(f"Write this morning's brief.\n\nNotes from the past week:\n{notes}")
    agent.close()
    memory.write("/memories/briefs/2026-10-05.md",
                 f"# Brief 2026-10-05\nsummary: morning brief for {config.NOW}\n\n{brief}",
                 "morning brief 2026-10-05")
    print(brief)


def main() -> None:
    parser = argparse.ArgumentParser(description="Operations agent for Lantern Yard Kitchen")
    sub = parser.add_subparsers(dest="cmd", required=True)
    load = sub.add_parser("load", help="build the database from data/ and docs/")
    load.add_argument("--reset-memory", action="store_true")
    sub.add_parser("ingest-log", help="replay GM_OPS_LOG.md notes into memory")
    sub.add_parser("chat", help="interactive questions and updates")
    sub.add_parser("brief", help="write the morning brief")
    args = parser.parse_args()

    if args.cmd == "load":
        cmd_load(args.reset_memory)
    elif args.cmd == "ingest-log":
        cmd_ingest_log()
    elif args.cmd == "chat":
        cmd_chat()
    elif args.cmd == "brief":
        cmd_brief()


if __name__ == "__main__":
    main()
