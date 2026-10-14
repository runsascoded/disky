import { useSearchParams } from 'react-router-dom'
import { ChildrenTable } from '../ChildrenTable'
import { StoreProvider } from '../store'
import { DEFAULT_STORE } from '../stores'
import type { TreeNode } from '../types'

const GiB = 1024 ** 3
const node: TreeNode = { n: 'demo', b: 600 * GiB, o: 6, k: 'dir', c: [
  { n: 'alpha', b: 300 * GiB, o: 3, k: 'dir' },
  { n: 'beta', b: 200 * GiB, o: 2, k: 'dir' },
  { n: 'gamma.bin', b: 100 * GiB, o: 1, k: 'file' },
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
