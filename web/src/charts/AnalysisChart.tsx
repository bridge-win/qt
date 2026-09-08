import { BarChart } from "echarts/charts";
import { GridComponent, TooltipComponent } from "echarts/components";
import { init, use } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { useEffect, useRef, type ReactElement } from "react";

export interface AnalysisPoint { label: string; value: number; }

use([BarChart, GridComponent, TooltipComponent, CanvasRenderer]);

export default function AnalysisChart({ title, points }: { title: string; points: AnalysisPoint[] }): ReactElement {
  const host = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!host.current || points.length === 0) return undefined;
    const chart = init(host.current, undefined, { renderer: "canvas" });
    chart.setOption({
      animation: false,
      backgroundColor: "transparent",
      tooltip: { trigger: "axis" },
      grid: { left: 42, right: 16, top: 36, bottom: 36 },
      xAxis: { type: "category", data: points.map((point) => point.label), axisLabel: { color: "#8fa1b8" } },
      yAxis: { type: "value", axisLabel: { color: "#8fa1b8" }, splitLine: { lineStyle: { color: "#1a2738" } } },
      series: [{ type: "bar", name: title, data: points.map((point) => point.value), itemStyle: { color: "#5985ff" } }],
    });
    const resize = () => chart.resize();
    window.addEventListener("resize", resize);
    return () => { window.removeEventListener("resize", resize); chart.dispose(); };
  }, [points, title]);
  if (points.length === 0) return <div className="chart-empty">服务未返回可绘制的分析序列。</div>;
  return <div ref={host} className="analysis-chart" aria-label={title} />;
}
