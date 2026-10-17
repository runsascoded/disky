"""`dt_cloud.run_dag`: Nextflow's own `-with-dag` Mermaid for job/nf/scan.nf reduced to the engine-neutral graph."""
from __future__ import annotations

import json

import pytest

from dt_cloud.run_dag import Dag, from_nextflow, read

# `nextflow run job/nf/scan.nf -preview -with-dag` over cw's profile (bucket-check + index-sync folded), Nextflow 26.10.2
MERMAID = """flowchart TB
    subgraph " "
    v0["channel.empty"]
    v1["channel.fromList"]
    end
    v2(["list_source"])
    v6(["index"])
    v7(["static_names"])
    v8(["publish_r2"])
    v9(["interval_store"])
    v10(["index_rows"])
    v14(["post"])
    subgraph " "
    v15[" "]
    end
    v3(( ))
    v11(( ))
    v0 --> v3
    v1 --> v2
    v2 --> v3
    v3 --> v6
    v6 --> v7
    v6 --> v8
    v6 --> v10
    v7 --> v11
    v8 --> v9
    v9 --> v11
    v10 --> v11
    v11 --> v14
    v14 --> v15
"""


def test_processes_joined_through_channels_and_operators():
    assert from_nextflow(MERMAID) == Dag(
        ("list_source", "index", "static_names", "publish_r2", "interval_store", "index_rows", "post"),
        (
            ("list_source", "index"),
            ("index", "index_rows"), ("index", "publish_r2"), ("index", "static_names"),
            ("static_names", "post"),
            ("publish_r2", "interval_store"),
            ("interval_store", "post"),
            ("index_rows", "post"),
        ),
    )


def test_labelled_edges_and_the_json():
    d = from_nextflow('flowchart TB\n    v0(["a"])\n    v1(( ))\n    v2(["b"])\n    v0 -->|out| v1\n    v1 --> v2\n')
    assert json.loads(d.to_json()) == {"nodes": [{"id": "a", "label": "a"}, {"id": "b", "label": "b"}], "edges": [["a", "b"]]}


def test_unknown_engine_refused():
    with pytest.raises(ValueError) as e:
        read("airflow", "")
    assert str(e.value) == "no DAG reader for engine 'airflow' (have: nextflow)"
