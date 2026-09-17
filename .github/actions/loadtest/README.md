# CI Load Test Action

Runs a [Locomotive](../../../README.md) load test in GitHub Actions: downloads
the branch-aware baseline, runs `loco ci`, writes the job summary, uploads the
report and the baseline, and keeps one comment on the pull request up to date.

The full documentation is in the main README, under
[GitHub Actions](../../../README.md#github-actions); this page is the short
reference.

## Usage

```yaml
name: Load Test

on:
  push:
    branches: [main]
  pull_request:

permissions:
  contents: read
  actions: read          # find the baseline artifact from earlier runs
  pull-requests: write   # post the results comment and keep it updated

jobs:
  loadtest:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7

      - uses: actions/setup-python@v7
        with:
          python-version: '3.12'

      # start the service under test here

      - name: Run load test
        id: loadtest
        uses: locomotive-lib/locomotive/.github/actions/loadtest@master
        with:
          config: loconfig.json
```

`loco init --github-workflow` generates this workflow.

## Inputs

| Input | Default | Description |
|-------|---------|-------------|
| `config` | `loconfig.json` | Path to the Locomotive config |
| `users` | from config | Override the number of users |
| `run_time` | from config | Override the run time, e.g. `2m` |
| `args` | (empty) | Extra arguments for `loco ci`, e.g. `--processes 4` |
| `set_baseline` | `true` | Record a passing run as the baseline and upload it |
| `post_pr_comment` | `true` | Post the summary on the pull request, editing the same comment on later pushes |
| `baseline_artifact` | `loadtest-baseline` | Name of the baseline artifact |
| `results_artifact` | `loadtest-results` | Name prefix of the results artifact |
| `workflow` | (empty) | Workflow file to search for baseline artifacts (empty = all workflows) |
| `fallback_branch` | default branch | Branch whose baseline is used when the compared branch has none |
| `github_token` | `github.token` | Token for baseline downloads and the comment |
| `locomotive_version` | (empty) | Install this version from PyPI instead of the one shipped with the action |

## Outputs

| Output | Description |
|--------|-------------|
| `status` | `PASS`, `WARNING`, `DEGRADATION` or `NO_DATA` |
| `metrics_path` | Path to `metrics.json` |
| `report_path` | Path to `report.html` |
| `summary_path` | Path to the markdown summary |
| `junit_path` | Path to JUnit XML with one test case per check |

```yaml
- name: Show the verdict
  if: always()
  run: echo "Load test: ${{ steps.loadtest.outputs.status }}"
```

## Permissions and forks

- `actions: read` lets the baseline be found among artifacts of earlier runs.
- `pull-requests: write` lets the comment be posted and updated.

A pull request from a fork gets a read-only `GITHUB_TOKEN`. The comment step
then fails without failing the job; the job summary still has the results.
