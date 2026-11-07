/** Controlled layout switch; use the same value for `<Treemap reclaimTitleSpace>`. */
export function TitleSpaceToggle({ checked, onChange }: { checked: boolean; onChange: (checked: boolean) => void }) {
  return <label className="dt-treemap-title-space-toggle"><input type="checkbox" checked={checked} onChange={e => onChange(e.target.checked)} /> reclaim title bar space</label>
}
