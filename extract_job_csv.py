#!/usr/bin/env python3
"""Extract job execution details from the Ansible Automation Platform / AWX REST API.

Writes one CSV row per job with:
  job_id, job_template_name, status, created, started, execution_image,
  first_stdout_timestamp, elapsed_seconds, playbook_run, last_stdout_line

With --show-execution-environment, also includes:
  execution_environment, download_policy

API calls used (AAP 2.5+ path shown; older AWX/Tower uses /api/v2/):
  GET  {host}/api/                                          (detect 2.5+ gateway vs 2.4)
  GET  {host}/api/controller/v2/ping/                       (AAP 2.5+)
  GET  {host}/api/v2/ping/                                   (AAP 2.4)
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
      --job-template-name "rbertol - repro" --day today

  python3 extract_job_csv.py --host https://controller.example.com \\
      --username admin --password secret --day 09012026 -o jobs.csv
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
from datetime import date, datetime, time, timedelta
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

ANSI_ESCAPE_RE = re.compile(r"(?:\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\r)")

# ansible.posix.profile_tasks, e.g.
# "Tuesday 01 September 2026 17:01:32 +0000 (0:00:00.038) 0:00:00.038 *****"
PROFILE_TASKS_TS_RE = re.compile(
    r"(?P<timestamp>"
    r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
    r"\s+\d{1,2}\s+[A-Za-z]+\s+\d{4}\s+"
    r"\d{2}:\d{2}:\d{2}\s+"
    r"[+-]\d{2}:?\d{2}"
    r")"
    r"(?:\s+\(\d+:\d{2}:\d{2}(?:\.\d+)?\))?"
    r"(?:\s+\d+:\d{2}:\d{2}(?:\.\d+)?)?"
    r"(?:\s+\*+)?"
)
ISO_TS_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)
# ansible.posix.timer: "Playbook run took 0 days, 0 hours, 0 minutes, 4 seconds"
PLAYBOOK_RUN_RE = re.compile(r"Playbook run took\s+.+", re.IGNORECASE)

CSV_FIELDS = [
    "job_id",
    "job_template_name",
    "status",
    "created",
    "started",
    "execution_image",
    "first_stdout_timestamp",
    "elapsed_seconds",
    "playbook_run",
    "last_stdout_line",
]

EE_CSV_FIELDS = [
    "execution_environment",
    "download_policy",
]

# Controller stores this as ExecutionEnvironment.pull (always / missing / never).
# An empty value means the EE has no pull policy set; ansible-runner then omits
# --pull, which matches the "missing" (only pull if not present) default.
DOWNLOAD_POLICY_LABELS = {
    "always": "Always pull container before running",
    "missing": "Only pull the image if not present before running",
    "never": "Never pull container before running",
}
DEFAULT_DOWNLOAD_POLICY = "missing"


class ControllerAPIError(RuntimeError):
    pass


class Status:
    """Progress and status messages on stderr so CSV on stdout stays clean."""

    def __init__(self) -> None:
        self._tty = sys.stderr.isatty()
        self._last_len = 0

    def info(self, message: str) -> None:
        self._clear_bar()
        print(message, file=sys.stderr, flush=True)

    def bar(self, current: int, total: int, message: str) -> None:
        if total <= 0:
            return
        width = 28
        filled = min(width, int(width * current / total))
        bar = "#" * filled + "-" * (width - filled)
        percent = int(100 * current / total)
        text = f"[{bar}] {current}/{total} {percent:3d}%  {message}"
        if self._tty:
            pad = max(0, self._last_len - len(text))
            sys.stderr.write("\r" + text + " " * pad)
            sys.stderr.flush()
            self._last_len = len(text)
            if current >= total:
                sys.stderr.write("\n")
                self._last_len = 0
        elif current == 1 or current == total or current % 25 == 0:
            print(f"{message} ({current}/{total})", file=sys.stderr, flush=True)

    def _clear_bar(self) -> None:
        if self._tty and self._last_len:
            sys.stderr.write("\r" + " " * self._last_len + "\r")
            sys.stderr.flush()
            self._last_len = 0


class ControllerClient:
    """Minimal Controller REST client using only the Python standard library."""

    def __init__(
        self,
        host: str,
        token: str | None = None,
        username: str | None = None,
        password: str | None = None,
        verify_ssl: bool = True,
        api_base: str | None = None,
    ) -> None:
        self.host = host.rstrip("/")
        self.token = token
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl
        self.api_base = self._normalize_api_base(api_base) if api_base else self._detect_api_base()
        self._ee_cache: dict[int, dict[str, Any]] = {}

    def _ssl_context(self) -> ssl.SSLContext | None:
        if self.host.startswith("http://"):
            return None
        if self.verify_ssl:
            return ssl.create_default_context()
        context = ssl._create_unverified_context()
        return context

    def _headers(self, require_auth: bool = True) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        elif self.username and self.password:
            basic = b64encode(f"{self.username}:{self.password}".encode()).decode()
            headers["Authorization"] = f"Basic {basic}"
        elif require_auth:
            raise ControllerAPIError(
                "Missing credentials. Set CONTROLLER_TOKEN or CONTROLLER_USERNAME/CONTROLLER_PASSWORD."
            )
        return headers

    def request(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        require_auth: bool = True,
    ) -> Any:
        if path.startswith("http://") or path.startswith("https://"):
            url = path
        else:
            url = urljoin(f"{self.host}/", path.lstrip("/"))
        if params:
            url = f"{url}?{urlencode(params, doseq=True)}"

        req = Request(url, headers=self._headers(require_auth=require_auth), method="GET")
        try:
            with urlopen(req, context=self._ssl_context(), timeout=30) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                content_type = resp.headers.get("Content-Type", "")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ControllerAPIError(f"HTTP {exc.code} for {url}: {detail}") from exc
        except URLError as exc:
            raise ControllerAPIError(self._url_error_message(url, exc.reason)) from exc

        if "application/json" in content_type or body.lstrip().startswith(("{", "[")):
            return json.loads(body)
        return body

    @staticmethod
    def _normalize_api_base(api_base: str) -> str:
        prefix = "/" + api_base.strip().strip("/")
        if prefix in {"/api/controller/v2", "/api/v2"}:
            return prefix
        raise ControllerAPIError(
            f'Unsupported --api-base "{api_base}". Use /api/controller/v2 (AAP 2.5+) or /api/v2 (AAP 2.4).'
        )

    @staticmethod
    def _is_tls_error(exc: BaseException) -> bool:
        return "certificate" in str(exc).lower() or "ssl" in str(exc).lower()

    def _url_error_message(self, url: str, reason: object) -> str:
        message = f"Failed to reach {url}: {reason}"
        if self.verify_ssl and self._is_tls_error(reason):
            message += (
                " TLS verification failed. If the Controller uses a private or self-signed "
                "certificate, re-run with --insecure or CONTROLLER_VERIFY_SSL=false."
            )
        return message

    def _detect_api_base(self) -> str:
        # AAP 2.5+ gateway: GET /api/ returns apis.controller, and ping is
        #   GET {host}/api/controller/v2/ping/
        # AAP 2.4 / older AWX: GET /api/ returns current_version /api/v2/, ping is
        #   GET {host}/api/v2/ping/
        errors: list[str] = []

        try:
            payload = self.request("/api/", require_auth=False)
            if isinstance(payload, dict):
                apis = payload.get("apis") or {}
                if "controller" in apis:
                    return "/api/controller/v2"
                current = str(payload.get("current_version") or "")
                if current.rstrip("/").endswith("/api/v2"):
                    return "/api/v2"
        except ControllerAPIError as exc:
            if self._is_tls_error(exc):
                raise
            errors.append(str(exc))

        for prefix in ("/api/controller/v2", "/api/v2"):
            try:
                self.request(f"{prefix}/ping/", require_auth=False)
                return prefix
            except ControllerAPIError as exc:
                if self._is_tls_error(exc):
                    raise
                errors.append(str(exc))

        detail = "\n".join(errors)
        raise ControllerAPIError(
            f"Could not detect the Controller API. Tried {self.host}/api/controller/v2/ping/ "
            f"(AAP 2.5+) and {self.host}/api/v2/ping/ (AAP 2.4)."
            + (f"\n{detail}" if detail else "")
        )

    def paginate(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        on_page: Any = None,
    ) -> Iterator[dict[str, Any]]:
        query = dict(params or {})
        query.setdefault("page_size", 100)
        next_url: str | None = None
        first = True
        seen = 0
        page_num = 0
        while first or next_url:
            payload = self.request(path if first else next_url, params=query if first else None)
            first = False
            if not isinstance(payload, dict) or "results" not in payload:
                raise ControllerAPIError(f"Unexpected list response from {path}")
            results = payload["results"]
            page_num += 1
            seen += len(results)
            if on_page:
                on_page(page_num, seen, payload.get("count"))
            yield from results
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
        job_template_ids: list[int] | None = None,
        created_gte: str | None = None,
        created_lt: str | None = None,
        on_page: Any = None,
    ) -> list[dict[str, Any]]:
        if job_id is not None:
            return [self.request(f"{self.api_base}/jobs/{job_id}/")]

        template_ids = list(job_template_ids or [])
        if not template_ids:
            params: dict[str, Any] = {"order_by": "-id", "type": "job"}
            if created_gte:
                params["created__gte"] = created_gte
            if created_lt:
                params["created__lt"] = created_lt
            return list(self.paginate(f"{self.api_base}/jobs/", params, on_page=on_page))

        jobs_by_id: dict[int, dict[str, Any]] = {}
        for template_id in template_ids:
            params = {"order_by": "-id", "type": "job", "job_template": template_id}
            if created_gte:
                params["created__gte"] = created_gte
            if created_lt:
                params["created__lt"] = created_lt
            for job in self.paginate(f"{self.api_base}/jobs/", params, on_page=on_page):
                jobs_by_id[int(job["id"])] = job
        return sorted(jobs_by_id.values(), key=lambda job: int(job["id"]), reverse=True)

    def get_stdout_text(self, job_id: int) -> str:
        payload = self.request(f"{self.api_base}/jobs/{job_id}/stdout/", {"format": "json"})
        if isinstance(payload, dict):
            return payload.get("content") or ""
        return str(payload or "")

    def get_execution_environment(self, ee_id: int) -> dict[str, Any]:
        if ee_id not in self._ee_cache:
            self._ee_cache[ee_id] = self.request(f"{self.api_base}/execution_environments/{ee_id}/")
        return self._ee_cache[ee_id]


def parse_day_arg(value: str) -> date:
    raw = value.strip()
    key = raw.lower()
    today = datetime.now().astimezone().date()
    if key == "today":
        return today
    if key == "yesterday":
        return today - timedelta(days=1)
    if len(raw) == 8 and raw.isdigit():
        try:
            parsed = datetime.strptime(raw, "%m%d%Y").date()
        except ValueError as exc:
            raise argparse.ArgumentTypeError(
                f'Invalid --day "{value}". Use today, yesterday, or MMDDYYYY (e.g. 09012026).'
            ) from exc
        return parsed
    raise argparse.ArgumentTypeError(
        f'Invalid --day "{value}". Use today, yesterday, or MMDDYYYY (e.g. 09012026).'
    )


def day_range_iso(day: date) -> tuple[str, str]:
    """Return local-midnight start (inclusive) and next midnight (exclusive) as ISO timestamps."""
    tz = datetime.now().astimezone().tzinfo
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = start + timedelta(days=1)
    return start.isoformat(), end.isoformat()


def job_on_local_day(job: dict[str, Any], day: date) -> bool:
    stamp = job.get("created") or ""
    if not stamp:
        return False
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return when.astimezone().date() == day


def strip_ansi(text: str) -> str:
    return ANSI_ESCAPE_RE.sub("", text)


def last_stdout_line(stdout: str) -> str:
    lines = [line.strip() for line in strip_ansi(stdout).splitlines() if line.strip()]
    return lines[-1] if lines else ""


def stdout_timestamps(stdout: str) -> tuple[str, str]:
    """Return (first profile_tasks timestamp, Playbook run line) from job stdout."""
    first_ts = ""
    playbook_run = ""
    iso_fallback = ""
    for raw in strip_ansi(stdout).splitlines():
        line = " ".join(raw.split())
        if not line:
            continue
        if not first_ts:
            match = PROFILE_TASKS_TS_RE.search(line)
            if match:
                first_ts = match.group("timestamp")
            elif not iso_fallback:
                iso_match = ISO_TS_RE.search(line)
                if iso_match:
                    iso_fallback = iso_match.group(0)
        match = PLAYBOOK_RUN_RE.search(line)
        if match:
            playbook_run = match.group(0).rstrip("*").strip()
    return first_ts or iso_fallback, playbook_run


def execution_image(job: dict[str, Any]) -> str:
    ee = (job.get("summary_fields") or {}).get("execution_environment") or {}
    return ee.get("image") or ""


def job_template_name(job: dict[str, Any]) -> str:
    jt = (job.get("summary_fields") or {}).get("job_template") or {}
    return jt.get("name") or job.get("name") or ""


def download_policy_label(pull: str | None) -> str:
    key = (pull or "").strip().lower()
    if not key:
        key = DEFAULT_DOWNLOAD_POLICY
    return DOWNLOAD_POLICY_LABELS.get(key, pull or "")


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
    stdout = client.get_stdout_text(job_id)
    first_ts, playbook_run = stdout_timestamps(stdout)
    elapsed = job.get("elapsed")
    row = {
        "job_id": job_id,
        "job_template_name": job_template_name(job),
        "status": job.get("status") or "",
        "created": job.get("created") or "",
        "started": job.get("started") or "",
        "execution_image": execution_image(job),
        "first_stdout_timestamp": first_ts,
        "elapsed_seconds": elapsed if elapsed is not None else "",
        "playbook_run": playbook_run,
        "last_stdout_line": last_stdout_line(stdout),
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
    parser.add_argument(
        "--api-base",
        default=env("CONTROLLER_API_BASE"),
        help="Controller API prefix. Default: auto-detect. "
        "Use /api/controller/v2 for AAP 2.5+ or /api/v2 for AAP 2.4.",
    )
    parser.add_argument("--job-id", type=int, help="Export a single job by id")
    parser.add_argument(
        "--job-template-id",
        type=int,
        action="append",
        dest="job_template_ids",
        help="Export jobs for this job template id. Repeat the flag to include more than one.",
    )
    parser.add_argument(
        "--job-template-name",
        action="append",
        dest="job_template_names",
        help="Export jobs for this job template name. Repeat the flag to include more than one.",
    )
    parser.add_argument(
        "--day",
        type=parse_day_arg,
        help="Only jobs created on this calendar day (local timezone). "
        "Use today, yesterday, or MMDDYYYY (e.g. 09012026).",
    )
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
    if not args.token and not (args.username and args.password):
        print(
            "Missing credentials. Set CONTROLLER_TOKEN or CONTROLLER_USERNAME/CONTROLLER_PASSWORD.",
            file=sys.stderr,
        )
        return 2

    try:
        return _run(args)
    except ControllerAPIError as exc:
        print(exc, file=sys.stderr)
        return 1


def _run(args: argparse.Namespace) -> int:
    status = Status()
    status.info(f"Connecting to {args.host}")
    if args.insecure:
        status.info("TLS verification disabled (--insecure)")

    if args.api_base:
        status.info(f"Using API prefix {args.api_base}")
    else:
        status.info("Detecting Controller API (AAP 2.5+ vs 2.4) ...")

    client = ControllerClient(
        host=args.host,
        token=args.token,
        username=args.username,
        password=args.password,
        verify_ssl=not args.insecure,
        api_base=args.api_base,
    )
    version_label = "AAP 2.5+" if client.api_base == "/api/controller/v2" else "AAP 2.4 / AWX"
    status.info(f"Detected {version_label} at {client.api_base}")

    template_ids: list[int] = list(args.job_template_ids or [])
    for name in args.job_template_names or []:
        status.info(f'Looking up job template "{name}" ...')
        template_id = client.find_job_template_id(name)
        status.info(f'Found "{name}" as job template id {template_id}')
        template_ids.append(template_id)

    unique_template_ids: list[int] = []
    seen_template_ids: set[int] = set()
    for template_id in template_ids:
        if template_id not in seen_template_ids:
            seen_template_ids.add(template_id)
            unique_template_ids.append(template_id)

    if args.job_id is not None:
        status.info(f"Fetching job {args.job_id} ...")
    elif unique_template_ids:
        status.info(f"Listing jobs for {len(unique_template_ids)} job template(s) ...")
    else:
        status.info("Listing jobs ...")

    created_gte = created_lt = None
    if args.day:
        created_gte, created_lt = day_range_iso(args.day)
        status.info(
            f"Filtering jobs created on {args.day.isoformat()} "
            f"(local day {created_gte} to {created_lt})"
        )

    def on_list_page(page: int, seen: int, total: Any) -> None:
        if total:
            status.bar(seen, int(total), f"Listing jobs (page {page})")
        else:
            status.info(f"Listing jobs ... {seen} so far (page {page})")

    jobs = client.list_jobs(
        job_id=args.job_id,
        job_template_ids=unique_template_ids or None,
        created_gte=created_gte,
        created_lt=created_lt,
        on_page=on_list_page,
    )
    if args.day:
        jobs = [job for job in jobs if job_on_local_day(job, args.day)]
    status.info(f"Found {len(jobs)} job(s)")

    rows: list[dict[str, Any]] = []
    total = len(jobs)
    for index, job in enumerate(jobs, start=1):
        job_id = job.get("id")
        status.bar(index, total, f"Fetching stdout for job {job_id}")
        rows.append(job_to_row(client, job, include_ee=args.show_execution_environment))

    fieldnames = list(CSV_FIELDS)
    if args.show_execution_environment:
        # Keep image next to the EE name/policy columns.
        insert_at = fieldnames.index("execution_image") + 1
        fieldnames[insert_at:insert_at] = EE_CSV_FIELDS

    destination = "stdout" if args.output == "-" else args.output
    status.info(f"Writing {len(rows)} row(s) to {destination} ...")

    out = sys.stdout if args.output == "-" else open(args.output, "w", newline="", encoding="utf-8")
    close_out = args.output != "-"
    try:
        writer = csv.DictWriter(out, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    finally:
        if close_out:
            out.close()

    status.info(f"Done. Wrote {len(rows)} row(s) to {destination}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
