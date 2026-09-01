# Extract job CSV

`extract_job_csv.py` collects execution details from the Ansible Automation Platform (AAP) Controller or AWX REST API and writes them as CSV.

Use it when you need a simple report of job runs: identifiers, timestamps, the container image that executed the job, elapsed time, and the first and last lines of job stdout.

## What it does

For each matching job the script:

1. Detects the Controller API path (`/api/controller/v2/` on AAP 2.5+, or `/api/v2/` on older AWX / Automation Controller).
2. Lists jobs (all jobs, one job id, or every run of a job template).
3. Fetches job stdout and takes the first and last non-empty lines (ANSI color codes are stripped).
4. Optionally loads the execution environment to include its name and image download/pull policy.
5. Writes one CSV row per job.

### Output columns

| Column | Description |
| --- | --- |
| `job_id` | Job id |
| `job_template_name` | Job template name |
| `created` | When the job record was created (UTC) |
| `started` | When the job started (UTC) |
| `execution_image` | Container image used to run the job |
| `first_stdout_line` | First non-empty line of job stdout |
| `elapsed_seconds` | Job duration in seconds |
| `last_stdout_line` | Last non-empty line of job stdout |

With `--show-execution-environment`, two extra columns are added after `execution_image`:

| Column | Description |
| --- | --- |
| `execution_environment` | Execution environment name |
| `download_policy` | Image pull policy: `Always pull container before running`, `Only pull the image if not present before running`, or `Never pull container before running`. Empty if the EE has no pull policy set. |

## Recommended Job Settings for better stdout

The script records the first and last non-empty lines of job stdout. Those lines are more useful when Ansible prints task timing and a playbook timer at the end of the run.

On the Controller, set this as an **Extra Environment Variable** under **Job Settings** (Settings → Jobs):

```json
{
  "ANSIBLE_CALLBACKS_ENABLED": "ansible.posix.profile_tasks,ansible.posix.timer"
}
```

- `ansible.posix.profile_tasks` — prints a per-task timing summary
- `ansible.posix.timer` — prints the total playbook runtime

Apply this before launching the jobs you want to report on. Existing completed jobs will not pick up the change. The execution environment must include the `ansible.posix` collection (it is present in the supported Red Hat execution environments).

## Requirements

- **Python 3.9 or later**
- Network access to the Controller API
- A Controller user token, or a username and password with permission to view jobs

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

If the Controller uses a certificate that is not trusted by this machine, add `--insecure` or set `CONTROLLER_VERIFY_SSL=false`.

Older `TOWER_*` environment variables (`TOWER_HOST`, `TOWER_USERNAME`, `TOWER_PASSWORD`, `TOWER_OAUTH_TOKEN`) are also accepted.

## How to use it

From this directory:

```bash
python3 extract_job_csv.py --help
```

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

Or by template id:

```bash
python3 extract_job_csv.py --job-template-id 6 -o demo-jobs.csv
```

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
job_id,job_template_name,created,started,execution_image,first_stdout_line,elapsed_seconds,last_stdout_line
1,Demo Job Template,2026-08-11T21:22:31.598097Z,2026-08-11T21:22:32.524598Z,registry.redhat.io/ansible-automation-platform-27/ee-supported-rhel9:latest,"PLAY [Hello World Sample] ******************************************************",10.594,"localhost                  : ok=2    changed=0    unreachable=0    failed=0    skipped=0    rescued=0    ignored=0"
```

With `--show-execution-environment`:

```csv
job_id,job_template_name,created,started,execution_image,execution_environment,download_policy,first_stdout_line,elapsed_seconds,last_stdout_line
1,Demo Job Template,2026-08-11T21:22:31.598097Z,2026-08-11T21:22:32.524598Z,registry.redhat.io/ansible-automation-platform-27/ee-supported-rhel9:latest,Default execution environment,,"PLAY [Hello World Sample] ******************************************************",10.594,"localhost                  : ok=2    changed=0    unreachable=0    failed=0    skipped=0    rescued=0    ignored=0"
```

Stdout fields are quoted when they contain commas or spaces.

## API calls

The script only performs GET requests:

| Call | Purpose |
| --- | --- |
| `GET /api/controller/v2/ping/` | Detect the API base path |
| `GET /api/controller/v2/job_templates/?name=<name>` | Resolve a job template name to an id |
| `GET /api/controller/v2/jobs/` | List jobs |
| `GET /api/controller/v2/jobs/{id}/` | Retrieve a single job |
| `GET /api/controller/v2/jobs/{id}/stdout/?format=json` | Read job stdout |
| `GET /api/controller/v2/execution_environments/{id}/` | EE name and pull policy (only with `--show-execution-environment`) |
