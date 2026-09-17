"""What the CLI prints when something is wrong.

Every case here used to end in a traceback. A traceback tells the person who
wrote Locomotive where the code broke; it tells the person whose CI job just
went red nothing at all. The contract these tests hold is narrow and worth
holding: one line saying what is wrong, one line saying how to see more, and
exit code 1 — with the full traceback still one flag away.
"""

import json
import re

import pytest

from locomotive import cli
from locomotive.cli import main


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


@pytest.fixture(autouse=True)
def _no_ambient_debug(monkeypatch):
    """LOCO_DEBUG in the developer's own shell must not change the assertions."""
    monkeypatch.delenv("LOCO_DEBUG", raising=False)


# ── malformed config files ────────────────────────────────────────────


class TestMalformedConfig:
    def test_bad_yaml_is_a_message_not_a_traceback(self, tmp_path, capsys):
        # A tab where YAML wanted spaces — the single most common way to break
        # a hand-written config, and the one that used to surface as a
        # ScannerError raised somewhere inside PyYAML's own scanner.
        path = _write(tmp_path, "bad.yaml", "target:\n\thost: http://x\n")

        code = main(["--config", path, "validate"])

        err = capsys.readouterr().err
        assert code == 1
        assert "could not parse YAML" in err
        assert "bad.yaml" in err
        assert "Traceback" not in err

    def test_bad_json_names_the_line_and_column(self, tmp_path, capsys):
        text = '{\n"target": {"host": "http://x"},\n}\n'
        path = _write(tmp_path, "bad.json", text)

        code = main(["--config", path, "validate"])

        err = capsys.readouterr().err
        assert code == 1
        assert "could not parse JSON" in err
        # Where the parser points depends on the Python: 3.13 names the
        # trailing comma, older versions the brace after it. Either is a place
        # the reader can go and fix, so the position is held to one of those
        # two characters rather than to one version's line number.
        match = re.search(r"\(line (\d+), column (\d+)\)", err)
        assert match, err
        line, column = int(match.group(1)), int(match.group(2))
        assert text.splitlines()[line - 1][column - 1] in (",", "}")
        assert "Traceback" not in err

    def test_top_level_list_says_so(self, tmp_path, capsys):
        path = _write(tmp_path, "list.json", '[{"target": {"host": "http://x"}}]')

        code = main(["--config", path, "validate"])

        err = capsys.readouterr().err
        assert code == 1
        assert "must be an object at the top level" in err
        assert "got list" in err
        assert "Traceback" not in err

    def test_empty_yaml_is_not_a_crash(self, tmp_path, capsys):
        # An empty file parses to None, which is a mapping-shaped nothing.
        # It should reach validation and be told what it is missing, not
        # explode on the way there.
        path = _write(tmp_path, "empty.yaml", "\n")

        code = main(["--config", path, "validate"])

        assert code in (0, 1)
        assert "Traceback" not in capsys.readouterr().err

    def test_missing_config_points_at_init(self, tmp_path, capsys):
        code = main(["--config", str(tmp_path / "nope.json"), "validate"])

        err = capsys.readouterr().err
        assert code == 1
        assert "config file not found" in err
        assert "loco init" in err
        assert "Traceback" not in err

    def test_hint_is_printed_once(self, tmp_path, capsys):
        path = _write(tmp_path, "bad.json", "{")

        main(["--config", path, "validate"])

        err = capsys.readouterr().err
        assert err.count("--debug") == 1


# ── --debug / LOCO_DEBUG ──────────────────────────────────────────────


class TestDebugFlag:
    def test_debug_flag_shows_the_traceback(self, tmp_path, capsys):
        path = _write(tmp_path, "bad.json", "{")

        code = main(["--debug", "--config", path, "validate"])

        err = capsys.readouterr().err
        assert code == 1
        assert "Traceback" in err
        # The message still comes last, where a reader's eye lands.
        assert "could not parse JSON" in err

    def test_debug_flag_drops_the_hint(self, tmp_path, capsys):
        path = _write(tmp_path, "bad.json", "{")

        main(["--debug", "--config", path, "validate"])

        assert "Run with --debug" not in capsys.readouterr().err

    def test_env_var_shows_the_traceback(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv("LOCO_DEBUG", "1")
        path = _write(tmp_path, "bad.json", "{")

        code = main(["--config", path, "validate"])

        assert code == 1
        assert "Traceback" in capsys.readouterr().err

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off", " OFF "])
    def test_env_var_off_switches(self, value, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv("LOCO_DEBUG", value)
        path = _write(tmp_path, "bad.json", "{")

        main(["--config", path, "validate"])

        assert "Traceback" not in capsys.readouterr().err

    def test_debug_flag_survives_the_subcommand(self, tmp_path, capsys):
        # argparse puts --debug on the root parser; the subcommand namespace
        # has to carry it through or the flag silently does nothing.
        path = _write(tmp_path, "bad.json", "{")

        main(["--debug", "--config", path, "validate"])

        assert "Traceback" in capsys.readouterr().err


# ── interruption and the unexpected ───────────────────────────────────


class TestInterrupt:
    def test_ctrl_c_exits_130(self, tmp_path, capsys, monkeypatch):
        def boom(args, parser):
            raise KeyboardInterrupt

        monkeypatch.setattr(cli, "_run_command", boom)

        code = main(["--config", str(tmp_path / "whatever.json"), "validate"])

        err = capsys.readouterr().err
        assert code == 130
        assert "Interrupted." in err
        assert "Traceback" not in err


class TestUnexpectedErrors:
    def _raising(self, exc):
        def boom(args, parser):
            raise exc

        return boom

    def test_unexpected_exception_is_still_one_line(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(cli, "_run_command", self._raising(RuntimeError("kaboom")))

        code = main(["--config", str(tmp_path / "x.json"), "validate"])

        err = capsys.readouterr().err
        assert code == 1
        assert "Error: kaboom" in err
        assert "unexpected" in err
        assert "Traceback" not in err

    def test_unexpected_exception_with_debug_is_a_traceback(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(cli, "_run_command", self._raising(RuntimeError("kaboom")))

        code = main(["--debug", "--config", str(tmp_path / "x.json"), "validate"])

        assert code == 1
        assert "Traceback" in capsys.readouterr().err

    def test_exception_with_no_message_still_says_something(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(cli, "_run_command", self._raising(RuntimeError()))

        main(["--config", str(tmp_path / "x.json"), "validate"])

        assert "RuntimeError" in capsys.readouterr().err

    def test_systemexit_is_not_swallowed(self, tmp_path, monkeypatch):
        # `except Exception` must not catch SystemExit — argparse and a few
        # code paths raise it deliberately.
        monkeypatch.setattr(cli, "_run_command", self._raising(SystemExit(3)))

        with pytest.raises(SystemExit):
            main(["--config", str(tmp_path / "x.json"), "validate"])


# ── _error_message ────────────────────────────────────────────────────


class TestErrorMessage:
    def test_file_not_found_names_the_file(self):
        exc = FileNotFoundError(2, "No such file or directory", "/tmp/gone.csv")
        assert "/tmp/gone.csv" in cli._error_message(exc)
        assert "file not found" in cli._error_message(exc)

    def test_directory_where_a_file_was_expected(self):
        exc = IsADirectoryError(21, "Is a directory", "/tmp/adir")
        message = cli._error_message(exc)
        assert "directory" in message
        assert "/tmp/adir" in message

    def test_permission_denied(self):
        exc = PermissionError(13, "Permission denied", "/root/secret.json")
        assert cli._error_message(exc) == "permission denied: /root/secret.json"

    def test_generic_oserror_uses_strerror(self):
        exc = OSError(28, "No space left on device", "/artifacts/run.csv")
        message = cli._error_message(exc)
        assert "No space left on device" in message
        assert "/artifacts/run.csv" in message

    def test_json_decode_error(self):
        try:
            json.loads("{")
        except json.JSONDecodeError as exc:
            assert "could not parse JSON" in cli._error_message(exc)

    def test_yaml_error_is_labelled(self):
        yaml = pytest.importorskip("yaml")
        try:
            yaml.safe_load("a:\n\tb: 1\n")
        except yaml.YAMLError as exc:
            assert cli._error_message(exc).startswith("could not parse YAML")

    def test_plain_value_error_passes_through(self):
        assert cli._error_message(ValueError("weight must be a number")) == (
            "weight must be a number"
        )


# ── _debug_enabled ────────────────────────────────────────────────────


class TestDebugEnabled:
    class _Args:
        def __init__(self, debug=False):
            self.debug = debug

    def test_flag_wins(self, monkeypatch):
        monkeypatch.setenv("LOCO_DEBUG", "0")
        assert cli._debug_enabled(self._Args(debug=True)) is True

    @pytest.mark.parametrize("value", ["1", "true", "yes", "on", "anything"])
    def test_env_truthy(self, value, monkeypatch):
        monkeypatch.setenv("LOCO_DEBUG", value)
        assert cli._debug_enabled(self._Args()) is True

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "OFF", "  "])
    def test_env_falsy(self, value, monkeypatch):
        monkeypatch.setenv("LOCO_DEBUG", value)
        assert cli._debug_enabled(self._Args()) is False

    def test_missing_attribute_is_not_a_crash(self, monkeypatch):
        monkeypatch.delenv("LOCO_DEBUG", raising=False)

        class Bare:
            pass

        assert cli._debug_enabled(Bare()) is False
