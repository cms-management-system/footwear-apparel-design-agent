import type { CanvasLayout } from "./workspace-api";
export const clamp = (value: number, min: number, max: number) => Math.min(max, Math.max(min, value));
export function zoomCanvas(layout: CanvasLayout, zoom: number, anchor: { x: number; y: number }): CanvasLayout {
  const old = layout.viewport; const next = clamp(zoom, .1, 4);
  return { ...layout, viewport: { x: clamp(anchor.x - (anchor.x - old.x) * next / old.zoom, -100000, 100000), y: clamp(anchor.y - (anchor.y - old.y) * next / old.zoom, -100000, 100000), zoom: next } };
}
export function fitCanvas(layout: CanvasLayout, width: number, height: number): CanvasLayout {
  if (!layout.nodes.length) return { ...layout, viewport: { x: width / 2, y: height / 2, zoom: 1 } };
  const left = Math.min(...layout.nodes.map(n => n.x)); const top = Math.min(...layout.nodes.map(n => n.y));
  const right = Math.max(...layout.nodes.map(n => n.x + n.width)); const bottom = Math.max(...layout.nodes.map(n => n.y + n.height));
  const zoom = clamp(Math.min((width - 120) / (right - left), (height - 120) / (bottom - top)), .1, 2);
  return { ...layout, viewport: { zoom, x: (width - (right - left) * zoom) / 2 - left * zoom, y: (height - (bottom - top) * zoom) / 2 - top * zoom } };
}
