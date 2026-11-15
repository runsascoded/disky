from json import loads
from pathlib import Path
from subprocess import run
from sys import executable


def test_spec_inventory_checks_real_links_and_ignores_code_examples(tmp_path):
    root = tmp_path / "specs"
    (root / "done").mkdir(parents=True)
    (root / "done/history.md").write_text("# History\n")
    (root / "live.md").write_text("\n".join([
        "# Live", "", "[ok](done/history.md)", "`[sample](fake)`", "",
        "```md", "[example](fake-code)", "```", "", "`multi",
        "[sample](fake-multiline)`", "", "[missing](absent.md)",
        "[ref]: absent-ref.md", "",
    ]))
    script = Path(__file__).resolve().parents[1] / "scripts/spec-audit.py"
    result = run([executable, str(script), "-j", "-r", str(root)], capture_output=True, text=True)
    assert (result.returncode, result.stderr) == (0, "")
    assert loads(result.stdout) == {
        "files": 2,
        "active": 1,
        "inventory": [
            {"path": "done/history.md", "archive": True, "title": "History", "opening": ""},
            {"path": "live.md", "archive": False, "title": "Live", "opening": "[ok](done/history.md)\n`[sample](fake)`\n\n```md\n[example](fake-code)\n```"},
        ],
        "broken_links": [
            {"path": "live.md", "line": 13, "target": "absent.md"},
            {"path": "live.md", "line": 14, "target": "absent-ref.md"},
        ],
    }
