import { createChart, LineSeries, type IChartApi, type LineData, type UTCTimestamp } from "lightweight-charts";
import { useEffect, useMemo, useRef, type ReactElement } from "react";

export interface PricePoint { time: string; value: number; }

export function toUtcLineData(points: PricePoint[]): LineData[] {
  const latestByTime = new Map<number, number>();
  for (const point of points) {
    const milliseconds = Date.parse(point.time);
    if (!Number.isFinite(milliseconds) || !Number.isFinite(point.value)) continue;
    latestByTime.set(Math.floor(milliseconds / 1_000), point.value);
  }
  return [...latestByTime.entries()]
    .sort(([left], [right]) => left - right)
    .map(([time, value]) => ({ time: time as UTCTimestamp, value }));
}

export function PriceChart({ points }: { points: PricePoint[] }): ReactElement {
  const container = useRef<HTMLDivElement>(null);
  const seriesData = useMemo(() => toUtcLineData(points), [points]);

  useEffect(() => {
    const host = container.current;
    if (!host || seriesData.length === 0) return undefined;
    const chart: IChartApi = createChart(host, {
      autoSize: true,
      height: 270,
      layout: { background: { color: "#0b1320" }, textColor: "#8fa1b8" },
      grid: { vertLines: { color: "#1a2738" }, horzLines: { color: "#1a2738" } },
      rightPriceScale: { borderColor: "#26364a" },
      timeScale: { borderColor: "#26364a" },
    });
    const series = chart.addSeries(LineSeries, { color: "#45c2a1", lineWidth: 2 });
    series.setData(seriesData);
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [seriesData]);

  if (seriesData.length === 0) return <div className="chart-empty">尚无有效的 API 价格/净值时间序列。</div>;
  return <div ref={container} aria-label="服务端返回的时间序列图表" className="price-chart" />;
}
