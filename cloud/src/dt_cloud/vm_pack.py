"""`dt-cloud vm-pack`: VM mode's pack writer (specs/vm-mode.md §6).

After the interval store's append, the store's current generation and its newest manifest become one slim pack
(`fsb pack-slim`, `bench/full-scan/`), published next to the store:

    <store>/vm-pack/<gen>/<manifest stem>.fsb     the pack (its header records `{gen, manifest}`: the pins vmd checks)
    <store>/vm-pack/CURRENT.json                  {gen, manifest, key, bytes, sha256, scans, written}, written last

`build` is idempotent: when `CURRENT.json` already names the generation and manifest, it does nothing. Older packs
beyond the newest `keep` are deleted (the serving VM may still be mapping the previous one).

`submit` runs `build` as a Batch job of its own (§6.3): the writer's loader holds the store's rows in RAM once (gcs: 117
GB), so it gets its own machine (default a spot n2-highmem-16) rather than sizing the scan's. One job per pack
(`job_id`), submitted and not waited on.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from functools import partial
from pathlib import Path

from click import IntRange, Path as PathType, argument, group, option

from .append_runner import current, label, manifest_keys
from .blobstore import Store, scheme, store_url

err = partial(print, file=sys.stderr)

#: The interval store's prefix in a store (the site's `interval-store/CURRENT.json`).
STORE_PREFIX = "interval-store"
#: Where packs go, beside it.
PREFIX = "vm-pack"
POINTER = f"{PREFIX}/CURRENT.json"


def newest(store: Store) -> tuple[str, str | None]:
    """`(generation, newest manifest's file name or None)`: `interval-store/CURRENT.json`'s generation and its newest
    manifest (by scan, then revision), as the site's `newestManifest` picks it."""
    gen = current(store, STORE_PREFIX)
    if gen is None:
        raise FileNotFoundError(f"{store.uri(STORE_PREFIX)}: no CURRENT.json")
    if not store.exists(f"{STORE_PREFIX}/{gen}/scans.json"):
        raise FileNotFoundError(f"{store.uri(f'{STORE_PREFIX}/{gen}')}: no scans.json")
    g = f"{STORE_PREFIX}/{gen}/"
    ms = manifest_keys([k.removeprefix(g) for k in store.listing(f"{g}manifests")])
    return gen, (ms[-1].removeprefix("manifests/") if ms else None)


def sorts(store: Store, gen: str, manifest: str | None) -> list[str]:
    """The keys `fsb` reads, relative to the generation: `scans.json`, the manifest, the base's `path` sort, then each
    of the manifest's runs' `path` sorts."""
    keys = ["scans.json", "served/path.parquet"]
    if manifest:
        keys.insert(1, f"manifests/{manifest}")
        m = store.read_json(f"{STORE_PREFIX}/{gen}/manifests/{manifest}")
        keys += [f"{r['key']}/served/path.parquet" for r in m["runs"]]
    return keys


def pack_key(gen: str, manifest: str | None) -> str:
    stem = manifest.removesuffix(".json") if manifest else "base"
    return f"{PREFIX}/{gen}/{stem}.fsb"


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while b := f.read(1 << 24):
            h.update(b)
    return h.hexdigest()


def fsb_path(given: str | None) -> str:
    """`fsb`: `--fsb`, `$FSB`, or on `PATH`."""
    p = given or os.environ.get("FSB") or shutil.which("fsb")
    if not p:
        raise FileNotFoundError("fsb not found (--fsb, $FSB, or PATH): the job image needs bench/full-scan's `fsb`")
    return p


def published(store: Store, gen: str, manifest: str | None) -> dict | None:
    """`vm-pack/CURRENT.json` when it already names `gen` and `manifest` (the pack is written), else None."""
    if store.exists(POINTER):
        cur = store.read_json(POINTER)
        if cur.get("gen") == gen and cur.get("manifest") == (manifest or ""):
            return cur
    return None


def build(root: str, work: Path, fsb: str, *, keep: int = 2, force: bool = False) -> dict:
    """Write and publish the pack of `root`'s current generation and newest manifest; the pointer's content (with
    `"skipped": true` when it already named them)."""
    store = Store(store_url(root))
    gen, manifest = newest(store)
    if not force and (cur := published(store, gen, manifest)):
        err(f"vm-pack: {POINTER} already holds {gen} / {manifest}")
        return {**cur, "skipped": True}
    dest = work / "gen" / gen
    if dest.exists():
        shutil.rmtree(dest)
    t0 = time.monotonic()
    for k in sorts(store, gen, manifest):
        (dest / k).parent.mkdir(parents=True, exist_ok=True)
        store.get(f"{STORE_PREFIX}/{gen}/{k}", dest / k)
    mirror_s = time.monotonic() - t0
    out = work / "pack.fsb"
    t1 = time.monotonic()
    r = subprocess.run([fsb, "pack-slim", str(dest), "-o", str(out)], stdout=subprocess.PIPE, text=True, check=True)
    info = json.loads(r.stdout.strip().splitlines()[-1])
    pack_s = time.monotonic() - t1
    key = pack_key(gen, manifest)
    doc = {"gen": gen, "manifest": manifest or "", "key": key, "bytes": out.stat().st_size, "sha256": sha256(out),
           "scans": info["scans"], "written": int(time.time()), "mirror_s": round(mirror_s, 1), "pack_s": round(pack_s, 1),
           "peak_rss_gb": info.get("load", {}).get("peak_rss_gb")}
    store.put(out, key)
    out.unlink()
    shutil.rmtree(dest)
    if (now := newest(store)) != (gen, manifest):
        # A newer manifest landed while this one was packed (two scans' jobs overlapping): its own job publishes it;
        # this pack must not move the pointer back, nor prune what the pointer may name.
        err(f"vm-pack: {key} written, but the store moved on to {now[0]} / {now[1]}: pointer left as it is")
        return {**doc, "superseded": True}
    store.write_text(POINTER, json.dumps(doc) + "\n")
    pruned = prune(store, keep, key)
    err(f"vm-pack: {key} ({doc['bytes']:,} B, {doc['scans']} scans; mirror {mirror_s:.0f} s, pack {pack_s:.0f} s), pruned {pruned}")
    return doc


def prune(store: Store, keep: int, current_key: str) -> list[str]:
    """Delete packs beyond the newest `keep` (by when they were written), never the current one."""
    packs = sorted(((t, k) for k, t in store.mtimes(PREFIX).items() if k.endswith(".fsb")), reverse=True)
    old = [k for _, k in packs[keep:] if k != current_key]
    if old:
        store.delete(old)
    return old


# The Batch job (§6.3): `build` on a machine of its own, submitted by the scan step and not waited on.

#: The job's machine unless one is named: the loader's peak (gcs: 117 GB) fits a 128 GB box.
MACHINE = "n2-highmem-16"
#: Memory a task may ask for per vCPU, by machine family (Batch refuses a task its machine can't hold); another family
#: names its own (`memory_mib`).
MIB_PER_VCPU = {"highmem": 7700, "standard": 3700, "highcpu": 900}
#: The work dir's local SSD (the mirrored sorts and the pack: ~1.3× the pack); 0: the boot disk (`BOOT_GB`).
SSD_GB = 375
BOOT_GB = 100
#: A run's cap: gcs's writer takes ~10 minutes (mirror, 9 minutes of `pack-slim`, the upload).
MAX_RUN_S = 4 * 3600
#: A spot job's retries (a preemption); a rerun after a published pack is a no-op.
SPOT_RETRIES = 2
#: How long a submit retries a rate limit or a server error (`batch.submit_job`'s `retry_for`).
SUBMIT_RETRY_S = 300
COMPONENT = "vm-pack"
#: What the step's `options.vm-pack.batch` may name, and each one's type (`submit_args` turns it into `submit`'s flags).
BATCH_KEYS: dict[str, type] = {"image": str, "service_account": str, "region": str, "project": str, "machine": str,
                               "spot": bool, "ssd_gb": int, "memory_mib": int, "env": dict, "secrets": dict,
                               "cross_region": bool}


def job_id(deployment: str, gen: str, manifest: str | None) -> str:
    """`vm-pack-<deployment>-<gen>-<manifest stem>` under Batch's id rule: one job per pack, so a resubmit (a resumed
    run, a retried step) meets the job already there (409: counted as submitted). Past 63 characters it is cut, with 8
    hex of the whole id's sha256, so two long ids still differ."""
    from .batch import check_job_id

    stem = manifest.removesuffix(".json") if manifest else "base"
    raw = f"{COMPONENT}-{deployment}-{gen}-{stem}".lower()
    jid = re.sub(r"-+", "-", re.sub(r"[^a-z0-9-]", "-", raw)).strip("-")
    if len(jid) > 63:
        jid = f"{jid[:54].rstrip('-')}-{hashlib.sha256(raw.encode()).hexdigest()[:8]}"
    return check_job_id(jid)


def gcs_bucket(root: str) -> str | None:
    """A `gs://` store's bucket (whose region the job's is checked against); None for any other store."""
    url = store_url(root)
    return url.split("://", 1)[1].split("/", 1)[0] if scheme(url) == "gs" else None


def secret_ref(name: str, project: str) -> str:
    """A Secret Manager secret's latest version: `name` as given when it is already a `projects/…` path."""
    return name if name.startswith("projects/") else f"projects/{project}/secrets/{name}/versions/latest"


def job_spec(root: str, *, gen: str, image: str, service_account: str, region: str, project: str, keep: int = 2,
             machine: str = MACHINE, spot: bool = True, ssd_gb: int = SSD_GB, memory_mib: int | None = None,
             env: Mapping[str, str] | None = None, secrets: Mapping[str, str] | None = None,
             environ: Mapping[str, str] | None = None) -> dict:
    """The Batch job running `dt-cloud vm-pack build -o root` once, in `image` (which must carry `fsb`: `job/Dockerfile`
    `FSB=build`), as `service_account`, in `region`, on `machine` (spot unless `spot` is false), its work dir on a
    `ssd_gb` local SSD (0: the boot disk). `env` and `secrets` (`{VAR: secret name}`, from `project`'s Secret Manager)
    reach the task: a store outside GCS needs its credentials there. Cost-labelled `component=vm-pack`
    (`label_batch_spec`, from `environ`'s `$DISKY_LABELS`, + its `$SCAN_ID` / `$SCAN_RUN_ID`)."""
    from .cost_labels import label_batch_spec

    parts = machine.split("-")
    # `<family>-<kind>-<vcpus>`, or a custom type `<family>-custom-<vcpus>-<MiB>`
    vcpus = int(parts[parts.index("custom") + 1] if "custom" in parts else parts[-1])
    if memory_mib is None:
        family = machine.split("-")[1] if machine.count("-") >= 2 else ""
        if family not in MIB_PER_VCPU:
            raise ValueError(f"machine {machine}: name its memory (memory_mib); known families: {', '.join(MIB_PER_VCPU)}")
        memory_mib = vcpus * MIB_PER_VCPU[family]
    work = "/stage/vm-pack" if ssd_gb else "/tmp/vm-pack"
    build_cmd = ["dt-cloud", "vm-pack", "build", "-o", root, "-w", work, "-k", str(keep)]
    environment: dict = {"variables": dict(env or {})}
    if secrets:
        environment["secretVariables"] = {k: secret_ref(v, project) for k, v in sorted(secrets.items())}
    container: dict = {"imageUri": image, "entrypoint": "bash", "commands": ["-c", f"dt-cloud image-sha >&2; exec {shlex.join(build_cmd)}"]}
    task: dict = {
        "runnables": [{"container": container}],
        "environment": environment,
        "computeResource": {"cpuMilli": vcpus * 1000, "memoryMib": memory_mib},
        "maxRetryCount": SPOT_RETRIES if spot else 0,
        "maxRunDuration": f"{MAX_RUN_S}s",
    }
    policy: dict = {"machineType": machine, "provisioningModel": "SPOT" if spot else "STANDARD",
                    "bootDisk": {"type": "pd-balanced", "sizeGb": str(BOOT_GB)}}
    if ssd_gb:
        container["volumes"] = ["/mnt/disks/stage:/stage:rw"]
        task["volumes"] = [{"deviceName": "stage", "mountPath": "/mnt/disks/stage"}]
        policy["disks"] = [{"newDisk": {"type": "local-ssd", "sizeGb": str(ssd_gb)}, "deviceName": "stage"}]
    return label_batch_spec({
        "taskGroups": [{"taskCount": 1, "taskSpec": task}],
        "allocationPolicy": {
            "instances": [{"policy": policy}],
            "serviceAccount": {"email": service_account},
            "location": {"allowedLocations": [f"regions/{region}"]},
        },
        "labels": {"purpose": COMPONENT, "gen": label(gen)},
        "logsPolicy": {"destination": "CLOUD_LOGGING"},
    }, COMPONENT, dict(os.environ if environ is None else environ))


def submit(root: str, *, image: str, service_account: str, region: str, project: str, keep: int = 2,
           machine: str = MACHINE, spot: bool = True, ssd_gb: int = SSD_GB, memory_mib: int | None = None,
           env: Mapping[str, str] | None = None, secrets: Mapping[str, str] | None = None, cross_region: bool = False,
           dry_run: bool = False, environ: Mapping[str, str] | None = None) -> dict:
    """Submit the job writing `root`'s pack (`job_spec`), unless `vm-pack/CURRENT.json` already names the store's
    current generation and newest manifest (`{…pointer, "skipped": true}`, no job). Through `batch.submit_job`: the
    cost labels and, for a `gs://` store, its bucket's region checked against the job's (unless `cross_region`); a job
    of that id already there counts as submitted. Not waited on. `dry_run`: the id and spec, nothing submitted."""
    store = Store(store_url(root))
    gen, manifest = newest(store)
    if cur := published(store, gen, manifest):
        err(f"vm-pack: {POINTER} already holds {gen} / {manifest}: no job")
        return {**cur, "skipped": True}
    from .cost_labels import deployment_labels

    environ = dict(os.environ if environ is None else environ)
    jid = job_id(deployment_labels(environ)["deployment"], gen, manifest)
    spec = job_spec(root, gen=gen, image=image, service_account=service_account, region=region, project=project,
                    keep=keep, machine=machine, spot=spot, ssd_gb=ssd_gb, memory_mib=memory_mib, env=env,
                    secrets=secrets, environ=environ)
    out = {"job": jid, "region": region, "gen": gen, "manifest": manifest or ""}
    if dry_run:
        return {**out, "spec": spec}
    from .batch import submit_job

    bucket = gcs_bucket(root)
    submit_job(spec, jid, region, project, buckets=[bucket] if bucket else [], cross_region=cross_region, exists_ok=True,
               retry_for=SUBMIT_RETRY_S)
    err(f"vm-pack: job {jid} [{region}] submitted for {gen} / {manifest}")
    return {**out, "submitted": True}


def submit_args(batch: Mapping[str, object]) -> list[str]:
    """The step's `options.vm-pack.batch` (`BATCH_KEYS`) as `vm-pack submit`'s flags."""
    if bad := sorted(set(batch) - set(BATCH_KEYS)):
        raise ValueError(f"options.vm-pack.batch: unknown {', '.join(bad)} (known: {', '.join(BATCH_KEYS)})")
    flags = {"image": "-i", "service_account": "-a", "region": "-L", "project": "-P", "machine": "-m", "ssd_gb": "-d",
             "memory_mib": "-M"}
    args: list[str] = []
    for k, f in flags.items():
        if batch.get(k) is not None:
            args += [f, str(batch[k])]
    if batch.get("spot") is False:
        args.append("-S")
    if batch.get("cross_region"):
        args.append("-X")
    for k, v in sorted((batch.get("env") or {}).items()):
        args += ["-e", f"{k}={v}"]
    for k, v in sorted((batch.get("secrets") or {}).items()):
        args += ["-s", f"{k}={v}"]
    return args


def batch_problems(batch: object) -> list[str]:
    """What's wrong with a step's `options.vm-pack.batch` (the profile check): unknown keys, wrong types. Not a mapping
    at all is the profile's own option check's to say."""
    if not isinstance(batch, Mapping):
        return []
    names = {str: "a string", int: "an integer", bool: "true or false", dict: "a mapping of strings"}
    out = [f"options.vm-pack.batch.{k}: unknown key (known: {', '.join(BATCH_KEYS)})" for k in sorted(set(batch) - set(BATCH_KEYS))]
    for k, t in BATCH_KEYS.items():
        v = batch.get(k)
        if v is None:
            continue
        ok = (isinstance(v, t) and not (t is int and isinstance(v, bool))
              and (t is not dict or all(isinstance(a, str) and isinstance(b, str) for a, b in v.items())))
        if not ok:
            out.append(f"options.vm-pack.batch.{k}: {names[t]}")
    return out


def pairs(items: Sequence[str], what: str) -> dict[str, str]:
    """`K=V` items as a mapping."""
    out = {}
    for item in items:
        k, eq, v = item.partition("=")
        if not (k and eq):
            raise ValueError(f"{what} {item!r}: expected K=V")
        out[k] = v
    return out


@group("vm-pack")
def cli() -> None:
    """VM mode's packs (specs/vm-mode.md §6)."""


@cli.command("build")
@option("-f", "--fsb", help="The `fsb` binary (default `$FSB`, then PATH)")
@option("-F", "--force", is_flag=True, help="Write even when the pointer already names this generation and manifest")
@option("-k", "--keep", type=IntRange(1), default=2, show_default=True, help="Packs to keep under vm-pack/")
@option("-o", "--store", "root", required=True, help="The store (a dir or a gs:// / s3:// / r2:// URL) holding interval-store/")
@option("-w", "--work", type=PathType(file_okay=False, path_type=Path), default=Path("work/vm-pack"), show_default=True, help="Local work dir (the mirrored generation and the pack: ~1.3x the pack's size)")
def build_cmd(fsb: str | None, force: bool, keep: int, root: str, work: Path) -> None:
    """Write the slim pack of the store's current interval-store generation and newest manifest, and publish it under
    vm-pack/ (the pack, then `vm-pack/CURRENT.json`). Prints the pointer's JSON."""
    work.mkdir(parents=True, exist_ok=True)
    print(json.dumps(build(root, work, fsb_path(fsb), keep=keep, force=force)))


@cli.command("submit")
@option("-a", "--service-account", envvar="JOB_SA", show_envvar=True, help="The job's service account (default `$JOB_SA`)")
@option("-d", "--ssd-gb", type=IntRange(0), default=SSD_GB, show_default=True, help="Local SSD for the work dir (a multiple of 375); 0: the boot disk")
@option("-e", "--env", "env_items", multiple=True, help="K=V in the task's env (e.g. a non-GCS store's endpoint)")
@option("-i", "--image", envvar="JOB_IMAGE", show_envvar=True, help="The image (default `$JOB_IMAGE`, the scan's own); it needs `fsb` (job/Dockerfile `FSB=build`)")
@option("-k", "--keep", type=IntRange(1), default=2, show_default=True, help="Packs to keep under vm-pack/")
@option("-L", "--region", help="The job's region (default `$BATCH_REGION`): the store's")
@option("-m", "--machine", default=MACHINE, show_default=True, help="The job's machine type")
@option("-M", "--memory-mib", type=IntRange(1), help="The task's memory (default by the machine's family: highmem 7,700 MiB per vCPU)")
@option("-n", "--dry-run", is_flag=True, help="Print the job id and spec; submit nothing")
@option("-o", "--store", "root", required=True, help="The store (a dir or a gs:// / s3:// / r2:// URL) holding interval-store/")
@option("-P", "--project", help="The job's GCP project (default `$GCP_PROJECT`, else the credentials')")
@option("-s", "--secret", "secret_items", multiple=True, help="VAR=SECRET: a Secret Manager secret (a name in the project, or a projects/… path) as VAR in the task's env")
@option("-S", "--no-spot", is_flag=True, help="A standard VM, not spot")
@option("-X", "--cross-region", is_flag=True, help="Submit even when the job's region isn't the store bucket's (pays for the transfer)")
def submit_cmd(service_account: str | None, ssd_gb: int, env_items: tuple[str, ...], image: str | None, keep: int,
               region: str | None, machine: str, memory_mib: int | None, dry_run: bool, root: str, project: str | None,
               secret_items: tuple[str, ...], no_spot: bool, cross_region: bool) -> None:
    """Submit `vm-pack build -o STORE` as a Batch job of its own (specs/vm-mode.md §6.3): a spot n2-highmem-16 by
    default, cost-labelled `component=vm-pack`, one job per generation and manifest (a resubmit meets it: no second
    job). Nothing when `vm-pack/CURRENT.json` already names them. Not waited on. Prints `{job, region, gen, manifest,
    submitted}` (or the pointer, `skipped`)."""
    from .deploy import batch_region
    from .gcp import gcp_project

    if not image:
        raise SystemExit("vm-pack submit: no image (-i or $JOB_IMAGE)")
    if not service_account:
        raise SystemExit("vm-pack submit: no service account (-a or $JOB_SA)")
    doc = submit(root, image=image, service_account=service_account, region=batch_region(region, "pass -L/--region"),
                 project=project or gcp_project(), keep=keep, machine=machine, spot=not no_spot, ssd_gb=ssd_gb,
                 memory_mib=memory_mib, env=pairs(env_items, "-e"), secrets=pairs(secret_items, "-s"),
                 cross_region=cross_region, dry_run=dry_run)
    print(json.dumps(doc))


@cli.command("current")
@argument("root")
def current_cmd(root: str) -> None:
    """Print ROOT's `vm-pack/CURRENT.json` (nothing when there is none)."""
    store = Store(store_url(root))
    if store.exists(POINTER):
        print(json.dumps(store.read_json(POINTER)))
