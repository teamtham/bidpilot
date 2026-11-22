#!/usr/bin/env python3
"""Dispatch durable BidPilot turn notifications to an authorized adapter.

This is intentionally only a wake/dispatch worker.  The configured adapter is
responsible for invoking QwenWork through an official integration and for
using the existing scoped BidPilot MCP tools.  It receives no conversation
body in the dispatch envelope.
"""
import argparse
import json
from pathlib import Path
import socket
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import operations


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--root", required=True, help="Private BidPilot runtime root")
    result.add_argument("--studio", required=True, help="Studio UUID from onboarding")
    target = result.add_mutually_exclusive_group(required=True)
    target.add_argument("--command-json", help="JSON argv array; executable must be absolute")
    target.add_argument("--url", help="Authorized HTTPS adapter URL (HTTP only on loopback)")
    result.add_argument("--token-file", help="Private bearer-token file for URL dispatch")
    result.add_argument("--timeout", type=float, default=30)
    result.add_argument("--poll-seconds", type=float, default=0.25)
    result.add_argument("--once", action="store_true")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    if not 0.05 <= args.poll_seconds <= 60:
        raise SystemExit("--poll-seconds must be from 0.05 to 60")
    if args.command_json is not None:
        command = json.loads(args.command_json)
        if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
            raise SystemExit("--command-json must be a JSON array of strings")
        config = operations.DispatchConfig(
            command=tuple(command), timeout_seconds=args.timeout,
            token_file=Path(args.token_file) if args.token_file else None,
        )
    else:
        if not args.token_file:
            raise SystemExit("--token-file is required with --url")
        config = operations.DispatchConfig(url=args.url, token_file=Path(args.token_file),
                                           timeout_seconds=args.timeout)
    registry = operations.StudioRegistry(args.root)
    paths = registry.paths(args.studio)
    worker = operations.TurnDispatcher(
        operations.OperationsStore(paths), config,
        worker_id=socket.gethostname() + ":" + str(__import__("os").getpid()),
        verifier=operations.AppWorkVerifier(paths.app_db),
    )
    if args.once:
        worker.store.discover_app_work()
        return 0 if worker.dispatch_one() else 2
    while True:
        # Recovery scan closes the crash window between the app committing its
        # durable work row and any best-effort wake signal reaching this process.
        worker.store.discover_app_work()
        if not worker.dispatch_one():
            time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
