process list_source {
    tag "${src.uri}"
    label 'list'
    machineType { src.machines ? src.machines[task.submitAttempt - 1] : (src.machine ?: params.list_default.machine) }
    disk { diskOf(src.disk ?: params.list_default.disk) }
    maxSubmitAwait { src.machines && task.submitAttempt < src.machines.size() ? src.queue_timeout : null }
    maxRetries params.list_retries + 1
    errorStrategy {
        if( !(task.previousException?.cause instanceof nextflow.exception.ProcessSubmitTimeoutException) ) {
            if( task.attempt - task.submitAttempt >= 1 ) return 'finish'
            log.warn "scan head: ${task.process} (${src.uri}) failed (exit ${task.exitStatus}); rerunning it " +
                     "(${task.attempt - task.submitAttempt + 1}/1)"
            return 'retry'
        }
        log.warn "scan head: ${task.process} queued ${src.queue_timeout} on ${task.machineType}; retrying on " +
                 "${src.machines[task.submitAttempt]}${task.submitAttempt + 1 < src.machines.size() ? '' : ' (the last: no queue timeout)'}"
        return 'retry'
    }
    input: val src
    output: path 'done'
    script: stepScript(task, ['s3://bucket-big': ['bucket-check', ['list', 's3://bucket-big']], 's3://bucket-b': [['list', 's3://bucket-b']], 's3://bucket-c': [['list', 's3://bucket-c']]][src.uri])
}

process index {
    label 'big'
    input: val deps
    output: path 'done'
    script: stepScript(task, ['index'])
}

process index_rows {
    label 'small'
    input: val deps
    output: path 'done'
    script: stepScript(task, ['index-rows'])
}

process publish_r2 {
    label 'medium'
    input: val deps
    output: path 'done'
    script: stepScript(task, ['publish-r2'])
}

process static_names {
    label 'small'
    time '6h'
    input: val deps
    output: path 'done'
    script: stepScript(task, ['static-names'])
}

process interval_store {
    label 'big'
    input: val deps
    output: path 'done'
    script: stepScript(task, ['interval-store'])
}

process post {
    label 'post'
    input: val deps
    output: path 'done'
    script: stepScript(task, ['index-sync', 'index-gc', 'warm-cache', 'probe', 'digest'])
}

process over_time {
    label 'post'
    input: val deps
    output: path 'done'
    script: stepScript(task, ['over-time'])
}

workflow {
    if (!params.scan || !params.gen || !params.job_start) error "--scan, --gen and --job_start are required (`dt-cloud scan head` sets them)"
    def list_source_done = list_source(Channel.fromList(params.sources))
    def index_done = index(gathered(list_source_done, 3))
    def index_rows_done = index_rows(gathered(index_done, 1))
    def publish_r2_done = publish_r2(gathered(index_rows_done, 1))
    def static_names_done = static_names(gathered(index_done, 1))
    def interval_store_done = interval_store(gathered(publish_r2_done, 1))
    def post_done = post(gathered(index_rows_done.mix(static_names_done, interval_store_done), 3))
    def over_time_done = over_time(gathered(index_rows_done.mix(static_names_done, interval_store_done), 3))
}
