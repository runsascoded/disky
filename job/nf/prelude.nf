// The fixed part of every scan's `scan.nf` (specs/orchestrator-abstraction.md §2): its params and the helpers its
// processes call. `dt-cloud scan head` writes each run's `scan.nf` into its launch dir: this file, then the processes and
// the workflow rendered from the scan's DAG (`scan_dag.build` over the deployment's profile, `scan_dag_nf.render`), so
// a deployment's options decide which processes exist. Each process runs `dt-cloud scan step <name> [arg]` calls in the
// scan image; steps hand off through the data store (the work dir's bucket, FUSE-mounted in every google-batch task);
// the channels carry only `done` markers, which `-resume` keys on.
nextflow.enable.dsl = 2

params.scan = null
params.gen = null
params.job_start = null
params.run_id = ''
params.sources = []        // [[uri:, machine:, disk: [request:, type:], secrets: [ENV=SECRET, …] (only a source's own),
                           //   machines: [primary, fallback…], queue_timeout: '<N>m' (with `fallback`)]]
params.list_retries = 0    // the most fallback types any source has (`list_source`'s submit-timeout retries)
params.list_default = [:]  // the profile's `machines.list`: [machine:, disk:]
params.secrets = []        // `ENV=SECRET`, fetched in-task (never Nextflow's `secret`: it lands in .command.run)
params.step_secrets = [:]  // step → its whole `ENV=SECRET` list, where `options.<step>.secrets` overlays the profile's
params.secrets_project = ''
params.env = [:]
params.profile_json = ''

def q(v) { "'" + v.toString().replace("'", "'\\''") + "'" }

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
