from .heartbeat import start_liveness
from .job_identity import install as _job_identity

# A Batch task's liveness line (`heartbeat.start_liveness`): a no-op elsewhere.
start_liveness()
# A local job's identity (`job_identity`): its minted token for every Google client; a no-op elsewhere.
_job_identity()
