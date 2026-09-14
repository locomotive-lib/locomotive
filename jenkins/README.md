# Locomotive for Jenkins

A Pipeline shared library with one step, `locomotiveLoadTest`. It runs a
Locomotive load test and does the Jenkins side of it the Jenkins way:

- takes the **baseline** from the last successful build of the branch being
  compared with — the target branch for a pull request, the same branch
  otherwise — with the [Copy Artifact](https://plugins.jenkins.io/copyartifact/) plugin;
- **archives** the stored runs so the next build can do the same;
- publishes every check as **JUnit** results, and the HTML report with
  [HTML Publisher](https://plugins.jenkins.io/htmlpublisher/);
- turns the result into a **build result**: `PASS` → success, `WARNING` →
  UNSTABLE, `DEGRADATION` or no data → FAILURE;
- on a pull/merge request build, posts the summary as **one comment** that later
  builds edit instead of adding new ones (GitHub, GitHub Enterprise, GitLab).

The library lives in the `jenkins/` directory of the Locomotive repository and is
tagged with it, so library `v0.3.0` goes with `locomotive` 0.3.0.

## Loading the library

**Without an administrator** — at the top of the `Jenkinsfile`:

```groovy
library identifier: 'locomotive@v0.3.0',
        retriever: modernSCM(
            scm: [$class: 'GitSCMSource', remote: 'https://github.com/locomotive-lib/locomotive.git'],
            libraryPath: 'jenkins/')
```

**As a Global (or folder) Pipeline Library** — in *Manage Jenkins → System →
Global Trusted Pipeline Libraries*: name `locomotive`, default version `v0.3.0`,
retrieval method *Modern SCM* → Git with
`https://github.com/locomotive-lib/locomotive.git`, and **Library Path**
`jenkins/`. Then:

```groovy
@Library('locomotive') _
```

The step only uses steps the Groovy sandbox allows, so it works as an untrusted
library and needs no script approval. `libraryPath` needs Pipeline: Groovy
Libraries 2.21 or newer. `loco init --jenkinsfile` writes a `Jenkinsfile` with
the right tag for the installed Locomotive.

## Usage

```groovy
pipeline {
    agent any
    options {
        copyArtifactPermission('my-app/*')   // see "Baseline" below
    }
    stages {
        stage('Load test') {
            steps {
                locomotiveLoadTest(config: 'loconfig.json', commentCredentialsId: 'locomotive-pr-comments')
            }
        }
    }
}
```

Complete examples: [`examples/Jenkinsfile`](examples/Jenkinsfile), and
[`examples/Jenkinsfile.without-library`](examples/Jenkinsfile.without-library)
for instances that do not allow shared libraries.

## Requirements

- A **Unix agent** with Python 3.9+ (`loco` is installed into a virtualenv in the
  workspace unless it is already on `PATH`).
- Plugins: Pipeline, **Copy Artifact** (baseline), **JUnit** (per-check results),
  **HTML Publisher** (report), Credentials Binding (comments). JUnit, HTML
  Publisher and Copy Artifact are optional: without one, the step says what is
  missing and carries on with the rest.

## Baseline

A build copies the `.loco` directory archived by the last successful build of
the branch it is compared with, falling back to `main`, then `master`. Only
branch builds record a new baseline; pull request builds are compared but never
become one.

Copy Artifact only copies from a job that allows it. The branch the baseline is
copied from must declare, in its own `Jenkinsfile`,

```groovy
options { copyArtifactPermission('my-app/*') }
```

where `my-app` is the multibranch project's full name, and must have built once
with that setting. Until then a pull request build warns that it has no baseline
and runs only the absolute thresholds.

## Options

| Option | Default | Description |
|--------|---------|-------------|
| `config` | `'loconfig.json'` | Path to the Locomotive config |
| `storage` | `'.loco'` | Stored runs and the baseline; archived for the next build |
| `resultsDir` | `'.loco-results'` | Summary, JUnit XML and the HTML report |
| `users` | from config | Override the number of users |
| `runTime` | from config | Override the run time, e.g. `'5m'` |
| `args` | `[]` | Extra `loco ci` arguments, as a list or a string: `'--processes 4'` |
| `setBaseline` | branch builds only | `true`/`false` to force recording (or not) a passing run as the baseline |
| `copyBaseline` | `true` | Copy the baseline from earlier builds |
| `fallbackBranches` | `['main', 'master']` | Branches tried when the compared branch has no baseline |
| `install` | `'auto'` | `'auto'`: use `loco` from `PATH`, otherwise install it; `true`: always install into `.loco-venv`; `false`: never install |
| `locomotiveVersion` | latest | Version to install from PyPI |
| `python` | `'python3'` | Python used to create the virtualenv |
| `failOnDegradation` | `true` | `false` marks a failed load test UNSTABLE instead of FAILURE — useful while piloting |
| `junit` | `true` | Publish JUnit results |
| `publishReport` | `true` | Publish the HTML report |
| `reportName` | `'Load test report'` | Name of the report link on the build page |
| `comment` | `true` | Comment on pull/merge request builds |
| `commentCredentialsId` | none | Secret text credential with a GitHub or GitLab token; without it no comment is posted |

The step returns a map: `exitCode`, `status` (`PASS`, `WARNING` or `FAILED`),
and the `summary`, `junit` and `report` paths.

## Comments on pull and merge requests

The comment goes through the code host's API, found from `CHANGE_URL`. The
token needs to be able to comment:

- **GitHub**: a fine-grained token with *Pull requests: write* on the repository
  (or a classic token with `repo`).
- **GitLab**: a project access token with the `api` scope and Reporter role or above.

Bitbucket is not supported yet; the build says so and carries on.

## Seeing the charts

Jenkins serves published reports under a Content-Security-Policy that blocks
scripts and inline styles. The report brings its styles in a `report.css` next
to `report.html`, which that policy allows, so it keeps its look; the charts
need JavaScript, so on a default installation each one says why it is empty.
To get them, ask the administrator to set a
[Resource Root URL](https://www.jenkins.io/doc/book/security/user-content/#resource-root-url)
— the recommended fix — rather than relaxing the policy. A downloaded
`report.html` opened locally always shows everything.
