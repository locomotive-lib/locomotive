"""Static checks on the Jenkins shared library, its examples and `init --jenkinsfile`.

No Jenkins runs in this suite. These catch drift between the Groovy step and
the CLI it drives, between the step's options and their documentation, and
the constructs that break on Jenkins itself: calls the Groovy sandbox rejects,
and trailing commas in argument lists, which Jenkins's Groovy 2.4 does not
parse.
"""

import re
from pathlib import Path

import pytest

from locomotive import __version__, cli
from locomotive.template import generate_jenkinsfile

ROOT = Path(__file__).resolve().parents[1]
STEP = ROOT / "jenkins" / "vars" / "locomotiveLoadTest.groovy"
README = ROOT / "jenkins" / "README.md"
EXAMPLES = sorted((ROOT / "jenkins" / "examples").iterdir())


def known_options():
    parser = cli.build_parser()
    subparsers = next(a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction")
    options = set(parser._option_string_actions)
    for name in ("ci", "comment"):
        options |= set(subparsers.choices[name]._option_string_actions)
    return options


def code_only(text):
    """Groovy without `//` comments."""
    return "\n".join(line.split("//")[0] for line in text.splitlines())


def flags(text):
    """`--flags` handed to loco: pip's own flags in the install lines are not."""
    lines = [line for line in code_only(text).splitlines() if "pip install" not in line]
    return set(re.findall(r"(?<![\w-])(--[a-z][a-z-]*)", "\n".join(lines)))


def step_defaults():
    block = re.search(r"Map defaults = \[(.*?)\n    \]", STEP.read_text(encoding="utf-8"), re.S)
    return re.findall(r"^\s*(\w+)\s*:", block.group(1), re.M)


def test_flags_the_step_passes_exist():
    assert flags(STEP.read_text(encoding="utf-8")) <= known_options()


def test_every_option_is_documented():
    documented = set(re.findall(r"^\| `(\w+)` \|", README.read_text(encoding="utf-8"), re.M))
    assert step_defaults()
    assert set(step_defaults()) == documented


@pytest.mark.parametrize("path", [STEP] + EXAMPLES, ids=lambda p: p.name)
def test_no_trailing_comma_before_a_closing_paren(path):
    code = "\n".join(line.split("//")[0] for line in path.read_text(encoding="utf-8").splitlines())
    assert not re.search(r",\s*\)", code)


def test_step_avoids_what_the_sandbox_rejects():
    code = code_only(STEP.read_text(encoding="utf-8"))
    for forbidden in ("new File", "JsonSlurper", "rawBuild", "Jenkins.instance", "@Grab", "URLEncoder"):
        assert forbidden not in code, forbidden


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_example_flags_exist(path):
    assert flags(path.read_text(encoding="utf-8")) <= known_options()


class TestGeneratedJenkinsfile:
    @pytest.fixture
    def text(self, tmp_path):
        path = tmp_path / "Jenkinsfile"
        generate_jenkinsfile(path, config_name="perf/loconfig.yaml")
        return path.read_text(encoding="utf-8")

    def test_pins_the_library_and_the_cli_to_this_release(self, text):
        assert f"identifier: 'locomotive@v{__version__}'" in text
        assert f"locomotiveVersion: '{__version__}'" in text

    def test_loads_the_library_from_its_directory(self, text):
        assert "libraryPath: 'jenkins/'" in text

    def test_passes_the_config(self, text):
        assert "config: 'perf/loconfig.yaml'" in text

    def test_allows_pull_requests_to_copy_the_baseline(self, text):
        assert "copyArtifactPermission(" in text

    def test_no_trailing_commas(self, text):
        code = "\n".join(line.split("//")[0] for line in text.splitlines())
        assert not re.search(r",\s*\)", code)


def test_init_writes_a_jenkinsfile(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init", "--jenkinsfile"]) == 0
    assert (tmp_path / "loconfig.json").exists()
    assert "config: 'loconfig.json'" in (tmp_path / "Jenkinsfile").read_text(encoding="utf-8")


def test_init_keeps_an_existing_jenkinsfile(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "Jenkinsfile").write_text("mine", encoding="utf-8")
    cli.main(["init", "--jenkinsfile"])
    assert (tmp_path / "Jenkinsfile").read_text(encoding="utf-8") == "mine"
    assert "Skipped: Jenkinsfile already exists" in capsys.readouterr().out


def test_the_step_knows_its_release():
    # The step installs, and checks for, the CLI of its own release by default.
    match = re.search(r"String libraryVersion\(\) \{.*?return '([^']+)'", STEP.read_text(encoding="utf-8"), re.S)
    assert match and match.group(1) == __version__


def test_the_example_pins_the_cli_it_was_written_for():
    example = ROOT / "jenkins" / "examples" / "Jenkinsfile.without-library"
    assert f"locomotive=={__version__}" in example.read_text(encoding="utf-8")


def test_warnings_exit_3_and_turn_the_build_unstable():
    # 2 is argparse's exit code for a mistyped command line.
    step = code_only(STEP.read_text(encoding="utf-8"))
    assert "'--warning-exit-code', '3'" in step
    assert "code == 3 ? 'WARNING'" in step
    assert "if (code == 3) {" in step

    example = code_only((ROOT / "jenkins" / "examples" / "Jenkinsfile.without-library").read_text(encoding="utf-8"))
    assert "--warning-exit-code 3" in example
    assert "env.LOCO_EXIT_CODE == '3'" in example


def test_the_virtualenv_goes_on_path():
    # loco starts `locust` by name from the same virtualenv. Calling loco by
    # its full path instead ended the run with "file not found: locust".
    step = code_only(STEP.read_text(encoding="utf-8"))
    assert "PATH+LOCO=" in step
    assert "/bin/loco" not in step

    example = code_only((ROOT / "jenkins" / "examples" / "Jenkinsfile.without-library").read_text(encoding="utf-8"))
    assert "export PATH=" in example
    assert ".loco-venv/bin/loco" not in example


def test_version_flag(capsys):
    # The step logs `loco --version` so a build shows which CLI judged it.
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"locomotive {__version__}"
