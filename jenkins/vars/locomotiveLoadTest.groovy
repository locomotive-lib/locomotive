// Locomotive load test as a Jenkins Pipeline step.
//
//   locomotiveLoadTest(config: 'loconfig.json')
//
// Call it inside a node on a Unix agent. Every option is described in
// jenkins/README.md. An unknown option name stops the build: a typo must not
// quietly fall back to a default.
//
// All the judging happens in `loco`; this step only does what is Jenkins's
// job — fetching the baseline from an earlier build, archiving, publishing,
// and turning the exit code into a build result. It uses only steps the
// Groovy sandbox allows, so it works as an untrusted library.

def call(Map config = [:]) {
    Map opts = resolveOptions(config)

    if (!isUnix()) {
        error('locomotiveLoadTest runs on Unix agents only: it drives `loco` through sh')
    }

    String storage = opts.storage
    String results = opts.resultsDir
    boolean changeRequest = env.CHANGE_ID ? true : false
    // A pull request build is compared with its target branch but must not
    // become anybody's baseline: with a gate configured, a run that regressed
    // on its rules is still eligible, and the pull request may never merge.
    boolean setBaseline = opts.setBaseline == null ? !changeRequest : opts.setBaseline == true

    // A workspace outlives the build on the same agent, and whatever an
    // earlier build left in it would pass for this build's baseline.
    dir(storage) { deleteDir() }
    dir(results) { deleteDir() }

    if (opts.copyBaseline) {
        fetchBaseline(opts)
    }
    if (!fileExists("${storage}/baseline.json")) {
        echo "WARNING: no baseline was found, so regression rules will be skipped and only " +
             "absolute thresholds run: a slowdown against earlier builds cannot be caught. " +
             baselineHint()
    }

    String loco = resolveLoco(opts)
    sh "${loco} --version"

    List args = ['--config', opts.config, 'ci',
                 '--storage', storage, '--prune',
                 '--summary', "${results}/summary.md",
                 '--junit', "${results}/junit.xml",
                 '--output', "${results}/report.html",
                 '--warning-exit-code', '2']
    if (setBaseline) {
        args.add('--set-baseline')
    }
    if (opts.users != null) {
        args.add('--users')
        args.add(opts.users.toString())
    }
    if (opts.runTime != null) {
        args.add('--run-time')
        args.add(opts.runTime.toString())
    }
    for (String extra in extraArgs(opts.args)) {
        args.add(extra)
    }

    int code = 1
    try {
        code = sh(script: "${loco} ${quoteAll(args)}", returnStatus: true)
    } finally {
        publishResults(opts)
    }

    if (changeRequest && opts.comment) {
        postComment(opts, loco)
    }
    return verdict(opts, code)
}

Map resolveOptions(Map config) {
    Map defaults = [
        config              : 'loconfig.json',
        storage             : '.loco',
        resultsDir          : '.loco-results',
        users               : null,
        runTime             : null,
        args                : [],
        setBaseline         : null,
        copyBaseline        : true,
        fallbackBranches    : ['main', 'master'],
        install             : 'auto',
        locomotiveVersion   : null,
        python              : 'python3',
        failOnDegradation   : true,
        junit               : true,
        publishReport       : true,
        reportName          : 'Load test report',
        comment             : true,
        commentCredentialsId: null,
    ]
    for (Object key in config.keySet()) {
        if (!defaults.containsKey(key)) {
            error("locomotiveLoadTest: unknown option '${key}'. Known options: ${defaults.keySet().join(', ')}")
        }
    }
    Map merged = [:]
    for (Object key in defaults.keySet()) {
        merged[key] = config.containsKey(key) ? config[key] : defaults[key]
    }
    return merged
}

// The baseline is the storage directory archived by the last successful build
// of the branch this build is compared with: the target branch for a pull
// request, otherwise this branch. `lastSuccessful` includes UNSTABLE builds;
// that is safe because baseline.json inside only ever points at a run that
// passed its checks.
def fetchBaseline(Map opts) {
    List projects = []
    if (env.BRANCH_NAME) {
        List branches = []
        String compared = env.CHANGE_TARGET ?: env.BRANCH_NAME
        branches.add(compared)
        for (Object branch in opts.fallbackBranches) {
            if (!branches.contains(branch)) {
                branches.add(branch)
            }
        }
        for (Object branch in branches) {
            // A name without a slash resolves inside the multibranch project.
            projects.add(branchJob(branch.toString()))
        }
    } else {
        // Not a multibranch job: the baseline comes from this job itself.
        projects.add("/${env.JOB_NAME}")
    }

    for (Object project in projects) {
        try {
            copyArtifacts(projectName: project, selector: lastSuccessful(),
                          filter: "${opts.storage}/**", optional: true, fingerprintArtifacts: false)
        } catch (NoSuchMethodError missing) {
            echo 'The Copy Artifact plugin is not installed, so no baseline can be taken from earlier builds.'
            return
        } catch (hudson.AbortException failed) {
            echo "Could not copy the baseline from ${project}: ${failed.message}"
        }
        if (fileExists("${opts.storage}/baseline.json")) {
            echo "Baseline taken from the last successful build of ${project}"
            return
        }
    }
}

// Multibranch names a branch job with NameEncoder, which escapes only these
// characters. URLEncoder would also escape '+', '@' and non-ASCII letters and
// name a job that does not exist. '%' has to go first.
String branchJob(String branch) {
    return branch.replace('%', '%25').replace('/', '%2F').replace('#', '%23')
                 .replace('?', '%3F').replace('[', '%5B').replace(']', '%5D')
                 .replace('\\', '%5C')
}

String baselineHint() {
    if (env.CHANGE_ID) {
        return "A pull request build copies the baseline from the last successful build of " +
               "${env.CHANGE_TARGET}. That branch must have built at least once, and its Jenkinsfile " +
               "must allow the copy: options { copyArtifactPermission('<multibranch project>/*') }."
    }
    return 'On the first build of a branch that is expected.'
}

String resolveLoco(Map opts) {
    boolean onPath = sh(script: 'command -v loco >/dev/null 2>&1', returnStatus: true) == 0
    boolean install = opts.install == true || (opts.install == 'auto' && (!onPath || opts.locomotiveVersion))
    if (!install) {
        if (!onPath) {
            error("`loco` is not on this agent's PATH. Install locomotive on the agent, or use install: true")
        }
        return 'loco'
    }
    String venv = "${env.WORKSPACE}/.loco-venv"
    String pkg = opts.locomotiveVersion ? "locomotive==${opts.locomotiveVersion}" : 'locomotive'
    sh """
        set -e
        ${quote(opts.python)} -m venv ${quote(venv)}
        ${quote(venv + '/bin/python')} -m pip install --quiet --upgrade pip
        ${quote(venv + '/bin/python')} -m pip install --quiet --upgrade ${quote(pkg)}
    """
    return quote(venv + '/bin/loco')
}

def publishResults(Map opts) {
    String results = opts.resultsDir
    // The storage directory is archived for the next build to copy as its
    // baseline; the results directory is for people.
    archiveArtifacts(artifacts: "${opts.storage}/**, ${results}/**", allowEmptyArchive: true, fingerprint: false)
    if (opts.junit) {
        try {
            // The exit code decides the build result; the test report only
            // describes the checks, so it must not mark the build unstable too.
            junit(testResults: "${results}/junit.xml", allowEmptyResults: true, skipMarkingBuildUnstable: true)
        } catch (NoSuchMethodError missing) {
            echo 'The JUnit plugin is not installed: per-check results are in the archived junit.xml.'
        }
    }
    if (opts.publishReport) {
        try {
            publishHTML(target: [reportName: opts.reportName, reportDir: results, reportFiles: 'report.html',
                                 keepAll: true, alwaysLinkToLastBuild: true, allowMissing: true])
        } catch (NoSuchMethodError missing) {
            echo 'The HTML Publisher plugin is not installed: the report is in the archived artifacts.'
        }
    }
}

def postComment(Map opts, String loco) {
    String summary = "${opts.resultsDir}/summary.md"
    if (!fileExists(summary)) {
        echo 'No summary was written, so there is nothing to comment.'
        return
    }
    if (!opts.commentCredentialsId) {
        echo 'Skipping the pull request comment: set commentCredentialsId to a Secret text ' +
             'credential holding a GitHub or GitLab token.'
        return
    }
    int status = 1
    withCredentials([string(credentialsId: opts.commentCredentialsId, variable: 'LOCO_COMMENT_TOKEN')]) {
        status = sh(script: "${loco} comment --body ${quote(summary)} --token-env LOCO_COMMENT_TOKEN",
                    returnStatus: true)
    }
    if (status != 0) {
        // The results are already in the build; a comment that could not be
        // posted is not a reason to fail it.
        echo 'The pull request comment could not be posted (see above).'
    }
}

Map verdict(Map opts, int code) {
    String status = code == 0 ? 'PASS' : (code == 2 ? 'WARNING' : 'FAILED')
    String line = "Load test: ${status}"
    currentBuild.description = currentBuild.description ? "${currentBuild.description}\n${line}" : line

    Map outcome = [
        exitCode: code,
        status  : status,
        summary : "${opts.resultsDir}/summary.md",
        junit   : "${opts.resultsDir}/junit.xml",
        report  : "${opts.resultsDir}/report.html",
    ]
    if (code == 2) {
        unstable("Load test: WARNING. A metric crossed its warn threshold; see ${opts.reportName}.")
    } else if (code != 0) {
        String message = "Load test failed (loco exit code ${code}): a degradation, missing data, " +
                         "or a run that could not start. See the console output and ${opts.reportName}."
        if (code == 1 && !opts.failOnDegradation) {
            unstable(message)
        } else {
            error(message)
        }
    }
    return outcome
}

List extraArgs(Object value) {
    List out = []
    if (value == null) {
        return out
    }
    if (value instanceof List) {
        for (Object item in value) {
            out.add(item.toString())
        }
        return out
    }
    return value.toString().tokenize()
}

String quote(Object value) {
    return "'" + value.toString().replace("'", "'\"'\"'") + "'"
}

String quoteAll(List values) {
    List quoted = []
    for (Object value in values) {
        quoted.add(quote(value))
    }
    return quoted.join(' ')
}
