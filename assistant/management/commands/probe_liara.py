"""Probe the live Liara API and print what answers.

    python manage.py probe_liara --token T [--team ID] --app THROWAWAY_APP [--write] [--notes API_NOTES.md]

Reads are always safe. ``--write`` also changes the throwaway app (one harmless env
var, a restart). Prints only HTTP status and top-level response keys, never values.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from assistant.liara_client import LiaraError, client_for_token


class Command(BaseCommand):
    help = "Call each Liara endpoint the assistant uses and report status + response keys."

    def add_arguments(self, parser):
        parser.add_argument("--token", required=True)
        parser.add_argument("--team", default="")
        parser.add_argument("--app", required=True, help="a throwaway app (writes only touch this one)")
        parser.add_argument("--write", action="store_true", help="also call the change endpoints on --app")
        parser.add_argument("--notes", default="", help="append the table to this markdown file")

    def handle(self, *args, **opts):
        client = client_for_token(opts["token"], opts["team"])
        app = opts["app"]
        steps = [
            ("GET /v1/projects", "verified", lambda: client.list_projects()),
            ("GET /v1/projects/{app}", "verified", lambda: client.get_project(app)),
            ("GET /v1/projects/{app}/logs", "verified", lambda: client.get_logs(app, int(time.time()) - 3600)),
            ("GET /v1/projects/{app}/releases", "verified", lambda: client.list_releases(app)),
            ("GET /v1/projects/{app}/metrics/summary", "verified", lambda: client.get_metrics_summary(app)),
            ("GET /v1/plans (guess)", "verified", lambda: client.list_plans()),
            ("GET /v1/databases", "verified", lambda: client.list_databases()),
            ("GET storage /api/v1/buckets", "verified", lambda: client.list_buckets()),
        ]
        if opts["write"]:
            steps += [
                ("POST /v1/projects/update-envs", "changed", lambda: client.update_envs(app, {"LIARA_ASSISTANT_PROBE": "1"})),
                ("POST /v1/projects/{app}/actions/restart", "changed", lambda: client.restart(app)),
            ]
        rows = []
        for label, ok_word, call in steps:
            client.calls.clear()
            try:
                call()
                verdict = ok_word
            except LiaraError as exc:
                verdict = "missing" if exc.code == "not_found" else f"error:{exc.code}"
            last = client.calls[-1] if client.calls else {}
            rows.append((label, verdict, str(last.get("status") or "-"), ", ".join(last.get("keys") or []) or "-"))
        table = self._table(rows)
        self.stdout.write(table)
        if opts["notes"]:
            path = Path(opts["notes"])
            if not path.parent.exists():
                raise CommandError(f"cannot write {path}")
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            with path.open("a", encoding="utf-8") as fh:
                fh.write(f"\n\n## Live probe {stamp}\n\n{table}\n")
            self.stdout.write(f"appended to {path}")

    @staticmethod
    def _table(rows) -> str:
        lines = ["| endpoint | result | HTTP status | response top-level keys |", "|---|---|---|---|"]
        lines += [f"| {a} | {b} | {c} | {d} |" for a, b, c, d in rows]
        return "\n".join(lines)
