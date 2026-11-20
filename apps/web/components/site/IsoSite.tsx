// The plan as a picture: the factory with what sits behind its meter (rooftop solar, a battery), the
// DISCOM's grid, and the open-access plant elsewhere in the state (solar, wind, a battery at the
// plant), drawn in isometric projection. It shows only what it is given: which components are in the
// plan, their targets and, once sized, the optimizer's sizes. Nothing here computes a number.

export type IsoPart = {
  on: boolean;          // in the plan
  target?: string;      // as typed, e.g. "5 MW"
  sized?: string;       // from the run, e.g. "4.32 MW"
  note?: string;        // a second line, e.g. "8.6 MWh"
};
export type IsoParts = {
  rooftop: IsoPart; oa_solar: IsoPart; oa_wind: IsoPart; btm_bess: IsoPart; oa_bess: IsoPart;
  factory?: string;     // the site's name
  discom?: string;      // the DISCOM's name
  state?: string;
};

const C30 = Math.cos(Math.PI / 6), S30 = 0.5, U = 24;
const OX = 600, OY = 300;
// isometric: x runs down-right, y down-left, z up
const P = (x: number, y: number, z = 0): [number, number] => [OX + (x - y) * C30 * U, OY + (x + y) * S30 * U - z * U];
const pts = (...p: [number, number][]) => p.map(([a, b]) => `${a.toFixed(1)},${b.toFixed(1)}`).join(" ");

const INK = {
  solar: "#f5b544", solarDark: "#1f3a5f", wind: "#4fb3d9", bess: "#7c6cf0", grid: "#8b95a3",
  wall: "var(--iso-wall, #d9dee5)", wallDark: "var(--iso-wall-dark, #b9c1cc)", roof: "var(--iso-roof, #eef1f5)",
  ground: "rgba(127, 201, 127, 0.12)", remote: "rgba(245, 181, 68, 0.12)", line: "var(--iso-line, #9aa5b1)",
};

function Box({ x, y, z = 0, w, d, h, top, left, right, opacity = 1 }: {
  x: number; y: number; z?: number; w: number; d: number; h: number; top: string; left: string; right: string; opacity?: number;
}) {
  return (
    <g opacity={opacity}>
      <polygon points={pts(P(x, y + d, z), P(x + w, y + d, z), P(x + w, y + d, z + h), P(x, y + d, z + h))} fill={left} />
      <polygon points={pts(P(x + w, y, z), P(x + w, y + d, z), P(x + w, y + d, z + h), P(x + w, y, z + h))} fill={right} />
      <polygon points={pts(P(x, y, z + h), P(x + w, y, z + h), P(x + w, y + d, z + h), P(x, y + d, z + h))} fill={top} />
    </g>
  );
}

function Tile({ x, y, w, d, fill, dashed }: { x: number; y: number; w: number; d: number; fill: string; dashed?: boolean }) {
  return <polygon points={pts(P(x, y), P(x + w, y), P(x + w, y + d), P(x, y + d))} fill={fill} stroke={INK.line} strokeWidth={1}
    strokeDasharray={dashed ? "5 4" : undefined} strokeOpacity={0.6} />;
}

// a tilted panel: a parallelogram raised on its back edge
function Panel({ x, y, w, d, z = 0, lift = 0.35 }: { x: number; y: number; w: number; d: number; z?: number; lift?: number }) {
  return <polygon points={pts(P(x, y, z + lift), P(x + w, y, z + lift), P(x + w, y + d, z), P(x, y + d, z))} fill={INK.solarDark} stroke={INK.solar} strokeWidth={0.8} />;
}

function Turbine({ x, y, h = 4.2, phase = 0 }: { x: number; y: number; h?: number; phase?: number }) {
  const [bx, by] = P(x, y), [tx, ty] = P(x, y, h);
  const blade = (deg: number) => {
    const a = ((deg + phase) * Math.PI) / 180, L = 1.55 * U;
    return `M${tx},${ty} L${(tx + Math.cos(a) * L).toFixed(1)},${(ty + Math.sin(a) * L).toFixed(1)}`;
  };
  return (
    <g>
      <line x1={bx} y1={by} x2={tx} y2={ty} stroke="#fff" strokeWidth={4} strokeLinecap="round" />
      <line x1={bx} y1={by} x2={tx} y2={ty} stroke={INK.wallDark} strokeWidth={2.4} strokeLinecap="round" />
      <g className="ep-iso__rotor" style={{ transformOrigin: `${tx}px ${ty}px` }}>
        {[-90, 30, 150].map((d) => <path key={d} d={blade(d)} stroke={INK.wind} strokeWidth={3} strokeLinecap="round" />)}
      </g>
      <circle cx={tx} cy={ty} r={3} fill={INK.wind} />
    </g>
  );
}

function Battery({ x, y, color, opacity = 1 }: { x: number; y: number; color: string; opacity?: number }) {
  const [cx, cy] = P(x + 1.3, y + 0.55, 0.55);
  return (
    <g opacity={opacity}>
      <Box x={x} y={y} w={1.9} d={1.1} h={1} top="#ecebfd" left={color} right="#6557d6" />
      <g transform={`translate(${cx - 9},${cy - 2})`} fill="none" stroke="#fff" strokeWidth={1.4}>
        <rect x={0} y={0} width={14} height={8} rx={1.5} /><rect x={14} y={2.5} width={2} height={3} fill="#fff" />
        <path d="M7.5 1.2 L5 4.3 H8 L6.5 6.8" />
      </g>
    </g>
  );
}

function Pylon({ x, y, h = 3.4 }: { x: number; y: number; h?: number }) {
  const [bx, by] = P(x, y), [tx, ty] = P(x, y, h);
  const arm = (z: number, half: number) => {
    const [ax, ay] = P(x, y - half, z), [cx, cy] = P(x, y + half, z);
    return <line x1={ax} y1={ay} x2={cx} y2={cy} stroke={INK.grid} strokeWidth={2} />;
  };
  return (
    <g>
      <path d={`M${bx - 7},${by} L${tx},${ty} L${bx + 7},${by} M${bx - 4},${by - (by - ty) * 0.45} L${bx + 4},${by - (by - ty) * 0.45}`} fill="none" stroke={INK.grid} strokeWidth={2} strokeLinejoin="round" />
      {arm(h - 0.3, 0.9)}{arm(h - 1.0, 0.7)}
    </g>
  );
}

function Label({ at, title, part, color, anchor = "start", to }: {
  at: [number, number]; title: string; part?: IsoPart; color: string; anchor?: "start" | "end"; to?: [number, number];
}) {
  const lines: string[] = [];
  if (part) {
    if (!part.on) lines.push("not in the plan");
    else if (part.sized) { lines.push(part.sized + (part.note ? ` · ${part.note}` : "")); if (part.target) lines.push(`target ${part.target}`); }
    else lines.push(part.target ? `target ${part.target}` : "optimizer decides");
  }
  const w = Math.max(title.length * 6.6, ...lines.map((l) => l.length * 6.1)) + 26;
  const x = anchor === "end" ? at[0] - w : at[0];
  const h = 22 + lines.length * 15;
  const from: [number, number] = to ? [to[0] < x ? x : to[0] > x + w ? x + w : Math.min(Math.max(to[0], x + 12), x + w - 12), to[1] < at[1] ? at[1] : to[1] > at[1] + h ? at[1] + h : at[1] + h / 2] : [0, 0];
  return (
    <g className="ep-iso__label" opacity={part && !part.on ? 0.55 : 1}>
      {to && <><line x1={from[0]} y1={from[1]} x2={to[0]} y2={to[1]} stroke="var(--ink-muted, #667085)" strokeWidth={1} strokeDasharray="2 3" /><circle cx={to[0]} cy={to[1]} r={2.5} fill={color} /></>}
      <rect x={x} y={at[1]} width={w} height={h} rx={8} fill="var(--surface, #fff)" stroke="var(--line, #e3e7ec)" />
      <circle cx={x + 11} cy={at[1] + 12} r={4} fill={color} />
      <text x={x + 20} y={at[1] + 16} fontSize={12} fontWeight={600} fill="var(--ink, #1c2430)">{title}</text>
      {lines.map((l, i) => (
        <text key={i} x={x + 20} y={at[1] + 31 + i * 15} fontSize={11.5} fill={i === 0 && part?.sized ? "var(--ink, #1c2430)" : "var(--ink-muted, #667085)"}
          fontWeight={i === 0 && part?.sized ? 600 : 400}>{l}</text>
      ))}
    </g>
  );
}

function Flow({ d, color, on = true, id }: { d: string; color: string; on?: boolean; id: string }) {
  return (
    <g opacity={on ? 1 : 0.25}>
      <path d={d} fill="none" stroke={INK.line} strokeWidth={2} />
      {on && <path id={id} d={d} fill="none" stroke={color} strokeWidth={2.6} strokeDasharray="2 10" strokeLinecap="round" className="ep-iso__flow" />}
    </g>
  );
}

export function IsoSite({ parts, caption }: { parts: IsoParts; caption?: string }) {
  const { rooftop, oa_solar, oa_wind, btm_bess, oa_bess } = parts;
  const dim = (p: IsoPart) => (p.on ? 1 : 0.22);
  const oaOn = oa_solar.on || oa_wind.on || oa_bess.on;
  // anchors
  const fac = { x: 3, y: 5, w: 7, d: 4.6, h: 2.3 };
  const pyl1 = P(-0.5, 6.5, 3.1), pyl2 = P(-3.5, 3.5, 3.1), meter = P(fac.x, fac.y + fac.d, 1.3);
  const plantOut = P(-5.7, 4.0, 0.6);
  const line = (a: [number, number], b: [number, number], sag = 10) => `M${a[0]},${a[1]} Q${(a[0] + b[0]) / 2},${(a[1] + b[1]) / 2 + sag} ${b[0]},${b[1]}`;
  return (
    <figure className="ep-iso">
      <svg viewBox="0 30 980 600" role="img" aria-label="The site, the DISCOM grid and the open-access plant, with the plan's components">
        {/* ground: the open-access plant (back left), the site (front right) */}
        <Tile x={-13} y={1} w={8} d={8} fill={INK.remote} dashed />
        <Tile x={1.2} y={3.6} w={12.4} d={8.2} fill={INK.ground} />

        {/* open-access solar: rows of tilted panels */}
        <g opacity={dim(oa_solar)}>
          {[0, 1, 2, 3].map((r) => [0, 1, 2, 3, 4].map((c) => <Panel key={`${r}-${c}`} x={-12.5 + c * 1.05} y={5.0 + r * 0.85} w={0.9} d={0.55} />))}
        </g>
        {/* open-access wind */}
        <g opacity={dim(oa_wind)}>
          <Turbine x={-12} y={1.9} phase={10} /><Turbine x={-9.5} y={1.6} h={4.6} phase={55} /><Turbine x={-7.1} y={2.3} h={4} phase={95} />
        </g>
        {/* battery at the plant, and the plant's substation */}
        <Battery x={-7.4} y={6.4} color={INK.bess} opacity={dim(oa_bess)} />
        <Box x={-6.4} y={3.6} w={1.1} d={0.9} h={0.8} top={INK.roof} left={INK.wall} right={INK.wallDark} opacity={oaOn ? 1 : 0.3} />

        {/* the grid: plant → pylons → the meter; the DISCOM's own supply shares the last span */}
        <Flow id="f-oa1" d={line(plantOut, pyl2, 14)} color={INK.solar} on={oaOn} />
        <Flow id="f-oa2" d={line(pyl2, pyl1, 18)} color={INK.solar} on={oaOn} />
        <Flow id="f-grid" d={line(pyl1, meter, 12)} color={INK.grid} />
        <Pylon x={-3.5} y={3.5} /><Pylon x={-0.5} y={6.5} />

        {/* the factory */}
        <Box x={fac.x} y={fac.y} w={fac.w} d={fac.d} h={fac.h} top={INK.roof} left={INK.wall} right={INK.wallDark} />
        <Box x={fac.x + 4.6} y={fac.y - 1.6} w={2.4} d={1.6} h={3.2} top={INK.roof} left={INK.wall} right={INK.wallDark} />
        <Box x={fac.x + 5.6} y={fac.y - 1.0} z={3.2} w={0.5} d={0.5} h={1.5} top="#c9ced6" left="#aab2bd" right="#9aa3af" />
        {/* doors and windows on the front wall */}
        {[0.6, 2.2, 3.8, 5.4].map((o) => (
          <polygon key={o} points={pts(P(fac.x + o, fac.y + fac.d, 1.0), P(fac.x + o + 1, fac.y + fac.d, 1.0), P(fac.x + o + 1, fac.y + fac.d, 1.7), P(fac.x + o, fac.y + fac.d, 1.7))} fill="#fff" opacity={0.7} />
        ))}
        {/* rooftop solar on the main roof */}
        <g opacity={dim(rooftop)}>
          {[0, 1, 2].map((r) => [0, 1, 2, 3, 4].map((c) => <Panel key={`r${r}-${c}`} x={fac.x + 0.35 + c * 1.28} y={fac.y + 0.4 + r * 1.35} w={1.1} d={0.95} z={fac.h} lift={0.22} />))}
        </g>
        {/* battery behind the meter, beside the factory */}
        <Battery x={fac.x + 7.6} y={fac.y + 2.6} color={INK.bess} opacity={dim(btm_bess)} />
        {/* the meter */}
        <circle cx={meter[0]} cy={meter[1]} r={6} fill="var(--surface, #fff)" stroke={INK.grid} strokeWidth={2} />
        <path d={`M${meter[0] - 2.5},${meter[1] + 1.5} L${meter[0]},${meter[1] - 2.5} L${meter[0] + 2.5},${meter[1] + 1.5}`} fill="none" stroke={INK.grid} strokeWidth={1.4} />

        {/* captions on the ground */}
        <text textAnchor="middle" x={P(-5, 9)[0] - 40} y={P(-5, 9)[1] + 30} fontSize={11} fill="var(--ink-muted, #667085)" letterSpacing={0.4}>OPEN-ACCESS PLANT · ELSEWHERE IN {(parts.state ?? "the state").toUpperCase()}</text>
        <text textAnchor="middle" x={P(13.6, 11.8)[0] - 120} y={P(13.6, 11.8)[1] + 8} fontSize={11} fill="var(--ink-muted, #667085)" letterSpacing={0.4}>{(parts.factory ?? "YOUR SITE").toUpperCase()} · BEHIND THE METER</text>

        {/* labels */}
        <Label at={[16, 60]} title="Open-access wind" part={oa_wind} color={INK.wind} to={P(-12, 1.9, 3.4)} />
        <Label at={[16, 236]} title="Open-access solar" part={oa_solar} color={INK.solar} to={P(-12.4, 7.4, 0.2)} />
        <Label at={[16, 400]} title="Battery at the plant" part={oa_bess} color={INK.bess} to={P(-7.4, 7.5, 0.5)} />
        <Label at={[520, 150]} title={parts.discom ? `${parts.discom} grid` : "DISCOM grid"} color={INK.grid} to={P(-3.5, 3.5, 3.3)} />
        <Label at={[964, 250]} title="Rooftop solar" part={rooftop} color={INK.solar} anchor="end" to={P(fac.x + 6.2, fac.y + 1.0, fac.h + 0.2)} />
        <Label at={[964, 440]} title="Battery behind the meter" part={btm_bess} color={INK.bess} anchor="end" to={P(fac.x + 9.4, fac.y + 3.1, 1.0)} />
      </svg>
      {caption && <figcaption className="eg-muted ep-small">{caption}</figcaption>}
    </figure>
  );
}
