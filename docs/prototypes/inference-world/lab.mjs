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
    modal.innerHTML = `<div class="lab-dialog-heading"><h2 id="lab-dialog-title">${e(title)}</h2><button class="icon-button" data-lab="close" aria-label="关闭 Agent 面板">×</button></div>${body}<p class="lab-error" role="alert"></p>`;
    if (!modal.open) modal.showModal();
    modal.querySelector("input,textarea,select,button")?.focus();
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
      `<p>连接码有效期 10 分钟，只能使用一次。请在要运行 Agent 的机器上执行下面的步骤。</p>
      <label>一次性连接码<input readonly value="${e(data.pairing_code)}" aria-label="一次性连接码"></label>
      <button class="button" data-lab="copy-code">复制连接码</button>
      <ol><li>安装 Euboulia 和所需的 Agent runtime。</li><li>在终端执行连接命令，按提示粘贴连接码。</li><li>运行连接程序；此面板关闭后也可以在成员详情查看命令。</li></ol>
      <pre>uv run euboulia lab connect \\\n  --url ${e(location.origin)} \\\n  --workspace /path/to/workspace \\\n  --config ~/.config/euboulia/agents/${e(data.agent_id)}/agent.json</pre>
      <pre>uv run euboulia lab agent run \\\n  --config ~/.config/euboulia/agents/${e(data.agent_id)}/agent.json</pre>
      <details><summary>在远程机器上接入</summary><p>让远程机器通过 SSH 转发访问这个 Lab。Lab 所在机器执行：</p><pre>ssh -N -o ExitOnForwardFailure=yes \\\n  -R ${location.port || "8773"}:127.0.0.1:${location.port || "8773"} YOUR_REMOTE_HOST</pre><p>然后在远程终端执行上面的连接命令。两个端口保持一致；连接期间保留 SSH 隧道。</p></details>
      <p class="muted">默认 Codex 以只读权限运行。其他 Agent 可使用本地配置的 command adapter，详细协议见仓库 docs/lab-agents.md。</p>
      <button class="button primary" data-lab="close">完成，查看连接状态</button>`,
    );
  }
  function card(agent) {
    return `<button class="computer-card lab-agent-card" data-lab="agent" data-id="${e(agent.id)}"><span class="eyebrow">${e(agent.runtime || "等待选择运行方式")}</span><strong>${e(agent.name)}</strong><p>${e(agent.role)}</p><div class="tag-row"><span class="tag">${e(agentStates[agent.status])}</span>${agent.host ? `<span class="tag">${e(agent.host)}</span>` : ""}</div><small>${e(agent.capabilities)}</small></button>`;
  }
  function jobsMarkup(jobs) {
    return jobs
      .map(
        (job) =>
          `<button class="lab-job" data-lab="job" data-id="${e(job.id)}"><span class="lab-job-meta">${e(snapshot.agents.find((a) => a.id === job.agent_id)?.name || "Agent")} · ${e(job.kind === "task" ? "任务" : "消息")} <span class="tag">${e(jobStates[job.state])}</span></span><strong>${e(job.prompt)}</strong>${job.result ? `<span class="lab-result-preview">${e(job.result)}</span>` : ""}<small>${e(job.context.goal_title || job.context.room_name || "直接联系")} · ${e(new Date(job.created * 1000).toLocaleString())}</small></button>`,
      )
      .join("");
  }
  function page() {
    if (mode === "checking") return "";
    if (mode === "demo")
      return `<div class="memory-card"><span class="eyebrow">真实 Agent 接入</span><h3>把你自己的 Agent 带进研究室</h3><p>启动 Lab 服务后，可以创建身份、连接机器、发送消息和任务。</p><pre>uv run euboulia lab serve</pre><a class="button" href="http://127.0.0.1:8773/prototypes/inference-world/">打开本地 Lab ↗</a></div>`;
    if (mode === "locked")
      return `<div class="page-body"><div class="empty"><span class="empty-icon">↗</span><h2>连接你的 Agent 团队</h2><p>用本机 Lab 管理密钥登录，创建 Agent 身份并连接它的工作环境。</p><pre>uv run euboulia lab token</pre><button class="button primary" data-lab="login">登录 Lab</button></div></div>`;
    return `<div class="page-body"><div class="page-intro"><div><span class="eyebrow">已接入的 Agent · 实际连接</span><h2>让不同机器上的队友一起工作</h2><p>每个 Agent 有自己的身份、职责和收件箱。连接程序在它所在的机器上运行。</p></div><button class="button primary" data-lab="create">＋ 连接 Agent</button></div>
      ${!connected ? '<p class="lab-error" role="status">Lab 服务连接中断。以下为上次收到的状态。</p>' : ""}
      <div class="computer-grid">${snapshot.agents.map(card).join("") || '<div class="empty"><h3>第一位 Agent，从这里加入</h3><p>先为它起个名字、说明职责，再把连接命令带到本地或远程机器。</p><button class="button" data-lab="create">连接第一个 Agent</button></div>'}</div>
      ${snapshot.jobs.length ? `<div class="section-title"><h2>最近的通信与工作</h2><span>最近 200 条</span></div><div class="lab-job-list">${jobsMarkup(snapshot.jobs)}</div>` : ""}
      <p class="lab-footnote">消息、任务和结果保存在 Lab 服务中。研究室与 Goal 仍保存在当前浏览器；每次派发会保存当时的上下文快照。Agent 的回答不自动成为已验证的研究结论。</p><button class="text-button" data-lab="logout">退出 Lab 管理</button></div>`;
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
      `<form id="lab-send"><p>上下文：${e(context.room_name || "直接联系")} ${context.goal_title ? " / " + e(context.goal_title) : ""}</p><label>接收人<select name="agent_id">${agents.map((a) => `<option value="${e(a.id)}" ${a.id === agentId ? "selected" : ""}>${e(a.name)} · ${e(agentStates[a.status])}</option>`).join("")}</select></label><label>发送方式<select name="kind"><option value="message">消息 · 交流、提问和补充信息</option><option value="task" ${context.goal_status && context.goal_status !== "active" ? "disabled" : ""}>任务 · 请 Agent 执行并返回结果</option></select></label><label>内容<textarea name="prompt" rows="6" maxlength="16000" required placeholder="告诉队友你需要什么，以及希望得到怎样的结果…"></textarea></label><p class="muted">发送会调用目标机器上的真实 Agent。连接中断时排队等待；正在执行的工作不会被新消息打断。</p><button class="button primary" type="submit">发送</button></form>`,
    );
    modal.querySelector("form").addEventListener("submit", (event) => {
      event.preventDefault();
      submit(async () => {
        const values = Object.fromEntries(new FormData(event.target));
        const job = await api("jobs", {
          ...values,
          context,
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
    const markup = `<p class="tag">${e(jobStates[job.state])}</p><p>${e(job.context.goal_title || job.context.room_name || "直接联系")}</p><div class="document-block">${e(job.prompt)}</div><div class="lab-events">${events
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
      if (action === "login") {
        show(
          "登录本地 Lab",
          '<form id="lab-login"><p>在 Lab 服务所在目录运行 <code>uv run euboulia lab token</code>，粘贴输出的管理密钥。</p><label>Lab 管理密钥<input name="key" type="password" autocomplete="off" required></label><button class="button primary" type="submit">登录</button></form>',
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
          '<form id="lab-create"><label>名字<input name="name" maxlength="80" required placeholder="例如 Prism"></label><label>职责<textarea name="role" maxlength="1000" rows="3" required placeholder="负责哪些研究问题，如何与队友协作"></textarea></label><label>能力<textarea name="capabilities" maxlength="1000" rows="2" required placeholder="例如：分析 trace、阅读 SGLang 源码"></textarea></label><p class="muted">职责与能力描述用于沟通；实际工具、工作目录和权限由目标机器上的运行配置决定。</p><button class="button primary" type="submit">创建并获取连接码</button></form>',
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
      if (action === "send") send(id);
      if (action === "job") await showJob(id);
      if (action === "agent") {
        const agent = snapshot.agents.find((a) => a.id === id);
        show(
          agent.name,
          `<span class="tag">${e(agentStates[agent.status])}</span><p>${e(agent.role)}</p><div class="document-block">能力：${e(agent.capabilities)}<br>机器：${e(agent.host || "尚未连接")}<br>运行方式：${e(agent.runtime || "尚未配置")}<br>工作目录：${e(agent.workspace || "尚未配置")}</div><p class="muted">${agent.last_seen ? "最后连接：" + e(new Date(agent.last_seen * 1000).toLocaleString()) : "还没有连接程序上线。"}</p><pre>uv run euboulia lab agent run \\\n  --config ~/.config/euboulia/agents/${e(id)}/agent.json</pre><div class="lab-actions">${!["unpaired", "revoked"].includes(agent.status) ? `<button class="button primary" data-lab="send" data-id="${e(id)}">发送消息或任务</button>` : ""}<button class="button" data-lab="repair-confirm" data-id="${e(id)}">重新配对</button>${agent.status !== "revoked" ? `<button class="text-button" data-lab="revoke-confirm" data-id="${e(id)}">撤销连接</button>` : ""}</div>`,
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
      if (mode === "live") {
        label.textContent = connected
          ? `${snapshot.agents.filter((a) => ["available", "busy"].includes(a.status)).length} 位 Agent 已连接`
          : "Lab 服务连接中断";
        document.querySelector(".prototype-label").textContent =
          "研究室原型 · Agent 通信已启用";
        document.querySelector(".composer-footnote").textContent =
          "此输入框保存本地讨论；向真实 Agent 发送请使用“联系真实 Agent”入口。";
        document.querySelector(".quiet-state").textContent = "研究记录 · 原型";
        document.querySelector(".demo-pill").textContent = "LAB";
      } else if (mode === "locked") label.textContent = "Lab 等待登录";
    },
  };
}
