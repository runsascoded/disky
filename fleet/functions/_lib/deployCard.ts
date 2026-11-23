/** A deployment's status card (open question #87): `@disk-tree/pages-kit/og/deployCard`, which each deployment's own site
 *  draws too (on the head of its run thread in `#<deploy>-admin`); here, its input from the fleet document. */
import type { DeployCardInput } from '@disk-tree/pages-kit/og/deployCard'
import type { Theme } from './card.js'
import type { FleetDeployment } from './fleetTypes.js'

export * from '@disk-tree/pages-kit/og/deployCard'

/** A fleet deployment's card (`/og/fleet.png?view=deploy&d=…`): its runs (the newest `CARD_RUNS` carry their
 *  `familySeconds`) and its primary store's daily sizes, as the fleet document keeps them. */
export const deployCardInput = (d: FleetDeployment, now: number, theme: Theme): DeployCardInput => ({
  name: d.name,
  color: d.color,
  theme,
  now,
  runs: d.runs,
  points: (d.objects ?? []).map(p => ({ t: p.t, bytes: p.bytes, objects: p.objects })),
  ...(d.errors.runs ? { runsError: d.errors.runs } : {}),
})
