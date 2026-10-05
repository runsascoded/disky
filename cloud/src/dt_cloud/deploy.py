"""The deployment's own resources, from its environment (specs/oa-decoupling.md).

dt-cloud carries no deployment's bucket or site as a default: each job script
(or an operator's `.envrc`) exports them. A missing one is an error naming the
variable, never a fallback to some other deployment's resources.
"""
import os
import sys

_NOTED: set[str] = set()


def env(new: str, *old: str) -> str | None:
    """`$new`, else the first set of its deprecated names `old` (one stderr
    note per old name per process); None when none is set. Old names are
    accepted for one release, then dropped."""
    v = os.environ.get(new, "").strip()
    if v:
        return v
    for o in old:
        v = os.environ.get(o, "").strip()
        if v:
            if o not in _NOTED:
                _NOTED.add(o)
                print(f"note: ${o} is deprecated; set ${new}", file=sys.stderr)
            return v
    return None


def require(new: str, *old: str, what: str) -> str:
    """`env(new, *old)`, or exit naming the variable and what it is."""
    v = env(new, *old)
    if v is None:
        raise SystemExit(f"{new} is unset: export {what}")
    return v


def words(new: str, *old: str, what: str) -> list[str]:
    """A required space-separated list (`$ACCESS_BUCKETS="a b c"`)."""
    return require(new, *old, what=what).split()


def data_bucket() -> str:
    """The data bucket: snapshots, index tiers, sweep plans and state (`$DATA_BUCKET`)."""
    b = os.environ.get("DATA_BUCKET", "").strip()
    if not b:
        raise SystemExit("DATA_BUCKET is unset: export the deployment's data bucket (snapshots, index tiers, sweep state)")
    return b


def site_url(explicit: str | None = None) -> str:
    """The deployment's site: `explicit`, else `$SITE_URL` (`$GCS_USAGE_URL`, deprecated)."""
    u = explicit or env("SITE_URL", "GCS_USAGE_URL")
    if not u:
        raise SystemExit("no site URL: pass -u or export SITE_URL")
    return u.rstrip("/")


def site_token(explicit: str | None = None) -> str | None:
    """The site's bearer token: `explicit`, else `$SITE_TOKEN` (`$GCS_USAGE_TOKEN`,
    deprecated); None for a public deployment."""
    return explicit.strip() if explicit else env("SITE_TOKEN", "GCS_USAGE_TOKEN")
