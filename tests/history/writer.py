"""A process that uses the history, for the tests of what two shells do to each other.

    python -m tests.history.writer <directory> <go-file> own <tag> <count>
    python -m tests.history.writer <directory> <go-file> shared <tag> <count> <session id>
    python -m tests.history.writer <directory> <go-file> hold

It waits for `go-file` to exist (so that every process starts at the same moment, with a deadline),
opens the history, and prints one JSON line: `own` takes the last session and appends `count`
messages to it; `shared` appends to a given session without claiming it (two writers in one
session, to test the write path alone); `hold` takes a session and keeps it until it is killed.
`own` and `shared` then keep the history open until `go-file.done` exists, so that the processes
overlap however fast each one is.
"""

import json
import sys
import time
from pathlib import Path

from tests.history.support import Ticks, user
from tumbleweed.core.ids import SessionId
from tumbleweed.history.db import Database
from tumbleweed.history.repository import MessageRepository
from tumbleweed.history.store import HistoryStore

DEADLINE = 30.0


def wait_for(path: Path) -> None:
    deadline = time.monotonic() + DEADLINE
    while not path.exists():
        if time.monotonic() > deadline:
            raise SystemExit("never told to go")
        time.sleep(0.001)


def report(value: dict[str, str]) -> None:
    sys.stdout.write(json.dumps(value) + "\n")
    sys.stdout.flush()


def main(argv: list[str]) -> None:
    directory, go, mode = Path(argv[0]), Path(argv[1]), argv[2]
    wait_for(go)
    if mode == "hold":
        store = HistoryStore.open(directory)
        session = store.begin(limit=10).session.id
        store.append(user("held"))  # an empty session is nobody's last one
        report({"session": str(session)})
        time.sleep(DEADLINE)
        return
    tag, count = argv[3], int(argv[4])
    if mode == "own":
        store = HistoryStore.open(directory)
        session = store.begin(limit=10).session.id
        for index in range(count):
            store.append(user(f"{tag}-{index}"))
        close = store.close
    else:
        session = SessionId(argv[5])
        database = Database.open(directory)
        repository = MessageRepository(database, clock=Ticks(), keep=10_000)
        for index in range(count):
            repository.append(session, user(f"{tag}-{index}"))
        close = database.close
    report({"session": str(session)})
    wait_for(go.with_name(go.name + ".done"))
    close()


if __name__ == "__main__":
    main(sys.argv[1:])
