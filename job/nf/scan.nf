// Every deployment's scan as one DAG (specs/pipelines-everywhere.md): each step is `dt-cloud scan step <name> [arg]`
// in the scan image, run as its own task once its inputs are done. Steps hand off through the data store (the work
// dir's bucket, FUSE-mounted in every google-batch task); the channels carry only `done` markers, which `-resume`
// keys on. `dt-cloud scan head` writes the params (from the deployment's profile), starts and finishes the scan-run
// record, and resumes a failed scan's session.
//
//   bucket-check, pre ─┐
//   list ×source ──────┴→ [index ×part →] index ─┬→ static-names ─────────────────┐
//                                ├→ publish-r2 → interval-store ──┼→ index-sync → post (over-time, index-gc, …)
//                                └→ index-rows ───────────────────┘
//
// `params.index_parts` (`options.index.parts`): the index sharded, one `index_part` task per part (`scan step index
// <part>`: a bucket, or a key range of one), then `index` merges them into the one pass's outputs (`index_parts`).
//
// `post` runs the enabled post steps in registry order (`scan_steps.STEPS`), e.g. `retire` (opt-in via the profile's
// `steps`): after both stores and the pointers, so a scan is retired only once the stores serve it.
//
// `params.fold`: bucket-check runs in the first source's list task, index-sync in the post task (a task's VM boots in
// ~1.5–2 min; the local executor can't take a cloud work dir, so tiny steps can't run in the head).
nextflow.enable.dsl = 2

params.scan = null
params.gen = null
params.job_start = null
params.run_id = ''
params.sources = []        // [[uri:, machine:, disk: [request:, type:], secrets: [ENV=SECRET, …] (only a source's own)]]
params.list_default = [:]  // the profile's `machines.list`: [machine:, disk:]
params.steps = []          // every enabled step (core minus `skip`, plus the profile's `steps`)
params.pre = []            // enabled extra steps beside `list`, before `index` (soft: index runs without them)
params.pre_label = 'small'
params.interval_label = 'big'
params.index_parts = []    // the sharded index's part ids (`options.index.parts`), each its own task before `index`
params.index_part_label = 'big'
params.over_time = false
params.over_time_label = 'post'
params.post = []           // the post task's steps, in order
params.fold = []
params.skip_list = false   // REPROC: run from `index`
params.static_after_publish = false   // a local data dir: static-names reads the serving store, after publish-r2
params.secrets = []        // `ENV=SECRET`, fetched in-task (never Nextflow's `secret`: it lands in .command.run)
params.step_secrets = [:]  // step → its whole `ENV=SECRET` list, where `options.<step>.secrets` overlays the profile's
params.secrets_project = ''
params.env = [:]
params.profile_json = ''

def q(v) { "'" + v.toString().replace("'", "'\\''") + "'" }

def on(String s) { s in params.steps }

def folded(String s) { on(s) && s in params.fold }

def stepEnv(task) {
    // NF_PROCESS / NF_TAG: the task's process and tag, so a step's scan-run phases name the DAG node that ran them;
    // NF_MACHINE: the machine type it asked for (a fallback label's: its submit attempt's, spec §7). `DT_RUN_ID` and
    // `DT_SCAN_PROFILE_JSON` come from the head's `run.config` `env` scope instead: in the script they'd be in every
    // task's hash, so a resume from a new head job (or after a profile edit) would miss the whole cache.
    def e = [SNAP_ID: params.scan, SCAN_ID: params.scan, GEN: params.gen, DT_JOB_START: params.job_start,
             NF_PROCESS: task.process, NF_TAG: task.tag ?: '', NF_MACHINE: task.machineType ?: ''] + params.env
    e.collect { k, v -> "export ${k}=${q(v)}" }.join('\n')
}

// A call's `ENV=SECRET` bindings: a list call's source's own (`sources[].secrets`, already overlaid), else the step's
// (`params.step_secrets`: `options.<step>.secrets` over the profile's), else the profile's. Each call is its own
// `secrets exec`, so a folded step never sees its neighbour's.
def secretsFor(String n, String a) {
    def src = (n == 'list' && a) ? params.sources.find { it.uri == a } : null
    src?.secrets != null ? src.secrets : (params.step_secrets[n] != null ? params.step_secrets[n] : params.secrets)
}

// The task's script: each [name, arg] step through `secrets exec`, then the `done` marker.
def stepScript(task, List steps) {
    def proj = params.secrets_project ? "-p ${q(params.secrets_project)} " : ''
    def calls = steps.collect { s ->
        def (n, a) = s instanceof List ? s : [s, '']
        def sec = secretsFor(n, a).collect { "-s ${q(it)} " }.join('')
        "dt-cloud secrets exec ${proj}${sec}-- dt-cloud scan step ${n}${a ? ' ' + q(a) : ''}"
    }
    def done = "echo ${q(steps.collect { it instanceof List ? it.join(' ') : it }.join(', '))} > done"
    ([stepEnv(task)] + calls + [done]).join('\n') + '\n'
}

// Every item of `ch` as one sorted list of strings (`[]` for none): a task's input must not depend on the order its
// upstream tasks finished in, or `-resume` misses its cache.
def gathered(ch) { ch.toList().map { l -> l.collect { it.toString() }.sort() } }

def diskOf(d) {
    if (!d) return null
    def size = nextflow.util.MemoryUnit.of("${d.request} GB")
    d.type ? [request: size, type: d.type] : size
}

process bucket_check {
    label 'small'
    output: path 'done'
    script: stepScript(task, ['bucket-check'])
}

process pre {
    label params.pre_label  // `pre` when the profile sizes it (`machines.pre`: gcs's access ingest wants ~64 GB), else `small`
    output: path 'done'
    script: stepScript(task, params.pre)
}

process list_source {
    tag "${src.uri}"
    label 'list'
    machineType { src.machine ?: params.list_default.machine }
    disk { diskOf(src.disk ?: params.list_default.disk) }
    input: val src
    output: path 'done'
    script:
    def first = params.sources && src.uri == params.sources[0].uri
    stepScript(task, (first && folded('bucket-check') ? ['bucket-check'] : []) + [['list', src.uri]])
}

process index_part {
    tag "${part}"
    label params.index_part_label  // `index_part` when the profile sizes it (`machines.index_part`), else `big`
    input: val part; val deps
    output: path 'done'
    script: stepScript(task, [['index', part]])
}

process index {
    label 'big'
    input: val deps
    output: path 'done'
    script: stepScript(task, ['index'])
}

process static_names {
    label 'small'
    time '6h'
    input: val indexed
    output: path 'done'
    script: stepScript(task, ['static-names'])
}

process publish_r2 {
    label 'medium'
    input: val indexed
    output: path 'done'
    script: stepScript(task, ['publish-r2'])
}

process interval_store {
    label params.interval_label  // `interval` when the profile sizes it (`machines.interval`), else `big` (index's machine)
    input: val published
    output: path 'done'
    script: stepScript(task, ['interval-store'])
}

process index_rows {
    label 'small'
    input: val indexed
    output: path 'done'
    script: stepScript(task, ['index-rows'])
}

process index_sync {
    label 'small'
    input: val deps
    output: path 'done'
    script: stepScript(task, ['index-sync'])
}

process post {
    label 'post'
    input: val deps
    output: path 'done'
    script: stepScript(task, (folded('index-sync') ? ['index-sync'] : []) + params.post)
}

// over-time apart from `post`: every Nth scan it builds a sealed group (cw's 16-scan groups: ~90 min), which in `post`
// held warm-cache, probe and digest behind it. Its own task (`machines.over_time`, else post's), beside `post` once
// the scan's stores are written (after index-sync when that is its own task; it needs the scan's outputs, not its
// listing), so a failed post step never stops it.
process over_time {
    label params.over_time_label
    input: val deps
    output: path 'done'
    script: stepScript(task, ['over-time'])
}

workflow {
    if (!params.scan || !params.gen || !params.job_start) error "--scan, --gen and --job_start are required (`dt-cloud scan head` sets them)"
    def ready = Channel.empty()
    if (on('bucket-check') && !folded('bucket-check') && !params.skip_list) ready = ready.mix(bucket_check())
    if (params.pre) ready = ready.mix(pre())
    if (!params.skip_list) ready = ready.mix(list_source(Channel.fromList(params.sources)))
    def listed = gathered(ready)
    def indexed = index(params.index_parts ? gathered(index_part(Channel.fromList(params.index_parts), listed)) : listed)
    def none = Channel.value('skipped')
    def pub = on('publish-r2') ? publish_r2(indexed) : indexed
    // On a local data dir the static index reads earlier scans from the serving store, so it waits for this one's publish
    def st = on('static-names') ? static_names(params.static_after_publish ? pub : indexed) : none
    def iv = on('interval-store') ? interval_store(pub) : pub
    def rows = index_rows(indexed)
    def serving = gathered(rows.mix(st, iv))
    def synced = folded('index-sync') ? serving : gathered(index_sync(serving))
    if (params.post || folded('index-sync')) post(synced)
    if (params.over_time) over_time(synced)
}
