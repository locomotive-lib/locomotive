"""Static checks on the composite GitHub Action and the generated workflow.

Nothing runs GitHub Actions in this test suite, so these catch the mistakes
that would otherwise surface only on someone's pull request: an input used
but never declared, a step output read from a step id that does not exist, a
`loco` flag the CLI does not accept, and context values pasted straight into
a shell script.
"""

import re
from pathlib import Path

import pytest
import yaml

from locomotive import cli
from locomotive.template import generate_github_workflow

ACTION = Path(__file__).resolve().parents[1] / ".github" / "actions" / "loadtest" / "action.yml"


@pytest.fixture(scope="module")
def action():
    return yaml.safe_load(ACTION.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def action_text():
    return ACTION.read_text(encoding="utf-8")


def _subcommand_options(name):
    """Options `loco <name>` accepts, global ones (--config, --debug) included."""
    parser = cli.build_parser()
    subparsers = next(a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction")
    return set(parser._option_string_actions) | set(subparsers.choices[name]._option_string_actions)


def test_every_input_used_is_declared(action, action_text):
    used = set(re.findall(r"inputs\.([A-Za-z0-9_]+)", action_text))
    assert used <= set(action["inputs"])


def test_every_step_referenced_exists(action, action_text):
    ids = {step["id"] for step in action["runs"]["steps"] if "id" in step}
    referenced = set(re.findall(r"steps\.([A-Za-z0-9_-]+)\.", action_text))
    assert referenced <= ids


def test_no_expression_is_pasted_into_a_script(action):
    # Branch names and inputs are attacker-controlled on pull requests; they
    # reach scripts through env, never as text substituted into the script.
    for step in action["runs"]["steps"]:
        if "run" in step:
            assert "${{" not in step["run"], step["name"]


def test_bash_steps_declare_their_shell(action):
    for step in action["runs"]["steps"]:
        if "run" in step:
            assert step.get("shell") == "bash", step["name"]


def test_loco_flags_exist(action):
    scripts = "\n".join(step.get("run", "") for step in action["runs"]["steps"])
    ci_flags = set(re.findall(r"(--[a-z][a-z-]+)", scripts[scripts.index("args=(") : scripts.index('echo "Command')]))
    assert ci_flags <= _subcommand_options("ci")
    comment_line = next(line for line in scripts.splitlines() if "loco comment" in line)
    assert set(re.findall(r"(--[a-z][a-z-]+)", comment_line)) <= _subcommand_options("comment")


def test_outputs_come_from_the_run_step(action):
    for name, output in action["outputs"].items():
        assert "steps.run.outputs." + name in output["value"]


class TestGeneratedWorkflow:
    @pytest.fixture
    def workflow(self, tmp_path):
        path = tmp_path / "loadtest.yml"
        generate_github_workflow(path, config_name="perf/loconfig.yaml")
        return path.read_text(encoding="utf-8")

    def test_is_valid_yaml(self, workflow):
        data = yaml.safe_load(workflow)
        assert data["jobs"]["loadtest"]["runs-on"] == "ubuntu-latest"

    def test_uses_the_action_with_the_config(self, workflow):
        steps = yaml.safe_load(workflow)["jobs"]["loadtest"]["steps"]
        step = next(s for s in steps if "loadtest@" in s.get("uses", ""))
        assert step["with"]["config"] == "perf/loconfig.yaml"

    def test_grants_what_the_action_needs(self, workflow):
        permissions = yaml.safe_load(workflow)["permissions"]
        assert permissions["actions"] == "read"
        assert permissions["pull-requests"] == "write"

    def test_runs_the_load_test_once(self, workflow):
        # The old template ran `loco ci`, then ran the whole test again with
        # --set-baseline, and still had no baseline on the next workflow run.
        steps = yaml.safe_load(workflow)["jobs"]["loadtest"]["steps"]
        assert not [s for s in steps if "loco " in s.get("run", "")]
        assert sum("loadtest@" in s.get("uses", "") for s in steps) == 1
