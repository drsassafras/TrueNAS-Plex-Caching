"""The link's API as recorded: tests/peer_api.json holds every command of the link between two Dionysus instances
(plexcache/peerlink.py: COMMANDS, API_SHAPES) with its version and the fields of its request and its answer.
test_the_link_api_is_versioned compares the code with the record, so a command whose shape changes needs its
version raised — and then this run, to record the new shape:

    python3 dev/peer_api.py
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from plexcache import peerlink  # noqa: E402

PATH = os.path.join(ROOT, "tests", "peer_api.json")


def record() -> dict:
    """The link's API, as the test checks it: its number, and each command's version and shape."""
    return {"api": peerlink.API, "commands": {c: {"version": peerlink.COMMANDS[c], **peerlink.API_SHAPES[c]} for c in sorted(peerlink.COMMANDS)}}


def main() -> None:
    with open(PATH, "w", encoding="utf-8") as fh:
        json.dump(record(), fh, indent=2, sort_keys=True)
        fh.write("\n")
    print(f"recorded {len(peerlink.COMMANDS)} link commands (API {peerlink.API}) in {os.path.relpath(PATH, ROOT)}")


if __name__ == "__main__":
    main()
