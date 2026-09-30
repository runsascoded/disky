import json

from click.testing import CliRunner

from dt_cloud.cli import main


def write_tree(path, tree):
    path.mkdir(parents=True)
    (path / "tree.json").write_text(json.dumps(tree))


def test_compare_bucket_level_and_depth2(tmp_path):
    a = {
        "n": "",
        "c": [
            {"n": "bkt1", "o": 10, "b": 2_000_000_000_000, "c": [
                {"n": "x", "o": 6, "b": 1_500_000_000_000},
                {"n": "y", "o": 4, "b": 500_000_000_000},
            ]},
            {"n": "bkt2", "o": 5, "b": 1_000_000_000_000},
        ],
    }
    b = {
        "n": "",
        "c": [
            {"n": "bkt1", "o": 30, "b": 5_000_000_000_000, "c": [
                {"n": "x", "o": 26, "b": 4_500_000_000_000},
                {"n": "y", "o": 4, "b": 500_000_000_000},
            ]},
            {"n": "bkt2", "o": 5, "b": 1_000_000_000_000},
        ],
    }
    write_tree(tmp_path / "a", a)
    write_tree(tmp_path / "b", b)

    r = CliRunner().invoke(main, ["compare", str(tmp_path / "a"), str(tmp_path / "b")])
    assert r.exit_code == 0
    assert r.output.rstrip().split("\n") == [
        "path          a objs         b objs        Δobjs      a TB      b TB      ΔTB",
        "bkt1              10             30          +20       2.0       5.0     +3.0",
        "bkt2               5              5           +0       1.0       1.0     +0.0",
        "TOTAL             15             35          +20       3.0       6.0     +3.0",
    ]

    r = CliRunner().invoke(main, ["compare", "-d", "2", "-n", "1", str(tmp_path / "a"), str(tmp_path / "b")])
    assert r.exit_code == 0
    # top-1 by |Δbytes| is bkt1/x; TOTAL still covers all paths, truncation is announced
    assert r.output.rstrip().split("\n") == [
        "path           a objs         b objs        Δobjs      a TB      b TB      ΔTB",
        "bkt1/x              6             26          +20       1.5       4.5     +3.0",
        "(… 2 more paths)",
        "TOTAL              15             35          +20       3.0       6.0     +3.0",
    ]
