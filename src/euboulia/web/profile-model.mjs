export const labels = {
  gpu_kernel: "GPU kernel",
  communication: "通信",
  cuda_api: "CUDA API",
  cpu_operator: "CPU 算子",
  annotation: "标注",
  memory_transfer: "内存传输",
  unknown: "未分类",
  other: "其他活动",
};
export function filterHotspots(
  rows,
  { rank = "", kind = "", search = "", sort = "total_ns" } = {},
) {
  return rows
    .filter(
      (r) =>
        (!rank || String(r.rank) === rank) &&
        (!kind || r.kind === kind) &&
        (!search || r.name.toLowerCase().includes(search.toLowerCase())),
    )
    .sort((a, b) => (b[sort] ?? -Infinity) - (a[sort] ?? -Infinity));
}
export function estimate(fraction, speedup) {
  if (
    !Number.isFinite(fraction) ||
    fraction < 0 ||
    fraction > 1 ||
    !Number.isFinite(speedup) ||
    speedup < 1
  )
    throw Error("Invalid assumptions");
  const ratio = 1 - fraction + fraction / speedup;
  return { latencyReduction: 1 - ratio, speedup: 1 / ratio };
}
export function hitTest(rects, x, y) {
  return (
    [...rects]
      .reverse()
      .find((r) => x >= r.x && x <= r.x + r.w && y >= r.y && y <= r.y + r.h)
      ?.event ?? null
  );
}
export function explain(row) {
  if (!row)
    return [
      "选择一个热点查看证据。",
      "点击时间线事件可查看记录中的调用栈和关联 ID。",
    ];
  if (row.kind === "communication")
    return [
      "通信 kernel 的执行区间值得检查。",
      "需要比较各 rank 的进入时间、前置计算与传输证据；仅凭耗时无法区分等待和搬运。",
    ];
  if (row.kind === "cuda_api")
    return [
      "CPU 发起 CUDA 工作的活动。",
      "跟随关联 ID 检查 GPU 启动时间、同步与 CPU 等待；调用耗时不等于 GPU 执行耗时。",
    ];
  if (row.kind === "gpu_kernel")
    return [
      `该 kernel 记录了 ${row.count} 次调用。`,
      "先定位阶段、shape 和相邻通信；是否带宽或计算受限，需要选定 kernel 的 NCU 指标验证。",
    ];
  return [
    "这是对应活动类别的累计耗时。",
    "查看嵌套范围和关联事件，避免把 CPU 父子范围相加作为端到端延迟。",
  ];
}

export function shortName(name) {
  return (
    String(name)
      .replace(/^void /, "")
      .replace(/\(anonymous namespace\)::/g, "")
      .split("<")[0]
      .split("(")[0] || String(name).slice(0, 100)
  );
}
