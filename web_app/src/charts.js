/* ============================================================================
 * charts.js · 纯 SVG 图表（不引第三方库）
 *   雷达图：知识点掌握度 S-08 / R-01
 *   柱状图：班级横向对比 R-01
 *   进度条：最薄弱知识点列表
 * 配色沿用设计系统：主蓝 #0071e3，弱项用暖色提示
 * ========================================================================== */
import { shortName, pct, levelOf } from "./core.js";

const clamp01 = (v) => Math.max(0, Math.min(1, Number(v) || 0));

/* ---------------------------------------------------------------- 雷达图 */
export function radar({ labels, values, size = 380 }) {
  const n = labels.length;
  if (n < 3) return "";
  const W = size + 162;
  const H = size + 16;
  const cx = W / 2;
  const cy = H / 2;
  const R = size * 0.335;

  const angle = (i) => -Math.PI / 2 + (i * 2 * Math.PI) / n;
  const at = (i, r) => [cx + Math.cos(angle(i)) * R * r, cy + Math.sin(angle(i)) * R * r];
  const fmt = ([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`;

  /* 网格环 */
  let grid = "";
  [0.25, 0.5, 0.75, 1].forEach((r) => {
    const pts = labels.map((_, i) => fmt(at(i, r))).join(" ");
    grid += `<polygon points="${pts}" fill="none" stroke="rgba(0,0,0,.07)" stroke-width="1"${r === 1 ? "" : ""}/>`;
  });
  /* 轴线 */
  labels.forEach((_, i) => {
    const [x, y] = at(i, 1);
    grid += `<line x1="${cx}" y1="${cy}" x2="${x}" y2="${y}" stroke="rgba(0,0,0,.055)" stroke-width="1"/>`;
  });

  /* 数据多边形 */
  const dataPts = values.map((v, i) => fmt(at(i, clamp01(v)))).join(" ");
  const dots = values.map((v, i) => {
    const [x, y] = at(i, clamp01(v));
    return `<circle cx="${x}" cy="${y}" r="3.2" fill="#0071e3" stroke="#fff" stroke-width="1.5">
      <title>${escapeXml(labels[i])}：${pct(v)}</title></circle>`;
  }).join("");

  /* 轴标签：放在 1.2 倍半径处，按象限改对齐方式，避免压住折线 */
  let text = "";
  labels.forEach((label, i) => {
    const a = angle(i);
    const [x, y] = at(i, 1.135);
    const cos = Math.cos(a);
    const anchor = cos > 0.3 ? "start" : cos < -0.3 ? "end" : "middle";
    const dy = Math.sin(a) > 0.6 ? 10 : Math.sin(a) < -0.6 ? -4 : 3.5;
    text += `<text x="${x.toFixed(1)}" y="${(y + dy).toFixed(1)}" text-anchor="${anchor}"
      font-size="10.5" fill="#6e6e73">${escapeXml(shortName(label, 7))}</text>`;
  });

  return `<svg viewBox="0 0 ${W} ${H}" width="100%" style="max-width:${W}px;display:block;margin:0 auto"
    role="img" aria-label="掌握度雷达图">
    ${grid}
    <polygon points="${dataPts}" fill="rgba(0,113,227,.12)" stroke="#0071e3" stroke-width="1.7"
      stroke-linejoin="round"/>
    ${dots}${text}
  </svg>`;
}

/* ---------------------------------------------------------------- 柱状图（班级对比）
 * 用 HTML 实现而不是 SVG：卡片宽度变化时不会把柱子和字一起放大，
 * 高度固定在 240px，读数保持 11.5px 不走形。 */
export function barChart({ items, labelKey = "class_name", valueKey = "avg_p_known" }) {
  if (!items.length) return "";
  return `<div class="vchart" role="img" aria-label="班级掌握度对比">
    <div class="vaxis" aria-hidden="true"><span>100%</span><span>0</span></div>
    ${items.map((it) => {
      const v = clamp01(it[valueKey]);
      const level = levelOf(v);
      const color = level.key === "weak" ? "#ff8a8a" : level.key === "mid" ? "#f5b45c" : "#3d8bfd";
      return `<div class="vcol" title="${escapeXml(it[labelKey])}：${pct(v)}">
        <div class="vval">${pct(v)}</div>
        <div class="vtrack"><i style="height:${Math.max(v * 100, 2).toFixed(1)}%;background:${color}"></i></div>
        <div class="vlab">${escapeXml(shortName(it[labelKey], 6))}</div>
      </div>`;
    }).join("")}
  </div>`;
}

/* ---------------------------------------------------------------- 横向进度条列表 */
export function barRows(items, { nameKey = "name", valueKey = "p_known", limit = 99 } = {}) {
  return items.slice(0, limit).map((it) => {
    const v = clamp01(it[valueKey]);
    const level = levelOf(v);
    return `<div class="bar-row">
      <div class="name" title="${escapeXml(it[nameKey] || it.kp_id)}">${escapeXml(shortName(it[nameKey] || it.kp_id, 10))}</div>
      <div class="bar ${level.cls}"><i style="width:${(v * 100).toFixed(1)}%"></i></div>
      <div class="val">${pct(v)}</div>
    </div>`;
  }).join("");
}

/* ---------------------------------------------------------------- 圆环（单个数值） */
export function ring(value, { size = 92, label = "" } = {}) {
  const v = clamp01(value);
  const r = size / 2 - 7;
  const c = 2 * Math.PI * r;
  const level = levelOf(v);
  const color = level.key === "weak" ? "#ff6b6b" : level.key === "mid" ? "#f0a13c" : "#0071e3";
  return `<svg viewBox="0 0 ${size} ${size}" width="${size}" height="${size}" role="img"
      aria-label="${escapeXml(label)} ${pct(v)}">
    <circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke="#f0f0f2" stroke-width="7"/>
    <circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke="${color}" stroke-width="7"
      stroke-linecap="round" stroke-dasharray="${(c * v).toFixed(1)} ${c.toFixed(1)}"
      transform="rotate(-90 ${size / 2} ${size / 2})"/>
    <text x="50%" y="${size / 2 + 5}" text-anchor="middle" font-size="17" font-weight="600"
      fill="#1d1d1f">${pct(v)}</text>
  </svg>`;
}

function escapeXml(str) {
  return String(str ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&apos;" }[c]
  ));
}
