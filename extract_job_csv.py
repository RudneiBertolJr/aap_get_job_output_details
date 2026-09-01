#!/usr/bin/env python3
"""Extract job execution details from the Ansible Automation Platform / AWX REST API.

Writes one CSV row per job with:
  job_id, job_template_name, created, started, execution_image,
  first_stdout_line, elapsed_seconds, last_stdout_line

With --show-execution-environment, also includes:
  execution_environment, download_policy

API calls used (AAP 2.5+ path shown; older AWX/Tower uses /api/v2/):
  GET  {host}/api/controller/v2/ping/
  GET  {host}/api/controller/v2/job_templates/?name=<name>
  GET  {host}/api/controller/v2/jobs/?job_template=<id>&order_by=-id
  GET  {host}/api/controller/v2/jobs/{id}/stdout/?format=json
  GET  {host}/api/controller/v2/execution_environments/{id}/

Authentication (first match wins):
  CONTROLLER_TOKEN     OAuth2 / personal access token  (Authorization: Bearer)
  CONTROLLER_USERNAME + CONTROLLER_PASSWORD            (HTTP Basic)

Examples:
  export CONTROLLER_HOST=https://controller.example.com
  export CONTROLLER_TOKEN=xxxx
  python3 extract_job_csv.py --job-template-name "Demo Job Template" \\
      --show-execution-environment

  python3 extract_job_csv.py --host https://controller.example.com \\
      --username admin --password secret --job-id 1 -o jobs.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import ssl
import sys
from base64 import b64encode
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")

CSV_FIELDS = [
    "job_id",
    "job_template_name",
    "created",
    "started",
    "execution_image",
    "first_stdout_line",
    "elapsed_seconds",
    "last_stdout_line",
]

EE_CSV_FIELDS = [
    "execution_environment",
    "download_policy",
]

# Controller stores this as ExecutionEnvironment.pull (always / missing / never).
DOWNLOAD_POLICY_LABELS = {
    "always": "Always pull container before running",
    "missing": "Only pull the image if not present before running",
    "never": "Never pull container before running",
}


class ControllerAPIError(RuntimeError):
    pass


class ControllerClient:
    """Minimal Controller REST client using only the Python standard library."""

    def __init__(
        self,
        host: str,
        token: str | None = None,
        username: str | None = None,
        password: str | None = None,
        verify_ssl: bool = True,
    ) -> None:
        self.host = host.rstrip("/")
        self.token = token
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl
        self.api_base = self._detect_api_base()
        self._ee_cache: dict[int, dict[str, Any]] = {}

    def _ssl_context(self) -> ssl.SSLContext | None:
        if self.host.startswith("http://"):
            return None
        if self.verify_ssl:
            return ssl.create_default_context()
        context = ssl._create_unverified_context()
        return context

    def _headers(self, accept: str = "application/json") -> dict[str, str]:
        headers = {
            "Accept": accept,
            "Content-Type": "application/json",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        elif self.username and self.password:
            basic = b64encode(f"{self.username}:{self.password}".encode()).decode()
            headers["Authorization"] = f"Basic {basic}"
        else:
            raise ControllerAPIError(
                "Missing credentials. Set CONTROLLER_TOKEN or CONTROLLER_USERNAME/CONTROLLER_PASSWORD."
            )
        return headers

    def request(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if path.startswith("http://") or path.startswith("https://"):
            url = path
        else:
            url = urljoin(f"{self.host}/", path.lstrip("/"))
        if params:
            url = f"{url}?{urlencode(params, doseq=True)}"

        req = Request(url, headers=self._headers(), method="GET")
        try:
            with urlopen(req, context=self._ssl_context()) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                content_type = resp.headers.get("Content-Type", "")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ControllerAPIError(f"HTTP {exc.code} for {url}: {detail}") from exc
        except URLError as exc:
            raise ControllerAPIError(f"Failed to reach {url}: {exc.reason}") from exc

        if "application/json" in content_type or body.lstrip().startswith(("{", "[")):
            return json.loads(body)
        return body

    def _detect_api_base(self) -> str:
        # AAP 2.5+ serves Controller under /api/controller/v2/.
        # Older AWX / Automation Controller uses /api/v2/.
        for prefix in ("/api/controller/v2", "/api/v2"):
            try:
                self.request(f"{prefix}/ping/")
                return prefix
            except ControllerAPIError:
                continue
        raise ControllerAPIError(
            f"Could not detect the Controller API under {self.host}/api/controller/v2/ or {self.host}/api/v2/. "
            "Check CONTROLLER_HOST and credentials."
        )

    def paginate(self, path: str, params: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
        query = dict(params or {})
        query.setdefault("page_size", 100)
        next_url: str | None = None
        first = True
        while first or next_url:
            payload = self.request(path if first else next_url, params=query if first else None)
            first = False
            if not isinstance(payload, dict) or "results" not in payload:
                raise ControllerAPIError(f"Unexpected list response from {path}")
            yield from payload["results"]
            next_url = payload.get("next")

    def find_job_template_id(self, name: str) -> int:
        results = list(self.paginate(f"{self.api_base}/job_templates/", {"name": name}))
        if not results:
            raise ControllerAPIError(f'Job template not found: "{name}"')
        if len(results) > 1:
            ids = ", ".join(str(item["id"]) for item in results)
            raise ControllerAPIError(f'Multiple job templates named "{name}" (ids: {ids}). Use --job-template-id.')
        return int(results[0]["id"])

    def list_jobs(
        self,
        job_id: int | None = None,
        job_template_id: int | None = None,
    ) -> list[dict[str, Any]]:
        if job_id is not None:
            return [self.request(f"{self.api_base}/jobs/{job_id}/")]

        params: dict[str, Any] = {"order_by": "-id", "type": "job"}
        if job_template_id is not None:
            params["job_template"] = job_template_id
        return list(self.paginate(f"{self.api_base}/jobs/", params))

    def get_stdout_text(self, job_id: int) -> str:
        payload = self.request(f"{self.api_base}/jobs/{job_id}/stdout/", {"format": "json"})
        if isinstance(payload, dict):
            return payload.get("content") or ""
        return str(payload or "")

    def get_execution_environment(self, ee_id: int) -> dict[str, Any]:
        if ee_id not in self._ee_cache:
            self._ee_cache[ee_id] = self.request(f"{self.api_base}/execution_environments/{ee_id}/")
        return self._ee_cache[ee_id]


def strip_ansi(text: str) -> str:
    return ANSI_ESCAPE_RE.sub("", text)


def first_and_last_stdout_lines(stdout: str) -> tuple[str, str]:
    lines = [line.strip() for line in strip_ansi(stdout).splitlines() if line.strip()]
    if not lines:
        return "", ""
    return lines[0], lines[-1]


def execution_image(job: dict[str, Any]) -> str:
    ee = (job.get("summary_fields") or {}).get("execution_environment") or {}
    return ee.get("image") or ""


def job_template_name(job: dict[str, Any]) -> str:
    jt = (job.get("summary_fields") or {}).get("job_template") or {}
    return jt.get("name") or job.get("name") or ""


def download_policy_label(pull: str | None) -> str:
    if not pull:
        return ""
    return DOWNLOAD_POLICY_LABELS.get(pull, pull)


def execution_environment_fields(client: ControllerClient, job: dict[str, Any]) -> dict[str, str]:
    summary = (job.get("summary_fields") or {}).get("execution_environment") or {}
    ee_id = job.get("execution_environment")
    name = summary.get("name") or ""
    pull = ""

    if ee_id:
        ee = client.get_execution_environment(int(ee_id))
        name = ee.get("name") or name
        pull = ee.get("pull") or ""

    return {
        "execution_environment": name,
        "download_policy": download_policy_label(pull),
    }


def job_to_row(
    client: ControllerClient,
    job: dict[str, Any],
    include_ee: bool = False,
) -> dict[str, Any]:
    job_id = job["id"]
    first_line, last_line = first_and_last_stdout_lines(client.get_stdout_text(job_id))
    elapsed = job.get("elapsed")
    row = {
        "job_id": job_id,
        "job_template_name": job_template_name(job),
        "created": job.get("created") or "",
        "started": job.get("started") or "",
        "execution_image": execution_image(job),
        "first_stdout_line": first_line,
        "elapsed_seconds": elapsed if elapsed is not None else "",
        "last_stdout_line": last_line,
    }
    if include_ee:
        row.update(execution_environment_fields(client, job))
    return row


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    env = os.environ.get
    parser = argparse.ArgumentParser(
        description="Collect job execution details from the Controller API and write CSV."
    )
    parser.add_argument(
        "--host",
        default=env("CONTROLLER_HOST") or env("TOWER_HOST"),
        help="Controller base URL, e.g. https://controller.example.com (env: CONTROLLER_HOST)",
    )
    parser.add_argument(
        "--token",
        default=env("CONTROLLER_TOKEN") or env("TOWER_OAUTH_TOKEN"),
        help="OAuth2 / personal access token (env: CONTROLLER_TOKEN)",
    )
    parser.add_argument(
        "--username",
        default=env("CONTROLLER_USERNAME") or env("TOWER_USERNAME"),
        help="Username for basic auth (env: CONTROLLER_USERNAME)",
    )
    parser.add_argument(
        "--password",
        default=env("CONTROLLER_PASSWORD") or env("TOWER_PASSWORD"),
        help="Password for basic auth (env: CONTROLLER_PASSWORD)",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        default=env("CONTROLLER_VERIFY_SSL", "true").lower() in {"0", "false", "no"},
        help="Skip TLS certificate verification",
    )
    parser.add_argument("--job-id", type=int, help="Export a single job by id")
    parser.add_argument("--job-template-id", type=int, help="Export all jobs for this job template id")
    parser.add_argument("--job-template-name", help="Export all jobs for this job template name")
    parser.add_argument(
        "--show-execution-environment",
        action="store_true",
        help="Include execution environment name and download/pull policy columns",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="-",
        help="Output CSV path. Default: stdout",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.host:
        print("CONTROLLER_HOST (or --host) is required.", file=sys.stderr)
        return 2

    client = ControllerClient(
        host=args.host,
        token=args.token,
        username=args.username,
        password=args.password,
        verify_ssl=not args.insecure,
    )

    job_template_id = args.job_template_id
    if args.job_template_name:
        job_template_id = client.find_job_template_id(args.job_template_name)

    jobs = client.list_jobs(job_id=args.job_id, job_template_id=job_template_id)
    rows = [
        job_to_row(client, job, include_ee=args.show_execution_environment)
        for job in jobs
    ]

    fieldnames = list(CSV_FIELDS)
    if args.show_execution_environment:
        # Keep image next to the EE name/policy columns.
        insert_at = fieldnames.index("execution_image") + 1
        fieldnames[insert_at:insert_at] = EE_CSV_FIELDS

    out = sys.stdout if args.output == "-" else open(args.output, "w", newline="", encoding="utf-8")
    close_out = args.output != "-"
    try:
        writer = csv.DictWriter(out, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    finally:
        if close_out:
            out.close()

    if args.output != "-":
        print(f"Wrote {len(rows)} row(s) to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
