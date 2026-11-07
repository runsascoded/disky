"""Anchor SQL timings and durable operator profiles for expensive statements."""
from hashlib import sha256
from json import dumps, loads
from pathlib import Path
from time import monotonic

from .resource_monitor import resource_stage
from .static_names import err, q


def profile_summary(profile: dict) -> dict:
    operators = []

    def visit(node: dict) -> None:
        if "operator_name" in node:
            operators.append({
                "name": node["operator_name"],
                "seconds": node.get("operator_timing", 0),
                "rows": node.get("operator_cardinality", 0),
                "scanned": node.get("operator_rows_scanned", 0),
            })
        for child in node.get("children", []):
            visit(child)

    visit(profile)
    return {
        "cpu_s": profile.get("cpu_time", 0),
        "peak_memory_bytes": profile.get("system_peak_buffer_memory", 0),
        "peak_spill_bytes": profile.get("system_peak_temp_dir_size", 0),
        "operators": sorted(operators, key=lambda op: op["seconds"], reverse=True)[:8],
    }


class AnchorConnection:
    def __init__(self, con, directory: Path, log=err, keep_seconds: float = 1):
        self.con, self.directory, self.log, self.keep_seconds = con, directory, log, keep_seconds
        self.sequence = 0
        directory.mkdir(parents=True, exist_ok=True)
        con.execute("SET enable_profiling='json'; SET profiling_coverage='ALL'")

    def __getattr__(self, name: str):
        return getattr(self.con, name)

    def execute(self, sql: str, parameters=None):
        self.sequence += 1
        profile = self.directory / f"q{self.sequence:06d}.json"
        self.con.execute(f"SET profiling_output={q(str(profile))}")
        statement = sql.strip().splitlines()[0][:120]
        info = {"query": self.sequence, "statement": statement, "sql_sha256": sha256(sql.encode()).hexdigest()}
        resource_stage("anchors SQL", **info)
        self.log("ANCHOR_QUERY " + dumps({**info, "event": "start"}, sort_keys=True))
        start = monotonic()
        try:
            result = self.con.execute(sql, parameters) if parameters is not None else self.con.execute(sql)
        except BaseException:
            self.log("ANCHOR_QUERY " + dumps({**info, "event": "failed", "wall_s": round(monotonic() - start, 3)}, sort_keys=True))
            raise
        elapsed = monotonic() - start
        detail = {}
        # Eager statements (CREATE/INSERT) finish their profile in execute; a SELECT may finish only when fetched.
        if profile.exists():
            if elapsed >= self.keep_seconds:
                detail = {"profile": profile.name, **profile_summary(loads(profile.read_text()))}
            else:
                profile.unlink()
        self.log("ANCHOR_QUERY " + dumps({**info, **detail, "event": "done", "wall_s": round(elapsed, 3)}, sort_keys=True))
        return result
