import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, useLocation } from 'react-router-dom'
import { stringParam, useUrlState } from 'use-prms'
import { UserChip } from '../../src/UserChip'
import { setRegistry } from '../../src/identities'
import { buildRegistry } from '../../src/identityRegistry'

setRegistry(buildRegistry([{ u: 'jane-doe', aliases: ['jane'], github: 'janedoe' }]))

function Fixture() {
  const location = useLocation()
  const [, setOwner] = useUrlState('o', stringParam())
  const [picks, setPicks] = useState(0)
  return <main>
    <h1>User card navigation</h1>
    <output data-testid="location">{location.pathname}{location.search}</output>
    <output data-testid="legend-picks">{picks}</output>
    <span role="button" aria-label="Filter to Jane" onClick={() => { setPicks(n => n + 1); setOwner('jane') }}>
      <UserChip who="jane-doe" />
    </span>
  </main>
}

createRoot(document.getElementById('root')!).render(<BrowserRouter><Fixture /></BrowserRouter>)
