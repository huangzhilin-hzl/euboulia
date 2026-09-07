const escapeHTML = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
export { escapeHTML };
export const agentStates = {
  unpaired: "等待配对",
  available: "可接收工作",
  busy: "正在工作",
  offline: "连接中断",
  revoked: "已撤销连接",
};
export const jobStates = {
  queued: "等待接收",
  running: "正在执行",
  cancelling: "等待停止确认",
  completed: "已返回结果",
  failed: "执行失败",
  cancelled: "已取消",
  interrupted: "执行中断 · 需核实",
};
export const inLabScope = (job, context) =>
  job.context.room_id === context.room_id &&
  job.context.goal_id === context.goal_id;
const e = escapeHTML;
const symbols = {
  plus: '<path d="M12 5v14M5 12h14"/>',
  arrow: '<path d="M5 12h14m-5-5 5 5-5 5"/>',
  laptop:
    '<rect x="4" y="4" width="16" height="12" rx="2"/><path d="M2 20h20M9 16v4m6-4v4"/>',
  server:
    '<rect x="4" y="3" width="16" height="7" rx="2"/><rect x="4" y="14" width="16" height="7" rx="2"/><path d="M8 6.5h.01M8 17.5h.01M12 6.5h4m-4 11h4"/>',
  book: '<path d="M12 5v15M3 4c4-1 6 0 9 2 3-2 5-3 9-2v15c-4-1-6 0-9 2-3-2-5-3-9-2Z"/>',
  shield:
    '<path d="M12 3 4 6v6c0 5 8 9 8 9s8-4 8-9V6Z"/><path d="m8 12 3 3 5-6"/>',
  copy: '<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3"/>',
};
const glyph = (name) =>
  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${symbols[name] || symbols.arrow}</svg>`;
const statusBadge = (status, label) =>
  `<span class="lab-status" data-status="${e(status)}"><i aria-hidden="true"></i>${e(label)}</span>`;
const connectionArt = () =>
  `<div class="lab-connection-art" aria-hidden="true"><div class="lab-art-orbit"></div><div class="lab-art-node lab-art-local">${glyph("laptop")}<span>本地环境<small>代码 · 工具</small></span></div><div class="lab-art-link lab-art-link-local"></div><div class="lab-art-hub"><span class="lab-art-mark">e<span>·</span></span><strong>Julian’s lab</strong><small>共享目标与上下文</small></div><div class="lab-art-link lab-art-link-remote"></div><div class="lab-art-node lab-art-remote">${glyph("server")}<span>远程环境<small>Agent · 算力</small></span></div><span class="lab-art-caption">不同的机器，同一个研究室</span></div>`;

export function createLabBridge({ getContext, onChange, notify }) {
  const modal = document.createElement("dialog");
  modal.className = "lab-dialog";
  modal.setAttribute("aria-labelledby", "lab-dialog-title");
  document.body.append(modal);
  let mode = "checking",
    snapshot = { agents: [], jobs: [] },
    detailId = null,
    signature = "",
    busy = false;
  let connected = true;

  async function api(path, body) {
    const response = await fetch(`/api/lab/${path}`, {
      method: body ? "POST" : "GET",
      credentials: "same-origin",
      headers: body
        ? { "Content-Type": "application/json", "X-Lab-Control": "1" }
        : {},
      body: body ? JSON.stringify(body) : undefined,
      signal: AbortSignal.timeout(8000),
    });
    const data = await response.json();
    if (!response.ok) {
      if (response.status === 403 && path !== "login") mode = "locked";
      throw new Error(data.error || "Lab 请求失败");
    }
    return data;
  }

  function show(title, body) {
    detailId = null;
    modal.innerHTML = `<div class="lab-dialog-heading"><div><span class="eyebrow">JULIAN’S LAB</span><h2 id="lab-dialog-title">${e(title)}</h2></div><button class="icon-button" data-lab="close" aria-label="关闭 Agent 面板">×</button></div><div class="lab-dialog-body">${body}<p class="lab-error" role="alert"></p></div>`;
    if (!modal.open) modal.showModal();
    const initialFocus =
      modal.querySelector("input:not([readonly]),textarea,select") ||
      modal.querySelector('[data-lab="close"]');
    initialFocus?.focus({ preventScroll: true });
    modal.scrollTop = 0;
  }
  function error(err) {
    const target = modal.querySelector(".lab-error");
    if (target)
      target.textContent =
        err.message || "无法连接 Lab，请检查服务和网络后重试。";
  }
  function pairing(data) {
    show(
      "让 Agent 加入 Lab",
      `<div class="lab-wizard-steps"><span class="done">1 · 创建身份</span><span class="current">2 · 连接环境</span><span>3 · 等待上线</span></div>
      <p class="lab-dialog-intro">身份已创建。在运行 Agent 的机器上完成连接，就可以加入研究。</p>
      <div class="lab-pairing-code"><label>一次性连接码 <small>10 分钟内有效 · 仅可使用一次</small><input readonly value="${e(data.pairing_code)}" aria-label="一次性连接码"></label><button class="button" data-lab="copy-code">${glyph("copy")}复制连接码</button></div>
      <details class="lab-remote-guide"><summary>Agent 在远程机器上？先建立 SSH 隧道</summary><p>在 Lab 所在机器执行，替换远程主机地址：</p><pre>ssh -N -o ExitOnForwardFailure=yes \\\n  -R ${location.port || "8773"}:127.0.0.1:${location.port || "8773"} YOUR_REMOTE_HOST</pre><p>接着在远程终端执行下方命令。连接期间保留 SSH 隧道。</p></details>
      <div class="lab-command-heading"><strong>01 <span>连接这位 Agent</span></strong><button class="text-button" data-lab="copy-command" data-target="lab-connect-command">${glyph("copy")}复制</button></div>
      <p class="lab-command-note">在安装了 Euboulia 的环境中运行，按提示粘贴连接码。默认使用这台机器上已登录的 Codex。</p>
      <pre id="lab-connect-command">uv run euboulia lab connect \\\n  --url ${e(location.origin)} \\\n  --config ~/.config/euboulia/agents/${e(data.agent_id)}/agent.json</pre>
      <p class="lab-inline-note">${glyph("laptop")}<span>连接程序会自动准备独立工作空间。要查看哪些源码或资料，可以在具体任务中提供多个路径或链接。</span></p>
      <div class="lab-command-heading"><strong>02 <span>启动连接程序</span></strong><button class="text-button" data-lab="copy-command" data-target="lab-run-command">${glyph("copy")}复制</button></div>
      <pre id="lab-run-command">uv run euboulia lab agent run \\\n  --config ~/.config/euboulia/agents/${e(data.agent_id)}/agent.json</pre>
      <details class="lab-advanced-settings"><summary>高级设置：默认目录与其他运行方式</summary><h3>指定默认工作目录（可选）</h3><p>通常无需设置。要沿用已有目录，可在上方连接命令末尾追加下面的参数，并替换为这台机器上的实际路径：</p><pre>--workspace /absolute/path/to/working-directory</pre><p>这只设置启动目录。一次任务可以参考多个目录，现有连接也会保留原来的路径。</p><h3>使用其他 Agent runtime</h3><p>先准备本地 command adapter，再在连接命令末尾追加参数。适配器协议见仓库 docs/lab-agents.md。</p><pre>--runtime command --command-file /absolute/path/to/adapter-argv.json</pre></details>
      <p class="lab-command-note">默认 Codex 以只读权限运行；自动准备工作空间不会改变运行权限。</p>
      <div class="lab-dialog-footer"><span>上线后会自动更新状态</span><button class="button primary" data-lab="close">查看 Agent ${glyph("arrow")}</button></div>`,
    );
  }
  function card(agent) {
    return `<button class="lab-agent-card" data-lab="agent" data-id="${e(agent.id)}"><span class="lab-agent-top"><span class="lab-agent-avatar">${e(agent.name.slice(0, 1))}</span>${statusBadge(agent.status, agentStates[agent.status])}</span><strong class="lab-agent-name">${e(agent.name)}</strong><span class="lab-agent-role">${e(agent.role)}</span>${agent.capabilities ? `<span class="lab-agent-skills"><span>擅长</span><span class="lab-agent-skill-text">${e(agent.capabilities)}</span></span>` : ""}<span class="lab-agent-bottom"><span>${glyph("laptop")}<span>${e(agent.host || "等待连接工作环境")}<small>${e(agent.runtime || "运行方式待配置")}</small></span></span>${glyph("arrow")}</span></button>`;
  }
  function jobsMarkup(jobs) {
    return jobs
      .map(
        (job) =>
          `<button class="lab-job" data-lab="job" data-id="${e(job.id)}"><span class="lab-job-meta"><span>${e(snapshot.agents.find((a) => a.id === job.agent_id)?.name || "Agent")} <span class="lab-job-kind">${e(job.kind === "task" ? "任务" : "消息")}</span></span>${statusBadge(job.state, jobStates[job.state])}</span><strong>${e(job.prompt)}</strong>${job.result ? `<span class="lab-result-preview">${e(job.result)}</span>` : ""}<small>${e(job.context.goal_title || job.context.room_name || "直接联系")}<time datetime="${e(new Date(job.created * 1000).toISOString())}">${e(new Date(job.created * 1000).toLocaleString())}</time></small></button>`,
      )
      .join("");
  }
  function page() {
    if (mode === "checking") return "";
    if (mode === "demo")
      return `<div class="memory-card"><span class="eyebrow">真实 Agent 接入</span><h3>把你自己的 Agent 带进研究室</h3><p>启动 Lab 服务后，可以创建身份、连接机器、发送消息和任务。</p><pre>uv run euboulia lab serve</pre><a class="button" href="http://127.0.0.1:8773/prototypes/inference-world/">打开本地 Lab ↗</a></div>`;
    if (mode === "locked")
      return `<div class="page-body lab-page"><section class="lab-onboarding"><div class="lab-onboarding-main"><div class="lab-onboarding-copy"><span class="lab-overline">你的 Agent 团队，从这里开始</span><h2>把研究队友<br>带进同一个 Lab。</h2><p>连接本地或远程的 Agent，共享研究目标，<br class="lab-desktop-break">把工作交给有对应能力的队友。</p><button class="button primary" data-lab="login">登录 Lab ${glyph("arrow")}</button><span class="lab-cta-note">使用本机 Lab 管理密钥</span></div>${connectionArt()}</div><div class="lab-login-hint">${glyph("shield")}<span>在 Lab 服务所在目录获取密钥</span><code>uv run euboulia lab token</code></div></section>${guides()}</div>`;
    const online = snapshot.agents.filter((a) =>
      ["available", "busy"].includes(a.status),
    ).length;
    const working = snapshot.agents.filter((a) => a.status === "busy").length;
    return `<div class="page-body lab-page"><div class="lab-page-toolbar"><div><span class="lab-overline">团队连接</span><p>每位 Agent 保留自己的职责、工具与工作环境。</p></div><button class="button" data-lab="guide">${glyph("book")}接入指南</button></div>
      ${!connected ? '<p class="lab-error" role="status">Lab 服务连接中断。以下为上次收到的状态。</p>' : ""}
      ${snapshot.agents.length ? `<section aria-label="已添加的 Agent"><div class="lab-roster-heading"><div><h2>研究队友 <span>${snapshot.agents.length}</span></h2><p>${online} 位在线 <span>·</span> ${working} 位正在工作</p></div><button class="button primary" data-lab="create">${glyph("plus")}连接 Agent</button></div><div class="lab-agent-grid">${snapshot.agents.map(card).join("")}</div></section>` : `<section class="lab-onboarding"><div class="lab-onboarding-main"><div class="lab-onboarding-copy">${statusBadge("unpaired", "尚未连接 Agent")}<h2>让第一位 Agent<br>加入你的研究。</h2><p>用一句话介绍它，再连接它所在的机器。<br class="lab-desktop-break">无论在本机还是远程，都能在这里一起工作。</p><button class="button primary" data-lab="create">${glyph("plus")}连接第一位 Agent ${glyph("arrow")}</button><span class="lab-cta-note">支持本地 Codex 与远程 Agent</span></div>${connectionArt()}</div><ol class="lab-setup-steps"><li><span>01</span><div><strong>定义队友</strong><p>名字与一句话职责</p></div></li><li><span>02</span><div><strong>连接环境</strong><p>运行命令，自动准备工作空间</p></div></li><li><span>03</span><div><strong>开始协作</strong><p>选择队友，发送消息或任务</p></div></li></ol></section>${guides()}`}
      ${snapshot.jobs.length ? `<div class="section-title lab-history-heading"><h2>最近的通信与工作</h2><span>最近 ${snapshot.jobs.length} 条</span></div><div class="lab-job-list">${jobsMarkup(snapshot.jobs)}</div>` : ""}
      <footer class="lab-page-footer"><span class="lab-service-state"><i class="${connected ? "online" : ""}"></i>${connected ? "Lab 服务已连接" : "Lab 服务连接中断"}</span><div><button class="text-button" data-lab="data-info">${glyph("shield")}数据与权限</button><span aria-hidden="true">·</span><button class="text-button" data-lab="logout">退出管理</button></div></footer></div>`;
  }
  function guides() {
    return `<div class="lab-guides"><button class="lab-guide-card" data-lab="guide-local"><span class="lab-guide-icon">${glyph("laptop")}</span><span><strong>连接本地 Agent</strong><small>在当前电脑上，让 Codex 加入研究</small></span>${glyph("arrow")}</button><button class="lab-guide-card" data-lab="guide-remote"><span class="lab-guide-icon">${glyph("server")}</span><span><strong>连接远程 Agent</strong><small>通过 SSH 隧道，连接另一台机器</small></span>${glyph("arrow")}</button></div>`;
  }
  function feed() {
    if (mode !== "live") return "";
    const jobs = snapshot.jobs.filter((job) => inLabScope(job, getContext()));
    return `<section class="lab-scope-feed"><div class="section-title"><h2>与真实 Agent 协作 <span>${jobs.length}</span></h2><button class="button" data-lab="send">联系 Agent ↗</button></div>${jobs.length ? jobsMarkup(jobs) : '<p class="muted">发送消息或任务时，将附上当前研究室和目标的上下文。</p>'}<p class="muted">目标状态管理原型记录；真实工作请在对应工作记录中单独取消。</p></section>`;
  }
  function send(agentId) {
    const agents = snapshot.agents.filter(
      (a) => !["revoked", "unpaired"].includes(a.status),
    );
    if (!agents.length) {
      notify("先连接一位 Agent，再发送工作。");
      return;
    }
    const context = structuredClone(getContext());
    const requestId = crypto.randomUUID();
    show(
      "联系 Agent",
      `<form id="lab-send"><p class="lab-context-caption">${e(context.room_name || "直接联系")} ${context.goal_title ? " / " + e(context.goal_title) : ""}</p><label>接收人<select name="agent_id">${agents.map((a) => `<option value="${e(a.id)}" ${a.id === agentId ? "selected" : ""}>${e(a.name)} · ${e(agentStates[a.status])}</option>`).join("")}</select></label><label>发送方式<select name="kind"><option value="message">消息 · 交流、提问和补充信息</option><option value="task" ${context.goal_status && context.goal_status !== "active" ? "disabled" : ""}>任务 · 请 Agent 执行并返回结果</option></select></label><label>内容<textarea name="prompt" rows="6" maxlength="16000" required placeholder="告诉队友你需要什么，以及希望得到怎样的结果…"></textarea></label><details class="lab-resource-settings"><summary>添加参考资料 <span class="lab-field-hint">可选 · 多个目录或链接</span></summary><label>参考资料<textarea name="resources" rows="3" maxlength="8000" aria-describedby="lab-resources-hint" placeholder="/repos/sglang&#10;/repos/vllm&#10;https://example.com/research-notes"></textarea></label><p id="lab-resources-hint" class="lab-command-note">每行一项，使用 Agent 所在机器上的路径或可访问的文档链接。这里只传递位置，文件需已在目标机器上准备好。</p></details><p class="lab-inline-note">${glyph("shield")}<span>发送会调用目标机器上的真实 Agent。连接中断时排队等待；正在执行的工作不会被新消息打断。</span></p><div class="lab-dialog-footer"><button class="button" type="button" data-lab="close">取消</button><button class="button primary" type="submit">发送给 Agent ${glyph("arrow")}</button></div></form>`,
    );
    modal.querySelector("form").addEventListener("submit", (event) => {
      event.preventDefault();
      submit(async () => {
        const { resources, ...values } = Object.fromEntries(
          new FormData(event.target),
        );
        const job = await api("jobs", {
          ...values,
          context: { ...context, resources },
          request_id: requestId,
        });
        await refresh();
        await showJob(job.id);
      });
    });
  }
  async function showJob(id) {
    const data = await api("jobs/" + encodeURIComponent(id));
    show("Agent 工作记录", '<div id="lab-job-detail"></div>');
    detailId = id;
    updateDetail(data);
  }
  function updateDetail({ job, events }) {
    const target = modal.querySelector("#lab-job-detail");
    if (!target) return;
    const markup = `${statusBadge(job.state, jobStates[job.state])}<p class="lab-context-caption">${e(job.context.goal_title || job.context.room_name || "直接联系")}</p><h3>发送的内容</h3><div class="document-block">${e(job.prompt)}</div>${job.context.resources ? `<h3>参考资料</h3><div class="document-block">${e(job.context.resources)}</div>` : ""}<div class="lab-events">${events
      .filter((item) => item.kind === "running" && item.text !== job.result)
      .map((item) => `<p>${e(item.text)}</p>`)
      .join(
        "",
      )}</div>${job.result ? `<h3>返回结果</h3><div class="document-block">${e(job.result)}</div>` : `<p class="muted">${["queued", "running", "cancelling"].includes(job.state) ? "收到进展或结果后，会自动显示在这里。" : "工作已结束，没有返回内容。"}</p>`}<p class="muted">${["interrupted", "failed"].includes(job.state) ? "请先核实目标机器上的执行情况。重新发送会创建一份新的工作记录。" : "结果来自 Agent，仍需结合实际证据进行判断。"}</p>${["queued", "running"].includes(job.state) ? `<button class="button" data-lab="cancel" data-id="${e(job.id)}">取消这项工作</button>` : ""}`;
    if (target.dataset.rendered !== markup) {
      target.innerHTML = markup;
      target.dataset.rendered = markup;
    }
  }
  async function submit(action) {
    if (busy) return;
    busy = true;
    modal
      .querySelectorAll('button[type="submit"]')
      .forEach((b) => (b.disabled = true));
    try {
      await action();
    } catch (err) {
      error(err);
    } finally {
      busy = false;
      modal
        .querySelectorAll('button[type="submit"]')
        .forEach((b) => (b.disabled = false));
    }
  }
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-lab]");
    if (!button) return;
    const action = button.dataset.lab,
      id = button.dataset.id;
    try {
      if (action === "close") {
        modal.close();
        detailId = null;
      }
      if (["guide", "guide-local", "guide-remote"].includes(action)) {
        const remote = action === "guide-remote";
        show(
          remote
            ? "连接远程 Agent"
            : action === "guide-local"
              ? "连接本地 Agent"
              : "Agent 接入指南",
          `<p class="lab-dialog-intro">${remote ? "Agent 在远程机器运行，通过 SSH 隧道访问你的 Lab。" : "每位 Agent 有自己的身份与职责。连接后，就可以带着不同的问题和资料，在研究室里联系它。"}</p><ol class="lab-guide-steps"><li><span>01</span><div><h3>准备工作环境</h3><p>在运行 Agent 的机器上安装 Euboulia 和所需 runtime。Codex 需要在那台机器上完成登录；其他 Agent 可配置 command adapter。</p></div></li><li><span>02</span><div><h3>创建身份，获取连接码</h3><p>在本页点击「连接 Agent」，填写名字和一句话职责即可。连接码有效期 10 分钟，只能使用一次，无需提前选择源码目录。</p></div></li><li><span>03</span><div><h3>${remote ? "建立隧道，运行连接命令" : "在目标机器完成连接"}</h3><p>${remote ? "先在 Lab 所在机器建立 SSH 反向隧道，再在远程机器执行连接与启动命令。创建身份后会提供完整命令。" : "运行页面提供的连接命令，自动准备独立工作空间，再启动连接程序。远程接入时，先展开 SSH 隧道说明。"}</p></div></li><li><span>04</span><div><h3>从一个消息开始</h3><p>队友上线后，打开详情发送消息。可以附上多个源码路径或文档链接；在研究室内发送时，还会带上当前目标的上下文。</p></div></li></ol><p class="lab-inline-note">${glyph("shield")}<span>Lab 不会接管已有的 Codex 桌面会话。连接程序启动自己的 Agent 会话；工具和权限在目标机器配置。</span></p><div class="lab-dialog-footer"><span>连接程序需要保持运行</span><button class="button primary" data-lab="close">知道了</button></div>`,
        );
      }
      if (action === "data-info") {
        show(
          "数据与权限",
          `<div class="lab-info-section"><h3>工作记录保存在 Lab</h3><p>消息、任务和结果保存在 Lab 服务中。研究室与 Goal 仍保存在当前浏览器；每次派发会保存当时的上下文快照。</p></div><div class="lab-info-section"><h3>执行权限由目标机器决定</h3><p>职责描述用于沟通。工作空间由连接程序准备，也可以在本机配置默认目录；实际工具和权限仍由目标机器的运行配置决定，默认 Codex 以只读权限运行。</p></div><div class="lab-info-section"><h3>研究结论仍需核实</h3><p>Agent 返回的内容是工作结果，不会自动成为已验证的研究结论。</p></div><div class="lab-dialog-footer"><button class="button primary" data-lab="close">知道了</button></div>`,
        );
      }
      if (action === "login") {
        show(
          "登录本地 Lab",
          '<form id="lab-login"><p class="lab-dialog-intro">在 Lab 服务所在目录获取管理密钥。</p><pre>uv run euboulia lab token</pre><label>Lab 管理密钥<input name="key" type="password" autocomplete="off" placeholder="粘贴终端输出的管理密钥" required></label><div class="lab-dialog-footer"><button class="button" type="button" data-lab="close">取消</button><button class="button primary" type="submit">登录 Lab</button></div></form>',
        );
        modal.querySelector("form").onsubmit = (event) => {
          event.preventDefault();
          submit(async () => {
            await api("login", Object.fromEntries(new FormData(event.target)));
            modal.close();
            await refresh();
          });
        };
      }
      if (action === "create") {
        show(
          "创建 Agent 身份",
          `<form id="lab-create"><div class="lab-wizard-steps"><span class="current">1 · 创建身份</span><span>2 · 连接环境</span><span>3 · 等待上线</span></div><p class="lab-dialog-intro">给队友起个名字，用一句话介绍它负责的事。</p><label>名字 <span class="lab-field-hint">用一个方便称呼的名字</span><input name="name" maxlength="80" required placeholder="例如 Prism"></label><label>职责 <span class="lab-field-hint">它在研究中负责什么？</span><textarea name="role" maxlength="1000" rows="3" required placeholder="例如：检查性能证据，区分观察与推测，提出下一步验证建议"></textarea></label><p class="lab-inline-note">${glyph("shield")}<span>创建时无需绑定源码目录。具体任务可以附上多个仓库、数据路径和文档链接。</span></p><div class="lab-dialog-footer"><button class="button" type="button" data-lab="close">取消</button><button class="button primary" type="submit">下一步，连接环境 ${glyph("arrow")}</button></div></form>`,
        );
        modal.querySelector("form").onsubmit = (event) => {
          event.preventDefault();
          submit(async () => {
            pairing(
              await api(
                "agents",
                Object.fromEntries(new FormData(event.target)),
              ),
            );
            await refresh();
          });
        };
      }
      if (action === "copy-code") {
        await navigator.clipboard.writeText(
          modal.querySelector('input[aria-label="一次性连接码"]').value,
        );
        notify("连接码已复制。");
      }
      if (action === "copy-command") {
        await navigator.clipboard.writeText(
          modal.querySelector(`#${button.dataset.target}`).textContent,
        );
        notify("命令已复制。");
      }
      if (action === "send") send(id);
      if (action === "job") await showJob(id);
      if (action === "agent") {
        const agent = snapshot.agents.find((a) => a.id === id);
        show(
          agent.name,
          `${statusBadge(agent.status, agentStates[agent.status])}<p class="lab-dialog-intro">${e(agent.role)}</p><dl class="lab-agent-facts">${agent.capabilities ? `<div><dt>擅长</dt><dd>${e(agent.capabilities)}</dd></div>` : ""}<div><dt>工作机器</dt><dd>${e(agent.host || "尚未连接")}</dd></div><div><dt>运行方式</dt><dd>${e(agent.runtime || "尚未配置")}</dd></div><div><dt>工作空间</dt><dd><code>${e(agent.workspace || "连接时自动准备")}</code><small class="lab-field-hint">默认启动位置；任务可以引用多个目录与资料。</small></dd></div></dl><p class="muted">${agent.last_seen ? "最后连接：" + e(new Date(agent.last_seen * 1000).toLocaleString()) : "还没有连接程序上线。"}</p><details><summary>查看启动命令</summary><pre>uv run euboulia lab agent run \\\n  --config ~/.config/euboulia/agents/${e(id)}/agent.json</pre></details><div class="lab-dialog-footer lab-agent-actions"><div><button class="text-button" data-lab="repair-confirm" data-id="${e(id)}">重新配对</button>${agent.status !== "revoked" ? `<button class="text-button" data-lab="revoke-confirm" data-id="${e(id)}">撤销连接</button>` : ""}</div>${!["unpaired", "revoked"].includes(agent.status) ? `<button class="button primary" data-lab="send" data-id="${e(id)}">发送消息或任务 ${glyph("arrow")}</button>` : ""}</div>`,
        );
      }
      if (["repair-confirm", "revoke-confirm"].includes(action)) {
        show(
          action === "repair-confirm"
            ? "重新配对这个 Agent"
            : "撤销 Agent 连接",
          `<p>原连接凭据将立即失效，排队工作会取消，执行中的工作会标记为中断。连接程序收到撤销后会停止自己启动的进程；离线时最多等待约 30 秒。请核实未完成工作。</p><button class="button primary" data-lab="${action === "repair-confirm" ? "repair" : "revoke"}" data-id="${e(id)}">确认${action === "repair-confirm" ? "重新配对" : "撤销连接"}</button>`,
        );
      }
      if (action === "repair") {
        pairing(await api("repair", { agent_id: id }));
        await refresh();
      }
      if (action === "revoke") {
        await api("revoke", { agent_id: id });
        modal.close();
        await refresh();
      }
      if (action === "cancel") {
        await api("cancel", { job_id: id });
        await refresh();
      }
      if (action === "logout") {
        await api("logout", {});
        mode = "locked";
        snapshot = { agents: [], jobs: [] };
        onChange();
      }
    } catch (err) {
      if (modal.open) error(err);
      else notify(err.message);
    }
  });
  modal.addEventListener("close", () => {
    detailId = null;
    modal.innerHTML = "";
  });
  async function refresh() {
    const before = mode,
      wasConnected = connected;
    try {
      snapshot = await api("state");
      mode = "live";
      connected = true;
      const next = JSON.stringify([
        snapshot.agents.map(({ last_seen, ...agent }) => agent),
        snapshot.jobs.map(({ updated, ...job }) => job),
      ]);
      if (signature !== next || before !== "live" || !wasConnected) {
        signature = next;
        onChange();
      }
      if (detailId && modal.open)
        updateDetail(await api("jobs/" + encodeURIComponent(detailId)));
    } catch (err) {
      connected = false;
      if (mode === "locked") {
        snapshot = { agents: [], jobs: [] };
        signature = "";
      }
      if (before !== mode || wasConnected) onChange();
      if (detailId) error(err);
    }
  }
  async function start() {
    try {
      const health = await api("health");
      if (health.service !== "euboulia-lab") throw Error();
      mode = "locked";
    } catch {
      mode = "demo";
      onChange();
      return;
    }
    await refresh();
    const tick = async () => {
      if (!document.hidden && !busy) await refresh();
      setTimeout(tick, 4000);
    };
    setTimeout(tick, 4000);
  }
  return {
    start,
    page,
    feed,
    get mode() {
      return mode;
    },
    get agents() {
      return snapshot.agents;
    },
    decorate() {
      const label = document.querySelector("#connection-label");
      const online = snapshot.agents.filter((a) =>
        ["available", "busy"].includes(a.status),
      ).length;
      label.parentElement.dataset.connected = String(
        mode === "live" && connected && online > 0,
      );
      if (mode === "live") {
        label.textContent = connected
          ? online
            ? `${online} 位 Agent 已连接`
            : snapshot.agents.length
              ? "暂无 Agent 在线"
              : "等待 Agent 加入"
          : "Lab 服务连接中断";
        document.querySelector(".prototype-label").textContent =
          "研究室原型 · Agent 通信已启用";
        document.querySelector(".composer-footnote").textContent =
          "此输入框保存本地讨论；向真实 Agent 发送请使用“联系真实 Agent”入口。";
        document.querySelector(".quiet-state").textContent = "研究记录 · 原型";
        document.querySelector(".demo-pill").textContent = "LAB";
      } else if (mode === "locked") {
        label.textContent = "Lab 等待登录";
        document.querySelector(".demo-pill").textContent = "LAB";
        document.querySelector(".prototype-label").textContent =
          "研究室原型 · 本地 Lab";
      }
    },
  };
}
