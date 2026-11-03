/** Controlled stroke switch; use the same value for `<Treemap borderStep>`. */
export function BorderStepToggle({ checked, onChange }: { checked: boolean; onChange: (checked: boolean) => void }) {
  return <label className="dt-treemap-border-step-toggle"><input type="checkbox" checked={checked} onChange={e => onChange(e.target.checked)} /> parent borders ≥1px wider</label>
}
