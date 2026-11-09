/** Optional V8 sampling profiles, collected separately from the unprofiled parity/latency passes. */
import { Session } from 'node:inspector'
import { readFile, writeFile } from 'node:fs/promises'

interface Frame { id: number; children?: number[]; callFrame: { functionName: string; url: string; lineNumber: number } }
interface Profile { nodes: Frame[]; samples: number[]; timeDeltas: number[]; startTime: number; endTime: number }
type Rpc = (method: string, params?: Record<string, unknown>) => Promise<Record<string, unknown>>

/** Categorize sampled stacks using esbuild's source-section comments. Reader includes planning and decode;
 * aggregation includes view/owner/filter work. These are sampled elapsed microseconds, not OS CPU counters. */
function summarize(profile: Profile, source: string) {
  const files: string[] = []
  let file = ''
  for (const line of source.split('\n')) {
    const marker = /^\/\/ (.+\.(?:ts|js))$/.exec(line)
    if (marker) file = marker[1]
    files.push(file)
  }
  const nodes = new Map(profile.nodes.map(node => [node.id, node]))
  const parents = new Map(profile.nodes.flatMap(node => (node.children ?? []).map(child => [child, node.id] as const)))
  const times = { readerAndDecodeUs: 0, aggregationUs: 0, otherUs: 0, idleUs: 0 }
  if (profile.samples.length !== profile.timeDeltas.length) throw new Error('CPU profile sample/delta mismatch')
  for (let i = 0; i < profile.samples.length; i++) {
    let id: number | undefined = profile.samples[i]
    let category: keyof typeof times = 'otherUs'
    while (id !== undefined) {
      const frame = nodes.get(id)?.callFrame
      if (!frame) break
      if (frame.functionName === '(idle)') { category = 'idleUs'; break }
      // Builtin/native frames have no source URL; classify by their nearest known caller instead.
      const path = frame.url ? files[frame.lineNumber] ?? '' : ''
      if (/hyparquet|fzstd|functions\/_lib\/index\.ts/.test(path)) { category = 'readerAndDecodeUs'; break }
      if (/functions\/_lib\/(view|owner\w*|filter|scope|staticBool)\.ts/.test(path)) { category = 'aggregationUs'; break }
      id = parents.get(id)
    }
    times[category] += profile.timeDeltas[i]
  }
  return { ...times, samples: profile.samples.length, intervalUs: 1000, durationUs: profile.endTime - profile.startTime }
}

async function capture(rpc: Rpc, task: () => Promise<void>) {
  await rpc('Profiler.enable')
  await rpc('Profiler.setSamplingInterval', { interval: 1000 })
  await rpc('Profiler.start')
  let result: Record<string, unknown>
  try { await task() } finally { result = await rpc('Profiler.stop') }
  return result.profile as Profile
}

async function workerInspector(url: URL) {
  const http = new URL('/json/list', url)
  http.protocol = 'http:'
  const targets = await (await fetch(http)).json() as { title: string; webSocketDebuggerUrl: string }[]
  const target = targets.find(target => target.title.includes('query-kernel'))
  if (!target) throw new Error(`query-kernel inspector target missing: ${targets.map(t => t.title).join(', ')}`)
  const socket = new WebSocket(target.webSocketDebuggerUrl)
  const pending = new Map<number, { resolve: (value: Record<string, unknown>) => void; reject: (error: Error) => void; timer: ReturnType<typeof setTimeout> }>()
  let next = 0
  socket.addEventListener('message', event => {
    const reply = JSON.parse(String(event.data)) as { id?: number; result?: Record<string, unknown>; error?: { message: string } }
    if (reply.id === undefined) return
    const call = pending.get(reply.id)
    if (!call) return
    pending.delete(reply.id); clearTimeout(call.timer)
    if (reply.error) call.reject(new Error(reply.error.message))
    else call.resolve(reply.result ?? {})
  })
  await new Promise<void>((resolve, reject) => {
    socket.addEventListener('open', () => resolve(), { once: true })
    socket.addEventListener('error', () => reject(new Error('worker inspector connection failed')), { once: true })
  })
  const rpc: Rpc = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++next
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`inspector timeout: ${method}`)) }, 10_000)
    pending.set(id, { resolve, reject, timer })
    socket.send(JSON.stringify({ id, method, params }))
  })
  return { rpc, close: () => socket.close() }
}

export async function profiles(options: { out: string; workerUrl: URL; workerSource: string; nodeTask: () => Promise<void>; workerTask: () => Promise<void> }) {
  const session = new Session()
  session.connect()
  const nodeRpc: Rpc = (method, params = {}) => new Promise((resolve, reject) => session.post(method, params, (error, result) => error ? reject(error) : resolve({ ...result })))
  let node: Profile
  const cpu = process.cpuUsage()
  try { node = await capture(nodeRpc, options.nodeTask) } finally { session.disconnect() }
  const nodeCpu = process.cpuUsage(cpu)
  const inspector = await workerInspector(options.workerUrl)
  let worker: Profile, heap: Record<string, unknown>
  try {
    worker = await capture(inspector.rpc, options.workerTask)
    heap = await inspector.rpc('Runtime.getHeapUsage')
  } finally { inspector.close() }
  await writeFile(`${options.out}/node.cpuprofile`, JSON.stringify(node))
  await writeFile(`${options.out}/worker.cpuprofile`, JSON.stringify(worker))
  const result = { node: summarize(node, await readFile(import.meta.filename, 'utf8')), worker: summarize(worker, options.workerSource), nodeCpu, nodeMemory: process.memoryUsage(), workerHeap: heap }
  await writeFile(`${options.out}/profiles.json`, JSON.stringify(result, null, 2) + '\n')
  return result
}
