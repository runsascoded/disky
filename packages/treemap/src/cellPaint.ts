import type { CellStyle } from './cellTypes'

export function bandRects(bands: CellStyle['bands'], height: number) {
  const total = bands?.reduce((sum, b) => sum + Math.max(0, b.frac), 0) ?? 0
  let top = 0
  return (bands ?? []).map(b => {
    const h = total ? height * Math.max(0, b.frac) / total : 0
    const rect = { color: b.color, top, height: h }
    top += h
    return rect
  })
}

export function stripeBackground(stripes: CellStyle['stripes']): string | undefined {
  return stripes?.map(s => `repeating-linear-gradient(${s.angle}deg, ${s.color} 0 ${s.width}px, transparent ${s.width}px ${s.period}px)`).join(', ') || undefined
}

export function drawStripes(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  w: number,
  h: number,
  stripes: NonNullable<CellStyle['stripes']>,
  color: (c: string) => string,
): void {
  ctx.save()
  ctx.beginPath()
  ctx.rect(x, y, w, h)
  ctx.clip()
  for (const s of [...stripes].reverse()) {
    if (s.period <= 0 || s.width <= 0) continue
    const angle = s.angle * Math.PI / 180
    const nx = Math.sin(angle), ny = -Math.cos(angle)
    const reach = Math.hypot(w, h)
    ctx.strokeStyle = color(s.color)
    ctx.lineWidth = s.width
    ctx.beginPath()
    for (let at = -reach; at <= reach; at += s.period) {
      const ox = x + w / 2 + nx * at, oy = y + h / 2 + ny * at
      ctx.moveTo(ox - ny * reach, oy + nx * reach)
      ctx.lineTo(ox + ny * reach, oy - nx * reach)
    }
    ctx.stroke()
  }
  ctx.restore()
}
