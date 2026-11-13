import { useSearchParams } from 'react-router-dom'
import { ChildrenTable } from '../ChildrenTable'
import { StoreProvider } from '../store'
import { DEFAULT_STORE } from '../stores'
import type { TreeNode } from '../types'

const GiB = 1024 ** 3
const node: TreeNode = { name: 'demo', bytes: 600 * GiB, objects: 6, kind: 'dir', children: [
  { name: 'alpha', bytes: 300 * GiB, objects: 3, kind: 'dir' },
  { name: 'beta', bytes: 200 * GiB, objects: 2, kind: 'dir' },
  { name: 'gamma.bin', bytes: 100 * GiB, objects: 1, kind: 'file' },
] }
const store = { ...DEFAULT_STORE, staging: true, owners: false }

export default function ChildrenTablePreview() {
  const [params, setParams] = useSearchParams()
  return <main className="federated-preview">
    <h1>Children table action fixture</h1>
    <p>Development-only, fictional data. Staging requests from this page are intercepted locally by Vite; no production objects or plans are changed.</p>
    <label>Simulated staging response <select value={params.get('outcome') ?? 'success'} onChange={e => setParams({ outcome: e.target.value })}>
      <option value="success">Success</option><option value="already">Already staged</option><option value="error">Error</option>
    </select></label>
    <StoreProvider store={store}><ChildrenTable key={params.get('outcome') ?? 'success'} node={node} segs={['demo']} scheme="gs://" onOpen={() => {}} onOpenObject={() => {}} /></StoreProvider>
  </main>
}
