// Codex-bar: folder/Git chips on the left; Codex model, context, and account quotas on the right.
import { View, div, svg } from "gpui-kit";
import { v_flex, h_flex } from "gpui-base";
import { spaces, panes, focused, exec, watchFolder, theme, home, appHome, layout, props, run, notify, openOverlay, closeOverlay } from "herdr";

const NAME = "codex-bar";
const ICON = (name) => `mods/${NAME}/${name}.svg`;
const READ = ["--no-optional-locks", "-c", "core.fsmonitor=false"];
const WSL = "wsl";

function collectorPath() {
  const path = `${appHome()}/mods/${NAME}/collector.py`.replace(/\\/g, "/");
  const drive = /^([A-Za-z]):\//.exec(path);
  return drive ? `/mnt/${drive[1].toLowerCase()}${path.slice(2)}` : path;
}

function where() {
  const f = focused(), space = spaces().find((s) => s.active);
  const pane = f?.pane ? panes().find((p) => p.pane === f.pane) : null;
  const on = f?.pane === null ? (f.terminal?.includes("/") ? f.terminal.split("/")[0] : null) : space?.machine ?? null;
  return { cwd: pane?.cwd ?? f?.cwd ?? space?.folder ?? null, on, pane, terminal: pane?.terminal ?? f?.terminal ?? null, space };
}

const folderName = (cwd) => (!cwd ? null : cwd === home() ? "~" : cwd.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || "/");
const isCodex = (pane) => [pane?.app, pane?.program?.key].some((value) => String(value ?? "").toLowerCase().includes("codex"));
const shortModel = (model) => String(model ?? "");

function frameRadius() {
  let pane = null;
  const walk = (node) => {
    if (!node || pane) return;
    if (node.content?.tab && node.content.pane) pane = node.content.pane;
    for (const child of node.children ?? []) walk(child);
  };
  try { walk(layout()); } catch {}
  const radius = Number(pane?.radius ?? 6);
  return Math.max(0, Math.min(6, Number.isFinite(radius) ? radius : 6));
}

function revealMod(mod) {
  const path = [];
  const walk = (node) => {
    if (!node) return false;
    path.push(node);
    if (node.content?.mod === mod) return true;
    for (const child of node.children ?? []) if (walk(child)) return true;
    path.pop();
    return false;
  };
  let found = false;
  try { found = walk(layout()); } catch {}
  if (!found) return false;
  path.forEach((node, i) => {
    if (node.type === "stack" && path[i + 1]?.id) run("layout.select", { stack: node.id, child: path[i + 1].id });
    if (i > 0 && node.id) run("layout.show", { id: node.id });
  });
  return true;
}

function parseStatus(stdout) {
  const git = { branch: null, upstream: "", ahead: 0, behind: 0, changed: 0, staged: 0, untracked: 0, conflicts: 0 };
  for (const rec of stdout.split("\0")) {
    if (rec.startsWith("## ")) {
      const body = rec.slice(3);
      git.branch = body.startsWith("HEAD (no branch)") ? "detached" : body.match(/^(?:No commits yet on |Initial commit on )?(.+?)(?:\.\.\.|\s|$)/)?.[1] ?? body;
      git.upstream = body.match(/\.\.\.(\S+)/)?.[1] ?? "";
      git.ahead = Number(body.match(/ahead (\d+)/)?.[1] ?? 0);
      git.behind = Number(body.match(/behind (\d+)/)?.[1] ?? 0);
      continue;
    }
    if (rec.length < 4) continue;
    const xy = rec.slice(0, 2);
    git.changed++;
    if (xy === "??") { git.untracked++; continue; }
    if (/U/.test(xy) || xy === "AA" || xy === "DD") { git.conflicts++; continue; }
    if (xy[0] !== " ") git.staged++;
  }
  return git;
}

function resetText(epoch) {
  if (!epoch) return null;
  const seconds = Math.max(0, Math.floor(epoch - Date.now() / 1000));
  const hours = Math.floor(seconds / 3600), minutes = Math.floor(seconds % 3600 / 60);
  return hours >= 24 ? `${Math.floor(hours / 24)}d ${hours % 24}h` : `${hours}h ${String(minutes).padStart(2, "0")}m`;
}

function ageText(epoch) {
  if (!epoch) return "time unknown";
  const minutes = Math.max(0, Math.round((Date.now() / 1000 - epoch) / 60));
  return minutes < 1 ? "just now" : `${minutes}m ago`;
}

export default class CodexBar extends View {
  init(_props, cx) {
    this.popup = props()?.popup ?? null;
    if (this.popup) return;
    this.git = null;
    this.quota = null;
    this.context = null;
    this.width = 1000;
    this.refresh(cx);
    this.refreshQuota(cx);
    this.refreshContext(cx);
    cx.timer.every(8000, (cx) => this.refresh(cx));
    cx.timer.every(15000, (cx) => this.refreshContext(cx));
    cx.timer.every(300000, (cx) => this.refreshQuota(cx));
  }

  refresh(cx) {
    const { cwd, on } = where();
    if (!cwd) { this.git = null; return; }
    if (this.gitLoading) { this.gitAgain = true; return; }
    const key = JSON.stringify([cwd, on]);
    this.gitLoading = true;
    cx.spawn(async (cx) => {
      try {
        const gitExec = (args) => exec("git", [...READ, ...args], { cwd, on });
        const top = await gitExec(["rev-parse", "--show-toplevel"]).catch(() => null);
        if (top?.code !== 0) { if (this.gitKey === key) this.git = null; return; }
        const [status, stash] = await Promise.all([
          gitExec(["status", "--porcelain=v1", "-z", "-b", "--untracked-files=normal"]),
          gitExec(["rev-list", "--walk-reflogs", "--count", "refs/stash"]).catch(() => null),
        ]);
        if (this.gitKey !== key || status.code !== 0) return;
        this.git = { ...parseStatus(status.stdout), stash: stash?.code === 0 ? Number(stash.stdout.trim()) || 0 : 0, top: top.stdout.trim(), cwd, on };
        this.watchRepo(this.git.top, on, cx);
      } catch { if (this.gitKey === key) this.git = null; }
      finally {
        this.gitLoading = false;
        if (this.gitAgain) { this.gitAgain = false; this.refresh(cx); }
        cx.notify();
      }
    });
    this.gitKey = key;
  }

  watchRepo(top, on, cx) {
    if (on || this.watched === top) return;
    this.watched = top;
    cx.spawn(async (cx) => {
      try { while (this.watched === top) { await watchFolder(top); if (this.watched === top) this.refresh(cx); } }
      catch { if (this.watched === top) this.watched = null; }
    });
  }

  refreshQuota(cx) {
    if (this.quotaLoading) { this.quotaAgain = true; return; }
    this.quotaLoading = true;
    cx.spawn(async (cx) => {
      try {
        const result = await exec("python", [`${appHome()}/mods/${NAME}/collector.py`.replace(/\\/g, "/"), "quota"]);
        this.quota = result.code === 0 ? JSON.parse(result.stdout) : { source: "unavailable", error: "collector_failed" };
      } catch { this.quota = { source: "unavailable", error: "collector_unavailable" }; }
      finally {
        this.quotaLoading = false;
        if (this.quotaAgain) { this.quotaAgain = false; this.refreshQuota(cx); }
        cx.notify();
      }
    });
  }

  refreshContext(cx) {
    const w = where(), key = JSON.stringify([w.terminal, w.cwd, w.on, isCodex(w.pane)]);
    if (key !== this.contextKey) { this.contextKey = key; this.context = null; }
    if (!isCodex(w.pane) || w.on !== WSL || !w.cwd) {
      if (this.context) { this.context = null; cx.notify(); }
      return;
    }
    if (this.contextLoading) return;
    this.contextLoading = true;
    cx.spawn(async (cx) => {
      try {
        const result = await exec("python3", [collectorPath(), "context", w.cwd], { on: WSL });
        if (this.contextKey === key) this.context = result.code === 0 ? JSON.parse(result.stdout) : { status: "unavailable" };
      } catch { if (this.contextKey === key) this.context = { status: "unavailable" }; }
      finally { this.contextLoading = false; cx.notify(); }
    });
  }

  openChanged() { revealMod("changes"); }

  openPopup(kind, height = 180) {
    openOverlay({ mod: NAME, kind: "popup", width: 300, height, props: { popup: { kind, git: this.git, quota: this.quota, context: this.context } } });
  }

  chip(id, t, radius, { icon, iconColor, color, tip, onClick }, ...kids) {
    return h_flex().id(id).h(20).px(7).gap(5).flex_none().items_center().rounded(radius)
      .text_size(11.5).text_color(color ?? t.muted)
      .when(Boolean(tip), (el) => el.tooltip(tip))
      .when(Boolean(onClick), (el) => el.cursor_pointer().hover((s) => s.bg(t.hover)).on_mouse_down("left", (_e, cx) => cx.stop_propagation()).on_click((_e, cx) => { onClick(cx); cx.notify(); }))
      .when(Boolean(icon), (el) => el.child(svg(ICON(icon)).size(11).flex_none().text_color(iconColor ?? color ?? t.dim)))
      .children(kids.filter((kid) => kid !== null && kid !== undefined && kid !== ""));
  }

  meterColor(t, used) { return used >= 95 ? t.ansi1 : used >= 80 ? t.ansi3 : t.ansi2; }

  track(t, used, color, width = 42, height = 4) {
    return div().w(width).h(height).flex_none().bg(t.raised)
      .child(div().h_full().w(Math.max(used > 0 ? 2 : 0, width * used / 100)).bg(color));
  }

  renderPopup() {
    const t = theme(), p = this.popup, close = () => closeOverlay(props().overlay);
    const item = (id, label, sub, action) => v_flex().id(id).px(10).py(6).gap(1).rounded(frameRadius()).cursor_pointer().hover((s) => s.bg(t.hover))
      .on_click(() => { close(); action(); }).child(div().text_size(12.5).text_color(t.foreground).child(label))
      .when(Boolean(sub), (el) => el.child(div().text_size(11).text_color(t.dim).child(sub)));
    const gitRun = (args, doing, done) => {
      const git = p.git;
      notify({ title: `${doing}…`, level: "info", timeout: 1500 });
      exec("git", args, { cwd: git.cwd, on: git.on })
        .then((r) => notify(r.code === 0 ? { title: done, body: (r.stderr || r.stdout).trim().split("\n").pop(), level: "success", timeout: 3000 } : { title: `${doing} failed`, body: (r.stderr || r.stdout).trim(), level: "error", timeout: 6000 }))
        .catch((e) => notify({ title: `${doing} failed`, body: String(e?.message ?? e), level: "error", timeout: 6000 }));
    };
    if (p.kind === "git") {
      const git = p.git, upstream = git.upstream || "its upstream";
      return v_flex().id("cb-git").size_full().p(5).bg(t.popover ?? t.surface).children([
        git.ahead ? item("cb-push", `Push ${git.ahead} commit${git.ahead === 1 ? "" : "s"}`, `to ${upstream}`, () => gitRun(["push"], "Pushing", "Pushed")) : null,
        git.behind ? item("cb-pull", `Pull ${git.behind} commit${git.behind === 1 ? "" : "s"}`, `from ${upstream}`, () => gitRun(["pull", "--ff-only"], "Pulling", "Pulled")) : null,
        item("cb-fetch", "Fetch", "update ↑ ↓ from the remote", () => gitRun(["fetch"], "Fetching", "Fetched")),
      ].filter(Boolean));
    }
    const quota = p.quota ?? {}, context = p.context?.status === "available" ? p.context : null;
    const windowRow = (label, window) => h_flex().gap(8).items_center()
      .child(div().w(62).text_color(t.dim).child(label))
      .child(div().flex_1().child(window?.used_percent == null ? "—" : `${window.used_percent}% used`))
      .child(div().text_color(t.dim).child(window?.expired ? "expired" : window?.reset_at ? `resets ${resetText(window.reset_at)}` : "reset unknown"));
    return v_flex().id("cb-codex").size_full().overflow_y_scrollbar().px(14).py(12).gap(8).bg(t.popover ?? t.surface).text_size(12).text_color(t.muted)
      .child(div().text_size(10.5).font_semibold().text_color(t.dim).child("CLAUDE USAGE"))
      .child(h_flex().gap(8).items_center().child(div().text_color(t.foreground).font_medium().child(quota.plan || "Claude"))
        .child(div().text_size(11).text_color(t.dim).child(quota.source === "api" ? "live API" : quota.source === "snapshot" ? "local snapshot" : "unavailable")))
      .child(windowRow("5-hour", quota.primary))
      .child(windowRow("Weekly", quota.secondary))
      .when(Boolean(context), (el) => el.child(div().text_size(11).text_color(t.dim).child(`${shortModel(context.model)} · context ${context.context_percent}% (${context.context_tokens.toLocaleString()}/${context.context_window.toLocaleString()})`)))
      .child(div().text_size(10.5).text_color(t.dim).child(`Quota updated ${ageText(quota.observed_at)}${quota.error ? ` · API ${quota.error}` : ""}`));
  }

  render(cx) {
    if (this.popup) return this.renderPopup();
    const t = theme(), w = where(), radius = frameRadius();
    const key = JSON.stringify([w.cwd, w.on]);
    if (key !== this.gitKey) { this.gitKey = key; this.git = null; this.refresh(cx); }
    const contextKey = JSON.stringify([w.terminal, w.cwd, w.on, isCodex(w.pane)]);
    if (contextKey !== this.contextKey) this.refreshContext(cx);
    const wide = this.width >= 900, mid = this.width >= 620;
    const git = this.git, quota = this.quota, context = this.context?.status === "available" ? this.context : null;
    const name = folderName(w.cwd), branch = git?.branch ?? w.pane?.branch ?? w.space?.branch ?? null;
    const chip = (id, opts, ...kids) => this.chip(id, t, radius, opts, ...kids);
    const left = [
      name ? h_flex().id("cb-folder").gap(6).flex_none().items_center().tooltip(w.cwd ?? "").child(svg(ICON("folder")).size(13).text_color(t.accent)).child(div().max_w(180).truncate().child(name)) : null,
      branch ? chip("cb-branch", { icon: "git-branch", iconColor: t.accent, color: t.foreground, tip: git?.upstream ? `Tracks ${git.upstream} · click to copy` : "Click to copy", onClick: (cx) => { cx.write_to_clipboard(branch); notify({ title: "Copied", body: branch, timeout: 1500 }); } }, div().max_w(wide ? 260 : 160).truncate().child(branch)) : null,
      git?.ahead ? chip("cb-ahead", { icon: "arrow-up", color: t.ansi6, tip: `${git.ahead} to push`, onClick: () => this.openPopup("git", 145) }, String(git.ahead)) : null,
      git?.behind ? chip("cb-behind", { icon: "arrow-down", color: t.ansi4, tip: `${git.behind} to pull`, onClick: () => this.openPopup("git", 145) }, String(git.behind)) : null,
      git?.changed ? chip("cb-changed", { icon: "pencil", color: t.ansi11 ?? t.ansi3, tip: `${git.changed} uncommitted (${git.untracked} untracked) · open Changed`, onClick: () => this.openChanged() }, String(git.changed)) : null,
      git?.staged && mid ? chip("cb-staged", { icon: "plus", color: t.ansi2, tip: `${git.staged} staged` }, String(git.staged)) : null,
      git?.conflicts ? chip("cb-conflicts", { icon: "triangle-alert", color: t.ansi1, tip: `${git.conflicts} with conflicts · open Changed`, onClick: () => this.openChanged() }, String(git.conflicts)) : null,
      git?.stash && mid ? chip("cb-stash", { icon: "archive", color: t.dim, tip: `${git.stash} stash${git.stash === 1 ? "" : "es"}` }, String(git.stash)) : null,
    ].filter(Boolean);
    const right = [];
    if (isCodex(w.pane)) {
      const ctxTip = context ? `${context.context_tokens.toLocaleString()} / ${context.context_window.toLocaleString()} tokens · latest Codex token-count snapshot` : "No fresh, unambiguous Codex context snapshot for this working directory";
      right.push(chip("cb-model", { color: context ? t.foreground : t.dim, tip: ctxTip, onClick: () => this.openPopup("codex") }, context ? shortModel(context.model) : "Codex"));
      if (mid) right.push(chip("cb-context", { tip: ctxTip, onClick: () => this.openPopup("codex") }, div().text_color(t.dim).child("ctx"), context ? `${context.context_percent}%` : "—"));
    }
    if (quota?.source && quota.source !== "unavailable") {
      right.push(chip("cb-plan", { color: t.ansi2, tip: `Claude ${quota.plan || "account"} · ${quota.source} · updated ${ageText(quota.observed_at)}`, onClick: () => this.openPopup("codex") }, quota.plan || "Claude"));
      for (const [label, window] of [["5h", quota.primary], ["7d", quota.secondary]]) {
        if (!window) continue;
        if (window.used_percent == null) {
          right.push(chip(`cb-${label}`, { color: t.dim, tip: `${label} quota expired or unknown · ${quota.source}`, onClick: () => this.openPopup("codex") }, label, "—"));
        } else {
          const color = this.meterColor(t, window.used_percent);
          right.push(chip(`cb-${label}`, { tip: `${label}: ${window.used_percent}% used${window.reset_at ? ` · resets ${resetText(window.reset_at)}` : ""}`, onClick: () => this.openPopup("codex") },
            div().text_color(t.dim).child(label), this.track(t, window.used_percent, color), div().child(`${window.used_percent}%`)));
        }
      }
    } else {
      right.push(chip("cb-unavailable", { color: t.dim, tip: "Claude quota unavailable · check WSL auth/network" }, "Claude", "—"));
    }
    if (quota?.source === "snapshot" || quota?.error) right.push(chip("cb-source", { color: t.dim, tip: quota.error ? `Live API unavailable: ${quota.error}` : "Quota from local Codex rollout snapshot" }, quota.source === "snapshot" ? "snapshot" : "offline"));
    return h_flex().id(NAME).size_full().px(14).gap(8).items_center().overflow_hidden().bg(t.chrome).border_t(1).border_color(t.border).text_size(12).text_color(t.muted)
      .on_size_change((size, cx) => { if (Math.abs(size.width - this.width) > 8) { this.width = size.width; cx.notify(); } })
      .children(left).child(div().flex_1().min_w(8)).children(right);
  }
}
