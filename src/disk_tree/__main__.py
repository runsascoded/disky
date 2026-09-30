"""`python -m disk_tree …` — the CLI without needing `disk-tree` on `PATH`
(what `disk-tree repos` shells out to, so it works from any interpreter that
has the package installed)."""
from disk_tree.cli.main import cli

cli(prog_name="disk-tree")
