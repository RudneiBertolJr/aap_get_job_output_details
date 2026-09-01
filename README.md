# Extract job CSV

`extract_job_csv.py` collects execution details from the Ansible Automation Platform (AAP) Controller or AWX REST API and writes them as CSV.

Use it when you need a simple report of job runs: identifiers, status, timestamps, the container image that executed the job, elapsed time, the first timestamp from job stdout, the `Playbook run` timer line, and the last line of job stdout.

**Required Controller setting:** before jobs are launched, set this **Extra Environment Variable** under **Job Settings** (Settings → Jobs). Without it, `first_stdout_timestamp` and `playbook_run` in the CSV will be empty.

```json
{
  "ANSIBLE_CALLBACKS_ENABLED": "ansible.posix.profile_tasks,ansible.posix.timer"
}
```

## What it does

For each matching job the script:

1. Detects the Controller API path (`/api/controller/v2/` on AAP 2.5+, or `/api/v2/` on older AWX / Automation Controller).
2. Lists jobs (all jobs, one job id, or every run of a job template).
3. Fetches job stdout, takes the first timestamp, the `Playbook run took ...` timer line, and the last non-empty line (ANSI color codes are stripped).
4. Optionally loads the execution environment to include its name and image download/pull policy.
5. Writes one CSV row per job.

### Output columns

| Column | Description |
| --- | --- |
| `job_id` | Job id |
| `job_template_name` | Job template name |
| `status` | Job status from the Controller (`successful`, `failed`, `error`, `canceled`, `pending`, `waiting`, `running`, and similar) |
| `created` | When the job record was created (UTC) |
| `started` | When the job started (UTC) |
| `execution_image` | Container image used to run the job |
| `first_stdout_timestamp` | First `ansible.posix.profile_tasks` timestamp in job stdout, for example `Tuesday 01 September 2026 17:01:32 +0000` from a line like `Tuesday 01 September 2026 17:01:32 +0000 (0:00:00.038) 0:00:00.038 *****`. Empty if that pattern is not in the output. |
| `elapsed_seconds` | Job duration in seconds (from the Controller job record) |
| `playbook_run` | Timer line from stdout, for example `Playbook run took 0 days, 0 hours, 0 minutes, 4 seconds`. Empty if `ansible.posix.timer` did not run. |
| `last_stdout_line` | Last non-empty line of job stdout |

With `--show-execution-environment`, two extra columns are added after `execution_image`:

| Column | Description |
| --- | --- |
| `execution_environment` | Execution environment name |
| `download_policy` | Image pull policy from the execution environment: `Always pull container before running`, `Only pull the image if not present before running`, or `Never pull container before running`. If the EE has `pull` unset, the script reports `Only pull the image if not present before running` (the Controller default when `--pull` is not passed). |

## Required Job Settings (Extra Environment Variables)

`first_stdout_timestamp` and `playbook_run` are read from job stdout. Ansible only writes those lines if these callbacks are enabled.

On the Controller, open **Settings → Jobs** (**Job Settings**) and set **Extra Environment Variables** to:

```json
{
  "ANSIBLE_CALLBACKS_ENABLED": "ansible.posix.profile_tasks,ansible.posix.timer"
}
```

| Callback | What it adds to stdout | CSV column |
| --- | --- | --- |
| `ansible.posix.profile_tasks` | A timestamp before each task, for example `Tuesday 01 September 2026 17:01:32 +0000 (0:00:00.038) 0:00:00.038 *****` | `first_stdout_timestamp` |
| `ansible.posix.timer` | `Playbook run took 0 days, 0 hours, 0 minutes, 4 seconds` at the end of the run | `playbook_run` |

This setting must be in place **before** the jobs are launched. Jobs that already finished will not pick it up. The execution environment must include the `ansible.posix` collection (it is present in the supported Red Hat execution environments).

## Requirements

- **Python 3.9 or later**
- Network access to the Controller API
- A Controller user token, or a username and password with permission to view jobs
- **Job Settings → Extra Environment Variables** set to `ANSIBLE_CALLBACKS_ENABLED: ansible.posix.profile_tasks,ansible.posix.timer` (see above). Needed for `first_stdout_timestamp` and `playbook_run`.

**You do not need to install any Python libraries.** The script uses only the Python standard library (`urllib`, `csv`, `argparse`, `json`, and similar). There is no `pip install` step.

Confirm Python is available:

```bash
python3 --version
```

## Authentication

Set the Controller URL and credentials as environment variables, or pass the same values as command-line flags.

**OAuth2 / personal access token (preferred):**

```bash
export CONTROLLER_HOST=https://controller.example.com
export CONTROLLER_TOKEN=your-oauth-token
```

**Username and password:**

```bash
export CONTROLLER_HOST=https://controller.example.com
export CONTROLLER_USERNAME=admin
export CONTROLLER_PASSWORD=secret
```

Equivalent flags: `--host`, `--token`, `--username`, `--password`.

If the Controller uses a certificate that is not trusted by this machine (common with HTTPS to an IP address or a private CA), add `--insecure` or set `CONTROLLER_VERIFY_SSL=false`. Without that, API detection fails with a TLS error before any job data is collected.

Older `TOWER_*` environment variables (`TOWER_HOST`, `TOWER_USERNAME`, `TOWER_PASSWORD`, `TOWER_OAUTH_TOKEN`) are also accepted.

## API version detection (AAP 2.4 vs 2.5+)

The script does not ask you for the AAP version. It probes the Controller and selects the API prefix:

| AAP version | Checking point | API prefix used afterwards |
| --- | --- | --- |
| 2.5+ (platform gateway) | `GET /api/` contains `apis.controller`. Fallback: `GET /api/controller/v2/ping/` | `/api/controller/v2` |
| 2.4 and older AWX | `GET /api/` contains `current_version: /api/v2/`. Fallback: `GET /api/v2/ping/` | `/api/v2` |

Ping itself does not require a token. Job listing and stdout still do.

To skip auto-detection:

```bash
python3 extract_job_csv.py --api-base /api/controller/v2   # AAP 2.5+
python3 extract_job_csv.py --api-base /api/v2              # AAP 2.4
```

## How to use it

From this directory:

```bash
python3 extract_job_csv.py --help
```

While it runs, status and a progress bar go to **stderr** (connecting, API detection, listing jobs, fetching each job's stdout). CSV is written separately to stdout or `-o`, so the bar does not mix into the file.

### Export every job

Prints CSV to the terminal:

```bash
python3 extract_job_csv.py
```

Write to a file:

```bash
python3 extract_job_csv.py -o jobs.csv
```

### Export one job

```bash
python3 extract_job_csv.py --job-id 1 -o job-1.csv
```

### Export all runs of a job template

```bash
python3 extract_job_csv.py --job-template-name "Demo Job Template" -o demo-jobs.csv
```

More than one template (repeat the flag):

```bash
python3 extract_job_csv.py \
  --job-template-name "Demo Job Template" \
  --job-template-name "rbertol - repro" \
  --day today \
  -o today.csv
```

Or by template id (also repeatable):

```bash
python3 extract_job_csv.py --job-template-id 6 -o demo-jobs.csv
```

### Filter by day

Jobs are matched on **created** time, using the calendar day on the machine that runs the script.

Today's runs of one or more job templates:

```bash
python3 extract_job_csv.py \
  --job-template-name "Demo Job Template" \
  --job-template-name "rbertol - repro" \
  --day today \
  -o today.csv
```

All jobs for a day (any template):

```bash
python3 extract_job_csv.py --day today -o today.csv
python3 extract_job_csv.py --day yesterday -o yesterday.csv
python3 extract_job_csv.py --day 09012026 -o jobs-2026-09-01.csv
```

`--day` can be combined with `--job-template-name` or `--job-template-id`. A specific `--job-id` is included only if that job was created on the given day.

### Include execution environment name and download policy

```bash
python3 extract_job_csv.py \
  --job-template-name "Demo Job Template" \
  --show-execution-environment \
  -o jobs.csv
```

### Username and password, skipping TLS verification

```bash
python3 extract_job_csv.py \
  --host https://controller.example.com \
  --username admin \
  --password secret \
  --insecure \
  --job-id 1 \
  --show-execution-environment \
  -o jobs.csv
```

## Example output

Default columns:

```csv
job_id,job_template_name,status,created,started,execution_image,first_stdout_timestamp,elapsed_seconds,playbook_run,last_stdout_line
1,Demo Job Template,successful,2026-08-11T21:22:31.598097Z,2026-08-11T21:22:32.524598Z,registry.redhat.io/ansible-automation-platform-27/ee-supported-rhel9:latest,Tuesday 11 August 2026 21:22:32 +0000,10.594,"Playbook run took 0 days, 0 hours, 0 minutes, 10 seconds","localhost                  : ok=2    changed=0    unreachable=0    failed=0    skipped=0    rescued=0    ignored=0"
```

With `--show-execution-environment`:

```csv
job_id,job_template_name,status,created,started,execution_image,execution_environment,download_policy,first_stdout_timestamp,elapsed_seconds,playbook_run,last_stdout_line
1,Demo Job Template,successful,2026-08-11T21:22:31.598097Z,2026-08-11T21:22:32.524598Z,registry.redhat.io/ansible-automation-platform-27/ee-supported-rhel9:latest,Default execution environment,Only pull the image if not present before running,Tuesday 11 August 2026 21:22:32 +0000,10.594,"Playbook run took 0 days, 0 hours, 0 minutes, 10 seconds","localhost                  : ok=2    changed=0    unreachable=0    failed=0    skipped=0    rescued=0    ignored=0"
```

Stdout fields are quoted when they contain commas or spaces.

## API calls

The script only performs GET requests:

| Call | Purpose |
| --- | --- |
| `GET /api/` | Detect AAP 2.5+ gateway vs AAP 2.4 |
| `GET /api/controller/v2/ping/` | Confirm AAP 2.5+ Controller API |
| `GET /api/v2/ping/` | Confirm AAP 2.4 / older AWX API |
| `GET /api/controller/v2/job_templates/?name=<name>` | Resolve a job template name to an id |
| `GET /api/controller/v2/jobs/` | List jobs (`created__gte` / `created__lt` when `--day` is set) |
| `GET /api/controller/v2/jobs/{id}/` | Retrieve a single job |
| `GET /api/controller/v2/jobs/{id}/stdout/?format=json` | Read job stdout |
| `GET /api/controller/v2/execution_environments/{id}/` | EE name and pull policy (only with `--show-execution-environment`) |
