#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["click"]
# ///
"""Read-only inventory and local-link audit of every spec, including archives."""
from json import dumps
from pathlib import Path
from re import finditer, match, sub

from click import command, option


def audit(root: Path) -> dict:
    root = root.resolve()
    files = sorted(root.rglob("*.md"))
    inventory = []
    broken = []
    for path in files:
        text = path.read_text()
        lines = text.splitlines()
        inventory.append({
            "path": str(path.relative_to(root)),
            "archive": "done" in path.relative_to(root).parts,
            "title": next((line.removeprefix("# ") for line in lines if line.startswith("# ")), None),
            "opening": "\n".join(lines[2:8]),
        })
        # Code examples are not Markdown links. Keep line numbers stable.
        prose = sub(r"```[^\n]*\n.*?```", lambda m: "\n" * m[0].count("\n"), text, flags=16)
        prose = sub(r"(`+)(.*?)\1", lambda m: sub(r"[^\n]", " ", m[0]), prose, flags=16)
        targets = [(m.start(), m[1]) for m in finditer(r"\]\(([^\s)]+)(?:\s+[^)]*)?\)", prose)]
        targets += [(m.start(), m[1]) for m in finditer(r"(?m)^\[[^\]]+\]:\s*(\S+)", prose)]
        for offset, target in targets:
            target = target.strip("<>").split("#", 1)[0]
            if not target or target.startswith("/") or match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target):
                continue
            target_path = path.parent / target
            if not target_path.exists():
                broken.append({"path": str(path.relative_to(root)), "line": prose[:offset].count("\n") + 1, "target": target})
    return {"files": len(files), "active": sum(not entry["archive"] for entry in inventory), "inventory": inventory, "broken_links": broken}


@command()
@option("-j", "--json", "as_json", is_flag=True, help="Emit complete structured inventory and broken-link report")
@option("-r", "--root", type=Path, default=Path("specs"), help="Specs directory (default specs)")
def cli(as_json: bool, root: Path) -> None:
    result = audit(root)
    if as_json:
        print(dumps(result, indent=2))
    else:
        print(f'{result["files"]} specs ({result["active"]} active); {len(result["broken_links"])} broken local links')
        for link in result["broken_links"]:
            print(f'{link["path"]}:{link["line"]}: {link["target"]}')


if __name__ == "__main__":
    cli()
