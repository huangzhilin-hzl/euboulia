import {
  labels,
  groupHotspots,
  phaseLabel,
  estimate,
  hitTest,
  explain,
  shortName,
} from "./profile-model.mjs";
const $ = (s) => document.querySelector(s),
  esc = (v) =>
    String(v ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
const num = (v, d = 2) =>
  v == null || !Number.isFinite(Number(v))
    ? "—"
    : Number(v).toLocaleString("en-US", {
        maximumFractionDigits:
          Math.abs(Number(v)) > 0 && Math.abs(Number(v)) < 0.1
            ? Math.min(
                6,
                Math.max(d, 1 - Math.floor(Math.log10(Math.abs(Number(v))))),
              )
            : d,
      });
const colors = {
  gpu_kernel: "#60d8eb",
  communication: "#b49bfc",
  cuda_api: "#e7c96c",
  cpu_operator: "#a2d78b",
  annotation: "#e88aab",
  memory_transfer: "#f5a36d",
  other: "#697f98",
  unknown: "#697f98",
};
let run = "",
  capture = "",
  data = null,
  rows = [],
  groups = [],
  expanded = new Set(),
  selected = null,
  events = [],
  rects = [],
  start = 0,
  span = 1,
  total = 1,
  nameFilter = "",
  timelineNote = "",
  generation = 0,
  timelineGeneration = 0,
  eventGeneration = 0,
  poll = null;
async function api(path, options = {}) {
  const r = await fetch(path, { cache: "no-store", ...options });
  const b = await r.json();
  if (!r.ok) throw Error(b.error || r.statusText);
  return b;
}
const base = () => `/api/runs/${encodeURIComponent(run)}/profiles/${capture}`;
function status(text) {
  $("#message").textContent = text;
}
async function loadRuns() {
  const r = await api("/api/runs");
  $("#run").innerHTML =
    '<option value="">选择实验</option>' +
    r.runs
      .map(
        (x) =>
          `<option value="${esc(x.run_uid)}">${esc(x.name || x.run_uid)} · ${esc(x.status)} · ${esc(x.run_uid.slice(-6))}</option>`,
      )
      .join("");
  const query = new URLSearchParams(location.search);
  run = query.get("run") || r.runs[0]?.run_uid || "";
  if (!r.runs.some((x) => x.run_uid === run)) run = r.runs[0]?.run_uid || "";
  $("#run").value = run;
  await loadCaptures();
}
async function loadCaptures() {
  generation++;
  clearTimeout(poll);
  data = null;
  capture = "";
  $("#workspace").hidden = true;
  $("#empty").hidden = true;
  if (!run) {
    $("#empty").hidden = false;
    $("#empty").textContent = "还没有实验。返回实验列表创建 baseline。";
    return;
  }
  const current = run;
  status("读取采样清单…");
  try {
    const r = await api(`/api/runs/${encodeURIComponent(run)}/profiles`);
    if (current !== run) return;
    $("#captures").innerHTML = r.profiles
      .map(
        (p) =>
          `<button data-capture="${p.id}" class="capture"><span class="capture-dot ${p.raw_available ? "retained" : ""}"></span><strong>${esc(p.workload_point || p.profile_id || "Invalid capture")}</strong><small>${p.status === "invalid" ? esc(p.error) : `${p.raw_available}/${p.raw_expected} raw · ${esc(p.purpose)}`}</small><small title="${esc(p.location)}">${esc(p.location?.split("/").slice(-3, -1).join(" / "))}</small></button>`,
      )
      .join("");
    if (!r.profiles.length) {
      $("#empty").hidden = false;
      $("#empty").innerHTML =
        '<div class="eyebrow">NO PROFILE EVIDENCE</div><h2>这次实验还没有可读的 Profile。</h2><p>完成采集并同步产物后，这里会出现采样窗口。已有 baseline 可以按锁定场景重新采样。</p>';
      status("");
      return;
    }
    await selectCapture(r.profiles[0].id);
  } catch (e) {
    status(e.message);
  }
}
async function selectCapture(id) {
  capture = id;
  data = null;
  timelineGeneration++;
  $("#workspace").hidden = true;
  status("加载采样证据…");
  selected = null;
  nameFilter = "";
  $("#search").value = "";
  for (const id of ["phase", "module", "step"]) $("#" + id).value = "";
  expanded.clear();
  $("#rank").value = "";
  $("#kind").value = "";
  start = 0;
  span = 1;
  total = 1;
  const token = ++generation;
  clearTimeout(poll);
  await loadCapture(token, true);
}
async function loadCapture(token = ++generation, reset = false) {
  if (!capture) return;
  try {
    const r = await api(base() + "?" + semanticParams());
    if (token !== generation) return;
    const newlyIndexed = !data?.quality && !!r.quality;
    data = r;
    rows = r.hotspots || [];
    if (selected)
      selected =
        rows.find(
          (h) =>
            h.name === selected.name &&
            h.kind === selected.kind &&
            String(h.rank) === String(selected.rank) &&
            h.phase === selected.phase,
        ) || null;
    $("#workspace").hidden = false;
    $("#empty").hidden = true;
    document
      .querySelectorAll("[data-capture]")
      .forEach((b) =>
        b.classList.toggle("active", b.dataset.capture === capture),
      );
    const ranks = [
      ...new Set([
        ...r.files.map((f) => f.rank),
        ...rows.map((x) => String(x.rank)),
      ]),
    ].sort((a, b) => Number(a) - Number(b));
    const old = $("#rank").value;
    $("#rank").innerHTML =
      '<option value="">全部 rank</option>' +
      ranks.map((x) => `<option>${esc(x)}</option>`).join("");
    $("#rank").value = reset ? (ranks.includes("0") ? "0" : "") : old;
    total = r.quality?.duration_ns || 1;
    if (reset || newlyIndexed || span === 1) span = total / 8;
    if (reset && rows.some((h) => h.kind === "gpu_kernel"))
      $("#kind").value = "gpu_kernel";
    if (reset && !selected)
      selected =
        rows.find(
          (h) => h.kind === "gpu_kernel" && String(h.rank) === $("#rank").value,
        ) || null;
    renderOverview();
    renderStages();
    renderHotspots();
    renderInspector();
    renderQuality();
    renderHypotheses();
    renderEstimate();
    await loadTimeline();
    if (token !== generation) return;
    if (r.index.state === "indexing") {
      status(
        `正在建立事件索引 · ${num(r.index.events, 0)} 个事件，原始文件保持不变`,
      );
      poll = setTimeout(() => loadCapture(token), 1800);
    } else status(r.index.state === "error" ? r.index.error : "");
  } catch (e) {
    if (token === generation) status(e.message);
  }
}
function renderOverview() {
  const m = data.manifest,
    q = data.quality,
    available = data.files.filter((f) => f.available).length;
  $("#subtitle").textContent =
    `${m.candidate_id || "Unknown candidate"} · ${m.profile_id || capture}`;
  $("#metrics").innerHTML = [
    [
      "RAW EVIDENCE",
      `${available} / ${data.files.length}`,
      available ? "本地保留" : "原始事件不可用",
    ],
    [
      "OBSERVATIONS",
      num(q?.events ?? data.raw_observations, 0),
      q ? "已索引事件" : "摘要记录的事件数",
    ],
    [
      "CAPTURE SPAN",
      q ? num(q.duration_ns / 1e6) + " ms" : "—",
      q ? "采样时间范围" : "摘要无法还原时间",
    ],
    [
      "SOURCE REVISION",
      (m.source_revision || "Unknown").slice(0, 10),
      "固定版本证据",
    ],
  ]
    .map(
      ([label, value, note]) =>
        `<article class="metric"><span>${label}</span><strong>${esc(value)}</strong><small>${esc(note)}</small></article>`,
    )
    .join("");
  $("#scope").innerHTML =
    `<span class="tag">${q ? "TRACE AVAILABLE" : "SUMMARY EVIDENCE"}</span><span>${esc(m.workload_point || "工作点未记入旧清单")}</span><span>${esc((m.capture?.activities || []).join(" + "))}</span><span>${esc(m.capture?.num_steps ?? "?")} engine steps</span><span>${esc(m.purpose || "diagnostic")} · 非性能判定</span>`;
  $("#index-state").textContent = {
    ready: "LIVE EVIDENCE",
    summary_only: "SUMMARY ONLY",
    indexing: "INDEXING",
    error: "INDEX ERROR",
  }[data.index.state];
  $("#perfetto").disabled = !available;
}
function semanticParams() {
  return new URLSearchParams(
    Object.fromEntries(
      ["phase", "module", "step"].map((id) => [id, $("#" + id).value]),
    ),
  );
}
function renderStages() {
  const stage = data.stages,
    coverage = stage?.coverage;
  const ranks = stage?.ranks || [],
    phases = [...new Set(ranks.map((r) => r.phase || "__unknown__"))];
  $("#stage-state").textContent =
    coverage == null
      ? "证据不可用"
      : coverage === 0
        ? "阶段归属未知"
        : "观测事实 · 显式关联";
  $("#stage-coverage").innerHTML =
    `<div class="coverage-heading"><span>阶段归属覆盖率 <strong>${coverage == null ? "—" : num(coverage * 100, 1) + "%"}</strong></span><small>${num(stage?.assigned_gpu_events, 0)} / ${num(stage?.gpu_events, 0)} 个 GPU 活动事件 · 按事件数统计</small></div><div class="coverage-track"><i style="width:${(coverage || 0) * 100}%"></i></div>`;
  for (const [id, values, label] of [
    ["phase", phases, "全窗口"],
    ["module", stage?.modules || [], "全部模块"],
    ["step", stage?.steps || [], "全部 step"],
  ]) {
    const old = $("#" + id).value;
    $("#" + id).innerHTML =
      `<option value="">${label}</option>` +
      [...new Set([...values, ...(old ? [old] : []), "__unknown__"])]
        .map(
          (v) =>
            `<option value="${esc(v)}">${esc(id === "phase" ? phaseLabel(v) : v === "__unknown__" ? "未记录" : v)}</option>`,
        )
        .join("");
    $("#" + id).value = old;
    $("#" + id).disabled = !stage;
  }
  $("#stage-cards").innerHTML = [
    { phase: "", label: "全窗口", count: stage?.gpu_events },
    ...phases.map((phase) => ({
      phase,
      label: phaseLabel(phase),
      count: ranks
        .filter((r) => (r.phase || "__unknown__") === phase)
        .reduce((n, r) => n + r.count, 0),
    })),
  ]
    .map(
      (item) =>
        `<button data-phase="${esc(item.phase)}" class="stage-card ${$("#phase").value === item.phase ? "active" : ""}" ${!stage ? "disabled" : ""}><small>${item.phase === "__unknown__" ? "UNASSIGNED" : item.phase ? "RECORDED" : "CAPTURE"}</small><strong>${esc(item.label)}</strong><span>${num(item.count, 0)} 个 GPU 事件</span></button>`,
    )
    .join("");
  const modules = stage?.modules || [],
    tree = new Map();
  for (const path of modules) {
    const parts = path.split(".");
    for (let i = 1; i <= parts.length; i++)
      tree.set(parts.slice(0, i).join("."), i - 1);
  }
  $("#module-tree-shell").hidden = !tree.size;
  $("#module-tree").innerHTML = [...tree]
    .sort((a, b) => a[0].localeCompare(b[0], undefined, { numeric: true }))
    .slice(0, 1500)
    .map(
      ([path, depth]) =>
        `<button data-module="${esc(path)}" style="padding-left:${12 + Math.min(depth, 8) * 14}px" class="${$("#module").value === path ? "active" : ""}">${esc(path.split(".").at(-1))}<small>${esc(path)}</small></button>`,
    )
    .join("");
  const currentModule = $("#module").value;
  const moduleOptions = new Set([...$("#module").options].map((o) => o.value));
  for (const [path] of tree)
    if (!moduleOptions.has(path)) $("#module").add(new Option(path, path));
  $("#module").value = currentModule;
  const filtered = ranks.filter(
    (r) =>
      !$("#phase").value || (r.phase || "__unknown__") === $("#phase").value,
  );
  const busiest = [...filtered].sort(
    (a, b) => b.activity_ns - a.activity_ns,
  )[0];
  $("#stage-note").textContent =
    coverage === 0
      ? "这份 trace 没有可用于阶段归属的证据。当前展示全窗口 kernel 活动，无法证明 Prefill / Decode 的热点。"
      : "阶段由记录字段、CPU 包含范围及 CUDA launch 关联建立；不按 kernel 名推测。";
  if (busiest && $("#phase").value)
    $("#stage-note").textContent +=
      ` 阶段概览 · Rank ${busiest.rank}：累计活动 ${num(busiest.activity_ns / 1e6)} ms，去重活动 ${num(busiest.busy_ns / 1e6)} ms，首末 GPU 事件跨度 ${num(busiest.projected_span_ns / 1e6)} ms。概览包含该阶段所有模块 / step；这些数值不等于请求延迟。`;
  if (data.quality?.truncated || data.files.some((f) => !f.available))
    $("#stage-note").textContent += " 数据不完整，覆盖率仅针对已索引事件。";
}
async function changeScope() {
  selected = null;
  nameFilter = "";
  expanded.clear();
  eventGeneration++;
  start = 0;
  span = total;
  await loadCapture(++generation);
}
function renderHotspots() {
  groups = groupHotspots(rows, {
    kind: $("#kind").value,
    search: $("#search").value,
    sort: $("#sort").value,
  });
  $("#hotspot-title").textContent =
    ($("#phase").value ? phaseLabel($("#phase").value) : "全窗口") +
    " · 热点证据";
  $("#hotspot-count").textContent =
    `${groups.length} 个热点 · Rank 已合并${groups.length > 80 ? " · 显示前 80 项" : ""}${data.hotspots_limited ? " · 查询已达上限，请选择更小范围" : ""}`;
  const heatRanks = [
    ...new Set([
      ...data.files.map((f) => String(f.rank)),
      ...rows.map((r) => String(r.rank)),
    ]),
  ].sort((a, b) => Number(a) - Number(b));
  $("#hotspots").innerHTML =
    groups
      .slice(0, 80)
      .map((g, i) => {
        const key = JSON.stringify([g.name, g.kind]),
          open = expanded.has(key);
        const heat = heatRanks
          .map((rank) => {
            const r = g.rows.find((row) => String(row.rank) === rank);
            if (!r)
              return `<span class="heat-cell absent" title="Rank ${esc(rank)}：当前查询中未观察到；不计为零耗时"><small>R${esc(rank)}</small><strong>—</strong></span>`;
            return `<button class="heat-cell" data-hotspot="${rows.indexOf(r)}" style="--heat:${0.12 + (0.65 * r.total_ns) / Math.max(1, g.max_rank_ns)}" title="Rank ${esc(r.rank)} · ${num(r.total_ns / 1e6)} ms · ${num(r.count, 0)} 次 · ${num(r.share * 100, 1)}% 本 Rank 所选范围活动"><small>R${esc(r.rank)}</small><strong>${num(r.total_ns / 1e6)}</strong></button>`;
          })
          .join("");
        return `<tr class="${g.rows.includes(selected) ? "selected" : ""}"><td><button class="hotspot-name" data-group="${i}" title="${esc(g.name)}">${esc(shortName(g.name))}</button><small>${esc(labels[g.kind] || g.kind)} · <button class="expand-ranks" data-expand="${i}" aria-expanded="${open}">${open ? "收起" : "展开"} ${g.rows.length} 个 Rank</button></small></td><td>${num(g.count, 0)}</td><td>${num(g.max_rank_ns / 1e6)}</td><td>${num(g.mean_ns / 1e3)}</td><td><div class="heat-row">${heat}</div></td></tr>${open ? `<tr class="rank-detail"><td colspan="5"><div class="rank-detail-grid">${g.rows.map((r) => `<button data-hotspot="${rows.indexOf(r)}"><b>Rank ${esc(r.rank)}</b><span>${num(r.total_ns / 1e6)} ms · ${num(r.count, 0)} 次</span><small>${num(r.share * 100, 1)}% 本 Rank 活动 · ${num(r.mean_ns / 1e3)} µs / 次</small></button>`).join("")}</div><p class="note">占比的分母为同 Rank、同活动域、当前阶段 / 模块 / step 的累计活动；不代表关键路径或预期收益。</p></td></tr>` : ""}`;
      })
      .join("") ||
    '<tr><td colspan="5">这个范围没有匹配的热点。调整阶段、模块、step、活动或搜索条件。</td></tr>';
}
async function loadTimeline() {
  const token = ++timelineGeneration;
  if (!data || data.index.state !== "ready") {
    events = [];
    timelineNote = "";
    drawTimeline();
    return;
  }
  const params = new URLSearchParams({
    start: String(Math.max(0, Math.round(start))),
    end: String(Math.max(1, Math.round(start + span))),
    rank: $("#rank").value,
    kind: $("#kind").value,
    name: nameFilter,
    ...Object.fromEntries(semanticParams()),
  });
  try {
    const r = await api(base() + "/timeline?" + params);
    if (token !== timelineGeneration) return;
    events = r.events;
    timelineNote =
      (r.limited ? "事件超过当前显示上限，请缩小窗口。 " : "") +
      (nameFilter ? "仅显示选中的热点；清除筛选可恢复上下文。 " : "") +
      "时间坐标仅用于事件定位；跨 rank 时钟尚未验证。";
    drawTimeline();
  } catch (e) {
    if (token === timelineGeneration) status(e.message);
  }
}
function drawTimeline() {
  const canvas = $("#timeline"),
    container = $("#timeline-shell");
  const lanes = [
    ...new Set(
      events.map((e) => [e.rank, e.kind, e.stream ?? e.tid].join("|")),
    ),
  ];
  const displayed = lanes.slice(0, 36),
    width = Math.max(300, container.clientWidth),
    height = Math.max(210, displayed.length * 34 + 36),
    dpr = devicePixelRatio || 1;
  canvas.width = width * dpr;
  canvas.height = height * dpr;
  canvas.style.width = width + "px";
  canvas.style.height = height + "px";
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, width, height);
  const left = 118,
    right = width - 16,
    plot = right - left;
  ctx.font = "10px ui-monospace, monospace";
  for (let i = 0; i <= 5; i++) {
    const x = left + (plot * i) / 5;
    ctx.strokeStyle = "#273747";
    ctx.setLineDash([2, 5]);
    ctx.beginPath();
    ctx.moveTo(x, 25);
    ctx.lineTo(x, height);
    ctx.stroke();
    ctx.fillStyle = "#879bb0";
    ctx.fillText(
      num((start + (span * i) / 5) / 1e6, 2) + "ms",
      Math.min(x, right - 46),
      15,
    );
  }
  $("#timeline-note").textContent =
    timelineNote +
    (lanes.length > 36
      ? " 仅绘制前 36 条轨道，请按 rank 或活动类型缩小范围。"
      : "");
  ctx.setLineDash([]);
  displayed.forEach((lane, i) => {
    const [rank, kind, stream] = lane.split("|");
    ctx.fillStyle = "#95a7ba";
    ctx.fillText(
      `R${rank} · ${(labels[kind] || kind).slice(0, 8)}`,
      9,
      46 + i * 34,
    );
    ctx.fillStyle = "#536980";
    ctx.fillText(`stream / ${stream}`.slice(0, 21), 9, 58 + i * 34);
  });
  rects = [];
  for (const event of events) {
    const lane = [event.rank, event.kind, event.stream ?? event.tid].join("|"),
      index = displayed.indexOf(lane);
    if (index < 0) continue;
    const x = Math.max(left, left + ((event.ts - start) / span) * plot),
      end = Math.min(
        right,
        left + ((event.ts + event.dur - start) / span) * plot,
      );
    if (end < x) continue;
    const y = 32 + index * 34,
      w = Math.max(1, end - x),
      h = 22;
    ctx.fillStyle = colors[event.kind] || colors.other;
    ctx.globalAlpha = selected && event.name !== selected.name ? 0.4 : 0.85;
    ctx.fillRect(x, y, w, h);
    ctx.globalAlpha = 1;
    if (w > 60) {
      ctx.fillStyle = "#08131c";
      ctx.save();
      ctx.beginPath();
      ctx.rect(x, y, w, h);
      ctx.clip();
      ctx.fillText(shortName(event.name), x + 4, y + 14);
      ctx.restore();
    }
    rects.push({ x, y, w, h, event });
  }
  $("#timeline-empty").hidden = !!events.length;
  $("#timeline-empty").innerHTML =
    data?.index.state === "summary_only"
      ? "<b>原始 trace 未保留</b><p>当前只能查看真实摘要。时间线、调用栈与事件关联需要重新采样或同步保留的 raw 文件。</p>"
      : data?.index.state === "indexing"
        ? "<b>正在建立查询索引</b><p>完成后会自动加载时间线。</p>"
        : data?.index.state === "error"
          ? `<b>索引失败</b><p>${esc(data.index.error)}</p>`
          : "<b>当前窗口没有匹配事件</b><p>扩大窗口或清除筛选查看其他活动。</p>";
  $("#time-label").textContent =
    `${num(start / 1e6)}–${num((start + span) / 1e6)} ms`;
  $("#position").value =
    total > span ? Math.round((start / (total - span)) * 1000) : 0;
  $("#position").disabled = total <= span;
  $("#event-list").innerHTML =
    events
      .slice(0, 150)
      .map(
        (e) =>
          `<button data-event="${e.id}">R${esc(e.rank)} · ${num(e.ts / 1e6)} ms · ${esc(e.name)} · ${num(e.dur / 1e3)} µs</button>`,
      )
      .join("") +
    (events.length > 150
      ? '<p class="note">列出前 150 项；缩小窗口可定位其余事件。</p>'
      : "");
}
function renderInspector(eventDetail = null) {
  const h = selected,
    info = explain(h),
    event = eventDetail?.event,
    args = event?.args || {},
    corr = event?.corr || {};
  const stack = Object.entries(args).filter(([key]) => /stack/i.test(key)),
    shapes = Object.entries(args).filter(([key]) =>
      /shape|dim|type/i.test(key),
    );
  const distribution = h
    ? rows
        .filter(
          (r) => r.name === h.name && r.kind === h.kind && r.phase === h.phase,
        )
        .sort((a, b) => Number(a.rank) - Number(b.rank))
    : [];
  const maxDuration = Math.max(1, ...distribution.map((r) => r.total_ns));
  const evidence = event?.evidence || {};
  const moduleSources = (eventDetail?.attribution_sources || []).flatMap(
    (source) => {
      try {
        const marker = JSON.parse(
          source.name.startsWith("euboulia::") ? source.name.slice(10) : "{}",
        );
        return marker.source?.file ? [marker.source] : [];
      } catch {
        return [];
      }
    },
  );
  const recordedSource = moduleSources
    .map(
      (source) =>
        `<p class="note">模块定义：${esc(source.file)}:${esc(source.line)}<br />${esc(source.symbol)} · 采集时记录；不等同于 kernel 实现位置。</p>`,
    )
    .join("");
  const semanticChain = `<div class="evidence-levels"><span class="tag">观测事实</span><span>瓶颈假设 · 待验证</span><span>实验收益 · 未验证</span></div><h4>阶段归属依据</h4>${["phase", "module", "step"].map((field) => `<div class="attribution-field"><small>${esc({ phase: "阶段", module: "模块", step: "Step" }[field])}</small><strong>${esc(evidence[field]?.value || "未知")}</strong><span>${esc({ recorded: "事件显式记录", cpu_scope: "同线程 CPU 包含范围", cuda_launch: "唯一 CUDA launch → GPU 关联" }[evidence[field]?.method] || "缺少归属证据")}</span></div>`).join("")}${(eventDetail?.attribution_sources || []).map((source) => `<button class="related" data-event="${source.id}">#${source.id} · ${esc(shortName(source.name))}<small>${num(source.ts / 1e6, 4)} ms · ${num(source.dur / 1e3)} µs</small></button>`).join("")}${!event ? `<p class="note">点击热点或 Rank 色块，查看一个真实事件及其归属链。</p>` : `<p class="note">证据定位：文件 ${esc(event.file)} / Rank ${esc(event.rank)} / 事件 #${event.id}。记录所属关系不等于已证明关键路径。</p>`}`;
  const rankChart = distribution.length
    ? `<h4>同一热点 · 各 rank 累计活动</h4><div class="rank-bars">${distribution.map((r) => `<div title="Rank ${esc(r.rank)}: ${num(r.total_ns / 1e6)} ms; ${num(r.count, 0)} 次"><span>${num(r.total_ns / 1e6, 1)}</span><i style="height:${Math.max(2, (60 * r.total_ns) / maxDuration)}px"></i><small>R${esc(r.rank)}</small></div>`).join("")}</div><p class="note">单位 ms；分别累计，不代表跨 rank 的关键路径或等待时间。</p>`
    : "";
  const launchRelations =
    eventDetail?.correlated?.filter(
      (e) =>
        e.corr?.launch &&
        e.corr.launch === event.corr?.launch &&
        (event.kind === "gpu_kernel" || event.kind === "communication"
          ? e.kind === "cuda_api"
          : event.kind === "cuda_api" &&
            ["gpu_kernel", "communication"].includes(e.kind)),
    ) || [];
  const launchChain = launchRelations.length
    ? `<div class="launch-chain"><span>显式 LAUNCH ID ${esc(event.corr.launch)}</span>${launchRelations
        .slice(0, 5)
        .map((e) => {
          const cpu = event.kind === "cuda_api" ? event : e,
            gpu = event.kind === "cuda_api" ? e : event;
          return `<button data-event="${cpu.id}"><small>CPU · ${num(cpu.dur / 1e3)} µs</small>${esc(shortName(cpu.name))}</button><i>↓ CUDA launch correlation</i><button data-event="${gpu.id}"><small>GPU · ${num(gpu.dur / 1e3)} µs</small>${esc(shortName(gpu.name))}</button>`;
        })
        .join("")}</div>`
    : "";
  const flow = eventDetail?.flows?.length
    ? `<details><summary>记录的 flow 端点 (${eventDetail.flows.length})</summary><p class="note">同一文件、类别和 scope 的显式 flow；端点与此事件在同一线程时间范围内。</p><pre>${esc(JSON.stringify(eventDetail.flows, null, 2))}</pre></details>`
    : "";
  $("#inspector").innerHTML =
    `<span class="tag">${event ? "EVENT " + event.id : h ? "HOTSPOT" : "SELECT EVIDENCE"}</span><h3 class="kernel-name">${esc(shortName(event?.name || h?.name || "选择一个 kernel 或时间线事件"))}</h3>${h ? `<div class="mini-metrics"><div><small>累计活动耗时</small><strong>${num(h.total_ns / 1e6)} ms</strong></div><div><small>调用次数</small><strong>${num(h.count, 0)}</strong></div><div><small>最短 / 最长</small><strong>${num(h.min_ns == null ? null : h.min_ns / 1e3)} / ${num(h.max_ns == null ? null : h.max_ns / 1e3)} µs</strong></div></div>` : ""}${semanticChain}${rankChart}<div class="finding"><span>观察</span><p>${esc(info[0])}</p><span>下一步证据</span><p>${esc(info[1])}</p></div><h4>计算关联 <small>RECORDED RELATIONS</small></h4>${launchChain}${event ? `<div class="chain"><span>${esc(labels[event.kind] || event.kind)}</span> → <strong>${esc(event.name.slice(0, 60))}</strong></div>${eventDetail.correlated.length ? eventDetail.correlated.map((e) => `<button class="related" data-event="${e.id}">${esc(labels[e.kind] || e.kind)} → ${esc(e.name)}<small>相同 scope 中的关联 ID，需结合时间与语义判断</small></button>`).join("") : '<p class="note">未找到显式关联事件。不会凭时间相邻建立调用关系。</p>'}${eventDetail.enclosing.length ? '<p class="note">同一线程中的包含范围：</p>' + eventDetail.enclosing.map((e) => `<button class="related" data-event="${e.id}">${esc(e.name)}</button>`).join("") : ""}<pre>${esc(JSON.stringify(corr, null, 2))}</pre>` : '<p class="note">点击时间线中的具体事件，查看 CPU/GPU 关联和包含范围。</p>'}${flow}<h4>源码与 shape <small>CAPTURED CONTEXT</small></h4>${recordedSource}<p class="note">Revision ${esc(data?.manifest.source_revision || "未记录")}</p>${stack.length ? `<pre>${esc(stack.map(([k, v]) => k + "\n" + JSON.stringify(v, null, 2)).join("\n"))}</pre>` : '<p class="missing">未记录源码调用栈。使用 CPU + GPU、with_stack 的理解采样补充证据；不能从 kernel 名推断代码行。</p>'}${shapes.length ? `<pre>${esc(JSON.stringify(Object.fromEntries(shapes), null, 2))}</pre>` : '<p class="note">Shape 未记录；针对选定工作点补采 record_shapes。</p>'}${h ? `<details><summary>完整 kernel / 算子名称</summary><pre>${esc(h.name)}</pre></details>` : ""}${event ? `<details><summary>完整事件参数</summary><pre>${esc(JSON.stringify(args, null, 2))}</pre></details>` : ""}`;
}
async function selectEvent(id) {
  const token = generation,
    eventToken = ++eventGeneration;
  try {
    const r = await api(base() + "/events/" + id);
    if (token !== generation || eventToken !== eventGeneration) return;
    selected =
      rows.find(
        (h) => h.name === r.event?.name && String(h.rank) === r.event?.rank,
      ) || null;
    renderHotspots();
    renderInspector(r);
    drawTimeline();
  } catch (e) {
    status(e.message);
  }
}
function renderQuality() {
  const q = data.quality,
    m = data.manifest,
    quality = [
      ["Capture purpose", m.purpose || "diagnostic"],
      [
        "CPU events",
        q
          ? num((q.kinds.cpu_operator || 0) + (q.kinds.cuda_api || 0), 0)
          : "未保留事件",
      ],
      ["With source stacks", q ? num(q.stack_events, 0) : "未知"],
      ["With shapes", q ? num(q.shape_events, 0) : "未知"],
      [
        "Correlation / flow",
        q
          ? `${num(q.correlated_events, 0)} / ${num(q.flow_events, 0)}`
          : "未知",
      ],
      [
        "Attributed phases",
        q ? Object.keys(q.phases).join(", ") || "未标记" : "未知",
      ],
      [
        "Checksum",
        q
          ? q.checksum_verified
            ? "已核对保留文件"
            : "部分文件缺少校验和"
          : "尚未验证",
      ],
      [
        "Coverage",
        q
          ? q.truncated
            ? "索引达到上限，数据不完整"
            : `跳过 ${q.skipped_events} 个无效/未闭合事件`
          : "仅摘要；不能证明阶段覆盖",
      ],
    ];
  $("#quality").innerHTML =
    '<div class="quality-grid">' +
    quality
      .map(
        ([k, v]) => `<div><small>${k}</small><strong>${esc(v)}</strong></div>`,
      )
      .join("") +
    '</div><div class="warnings">' +
    data.warnings.map((w) => `<p>${esc(w)}</p>`).join("") +
    "</div>";
  $("#files").innerHTML =
    '<div class="table-scroll"><table><thead><tr><th>原始产物</th><th>Rank</th><th>大小</th><th>保留状态</th><th>操作</th></tr></thead><tbody>' +
    data.files
      .map(
        (f) =>
          `<tr><td class="file-name" title="${esc(f.name)}">${esc(f.name)}</td><td>${esc(f.rank)}</td><td>${num(f.size_bytes == null ? null : f.size_bytes / 1024 / 1024)} MiB</td><td>${esc({ local: "本地保留", evicted: "采集后已删除", missing_local: "本地缺失" }[f.state])}</td><td>${f.available ? `<a href="${base()}/raw/${f.id}" download>下载 ↗</a> <button data-perfetto="${f.id}">Perfetto</button>` : "—"}</td></tr>`,
      )
      .join("") +
    "</tbody></table></div>";
}
function renderHypotheses() {
  $("#hypotheses").innerHTML = data.hypotheses.length
    ? data.hypotheses
        .map(
          (h) =>
            `<article class="draft"><span class="tag">DRAFT · 未执行</span><h4>${esc(h.title)}</h4><p>${esc(h.evidence)}</p><details><summary>验证与否定条件</summary><p>${esc(h.validation)}</p><p>${esc(h.rejection)}</p></details></article>`,
        )
        .join("")
    : '<p class="note">还没有本地假设。选择热点后记录观察、验证办法和否定条件。</p>';
}
function renderEstimate() {
  const f = Number($("#fraction").value) / 100,
    s = Number($("#speedup").value),
    r = estimate(f, s);
  $("#fraction-value").textContent = num(f * 100, 0) + "%";
  $("#speedup-value").textContent = s + "×";
  $("#estimate").innerHTML =
    `<strong>${num(r.latencyReduction * 100, 1)}%</strong><span>理想模型下的延迟下降<br><small>其他项和重叠关系不变；吞吐收益需单独实测。</small></span>`;
}
async function openPerfetto(id) {
  const raw = data.files.find((f) => f.id === id && f.available);
  if (!raw) return;
  if (raw.size_bytes > 256 * 1024 * 1024) {
    status("此文件超过 256 MiB，请下载后使用本地 Perfetto 打开。");
    return;
  }
  const target = window.open("https://ui.perfetto.dev", "_blank");
  if (!target) {
    status("浏览器阻止了新窗口，请允许后重试。");
    return;
  }
  try {
    status("读取原始 trace，准备在 Perfetto 中打开…");
    const r = await fetch(base() + "/raw/" + id);
    if (!r.ok) throw Error("无法读取原始 trace");
    const buffer = await r.arrayBuffer();
    let timer, timeout;
    const listen = (e) => {
      if (
        e.source !== target ||
        e.origin !== "https://ui.perfetto.dev" ||
        e.data !== "PONG"
      )
        return;
      clearInterval(timer);
      clearTimeout(timeout);
      window.removeEventListener("message", listen);
      target.postMessage(
        { perfetto: { buffer, title: raw.name, fileName: raw.name } },
        "https://ui.perfetto.dev",
      );
      status("已将本地 trace 交给 Perfetto 查看器。");
    };
    window.addEventListener("message", listen);
    timer = setInterval(
      () => target.postMessage("PING", "https://ui.perfetto.dev"),
      200,
    );
    timeout = setTimeout(() => {
      clearInterval(timer);
      window.removeEventListener("message", listen);
      status("Perfetto 未就绪；可下载原始文件后打开。");
    }, 30000);
  } catch (e) {
    status(e.message);
  }
}
$("#run").onchange = () => {
  run = $("#run").value;
  history.replaceState(null, "", "/profiles?run=" + encodeURIComponent(run));
  loadCaptures();
};
$("#captures").onclick = (e) => {
  const b = e.target.closest("[data-capture]");
  if (b) selectCapture(b.dataset.capture);
};
$("#refresh").onclick = () => loadCapture(++generation);
$("#rank").onchange = $("#kind").onchange = () => {
  nameFilter = "";
  selected = null;
  renderInspector();
  renderHotspots();
  loadTimeline();
};
$("#search").oninput = renderHotspots;
$("#sort").onchange = renderHotspots;
$("#clear-filter").onclick = () => {
  nameFilter = "";
  selected = null;
  $("#search").value = "";
  $("#kind").value = "gpu_kernel";
  renderHotspots();
  renderInspector();
  loadTimeline();
};
$("#hotspots").onclick = async (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.dataset.expand != null) {
    const g = groups[Number(b.dataset.expand)],
      key = JSON.stringify([g.name, g.kind]);
    expanded.has(key) ? expanded.delete(key) : expanded.add(key);
    renderHotspots();
    return;
  }
  if (b.dataset.group != null)
    selected = [...groups[Number(b.dataset.group)].rows].sort(
      (a, b) => b.total_ns - a.total_ns,
    )[0];
  else if (b.dataset.hotspot != null)
    selected = rows[Number(b.dataset.hotspot)];
  else return;
  nameFilter = selected.name;
  $("#rank").value = String(selected.rank ?? "");
  renderHotspots();
  renderInspector();
  start = 0;
  span = total;
  loadTimeline();
  if (selected?.event_id) await selectEvent(selected.event_id);
};
for (const id of ["phase", "module", "step"])
  $("#" + id).onchange = changeScope;
$("#stage-cards").onclick = (e) => {
  const b = e.target.closest("[data-phase]");
  if (b) {
    $("#phase").value = b.dataset.phase;
    changeScope();
  }
};
$("#module-tree").onclick = (e) => {
  const b = e.target.closest("[data-module]");
  if (b) {
    $("#module").value = b.dataset.module;
    changeScope();
  }
};
$("#reset-stage").onclick = () => {
  for (const id of ["phase", "module", "step"]) $("#" + id).value = "";
  changeScope();
};
$("#zoom-in").onclick = () => {
  span = Math.min(total, Math.max(1, span / 2));
  loadTimeline();
};
$("#zoom-out").onclick = () => {
  span = Math.min(total, span * 2);
  start = Math.min(start, total - span);
  loadTimeline();
};
$("#reset-time").onclick = () => {
  start = 0;
  span = total;
  loadTimeline();
};
$("#position").oninput = () => {
  start = (Number($("#position").value) / 1000) * Math.max(0, total - span);
  loadTimeline();
};
$("#timeline").onpointermove = (e) => {
  const box = e.target.getBoundingClientRect(),
    x = e.clientX - box.left,
    y = e.clientY - box.top,
    event = hitTest(rects, x, y),
    tip = $("#tooltip");
  tip.hidden = !event;
  if (event) {
    tip.innerHTML = `<strong>${esc(shortName(event.name))}</strong><span>Rank ${esc(event.rank)} · ${esc(labels[event.kind] || event.kind)}</span><span>${num(event.ts / 1e6, 4)} ms · ${num(event.dur / 1e3)} µs</span>`;
    tip.style.left =
      Math.min(Math.max(0, x + 10), Math.max(0, box.width - 300)) + "px";
    tip.style.top = Math.max(0, y - 65) + "px";
  }
};
$("#timeline").onpointerleave = () => {
  $("#tooltip").hidden = true;
};
$("#timeline").onclick = (e) => {
  const r = e.target.getBoundingClientRect(),
    event = hitTest(rects, e.clientX - r.left, e.clientY - r.top);
  if (event) selectEvent(event.id);
};
$("#event-list-toggle").onclick = () => {
  const hidden = !$("#event-list").hidden;
  $("#event-list").hidden = hidden;
  $("#event-list-toggle").setAttribute("aria-expanded", String(!hidden));
};
document.addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (b?.dataset.event) selectEvent(b.dataset.event);
  if (b?.dataset.perfetto) openPerfetto(b.dataset.perfetto);
});
$("#perfetto").onclick = () => {
  const f =
    data.files.find(
      (f) => f.available && (!$("#rank").value || f.rank === $("#rank").value),
    ) || data.files.find((f) => f.available);
  if (f) openPerfetto(f.id);
};
$("#fraction").oninput = $("#speedup").oninput = renderEstimate;
$("#new-hypothesis").onclick = () => {
  $("#draft-title").value = selected
    ? "验证 " + selected.name.slice(0, 160) + " 的优化方向"
    : "";
  $("#draft-evidence").value =
    `Profile ${data.manifest.profile_id} · ${data.manifest.workload_point || "legacy point"}\n${selected ? `Rank ${selected.rank}: ${selected.name}; 累计活动耗时 ${num(selected.total_ns / 1e6)} ms，${selected.count} 次。此占比不是关键路径占比。` : ""}`;
  $("#draft-validation").value =
    "在同一锁定场景执行无 Profile 的 A/B，检查正确性、目标指标和回退；必要时补采机制证据。";
  $("#draft-rejection").value = "";
  $("#draft-error").textContent = "";
  $("#draft-dialog").showModal();
};
$("#close-draft").onclick = () => $("#draft-dialog").close();
$("#draft-form").onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api(base() + "/hypotheses", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Euboulia-Control": "1",
      },
      body: JSON.stringify({
        title: $("#draft-title").value,
        evidence: $("#draft-evidence").value,
        validation: $("#draft-validation").value,
        rejection: $("#draft-rejection").value,
      }),
    });
    $("#draft-dialog").close();
    await loadCapture(++generation);
    status("假设已保存在本实验的本地分析记录中，未执行实验。");
  } catch (error) {
    $("#draft-error").textContent = error.message;
  }
};
new ResizeObserver(() => {
  if (data) drawTimeline();
}).observe($("#timeline-shell"));
loadRuns().catch((e) => status(e.message));
