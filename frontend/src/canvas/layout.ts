// 新节点落点：视口中心减去节点半宽(≈65)/半高(≈20)，再按已有节点数错位（每个 +24，6 个一循环）防完全重叠。
export function nodeDropPosition(center: { x: number; y: number }, count: number): { x: number; y: number } {
  const k = count % 6
  return { x: center.x - 65 + k * 24, y: center.y - 20 + k * 24 }
}
