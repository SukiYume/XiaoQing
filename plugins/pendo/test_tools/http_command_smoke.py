"""Replay real `/pendo ...` commands through the HTTP command endpoint.

Set `PENDO_HTTP_AUTH` to the Authorization header value used by the local
command service before running.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_COMMANDS = [
    "/pendo help",
    "/pendo help event",
    "/pendo event list today",
    "/pendo note list TEST_HTTP_SMOKE",
    "/pendo task list today",
    "/pendo ledger list month",
    "/pendo diary list month",
    "/pendo search TEST_HTTP_SMOKE",
    "/pendo settings",
]


def onebot_payload(command: str, user_id: str) -> dict:
    return {
        "time": int(datetime.now().timestamp()),
        "self_id": 0,
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "user_id": int(user_id) if str(user_id).isdigit() else user_id,
        "message_id": int(datetime.now().timestamp() * 1000) % 2147483647,
        "raw_message": command,
        "message": [{"type": "text", "data": {"text": command}}],
        "font": 0,
    }


def send_command(endpoint: str, auth: str, command: str, user_id: str, timeout: float) -> dict:
    body = json.dumps(onebot_payload(command, user_id), ensure_ascii=False).encode("utf-8")
    request = Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/json", "Authorization": auth},
        method="POST",
    )
    started = datetime.now().isoformat(timespec="seconds")
    try:
        with urlopen(request, timeout=timeout) as response:
            content = response.read().decode("utf-8", errors="replace")
            return {
                "command": command,
                "started_at": started,
                "status": response.status,
                "ok": 200 <= response.status < 300,
                "response": json.loads(content) if content else None,
            }
    except HTTPError as exc:
        return {
            "command": command,
            "started_at": started,
            "status": exc.code,
            "ok": False,
            "response": exc.read().decode("utf-8", errors="replace"),
        }
    except URLError as exc:
        return {
            "command": command,
            "started_at": started,
            "status": None,
            "ok": False,
            "error": str(exc.reason),
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default=os.getenv("PENDO_HTTP_ENDPOINT", "http://127.0.0.1:12000/event"))
    parser.add_argument("--auth", default=os.getenv("PENDO_HTTP_AUTH", ""))
    parser.add_argument("--user-id", default=os.getenv("PENDO_HTTP_USER_ID", "1001"))
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--command", action="append", default=[])
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    if not args.auth:
        print("PENDO_HTTP_AUTH is required", file=sys.stderr)
        return 2

    commands = args.command or DEFAULT_COMMANDS
    output = Path(args.output) if args.output else Path("plugins/pendo/test_reports") / (
        "http-command-smoke-" + datetime.now().strftime("%Y%m%d%H%M%S") + ".jsonl"
    )
    output.parent.mkdir(parents=True, exist_ok=True)

    failures = 0
    with output.open("w", encoding="utf-8") as handle:
        for command in commands:
            result = send_command(args.endpoint, args.auth, command, args.user_id, args.timeout)
            failures += 0 if result.get("ok") else 1
            handle.write(json.dumps(result, ensure_ascii=False) + "\n")
            print(f"{'OK' if result.get('ok') else 'FAIL'} {command}")
    print(f"wrote {output}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
