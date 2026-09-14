"""The report's stylesheet file, for viewers that refuse inline styles.

Jenkins serves published HTML with `style-src 'self'`: the inline <style>
block is dropped, and the report opened as bare, unstyled tables. A stylesheet
next to the report comes from the same origin, which that policy allows.
"""

from locomotive import cli
from locomotive.report_config import resolve_report_config
from locomotive.reporter import render_report, render_stylesheet
from locomotive.storage import Storage


def render(**kwargs):
    return render_report({"run_id": "r1"}, {"requests": 100, "p95_ms": 12.0}, None, None, "T", **kwargs)


def inline_css(page):
    start = page.index("<style>\n") + len("<style>\n")
    return page[start:page.index("\n  </style>", start)]


def test_no_link_unless_asked():
    assert "<link" not in render()


def test_links_the_stylesheet():
    assert '<link rel="stylesheet" href="report.css" />' in render(stylesheet="report.css")


def test_keeps_the_inline_copy_for_a_report_opened_on_its_own():
    assert "<style>" in render(stylesheet="report.css")


def test_the_href_is_escaped():
    assert 'href="x&quot;&gt;.css"' in render(stylesheet='x">.css')


def test_the_file_carries_the_same_rules_as_the_page():
    raw = {"theme": {"mode": "dark", "colors": {"primary": "#123456"}}, "branding": {"color": "#ff0000"}}
    page = render(report_config=resolve_report_config(raw))
    assert render_stylesheet(resolve_report_config(raw)).strip() == inline_css(page).strip()


def test_default_stylesheet():
    css = render_stylesheet()
    assert ":root" in css and ".chart-fallback" in css


def test_every_copy_of_the_report_gets_its_stylesheet(tmp_path):
    storage = Storage.from_root(tmp_path / "artifacts")
    storage.ensure_run("r1")
    storage.save_json(storage.metrics_path("r1"), {"requests": 10, "p95_ms": 5.0})
    output = tmp_path / "published" / "index.html"

    cli._report(storage, "r1", None, "T", str(output), report_cfg={})

    run_report = storage.report_path("r1")
    assert 'href="report.css"' in run_report.read_text(encoding="utf-8")
    assert (run_report.parent / "report.css").read_text(encoding="utf-8").strip()
    assert 'href="index.css"' in output.read_text(encoding="utf-8")
    assert (output.parent / "index.css").read_text(encoding="utf-8").strip()
