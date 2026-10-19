import {
  Callout,
  Divider,
  H1,
  H2,
  Row,
  Stack,
  Stat,
  Table,
  Text,
  useHostTheme,
} from "cursor/canvas";

type Point = {
  x: number;
  y: number;
  label: string;
};

type Series = {
  name: string;
  color: string;
  tag: { x: number; y: number };
  points: Point[];
  highlight?: boolean;
};

const COLORS = {
  gb300: "#81a833",
  gb200: "#4a9232",
  b300: "#007c34",
  b200: "#006435",
  vllm: "#c2410c",
  vllmDisagg: "#9a3412",
  mi355x: "#c53732",
  atomesh: "#7c3aed",
};

// Pareto frontiers from plot_inferencex_kimi_k3_pareto.py.
// ATOMesh is the itl frontier; the E2E chart reuses those points with e2e as x.
const e2eSeries: Series[] = [
  {
    name: "GB300 NVL72 (Dynamo vLLM)",
    color: COLORS.gb300,
    tag: { x: 55, y: 7000 },
    points: [
      { x: 19.92, y: 19302.85, label: "TP8" },
      { x: 25.85, y: 15021.3, label: "1xTP8+1xTP8" },
      { x: 29.85, y: 13417.66, label: "1xTP8+1xTP8" },
      { x: 33.5, y: 10734.42, label: "1xTP8+2xTP8" },
      { x: 74.56, y: 3403.79, label: "TP8" },
      { x: 85.23, y: 2195.24, label: "TP8" },
    ],
  },
  {
    name: "GB200 NVL72 (Dynamo vLLM)",
    color: COLORS.gb200,
    tag: { x: 40, y: 2000 },
    points: [
      { x: 7.51, y: 9823.84, label: "TP8" },
      { x: 11.03, y: 9822.32, label: "TP8" },
      { x: 17.2, y: 8144.42, label: "TP8" },
      { x: 22.66, y: 7139.21, label: "TP8" },
      { x: 33.41, y: 4788.31, label: "TP8" },
      { x: 39.73, y: 915.73, label: "TP16" },
    ],
  },
  {
    name: "B300 (vLLM)",
    color: COLORS.b300,
    tag: { x: 42, y: 15000 },
    points: [
      { x: 7.0, y: 12566.49, label: "TP8" },
      { x: 9.95, y: 11932.23, label: "TP8" },
      { x: 11.04, y: 11428.0, label: "TP8" },
      { x: 13.74, y: 10455.94, label: "TP8" },
      { x: 15.11, y: 10042.86, label: "TP8" },
      { x: 18.85, y: 8922.61, label: "TP8" },
      { x: 22.54, y: 7650.7, label: "TP8" },
      { x: 41.8, y: 5180.44, label: "TP8" },
      { x: 53.93, y: 3119.89, label: "TP8" },
      { x: 59.13, y: 2025.33, label: "TP8" },
    ],
  },
  {
    name: "B200 (Dynamo vLLM)",
    color: COLORS.b200,
    tag: { x: 64, y: 6200 },
    points: [
      { x: 14.98, y: 10584.85, label: "TP8" },
      { x: 19.84, y: 9976.39, label: "TP8" },
      { x: 22.51, y: 9819.57, label: "TP8" },
      { x: 29.8, y: 8518.09, label: "TP8" },
      { x: 38.09, y: 7110.95, label: "TP8" },
      { x: 54.39, y: 4191.69, label: "TP8" },
      { x: 75.48, y: 2881.21, label: "TP8" },
      { x: 81.51, y: 1618.7, label: "TP8" },
    ],
  },
  {
    name: "MI355X (vLLM)",
    color: COLORS.vllm,
    tag: { x: 24, y: 10000 },
    points: [
      { x: 15.16, y: 14880.89, label: "TP8" },
      { x: 21.32, y: 13514.16, label: "TP8" },
      { x: 35.44, y: 8438.38, label: "TP8" },
      { x: 53.13, y: 6381.69, label: "TP8" },
      { x: 62.99, y: 2818.74, label: "TP8" },
      { x: 68.21, y: 1850.91, label: "TP8" },
    ],
  },
  {
    name: "MI355X (vLLM Disagg)",
    color: COLORS.vllmDisagg,
    tag: { x: 40, y: 6000 },
    points: [
      { x: 12.59, y: 10882.34, label: "1xTP8+1xTP8" },
      { x: 37.6, y: 5556.12, label: "1xTP8+2xTP8" },
      { x: 38.75, y: 4346.63, label: "1xTP8+3xTP8" },
      { x: 57.78, y: 1783.97, label: "1xTP8+3xTP8" },
    ],
  },
  {
    name: "MI355X (ATOM)",
    color: COLORS.mi355x,
    tag: { x: 50, y: 10500 },
    points: [
      { x: 10.41, y: 17482.09, label: "TP8" },
      { x: 13.06, y: 17097.27, label: "TP8" },
      { x: 30.6, y: 9013.39, label: "TP8" },
      { x: 39.94, y: 7822.22, label: "TP8" },
      { x: 65.42, y: 2877.97, label: "TP8" },
    ],
  },
  {
    name: "MI355X (ATOMesh)",
    color: COLORS.atomesh,
    highlight: true,
    tag: { x: 36, y: 16500 },
    points: [
      { x: 5.81, y: 14414.87, label: "1P1D-DS3 C88" },
      { x: 10.39, y: 14152.96, label: "1P1D-DS3 C72" },
      { x: 17.41, y: 12569.33, label: "1P1D-DS3 C56" },
      { x: 24.78, y: 12104.43, label: "1P1D-DS3 C48" },
      { x: 26.42, y: 9703.69, label: "1P2D-DS3 C48" },
      { x: 27.26, y: 694.19, label: "1P1D-DSPARK7 C1" },
      { x: 31.76, y: 1364.65, label: "1P1D-DSPARK7 C4" },
    ],
  },
];

const interactivitySeries: Series[] = [
  {
    name: "GB300 NVL72 (Dynamo vLLM)",
    color: COLORS.gb300,
    tag: { x: 155, y: 12000 },
    points: [
      { x: 25.88, y: 19302.85, label: "TP8" },
      { x: 55.59, y: 15021.3, label: "1xTP8+1xTP8" },
      { x: 60.75, y: 13417.66, label: "1xTP8+1xTP8" },
      { x: 69.49, y: 12882.16, label: "1xTP8+2xTP8" },
      { x: 85.47, y: 10734.42, label: "1xTP8+2xTP8" },
      { x: 113.51, y: 8831.86, label: "1xTP8+3xTP8" },
      { x: 202.02, y: 3403.79, label: "TP8" },
      { x: 223.71, y: 2195.24, label: "TP8" },
      { x: 251.26, y: 546.18, label: "1xTP8+3xTP8" },
    ],
  },
  {
    name: "GB200 NVL72 (Dynamo vLLM)",
    color: COLORS.gb200,
    tag: { x: 80, y: 3500 },
    points: [
      { x: 8.35, y: 9823.84, label: "TP8" },
      { x: 12.63, y: 9822.32, label: "TP8" },
      { x: 20.3, y: 8144.42, label: "TP8" },
      { x: 27.46, y: 7139.21, label: "TP8" },
      { x: 46.99, y: 4788.31, label: "TP8" },
      { x: 101.94, y: 2333.78, label: "TP16" },
      { x: 137.93, y: 1449.93, label: "TP16" },
      { x: 155.28, y: 961.67, label: "TP16" },
      { x: 198.41, y: 915.73, label: "TP16" },
    ],
  },
  {
    name: "B300 (vLLM)",
    color: COLORS.b300,
    tag: { x: 100, y: 15000 },
    points: [
      { x: 7.89, y: 12566.49, label: "TP8" },
      { x: 11.06, y: 11932.23, label: "TP8" },
      { x: 12.53, y: 11428.0, label: "TP8" },
      { x: 15.22, y: 10455.94, label: "TP8" },
      { x: 17.61, y: 10042.86, label: "TP8" },
      { x: 21.65, y: 8922.61, label: "TP8" },
      { x: 32.12, y: 7650.7, label: "TP8" },
      { x: 63.9, y: 5180.44, label: "TP8" },
      { x: 121.07, y: 3119.89, label: "TP8" },
      { x: 132.45, y: 2109.97, label: "TP8" },
      { x: 202.84, y: 2025.33, label: "TP8" },
    ],
  },
  {
    name: "B200 (Dynamo vLLM)",
    color: COLORS.b200,
    tag: { x: 125, y: 7000 },
    points: [
      { x: 16.59, y: 10584.85, label: "TP8" },
      { x: 22.29, y: 9976.39, label: "TP8" },
      { x: 25.77, y: 9819.57, label: "TP8" },
      { x: 35.21, y: 8518.09, label: "TP8" },
      { x: 45.48, y: 7110.95, label: "TP8" },
      { x: 69.78, y: 4191.69, label: "TP8" },
      { x: 102.35, y: 2881.21, label: "TP8" },
      { x: 152.21, y: 1618.7, label: "TP8" },
      { x: 192.68, y: 985.99, label: "TP8" },
    ],
  },
  {
    name: "MI355X (vLLM)",
    color: COLORS.vllm,
    tag: { x: 36, y: 9000 },
    points: [
      { x: 18.7, y: 14880.89, label: "TP8" },
      { x: 25.69, y: 13514.16, label: "TP8" },
      { x: 46.84, y: 8438.38, label: "TP8" },
      { x: 70.22, y: 6381.69, label: "TP8" },
      { x: 118.48, y: 2818.74, label: "TP8" },
      { x: 131.06, y: 1850.91, label: "TP8" },
      { x: 172.41, y: 1619.44, label: "TP8" },
    ],
  },
  {
    name: "MI355X (vLLM Disagg)",
    color: COLORS.vllmDisagg,
    tag: { x: 80, y: 8000 },
    points: [
      { x: 46.69, y: 10882.34, label: "1xTP8+1xTP8" },
      { x: 74.24, y: 7963.88, label: "1xTP8+2xTP8" },
      { x: 98.62, y: 5556.12, label: "1xTP8+2xTP8" },
      { x: 110.13, y: 4346.63, label: "1xTP8+3xTP8" },
      { x: 131.41, y: 1783.97, label: "1xTP8+3xTP8" },
    ],
  },
  {
    name: "MI355X (ATOM)",
    color: COLORS.mi355x,
    tag: { x: 34, y: 11500 },
    points: [
      { x: 20.68, y: 17482.09, label: "TP8" },
      { x: 23.55, y: 17097.27, label: "TP8" },
      { x: 57.01, y: 9013.39, label: "TP8" },
      { x: 120.34, y: 2877.97, label: "TP8" },
      { x: 151.52, y: 1621.02, label: "TP8" },
    ],
  },
  {
    name: "MI355X (ATOMesh)",
    color: COLORS.atomesh,
    highlight: true,
    tag: { x: 70, y: 15500 },
    points: [
      { x: 28.09, y: 14414.87, label: "1P1D-DS3 C88" },
      { x: 33.41, y: 14152.96, label: "1P1D-DS3 C72" },
      { x: 40.91, y: 12569.33, label: "1P1D-DS3 C56" },
      { x: 48.95, y: 12104.43, label: "1P1D-DS3 C48" },
      { x: 69.42, y: 9703.69, label: "1P2D-DS3 C48" },
      { x: 147.49, y: 1364.65, label: "1P1D-DSPARK7 C4" },
      { x: 156.25, y: 694.19, label: "1P1D-DSPARK7 C1" },
    ],
  },
];

function smoothPath(
  points: Point[],
  sx: (value: number) => number,
  sy: (value: number) => number,
) {
  if (points.length === 0) return "";
  const coordinates = points.map((point) => ({ x: sx(point.x), y: sy(point.y) }));
  if (coordinates.length === 1) {
    return `M ${coordinates[0].x} ${coordinates[0].y}`;
  }
  let path = `M ${coordinates[0].x} ${coordinates[0].y}`;
  for (let index = 0; index < coordinates.length - 1; index += 1) {
    const previous = coordinates[index - 1] ?? coordinates[index];
    const current = coordinates[index];
    const next = coordinates[index + 1];
    const afterNext = coordinates[index + 2] ?? next;
    path += ` C ${current.x + (next.x - previous.x) / 6} ${current.y + (next.y - previous.y) / 6}, ${next.x - (afterNext.x - current.x) / 6} ${next.y - (afterNext.y - current.y) / 6}, ${next.x} ${next.y}`;
  }
  return path;
}

function shouldLabel(points: Point[], index: number) {
  const label = points[index].label;
  if (index === 0 || index === points.length - 1) return true;
  if (points[index - 1].label === label && points[index + 1].label === label) return false;
  if (points[index - 1].label === label) return false;
  return true;
}

const STAR_COLOR = "#2563eb";

function starPolygon(cx: number, cy: number, radius: number) {
  const inner = radius * 0.4;
  const points: string[] = [];
  for (let index = 0; index < 10; index += 1) {
    const r = index % 2 === 0 ? radius : inner;
    const angle = -Math.PI / 2 + (index * Math.PI) / 5;
    points.push(`${cx + Math.cos(angle) * r},${cy + Math.sin(angle) * r}`);
  }
  return points.join(" ");
}

function labelShift(label: string) {
  const atomesh: Record<string, { dx: number; dy: number; anchor: "start" | "middle" | "end" }> = {
    "1P1D-DS3 C88": { dx: 0, dy: 16, anchor: "middle" },
    "1P1D-DS3 C72": { dx: -12, dy: 6, anchor: "end" },
    "1P1D-DS3 C56": { dx: 12, dy: 6, anchor: "start" },
    "1P1D-DS3 C48": { dx: 12, dy: -14, anchor: "start" },
    "1P2D-DS3 C48": { dx: -12, dy: -12, anchor: "end" },
    "1P1D-DSPARK7 C4": { dx: -8, dy: 14, anchor: "end" },
    "1P1D-DSPARK7 C1": { dx: 8, dy: -14, anchor: "start" },
  };
  return atomesh[label] ?? { dx: 0, dy: 12, anchor: "middle" as const };
}

function InferencexChart({
  title,
  xLabel,
  xMax,
  xStep,
  yMax,
  yStep,
  updatedDate,
  series,
  star,
}: {
  title: string;
  xLabel: string;
  xMax: number;
  xStep: number;
  yMax: number;
  yStep: number;
  updatedDate: string;
  series: Series[];
  star: Point;
}) {
  const width = 1180;
  const height = 680;
  const margin = { left: 78, right: 22, top: 58, bottom: 78 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const sx = (value: number) => margin.left + (value / xMax) * plotWidth;
  const sy = (value: number) => margin.top + plotHeight - (value / yMax) * plotHeight;
  const xTicks = Array.from({ length: Math.floor(xMax / xStep) + 1 }, (_, i) => i * xStep);
  const yTicks = Array.from({ length: Math.floor(yMax / yStep) + 1 }, (_, i) => i * yStep);
  const legendWidth = 292;
  const legendHeight = 18 + (series.length + 1) * 20;
  const legendX = width - legendWidth - 16;

  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={title}
      style={{ display: "block", width: "100%", background: "#eaebec" }}
    >
      <rect x={0} y={0} width={width} height={height} fill="#eaebec" />
      <text x={18} y={26} fill="#1b1c1d" fontSize={15} fontWeight={700}>
        {title}
      </text>
      <text x={18} y={44} fill="#5c5f63" fontSize={11}>
        Updated: {updatedDate}      Source: SemiAnalysis InferenceX™
      </text>
      <rect x={margin.left} y={margin.top} width={plotWidth} height={plotHeight} fill="#f4f5f6" />
      {xTicks.map((tick) => (
        <g key={`x-${tick}`}>
          <line
            x1={sx(tick)}
            y1={margin.top}
            x2={sx(tick)}
            y2={margin.top + plotHeight}
            stroke="#d5d6d8"
            strokeWidth={1}
          />
          <text x={sx(tick)} y={margin.top + plotHeight + 18} textAnchor="middle" fill="#5c5f63" fontSize={11}>
            {tick}
          </text>
        </g>
      ))}
      {yTicks.map((tick) => (
        <g key={`y-${tick}`}>
          <line
            x1={margin.left}
            y1={sy(tick)}
            x2={margin.left + plotWidth}
            y2={sy(tick)}
            stroke="#d5d6d8"
            strokeWidth={1}
          />
          <text x={margin.left - 10} y={sy(tick) + 4} textAnchor="end" fill="#5c5f63" fontSize={11}>
            {tick === 0 ? "0" : `${tick / 1000}k`}
          </text>
        </g>
      ))}
      <text
        x={margin.left + plotWidth / 2}
        y={height - 28}
        textAnchor="middle"
        fill="#1b1c1d"
        fontSize={13}
      >
        {xLabel}
      </text>
      <text
        x={18}
        y={margin.top + plotHeight / 2}
        textAnchor="middle"
        fill="#1b1c1d"
        fontSize={13}
        transform={`rotate(-90 18 ${margin.top + plotHeight / 2})`}
      >
        Token Throughput per Chip (tok/s/chip)
      </text>
      <text
        x={margin.left + plotWidth / 2}
        y={margin.top + plotHeight / 2}
        textAnchor="middle"
        fill="#9aa0a6"
        fontSize={42}
        fontStyle="italic"
        opacity={0.18}
      >
        semianalysis
      </text>
      {series.map((item) => (
        <g key={item.name}>
          <path
            d={smoothPath(item.points, sx, sy)}
            fill="none"
            stroke={item.color}
            strokeWidth={item.highlight ? 3.4 : 2.6}
            strokeLinecap="round"
            strokeLinejoin="round"
          />
          {item.points.map((point, index) => {
            const shift = labelShift(point.label);
            return (
              <g key={`${item.name}-${point.label}-${point.x}-${point.y}`}>
                <circle
                  cx={sx(point.x)}
                  cy={sy(point.y)}
                  r={item.highlight ? 6 : 5}
                  fill="#f7f8f8"
                  stroke={item.color}
                  strokeWidth={item.highlight ? 2.2 : 1.8}
                />
                {shouldLabel(item.points, index) ? (
                  <text
                    x={sx(point.x) + shift.dx}
                    y={sy(point.y) - shift.dy}
                    textAnchor={shift.anchor}
                    fill="#111111"
                    fontSize={10}
                    fontWeight={700}
                  >
                    {point.label}
                  </text>
                ) : null}
              </g>
            );
          })}
          <rect
            x={sx(item.tag.x)}
            y={sy(item.tag.y) - 10}
            width={Math.min(250, item.name.length * 6.6 + 16)}
            height={18}
            rx={4}
            fill={item.color}
          />
          <text x={sx(item.tag.x) + 8} y={sy(item.tag.y) + 3} fill="#ffffff" fontSize={10} fontWeight={700}>
            {item.name}
          </text>
        </g>
      ))}
      <rect
        x={legendX}
        y={margin.top + 10}
        width={legendWidth}
        height={legendHeight}
        rx={8}
        fill="#eceeef"
        stroke="#c5c7ca"
      />
      {series.map((item, index) => (
        <g key={`legend-${item.name}`}>
          <line
            x1={legendX + 16}
            y1={margin.top + 28 + index * 20}
            x2={legendX + 44}
            y2={margin.top + 28 + index * 20}
            stroke={item.color}
            strokeWidth={item.highlight ? 4 : 3}
          />
          <text x={legendX + 52} y={margin.top + 32 + index * 20} fill="#1b1c1d" fontSize={11}>
            {item.name}
          </text>
        </g>
      ))}
      <g>
        <polygon
          points={starPolygon(sx(star.x), sy(star.y), 9)}
          fill={STAR_COLOR}
          stroke="#1e3a8a"
          strokeWidth={0.6}
        />
        <text
          x={sx(star.x) + 14}
          y={sy(star.y) + (xMax > 100 ? -16 : 16)}
          textAnchor="start"
          fill="#111111"
          fontSize={10}
          fontWeight={700}
        >
          {star.label}
        </text>
        <polygon
          points={starPolygon(legendX + 30, margin.top + 28 + series.length * 20, 7)}
          fill={STAR_COLOR}
          stroke="#1e3a8a"
          strokeWidth={0.6}
        />
        <text
          x={legendX + 52}
          y={margin.top + 32 + series.length * 20}
          fill="#1b1c1d"
          fontSize={11}
        >
          MI355X (ATOMesh PP8)
        </text>
      </g>
      <text x={width / 2} y={height - 8} textAnchor="middle" fill="#5c5f63" fontSize={11}>
        inferencex.semianalysis.com/inference/kimi-k3
      </text>
    </svg>
  );
}

export default function KimiK3InferencexPareto() {
  const theme = useHostTheme();
  return (
    <Stack
      gap={18}
      style={{
        padding: 20,
        minHeight: "100%",
        background: theme.bg.chrome,
        color: theme.text.primary,
      }}
    >
      <Stack gap={6}>
        <H1>Kimi K3 InferenceX Pareto</H1>
        <Text tone="secondary">
          Agentic token throughput per chip for Kimi K3 2.8T. InferenceX curves are
          the upper-right frontier of each hardware key. The purple ATOMesh curve is
          the same seven local runs on both charts. The blue star is the PP8 C88
          run and is not on that curve.
        </Text>
        <Text size="small" tone="tertiary">
          Source: InferenceX_Kimi-K3_e2e (1).csv and InferenceX_Kimi-K3_interactivity (1).csv
          (2026-10-15) · ATOMesh points unchanged · Updated 2026-10-16
        </Text>
      </Stack>
      <Row gap={16}>
        <Stat value="14,414.87" label="ATOMesh peak tok/s/GPU (DS3 C88)" />
        <Stat value="7" label="Shared ATOMesh points on both charts" />
        <Stat value="19,302.85" label="GB300 peak tok/s/chip" />
      </Row>
      <Callout tone="info" title="How the purple curve is built">
        ATOMesh is Pareto-filtered once on P90 Interactivity. The E2E chart plots
        those same runs with P90 E2E Normalized on the x-axis. DSpark3 C48/C56/C72/C88
        replace the no-spec DCP8 runs. H200 stays in the CSV but is not drawn: its
        frontier is below 264 tok/s/chip.
      </Callout>
      <InferencexChart
        title="Kimi K3 2.8T Agentic Token Throughput per Chip vs. P90 E2E Normalized Interactivity"
        xLabel="P90 E2E Normalized Interactivity (tok/s/user)"
        xMax={100}
        xStep={10}
        yMax={25000}
        yStep={5000}
        updatedDate="2026-10-09"
        series={e2eSeries}
        star={{ x: 11.14, y: 15604.35, label: "PP8 C88" }}
      />
      <Divider />
      <InferencexChart
        title="Kimi K3 2.8T Agentic Token Throughput per Chip vs. P90 Interactivity"
        xLabel="P90 Interactivity (tok/s/user)"
        xMax={280}
        xStep={20}
        yMax={25000}
        yStep={5000}
        updatedDate="2026-10-09"
        series={interactivitySeries}
        star={{ x: 27.68, y: 15604.35, label: "PP8 C88" }}
      />
      <H2>ATOMesh points, both charts</H2>
      <Text size="small" tone="tertiary">
        y is Total tok/s/GPU. Interactivity x is the measured P90 Interactivity,
        which matches 1000 / TPOT p90. DS3 is 1P1D-TP8-DCP8-DSPARK3. The no-spec
        DCP8 C48/C56/C72 runs were worse on both axes and are not plotted.
      </Text>
      <Table
        headers={[
          "Topology",
          "Concurrency",
          "E2E x (tok/s/user)",
          "Interactivity x (tok/s/user)",
          "TPOT p90 (ms)",
          "tok/s/GPU",
        ]}
        columnAlign={["left", "right", "right", "right", "right", "right"]}
        striped
        rows={[
          ["1P1D-TP8-DSPARK7", "1", "27.26", "156.25", "6.40", "694.19"],
          ["1P1D-TP8-DSPARK7", "4", "31.76", "147.49", "6.78", "1,364.65"],
          ["1P1D-TP8-DCP8-DSPARK3", "48", "24.78", "48.95", "20.43", "12,104.43"],
          ["1P2D-TP8-DCP8-DSPARK3", "48", "26.42", "69.42", "14.41", "9,703.69"],
          ["1P1D-TP8-DCP8-DSPARK3", "56", "17.41", "40.91", "24.44", "12,569.33"],
          ["1P1D-TP8-DCP8-DSPARK3", "72", "10.39", "33.41", "29.93", "14,152.96"],
          ["1P1D-TP8-DCP8-DSPARK3", "88", "5.81", "28.09", "35.60", "14,414.87"],
        ]}
      />
    </Stack>
  );
}
