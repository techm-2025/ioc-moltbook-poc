#!/usr/bin/env python3
"""Command-line entrypoint for the Moltbook <-> Mycelium bridge (replaces the Colab notebook).

    list                      show new (unprocessed) topics in the submolt          [read-only]
    process POST_ID           run one topic end to end
    process POST_ID --no-publish   run the negotiation but write NOTHING to Moltbook
    watch --confirm-public-posting  poll for new topics and process them one at a time

Everything is configured from environment variables (see make_config_from_env in
bridge_service.py); docker-compose.yml supplies them from the shared .env.

`process` and `watch` post PUBLIC comments on Moltbook (kickoff + consensus + 4 agent replies)
unless --no-publish is given.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from bridge_service import MoltbookMyceliumBridge, make_config_from_env


def build_bridge() -> MoltbookMyceliumBridge:
    return MoltbookMyceliumBridge(make_config_from_env())


def cmd_list(bridge: MoltbookMyceliumBridge, args: argparse.Namespace) -> int:
    topics = bridge.find_new_topics(limit=args.limit)
    if not topics:
        print("No new topics.")
        return 0
    for t in topics:
        print(f"{t.get('id')}  {t.get('title') or t.get('name') or ''}")
    return 0


def cmd_process(bridge: MoltbookMyceliumBridge, args: argparse.Namespace) -> int:
    seen = bridge.state.has_seen_topic(args.post_id)
    if seen and not args.force:
        print(f"Topic {args.post_id} was already processed (status: "
              f"{bridge.state.get_topic(args.post_id).get('status')}). Use --force to run it again.")
        return 1
    if args.no_publish or args.force or seen:
        # rerun_topic_by_id is the path that can skip the public Moltbook writes.
        result = bridge.rerun_topic_by_id(
            args.post_id,
            post_kickoff_comment=not args.no_publish,
            post_final_comment=not args.no_publish,
            force=True,
        )
    else:
        result = bridge.process_topic_by_id(args.post_id)
    print(json.dumps({"post_id": args.post_id, "status": result.get("status")}, indent=2))
    return 0 if str(result.get("status", "")).endswith(("final_posted", "complete_not_published")) else 2


def cmd_watch(bridge: MoltbookMyceliumBridge, args: argparse.Namespace) -> int:
    if not args.confirm_public_posting:
        print("watch posts public comments on Moltbook for every new topic. "
              "Re-run with --confirm-public-posting to enable it.", file=sys.stderr)
        return 1
    print(f"Watching every {args.interval}s. Ctrl-C to stop.")
    while True:
        try:
            for topic in bridge.find_new_topics(limit=args.limit):
                print(f"New topic {topic.get('id')}: {topic.get('title') or ''}")
                bridge.process_topic(topic)   # one at a time: the bridge holds a single session id
        except Exception as exc:  # keep watching after a transient failure
            print(f"[watch] error: {exc}", file=sys.stderr)
        time.sleep(args.interval)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="show new topics (read-only)")
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("process", help="run one topic through the negotiation")
    p.add_argument("post_id")
    p.add_argument("--no-publish", action="store_true", help="do not write anything to Moltbook")
    p.add_argument("--force", action="store_true", help="run again even if already processed")

    p = sub.add_parser("watch", help="poll for new topics and process them")
    p.add_argument("--interval", type=int, default=60)
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--confirm-public-posting", action="store_true")

    args = ap.parse_args()
    bridge = build_bridge()
    return {"list": cmd_list, "process": cmd_process, "watch": cmd_watch}[args.cmd](bridge, args)


if __name__ == "__main__":
    sys.exit(main())
