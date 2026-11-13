import { readFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { createInterface } from 'node:readline'
import { nodeExecutor, type CapsuleFile } from './node.js'
import { captureFixture } from './fixtures.js'

const [command, file, root = '.'] = process.argv.slice(2)
if (command === 'capture' && file) {
  const document = await captureFixture(resolve(root), file)
  console.log(JSON.stringify({ capsule: document.capsule.id, objects: document.objects.length }))
} else if (command === 'run' && file) {
  const document = JSON.parse(await readFile(file, 'utf8')) as CapsuleFile
  const execute = await nodeExecutor(document, resolve(root))
  for await (const line of createInterface({ input: process.stdin, crlfDelay: Infinity })) {
    if (!line.trim()) continue
    const cpu = process.cpuUsage(), memory = process.memoryUsage()
    const result = await execute(JSON.parse(line))
    console.log(JSON.stringify({ result, process: { cpu: process.cpuUsage(cpu), rss: process.memoryUsage().rss, rssBefore: memory.rss } }))
  }
} else {
  console.error('usage: executor capture|run CAPSULE.json [SITE_ROOT]; run reads one request per stdin line')
  process.exitCode = 2
}
