/* FinOps Kanban frontend v2.
 * Multi-project (URL /p/{slug}), drag-drop, search+filter, collapsible cols,
 * compact mode, quick-add, keyboard shortcuts, polling /api/board every 10 s.
 */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const STATUS_TITLE = {
  backlog: "Backlog",
  approved: "Approved",
  analyst: "Analyst",
  in_progress: "In progress",
  testing: "Testing",
  uat: "UAT",
  done: "Done",
  blocked: "Blocked",
  cancelled: "Cancelled",
};

const LS = {
  DENSITY: "kb.density",
  SIDEBAR: "kb.sidebar",
  COLLAPSED_COLS: "kb.collapsedCols",
  EXPANDED_COLS: "kb.expandedCols",
  ARCHIVE_OPEN: "kb.archiveOpen",
  THEME: "kb.theme",
  PROFILE: "kb.profile",
  LAST_PROJECT: "kb.lastProject",
  GROUP_EPICS: "kb.groupEpics",
};

const PROFILES = ["standard", "cyberpunk", "horizon"];
const PROFILE_LABELS = {
  standard: "Standard",
  cyberpunk: "Cyberpunk",
  horizon:   "Horizon",
};

// ----------------------------------------------------------- State

const state = {
  projectId: null,                  // picked from URL or the first project
  project: null,                   // full metadata for the current project
  projects: [],                    // all projects (active + archived)
  board: { columns: [], tasks: {} },
  search: "",
  filters: new Set(),              // 'prio:high', 'blocked', 'assignee:claude', ...
  density: localStorage.getItem(LS.DENSITY) || "comfortable",
  theme: document.documentElement.getAttribute("data-theme") || "dark",
  sidebarCollapsed: localStorage.getItem(LS.SIDEBAR) === "collapsed",
  collapsedCols: _loadJSON(LS.COLLAPSED_COLS),
  expandedCols:  _loadJSON(LS.EXPANDED_COLS),
  groupEpics: localStorage.getItem(LS.GROUP_EPICS) === "1",
  isDragging: false,
};

function _loadJSON(key) {
  try {
    const raw = localStorage.getItem(key);
    return raw ? JSON.parse(raw) : {};
  } catch { return {}; }
}
function saveCollapsed() {
  localStorage.setItem(LS.COLLAPSED_COLS, JSON.stringify(state.collapsedCols));
  localStorage.setItem(LS.EXPANDED_COLS, JSON.stringify(state.expandedCols));
}

// ----------------------------------------------------------- API

async function api(method, path, body) {
  const opts = { method, headers: { "Content-Type": "application/json" } };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const r = await fetch(path, opts);
  if (!r.ok) {
    const text = await r.text();
    throw new Error(`HTTP ${r.status}: ${text}`);
  }
  if (r.status === 204) return null;
  return r.json();
}

// ----------------------------------------------------------- Toast

function toast(msg, kind = "info") {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.toggle("toast--err", kind === "err");
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, 2400);
}

// ----------------------------------------------------------- URL routing

function parseRoute() {
  const m = location.pathname.match(/^\/p\/([a-z][a-z0-9-]*)\/?$/);
  return m ? m[1] : null;     // null = not picked yet, will fall back to first project
}

function navigateTo(projectId, { replace = false } = {}) {
  const url = `/p/${projectId}`;
  if (replace) history.replaceState({ projectId }, "", url);
  else history.pushState({ projectId }, "", url);
  state.projectId = projectId;
  localStorage.setItem(LS.LAST_PROJECT, projectId);
}

// ----------------------------------------------------------- Helpers

function escapeHTML(s) {
  return String(s ?? "").replace(/[&<>"']/g, (m) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[m]);
}

function ownerLabel(owner) {
  if (owner === "user") return "user";
  if (owner === "agent") return "agent";
  if (owner === "any") return "—";
  return "";
}

// ----------------------------------------------------------- Sidebar

function renderSidebar() {
  const list = $("#proj-list");
  list.innerHTML = "";
  const active = state.projects.filter(p => !p.archived);
  for (const p of active) list.appendChild(projectEl(p));

  // archive section
  const archived = state.projects.filter(p => p.archived);
  const archiveBox = $("#proj-archive");
  if (archived.length === 0) {
    archiveBox.hidden = true;
  } else {
    archiveBox.hidden = false;
    $("#archive-count").textContent = archived.length;
    const archList = $("#proj-archive-list");
    archList.innerHTML = "";
    for (const p of archived) archList.appendChild(projectEl(p));
    const open = localStorage.getItem(LS.ARCHIVE_OPEN) === "1";
    archiveBox.classList.toggle("is-open", open);
    archList.hidden = !open;
  }

  // sidebar collapsed state
  $("#sidebar").classList.toggle("is-collapsed", state.sidebarCollapsed);
}

function projectEl(p) {
  const a = document.createElement("a");
  a.className = "proj";
  if (p.id === state.projectId) a.classList.add("proj--active");
  a.dataset.proj = p.id;
  a.href = `/p/${p.id}`;
  a.title = p.path ? `${p.name} — ${p.path}` : p.name;
  a.innerHTML = `
    <span class="proj__mark" style="background:${escapeHTML(p.color)}">${escapeHTML((p.icon || p.name[0] || "?").toUpperCase().slice(0,2))}</span>
    <span class="proj__name">${escapeHTML(p.name)}</span>
    <span class="proj__count">${p.total_tasks || 0}</span>
    <button class="proj__menu" title="Edit" aria-label="Edit">⋯</button>
  `;
  a.addEventListener("click", (e) => {
    if (e.target.closest(".proj__menu")) {
      e.preventDefault();
      e.stopPropagation();
      openEditProj(p);
      return;
    }
    e.preventDefault();
    if (p.id === state.projectId) return;
    navigateTo(p.id);
    loadBoard();
    renderSidebar();
  });
  return a;
}

// ----------------------------------------------------------- Board render

function render(board) {
  state.board = board;
  if (board.project) {
    state.project = board.project;
    $("#topbar-title").textContent = board.project.name;
    $("#proj-mark").style.background = board.project.color;
    document.title = `Kanban — ${board.project.name}`;
  }
  const root = $("#board");
  root.innerHTML = "";
  let total = 0;
  let visible = 0;
  for (const col of board.columns) {
    const list = board.tasks[col.id] || [];
    total += list.length;
    const colEl = document.createElement("section");
    colEl.className = "col";
    colEl.dataset.status = col.id;
    if (isColEffectivelyCollapsed(col.id, list.length > 0)) {
      colEl.classList.add("is-collapsed");
    }
    colEl.innerHTML = `
      <header class="col__head">
        <span class="col__chevron">▾</span>
        <span class="col__title">${escapeHTML(col.title)}</span>
        <span class="col__count">${list.length}</span>
        <button class="col__quickadd" title="Quick add (n)" aria-label="Add">+</button>
      </header>
      <div class="col__list" data-status="${col.id}"></div>
    `;
    root.appendChild(colEl);
    const listEl = colEl.querySelector(".col__list");
    const items = state.groupEpics ? epicGrouping(list) : list.map(t => ({ type: "card", task: t, epicId: null }));
    for (const it of items) {
      if (it.type === "head") {
        const head = document.createElement("div");
        head.className = "epic-group";
        head.dataset.epicId = it.epic.id;
        head.innerHTML = `
          <button class="epic-group__head" title="${escapeHTML(it.epic.title)}">
            <span class="epic-group__mark">EPIC</span>
            <span class="epic-group__id">${escapeHTML(it.epic.id)}</span>
            <span class="epic-group__title">${escapeHTML(it.epic.title)}</span>
            <span class="epic-group__count">${it.count}</span>
          </button>`;
        head.addEventListener("click", (e) => {
          e.stopPropagation();
          if (state.isDragging) return;
          openTaskModal(it.epic.id);
        });
        listEl.appendChild(head);
        continue;
      }
      const card = cardEl(it.task);
      if (it.epicId) card.dataset.epicGroup = it.epicId;
      if (!matchesFilters(it.task)) card.classList.add("is-hidden");
      else visible++;
      listEl.appendChild(card);
    }
    Sortable.create(listEl, {
      group: "tasks",
      animation: 140,
      draggable: ".card",
      ghostClass: "sortable-ghost",
      chosenClass: "sortable-chosen",
      dragClass: "sortable-drag",
      onStart: () => { state.isDragging = true; },
      onEnd: handleDrop,
    });
    // col head click toggles collapse
    colEl.querySelector(".col__head").addEventListener("click", (e) => {
      if (e.target.closest(".col__quickadd")) return;
      toggleColCollapse(col.id, list.length > 0);
    });
    colEl.querySelector(".col__quickadd").addEventListener("click", (e) => {
      e.stopPropagation();
      // If the column is collapsed — expand first, then open quickadd
      if (colEl.classList.contains("is-collapsed")) {
        toggleColCollapse(col.id, list.length > 0);
        setTimeout(() => openQuickAdd(col.id, colEl), 220);
      } else {
        openQuickAdd(col.id, colEl);
      }
    });
  }
  // density
  root.classList.toggle("is-compact", state.density === "compact");
  const groupBtn = $("#btn-group-epics");
  if (groupBtn) groupBtn.classList.toggle("is-active", state.groupEpics);
  // stats
  const filterLabel = (state.search || state.filters.size > 0)
    ? `${visible} / ${total}` : `${total}`;
  $("#stats").textContent = `${filterLabel} tasks`;
}

function cardEl(t) {
  const div = document.createElement("article");
  div.className = "card";
  if (t.priority === "high") div.classList.add("is-prio-high");
  div.dataset.taskId = t.id;
  div.innerHTML = `
    <div class="card__row">
      <span class="card__id">${t.id}</span>
      ${t.priority === "high" ? `<span class="chip chip--prio-high">HIGH</span>` : ""}
      <span class="chip chip--size-${t.size}">${t.size}</span>
      ${t.assignee ? `<span class="chip chip--assignee">${escapeHTML(t.assignee)}</span>` : ""}
    </div>
    <div class="card__title">${escapeHTML(t.title)}</div>
    ${t.external_blocker ? `<div class="card__meta"><span class="card__blocker" title="${escapeHTML(t.external_blocker)}">${escapeHTML(t.external_blocker)}</span></div>` : ""}
  `;
  div.addEventListener("click", () => {
    if (state.isDragging) return;
    openTaskModal(t.id);
  });
  return div;
}

// ----------------------------------------------------------- Epic grouping

// Root epic of a task's ancestor chain (walks parent_id through the full
// board task set), or null when the task is top-level / not under an epic.
function rootEpic(t, all) {
  let cur = t;
  let guard = 0;
  while (cur && cur.parent_id && guard++ < 20) {
    const p = all.get(cur.parent_id);
    if (!p) break;
    if (p.kind === "epic") return p;
    cur = p;
  }
  return null;
}

// Column render items when "group by epic" is on: epic group headers
// followed by their descendant cards (first-seen order), then ungrouped
// cards in board order. Epic cards themselves are ungrouped cards.
function epicGrouping(list) {
  const all = new Map();
  for (const col of state.board.columns) {
    for (const t of state.board.tasks[col.id] || []) all.set(t.id, t);
  }
  const groups = [];
  const idx = new Map();
  const ungrouped = [];
  for (const t of list) {
    if (t.kind === "epic") { ungrouped.push(t); continue; }
    const epic = rootEpic(t, all);
    if (epic) {
      if (!idx.has(epic.id)) {
        idx.set(epic.id, groups.length);
        groups.push({ epic, children: [] });
      }
      groups[idx.get(epic.id)].children.push(t);
    } else {
      ungrouped.push(t);
    }
  }
  const items = [];
  for (const g of groups) {
    items.push({ type: "head", epic: g.epic, count: g.children.length });
    for (const t of g.children) items.push({ type: "card", task: t, epicId: g.epic.id });
  }
  for (const t of ungrouped) items.push({ type: "card", task: t, epicId: null });
  return items;
}

function toggleEpicGrouping() {
  state.groupEpics = !state.groupEpics;
  localStorage.setItem(LS.GROUP_EPICS, state.groupEpics ? "1" : "0");
  loadBoard();
}

// ----------------------------------------------------------- Filters

function matchesFilters(t) {
  // search
  if (state.search) {
    const q = state.search.toLowerCase();
    const hay = `${t.id} ${t.title}`.toLowerCase();
    if (!hay.includes(q)) return false;
  }
  // filter chips
  for (const f of state.filters) {
    if (f === "prio:high" && t.priority !== "high") return false;
    if (f === "blocked" && !t.external_blocker) return false;
    if (f === "unassigned" && t.assignee) return false;
    if (f.startsWith("assignee:")) {
      const want = f.slice("assignee:".length);
      const have = t.assignee || "";
      if (want === "agent") {
        if (!(have === "claude" || have.startsWith("agent:"))) return false;
      } else if (have !== want) return false;
    }
  }
  return true;
}

function applyFilters() {
  // hide/unhide cards without a full re-render (faster)
  let visible = 0; let total = 0;
  for (const col of state.board.columns) {
    const tasks = state.board.tasks[col.id] || [];
    total += tasks.length;
    for (const t of tasks) {
      const card = $(`.card[data-task-id="${t.id}"]`);
      if (!card) continue;
      if (matchesFilters(t)) {
        card.classList.remove("is-hidden");
        visible++;
      } else {
        card.classList.add("is-hidden");
      }
    }
    // epic group headers hide when none of their cards match
    if (state.groupEpics) {
      $$(`.col[data-status="${col.id}"] .epic-group`).forEach(head => {
        const any = Array.from(head.parentElement.querySelectorAll(
          `.card[data-epic-group="${head.dataset.epicId}"]`))
          .some(c => !c.classList.contains("is-hidden"));
        head.classList.toggle("is-hidden", !any);
      });
    }
  }
  const filterLabel = (state.search || state.filters.size > 0)
    ? `${visible} / ${total}` : `${total}`;
  $("#stats").textContent = `${filterLabel} tasks`;
}

// ----------------------------------------------------------- Collapsed columns
//
// "Effectively collapsed" logic:
//   - manually collapsed (in collapsedCols) → ALWAYS collapsed
//   - manually expanded (in expandedCols)   → ALWAYS expanded
//   - default: empty column → collapsed, non-empty → expanded
//
// This auto-collapses empty columns but still respects an explicit user choice
// (click on the header).

function isColEffectivelyCollapsed(colId, hasTasks) {
  const proj = state.projectId;
  const manualC = (state.collapsedCols[proj] || []).includes(colId);
  if (manualC) return true;
  const manualE = (state.expandedCols[proj] || []).includes(colId);
  if (manualE) return false;
  return !hasTasks;
}

function toggleColCollapse(colId, hasTasks) {
  const proj = state.projectId;
  const collapsedList = state.collapsedCols[proj] || [];
  const expandedList = state.expandedCols[proj] || [];
  const wasCollapsed = isColEffectivelyCollapsed(colId, hasTasks);

  // remove colId from both lists (for cleanliness)
  const cIdx = collapsedList.indexOf(colId);
  if (cIdx >= 0) collapsedList.splice(cIdx, 1);
  const eIdx = expandedList.indexOf(colId);
  if (eIdx >= 0) expandedList.splice(eIdx, 1);

  // add to the opposite list
  if (wasCollapsed) expandedList.push(colId);
  else              collapsedList.push(colId);

  state.collapsedCols[proj] = collapsedList;
  state.expandedCols[proj]  = expandedList;
  saveCollapsed();

  const colEl = $(`.col[data-status="${colId}"]`);
  if (colEl) colEl.classList.toggle("is-collapsed");
}

// ----------------------------------------------------------- Quick-add

function openQuickAdd(status, colEl) {
  // If a quickadd-form already exists — focus it
  let form = colEl.querySelector(".col__quickadd-form");
  if (form) { form.querySelector("input").focus(); return; }
  form = document.createElement("div");
  form.className = "col__quickadd-form";
  form.innerHTML = `<input type="text" placeholder="Title (Enter — create, Esc — cancel)" />`;
  const list = colEl.querySelector(".col__list");
  colEl.insertBefore(form, list);
  const input = form.querySelector("input");
  input.focus();
  input.addEventListener("keydown", async (e) => {
    if (e.key === "Enter") {
      const title = input.value.trim();
      if (!title) return;
      try {
        await api("POST", "/api/tasks", {
          title,
          status,
          priority: "normal",
          size: "M",
          project_id: state.projectId,
        });
        toast("Created");
        form.remove();
        await loadBoard();
        await loadProjects();
      } catch (err) {
        toast(`Error: ${err.message}`, "err");
      }
    } else if (e.key === "Escape") {
      form.remove();
    }
  });
  input.addEventListener("blur", () => {
    if (!input.value.trim()) form.remove();
  });
}

// ----------------------------------------------------------- Drag-drop

async function handleDrop(evt) {
  setTimeout(() => { state.isDragging = false; }, 80);
  const taskId = evt.item.dataset.taskId;
  const toStatus = evt.to.dataset.status;
  // newDraggableIndex is among non-hidden; we pass newIndex relative to all elements
  const newOrder = evt.newDraggableIndex;
  try {
    await api("POST", `/api/tasks/${taskId}/move`, {
      to_status: toStatus,
      column_order: newOrder,
    });
    toast(`${taskId} → ${STATUS_TITLE[toStatus] || toStatus}`);
    await loadBoard();
    await loadProjects();
  } catch (e) {
    toast(`Error: ${e.message}`, "err");
    await loadBoard();
  }
}

// ----------------------------------------------------------- Task modal
// Holds the active EventSource for agent log streaming, if any.
let agentLogSource = null;

async function openTaskModal(taskId) {
  let t;
  try {
    t = await api("GET", `/api/tasks/${taskId}`);
  } catch (e) {
    toast(`Failed to load ${taskId}`, "err");
    return;
  }
  $("#modal").dataset.taskId = t.id;
  $("#m-id").textContent = t.id;
  $("#m-status").textContent = STATUS_TITLE[t.status] || t.status;
  renderBreadcrumb(t);
  // epic/story can spawn children (story under epic, task under story)
  const childBtn = $("#btn-create-child");
  childBtn.hidden = !(t.kind === "epic" || t.kind === "story");
  $("#modal").dataset.kind = t.kind || "task";
  $("#m-title").value = t.title || "";
  $("#m-priority").value = t.priority || "normal";
  $("#m-size").value = t.size || "M";
  $("#m-assignee").value = t.assignee || "";
  $("#m-description").value = t.description || "";
  $("#m-acceptance").value = t.acceptance || "";
  $("#m-blocker").value = t.external_blocker || "";
  renderLinks(t.links);
  renderHistory(t.history);
  $("#m-comment").value = "";

  // Agent log streaming: only for live statuses
  const agentEl = $("#m-agent-output");
  const logEl = $("#m-agent-logs");
  const dotEl = $("#m-agent-dot");
  // Close any previous stream
  if (agentLogSource) { agentLogSource.close(); agentLogSource = null; }
  if (t.status === "in_progress" || t.status === "testing" || t.status === "analyst") {
    agentEl.hidden = false;
    agentEl.open = true;
    logEl.innerHTML = "";
    dotEl.className = "agent-dot agent-dot--live";
    agentLogSource = new EventSource(`/api/tasks/${t.id}/log/stream`);
    agentLogSource.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data);
        if (data.done) {
          dotEl.className = "agent-dot agent-dot--done";
          agentLogSource.close();
          agentLogSource = null;
          return;
        }
        if (data.lines) {
          // Append new lines, remove the placeholder
          const placeholder = logEl.querySelector(".muted");
          if (placeholder) placeholder.remove();
          logEl.appendChild(document.createTextNode(data.lines));
          logEl.scrollTop = logEl.scrollHeight;
        }
      } catch (_) {}
    };
    agentLogSource.onerror = () => {
      dotEl.className = "agent-dot agent-dot--error";
      agentLogSource.close();
      agentLogSource = null;
    };
  } else {
    agentEl.hidden = true;
    dotEl.className = "agent-dot";
  }

  $("#modal").hidden = false;
}

function renderBreadcrumb(t) {
  const el = $("#m-breadcrumb");
  const chain = [...(t.ancestors || []), { id: t.id, title: t.title, kind: t.kind || "task" }];
  if (chain.length < 2) {
    el.hidden = true;
    el.innerHTML = "";
    return;
  }
  el.hidden = false;
  el.innerHTML = "";
  chain.forEach((a, i) => {
    if (i > 0) el.appendChild(document.createTextNode("›"));
    const span = document.createElement("span");
    span.className = "breadcrumb__item";
    if (i < chain.length - 1) span.classList.add("is-link");
    span.title = a.title || "";
    span.innerHTML = `<span class="breadcrumb__kind">${escapeHTML((a.kind || "task").toUpperCase())}</span> ${escapeHTML(a.id)}`;
    if (i < chain.length - 1) {
      span.addEventListener("click", () => openTaskModal(a.id));
    }
    el.appendChild(span);
  });
}

// "＋ Child" in the task modal: open the new-task modal pre-pinned to the
// current epic/story (kind derived: epic → story, story → task).
function createChild() {
  const taskId = $("#modal").dataset.taskId;
  const kind = $("#modal").dataset.kind;
  if (!taskId || !(kind === "epic" || kind === "story")) return;
  openNewModal({ parentId: taskId, kind: kind === "epic" ? "story" : "task" });
}

function renderLinks(links) {
  const el = $("#m-links");
  el.innerHTML = "";
  for (const l of (links || [])) {
    const a = document.createElement("a");
    if (l.type === "url" || l.type === "pr") a.href = l.value;
    a.target = "_blank";
    a.rel = "noopener";
    a.innerHTML = `<span class="link-type">${l.type}</span>${escapeHTML(l.value)}`;
    el.appendChild(a);
  }
}

function renderHistory(history) {
  const el = $("#m-history");
  el.innerHTML = "";
  for (const h of (history || [])) {
    const row = document.createElement("div");
    row.className = "h-row";
    const ts = h.ts.slice(5, 16).replace("T", " ");
    const text = h.action === "move"
      ? `${h.from_status || "—"} → ${h.to_status || "—"}` + (h.comment ? ` — ${h.comment}` : "")
      : (h.comment || "");
    row.innerHTML = `
      <span class="h-ts">${ts}</span>
      <span class="h-actor">${escapeHTML(h.actor)}</span>
      <span class="h-act">${h.action}</span>
      <span class="h-text">${escapeHTML(text)}</span>
    `;
    el.appendChild(row);
  }
  if (!history || !history.length) {
    el.innerHTML = `<span class="h-text" style="color: var(--muted)">empty</span>`;
  }
}

function closeModal() {
  $("#modal").hidden = true;
  delete $("#modal").dataset.taskId;
  if (agentLogSource) { agentLogSource.close(); agentLogSource = null; }
}

async function saveModal() {
  const taskId = $("#modal").dataset.taskId;
  if (!taskId) return;
  const payload = {
    title: $("#m-title").value.trim(),
    description: $("#m-description").value,
    acceptance: $("#m-acceptance").value,
    priority: $("#m-priority").value,
    size: $("#m-size").value,
    external_blocker: $("#m-blocker").value.trim() || null,
    assignee: $("#m-assignee").value.trim() || null,
  };
  try {
    await api("PATCH", `/api/tasks/${taskId}`, payload);
    toast(`${taskId} saved`);
    closeModal();
    await loadBoard();
  } catch (e) {
    toast(`Error: ${e.message}`, "err");
  }
}

async function addLink() {
  const taskId = $("#modal").dataset.taskId;
  if (!taskId) return;
  const type = $("#m-link-type").value;
  const value = $("#m-link-value").value.trim();
  if (!value) return;
  try {
    await api("POST", `/api/tasks/${taskId}/links`, { type, value });
    $("#m-link-value").value = "";
    const t = await api("GET", `/api/tasks/${taskId}`);
    renderLinks(t.links);
  } catch (e) {
    toast(`Error: ${e.message}`, "err");
  }
}

async function addComment() {
  const taskId = $("#modal").dataset.taskId;
  if (!taskId) return;
  const text = $("#m-comment").value.trim();
  if (!text) return;
  try {
    await api("POST", `/api/tasks/${taskId}/comment`, { text });
    $("#m-comment").value = "";
    const t = await api("GET", `/api/tasks/${taskId}`);
    renderHistory(t.history);
  } catch (e) {
    toast(`Error: ${e.message}`, "err");
  }
}

// Populate the parent picker for the given kind. story → epics only;
// task → epics + stories; epic → hidden (epics are top-level).
function populateParentSelect(kind, selectedId = "") {
  const sel = $("#n-parent");
  sel.innerHTML = "";
  const want = kind === "story" ? ["epic"] : kind === "task" ? ["epic", "story"] : [];
  const candidates = [];
  for (const col of state.board.columns) {
    for (const t of state.board.tasks[col.id] || []) {
      if (want.includes(t.kind)) candidates.push(t);
    }
  }
  if (candidates.length === 0) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = kind === "story" ? "No epics yet — create one first" : "No epic/story yet — create one first";
    sel.appendChild(opt);
    return;
  }
  const ph = document.createElement("option");
  ph.value = "";
  ph.textContent = kind === "story" ? "— Epic —" : "— Epic or story —";
  sel.appendChild(ph);
  for (const t of candidates) {
    const opt = document.createElement("option");
    opt.value = t.id;
    opt.textContent = `${t.id} — ${t.title}`;
    if (t.id === selectedId) opt.selected = true;
    sel.appendChild(opt);
  }
}

// opts.parentId — pre-pinned parent (create-child from the task modal):
// kind is locked to the derived child kind and the parent picker is hidden.
function openNewModal(opts = {}) {
  const parentId = opts.parentId || "";
  const kind = opts.kind || "task";
  $("#n-title").value = "";
  $("#n-description").value = "";
  $("#n-acceptance").value = "";
  $("#n-priority").value = "normal";
  $("#n-size").value = "M";
  $("#n-status").value = "backlog";
  $("#n-proj-chip").textContent = state.project ? state.project.name : state.projectId;
  $("#n-kind").value = kind;
  $("#n-kind").disabled = Boolean(parentId);
  populateParentSelect(kind, parentId);
  $("#n-parent-field").hidden = kind === "epic" || Boolean(parentId);
  $("#new-modal").dataset.parentId = parentId;
  $("#new-modal").hidden = false;
  $("#n-title").focus();
}

function closeNewModal() {
  $("#new-modal").hidden = true;
  delete $("#new-modal").dataset.parentId;
  $("#n-kind").disabled = false;
  $("#n-parent-field").hidden = false;
}

async function createTask() {
  const title = $("#n-title").value.trim();
  if (!title) {
    toast("Title is required", "err");
    return;
  }
  const payload = {
    title,
    description: $("#n-description").value,
    acceptance: $("#n-acceptance").value,
    priority: $("#n-priority").value,
    size: $("#n-size").value,
    status: $("#n-status").value,
    project_id: state.projectId,
    kind: $("#n-kind").value,
    parent_id: $("#new-modal").dataset.parentId || $("#n-parent").value || null,
  };
  try {
    const t = await api("POST", `/api/tasks`, payload);
    toast(`${t.id} created`);
    closeNewModal();
    await loadBoard();
    await loadProjects();
  } catch (e) {
    toast(`Error: ${e.message}`, "err");
  }
}

// ----------------------------------------------------------- Project modal (create + edit)

// editingProjId — null when creating a new project, otherwise id of the project being edited.
let editingProjId = null;
// activeSourceTab — which task-source tab is selected in the wizard ('new'|'local'|'git').
let activeSourceTab = "new";
// selectedPlanFiles — Set of plan-file paths selected in the current wizard session.
let selectedPlanFiles = new Set();

function resetSourceWizard() {
  activeSourceTab = "new";
  $$(".source-tab").forEach(t =>
    t.classList.toggle("is-active", t.dataset.source === "new")
  );
  $$(".source-pane").forEach(p =>
    p.hidden = p.dataset.pane !== "new"
  );
  selectedPlanFiles = new Set();
  $("#src-git-url").value = "";
  $("#src-git-token").value = "";
  $("#source-current").hidden = true;
  $("#source-current-text").textContent = "—";
}

function selectSourceTab(name) {
  activeSourceTab = name;
  $$(".source-tab").forEach(t =>
    t.classList.toggle("is-active", t.dataset.source === name)
  );
  $$(".source-pane").forEach(p =>
    p.hidden = p.dataset.pane !== name
  );
  if (name === "local") loadPlanCandidates();
}

// ----- Plan candidates list -----

function _humanSize(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function _humanTime(ts) {
  const now = Date.now() / 1000;
  const diff = now - ts;
  if (diff < 60)        return "just now";
  if (diff < 3600)      return `${Math.floor(diff / 60)} min ago`;
  if (diff < 86400)     return `${Math.floor(diff / 3600)} h ago`;
  if (diff < 86400 * 7) return `${Math.floor(diff / 86400)} d ago`;
  return new Date(ts * 1000).toISOString().slice(0, 10);
}

async function loadPlanCandidates() {
  const box = $("#src-candidates");
  const path = $("#p-path").value.trim();
  if (!path) {
    box.innerHTML = `
      <div class="src-candidates__empty">
        First specify the project directory.<br>
        <button type="button" class="btn btn--ghost" data-action="pick-folder" style="margin-top:8px;">Specify directory…</button>
      </div>`;
    return;
  }
  box.innerHTML = '<div class="src-candidates__empty">Scanning…</div>';
  try {
    // Always use path from input — in edit mode the user may have just
    // picked a new path via the picker, and it isn't saved to the DB yet.
    const r = await api("GET", `/api/system/list-md-files?path=${encodeURIComponent(path)}`);
    if (!r.items || r.items.length === 0) {
      box.innerHTML = `<div class="src-candidates__empty">No .md files found in the directory. Use "Choose another folder…" below.</div>`;
      $("#src-local-file").value = "";
      return;
    }
    renderCandidates(r.items);
  } catch (e) {
    box.innerHTML = `<div class="src-candidates__empty">Error: ${escapeHTML(e.message)}</div>`;
  }
}

function renderCandidates(items) {
  const box = $("#src-candidates");
  box.innerHTML = "";
  // Auto-select all plan-files (prio ≤ 5: PLAN/BACKLOG/TASKS/TODO/ROADMAP).
  // CLAUDE.md/README.md/etc. are prio 7+, left unchecked.
  selectedPlanFiles = new Set(
    items.filter(it => it.prio <= 5).map(it => it.file)
  );
  // If none qualify (all low-priority) — check the first so the wizard isn't empty.
  if (selectedPlanFiles.size === 0 && items.length > 0) {
    selectedPlanFiles.add(items[0].file);
  }
  for (const it of items) {
    box.appendChild(_candidateEl(it));
  }
  _updateCandidatesCounter();
}

function _candidateEl(it) {
  const sel = selectedPlanFiles.has(it.file);
  const el = document.createElement("div");
  el.className = "src-candidate";
  if (sel) el.classList.add("is-selected");
  el.dataset.file = it.file;
  el.innerHTML = `
    <span class="src-candidate__check">${sel ? "☑" : "☐"}</span>
    <span class="src-candidate__name">${escapeHTML(it.file)}</span>
    <span class="src-candidate__meta">${_humanSize(it.size)} · ${_humanTime(it.modified)}</span>
  `;
  el.addEventListener("click", () => toggleCandidate(it.file));
  return el;
}

function toggleCandidate(file) {
  if (selectedPlanFiles.has(file)) selectedPlanFiles.delete(file);
  else selectedPlanFiles.add(file);
  const el = $(`.src-candidate[data-file="${CSS.escape(file)}"]`);
  if (el) {
    const sel = selectedPlanFiles.has(file);
    el.classList.toggle("is-selected", sel);
    const check = el.querySelector(".src-candidate__check");
    if (check) check.textContent = sel ? "☑" : "☐";
  }
  _updateCandidatesCounter();
}

function _updateCandidatesCounter() {
  const counter = $("#src-candidates-counter");
  if (counter) counter.textContent = `Selected: ${selectedPlanFiles.size}`;
}


async function showCurrentSource(projectId) {
  // Fetch the project's current source (if any). 404 = none.
  try {
    const src = await api("GET", `/api/projects/${projectId}/source`);
    let label;
    if (src.type === "plan_md") {
      const files = src.config.files || (src.config.file ? [src.config.file] : []);
      label = files.length > 0
        ? `Plan files (${files.length}): ${files.join(", ")}`
        : `Plan: ${JSON.stringify(src.config)}`;
    } else if (src.type === "git") {
      label = `Git: ${src.config.repo_url}`;
    } else {
      label = `${src.type}: ${JSON.stringify(src.config)}`;
    }
    if (src.last_sync_at) label += ` · sync: ${src.last_sync_at.slice(5,16).replace("T"," ")}`;
    $("#source-current").hidden = false;
    $("#source-current-text").textContent = label;
  } catch (e) {
    $("#source-current").hidden = true;
  }
}

function openNewProj() {
  editingProjId = null;
  $("#proj-modal-title").textContent = "New project";
  $("#btn-create-proj").textContent = "Create";
  $("#p-name").value = "";
  $("#p-id").value = "";
  $("#p-id").disabled = false;
  $("#p-path").value = "";
  $("#p-model").value = "";
  $$(".swatch").forEach(s => s.classList.toggle("is-active", s.dataset.color === "#F10D30"));
  resetSourceWizard();
  $("#proj-modal").hidden = false;
  $("#p-name").focus();
}

function openEditProj(p) {
  editingProjId = p.id;
  $("#proj-modal-title").textContent = `Project: ${p.name}`;
  $("#btn-create-proj").textContent = "Save";
  $("#p-name").value = p.name;
  $("#p-id").value = p.id;
  $("#p-id").disabled = true;     // id is immutable (FK on tasks)
  $("#p-path").value = p.path || "";
  $("#p-model").value = p.model || "";
  $$(".swatch").forEach(s => s.classList.toggle("is-active", s.dataset.color === p.color));
  resetSourceWizard();
  showCurrentSource(p.id);
  $("#proj-modal").hidden = false;
  $("#p-name").focus();
}

function closeNewProj() {
  $("#proj-modal").hidden = true;
  editingProjId = null;
}

async function saveProj() {
  const name = $("#p-name").value.trim();
  const id = $("#p-id").value.trim();
  const path = $("#p-path").value.trim();
  const model = $("#p-model").value.trim();
  const colorEl = $(".swatch.is-active");
  const color = colorEl ? colorEl.dataset.color : "#F10D30";
  // The icon is auto-generated from the first letter of the name; the
  // backend does the same when icon is empty (see Store.create_project).
  const icon = "";
  if (!name) { toast("Name is required", "err"); return; }

  // Pre-validation: types new/local require a path. Otherwise the wizard does
  // nothing and the user ends up with an empty board (as happened with Aizav2).
  if ((activeSourceTab === "new" || activeSourceTab === "local") && !path) {
    toast("First specify the project directory (field \"Project directory\")", "err");
    $("#p-path").focus();
    $("#p-path").classList.add("field--error");
    setTimeout(() => $("#p-path").classList.remove("field--error"), 2000);
    return;
  }
  if (activeSourceTab === "local") {
    if (selectedPlanFiles.size === 0) {
      toast("Tick at least one plan file", "err");
      return;
    }
  }
  if (activeSourceTab === "git") {
    const url = $("#src-git-url").value.trim();
    if (!url) {
      toast("Enter the repository URL (or switch to another source)", "err");
      $("#src-git-url").focus();
      return;
    }
  }

  // Save base project (create or edit)
  let savedId = editingProjId;
  if (editingProjId) {
    try {
      await api("PATCH", `/api/projects/${editingProjId}`, { name, color, icon, path, model });
    } catch (e) {
      toast(`Error: ${e.message}`, "err");
      return;
    }
  } else {
    if (!id) { toast("ID is required", "err"); return; }
    if (!/^[a-z][a-z0-9-]{1,31}$/.test(id)) {
      toast("ID: latin lowercase, 2-32 characters", "err");
      return;
    }
    try {
      await api("POST", "/api/projects", { id, name, color, icon, path, model });
      savedId = id;
    } catch (e) {
      toast(`Error: ${e.message}`, "err");
      return;
    }
  }

  // Setup source
  const sourceResult = await applySourceWizard(savedId, path);

  toast(editingProjId ? `Project ${name} saved${sourceResult ? " · " + sourceResult : ""}` : `Project ${name} created${sourceResult ? " · " + sourceResult : ""}`);
  closeNewProj();
  await loadProjects();
  if (!editingProjId) navigateTo(savedId);
  await loadBoard();
  renderSidebar();
}

// Apply the source-wizard outcome for `projectId`. Returns a short
// description for the toast, or null if nothing was done.
async function applySourceWizard(projectId, path) {
  const tab = activeSourceTab;
  // Types new/local need a path. Git does not.
  if ((tab === "new" || tab === "local") && !path) {
    return null;          // user did not specify a directory — skip setup
  }
  try {
    if (tab === "new") {
      const r = await api("POST", `/api/projects/${projectId}/source/plan-new`);
      return `created ${r.plan_md.split("/").pop()}`;
    }
    if (tab === "local") {
      const files = Array.from(selectedPlanFiles);
      if (files.length === 0) return null;
      const r = await api("POST", `/api/projects/${projectId}/source/plan-local`, { files });
      const c = r.imported || {};
      return `imported from ${files.length} file(s): ${c.created || 0} created, ${c.skipped || 0} skipped`;
    }
    if (tab === "git") {
      const repo_url = $("#src-git-url").value.trim();
      const token = $("#src-git-token").value;
      if (!repo_url) return null;
      await api("POST", `/api/projects/${projectId}/source/git`, { repo_url, token });
      return token ? "git connected · token saved" : "git connected";
    }
  } catch (e) {
    toast(`Task source: ${e.message}`, "err");
  }
  return null;
}

// Open the native folder picker (macOS Finder / Linux zenity / Windows FBD).
async function pickFolder() {
  const btn = $("#btn-pick-folder");
  if (btn) {
    btn.disabled = true;
    btn.dataset.oldText = btn.textContent;
    btn.textContent = "Opening…";
  }
  try {
    const r = await api("POST", "/api/system/pick-folder");
    if (r.cancelled) return;
    if (r.path) {
      $("#p-path").value = r.path;
      $("#p-path").classList.remove("field--error");
      // If local is active — reload candidates with the new path.
      if (activeSourceTab === "local") loadPlanCandidates();
    }
  } catch (e) {
    if (String(e.message).includes("HTTP 501")) {
      toast("Native picker not available — enter the path manually", "err");
    } else {
      toast(`Picker: ${e.message}`, "err");
    }
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = btn.dataset.oldText || "Choose…";
    }
  }
}

// Auto-fill id from name (only in create mode)
function autoFillProjId() {
  if (editingProjId) return;
  const idInput = $("#p-id");
  if (idInput.value) return;
  const slug = $("#p-name").value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 32);
  if (slug && /^[a-z]/.test(slug)) idInput.value = slug;
}

// ----------------------------------------------------------- Snapshot

async function snapshot() {
  try {
    const r = await api("POST", `/api/snapshot`);
    toast(`Snapshot → ${r.path.split("/").pop()}`);
  } catch (e) {
    toast(`Error: ${e.message}`, "err");
  }
}

// ----------------------------------------------------------- Density

function toggleDensity() {
  state.density = state.density === "compact" ? "comfortable" : "compact";
  localStorage.setItem(LS.DENSITY, state.density);
  $("#board").classList.toggle("is-compact", state.density === "compact");
}

// ----------------------------------------------------------- Sidebar toggle

function toggleSidebar() {
  state.sidebarCollapsed = !state.sidebarCollapsed;
  localStorage.setItem(LS.SIDEBAR, state.sidebarCollapsed ? "collapsed" : "open");
  $("#sidebar").classList.toggle("is-collapsed", state.sidebarCollapsed);
}

// ----------------------------------------------------------- Theme

function toggleTheme() {
  state.theme = state.theme === "light" ? "dark" : "light";
  localStorage.setItem(LS.THEME, state.theme);
  document.documentElement.setAttribute("data-theme", state.theme);
}

// ----------------------------------------------------------- Claude auth indicator

async function refreshClaudeAuth() {
  const btn = $("#btn-claude-auth");
  if (!btn) return;
  try {
    const r = await api("GET", "/api/system/claude-auth-status");
    btn.classList.remove("claude-auth--ok", "claude-auth--no", "claude-auth--unknown", "is-busy");
    if (!r.available) {
      btn.classList.add("claude-auth--no");
      btn.title = "Claude CLI not found — install Claude Code";
    } else if (r.loggedIn) {
      btn.classList.add("claude-auth--ok");
      btn.title = `Claude CLI: signed in (${r.authMethod || "ok"})`;
    } else {
      btn.classList.add("claude-auth--no");
      btn.title = "Claude CLI: NOT signed in — click to log in";
    }
  } catch (e) {
    btn.classList.remove("claude-auth--ok", "claude-auth--unknown");
    btn.classList.add("claude-auth--no");
    btn.title = `Status check failed: ${e.message}`;
  }
}

async function clickClaudeAuth() {
  const btn = $("#btn-claude-auth");
  if (!btn) return;
  // If already signed in — click does nothing (just a status toast).
  if (btn.classList.contains("claude-auth--ok")) {
    toast("Claude CLI is signed in");
    return;
  }
  btn.classList.add("is-busy");
  try {
    const r = await api("POST", "/api/system/claude-auth-login");
    toast(`Browser login launched (pid ${r.pid}). Finish OAuth and wait ~10 s.`);
    // Re-poll after 10/30/60 s.
    [10, 25, 60].forEach(sec => setTimeout(refreshClaudeAuth, sec * 1000));
  } catch (e) {
    toast(`Auth login: ${e.message}`, "err");
    btn.classList.remove("is-busy");
  }
}

// ----------------------------------------------------------- Profile

function toggleProfile() {
  const cur = document.documentElement.getAttribute("data-profile") || "standard";
  const idx = PROFILES.indexOf(cur);
  const next = PROFILES[(idx + 1) % PROFILES.length];
  document.documentElement.setAttribute("data-profile", next);
  localStorage.setItem(LS.PROFILE, next);
  toast(`Profile: ${PROFILE_LABELS[next]}`);
}

// ----------------------------------------------------------- Loaders

async function loadProjects() {
  try {
    const r = await api("GET", "/api/projects?include_archived=true");
    state.projects = r.projects;
    renderSidebar();
  } catch (e) {
    toast(`Failed to load projects: ${e.message}`, "err");
  }
}

async function loadBoard() {
  if (!state.projectId) return;
  if (state.isDragging) return;
  if ($("#modal").hidden === false) return;
  if ($("#new-modal").hidden === false) return;
  if ($("#proj-modal").hidden === false) return;
  try {
    const board = await api("GET", `/api/board?project=${encodeURIComponent(state.projectId)}`);
    render(board);
  } catch (e) {
    if (String(e.message).includes("HTTP 404")) {
      // Project not found — fall back to the first available
      const fallback = state.projects.find(p => !p.archived);
      if (fallback && fallback.id !== state.projectId) {
        navigateTo(fallback.id, { replace: true });
        await loadBoard();
      } else {
        toast("No projects available", "err");
      }
    } else {
      toast(`Network: ${e.message}`, "err");
    }
  }
}

// ----------------------------------------------------------- Search

let searchDebounce = null;
function onSearchInput(e) {
  const v = e.target.value;
  $("#btn-search-clear").hidden = !v;
  clearTimeout(searchDebounce);
  searchDebounce = setTimeout(() => {
    state.search = v.trim();
    applyFilters();
  }, 120);
}

function clearSearch() {
  $("#search").value = "";
  $("#btn-search-clear").hidden = true;
  state.search = "";
  applyFilters();
}

// ----------------------------------------------------------- Filter chips

function toggleFilter(filter) {
  if (state.filters.has(filter)) state.filters.delete(filter);
  else state.filters.add(filter);
  $$(`#filters .filter-chip`).forEach(b => {
    b.classList.toggle("is-on", state.filters.has(b.dataset.filter));
  });
  applyFilters();
}

// ----------------------------------------------------------- Middle-click pan

function initMiddleClickPan() {
  const board = $("#board");
  if (!board) return;
  let panning = false;
  let startX = 0;
  let startScrollLeft = 0;

  board.addEventListener("mousedown", (e) => {
    if (e.button !== 1) return;     // middle button only
    e.preventDefault();             // suppress the native autoscroll cursor
    panning = true;
    startX = e.clientX;
    startScrollLeft = board.scrollLeft;
    document.body.style.cursor = "grabbing";
    board.style.cursor = "grabbing";
  });

  document.addEventListener("mousemove", (e) => {
    if (!panning) return;
    e.preventDefault();
    const dx = e.clientX - startX;
    board.scrollLeft = startScrollLeft - dx;
  });

  const stop = () => {
    if (!panning) return;
    panning = false;
    document.body.style.cursor = "";
    board.style.cursor = "";
  };
  document.addEventListener("mouseup", (e) => {
    if (e.button === 1) stop();
  });
  // Release if the window loses focus
  window.addEventListener("blur", stop);
}

// ----------------------------------------------------------- Init

document.addEventListener("DOMContentLoaded", async () => {
  state.projectId = parseRoute();

  // Modal close handlers
  $$("[data-close]").forEach((el) => el.addEventListener("click", closeModal));
  $$("[data-close-new]").forEach((el) => el.addEventListener("click", closeNewModal));
  $$("[data-close-proj]").forEach((el) => el.addEventListener("click", closeNewProj));

  // Buttons
  $("#btn-save").addEventListener("click", saveModal);
  $("#btn-add-link").addEventListener("click", addLink);
  $("#btn-add-comment").addEventListener("click", addComment);
  $("#btn-create-child").addEventListener("click", createChild);
  $("#n-kind").addEventListener("change", () => {
    const kind = $("#n-kind").value;
    populateParentSelect(kind, "");
    $("#n-parent-field").hidden = kind === "epic";
  });
  $("#btn-new").addEventListener("click", () => openNewModal());
  $("#btn-create").addEventListener("click", createTask);
  $("#btn-refresh").addEventListener("click", () => { loadBoard(); loadProjects(); });
  $("#btn-snapshot").addEventListener("click", snapshot);
  $("#btn-group-epics").addEventListener("click", toggleEpicGrouping);
  $("#btn-theme").addEventListener("click", toggleTheme);
  $("#btn-profile").addEventListener("click", toggleProfile);
  $("#btn-claude-auth").addEventListener("click", clickClaudeAuth);
  $("#btn-sidebar").addEventListener("click", toggleSidebar);
  // Initial claude auth state + periodic refresh every 60 s
  refreshClaudeAuth();
  setInterval(refreshClaudeAuth, 60000);
  $("#btn-new-proj").addEventListener("click", openNewProj);
  $("#btn-create-proj").addEventListener("click", saveProj);
  // Source-wizard tabs
  $$(".source-tab").forEach(t =>
    t.addEventListener("click", () => selectSourceTab(t.dataset.source))
  );
  // On path change — refetch candidates (if local mode is active)
  $("#p-path").addEventListener("change", () => {
    if (activeSourceTab === "local") loadPlanCandidates();
  });

  // Delegated click handler for [data-action="..."] — works for
  // dynamically inserted buttons (inline in src-candidates__empty)
  // and across pane re-renders.
  document.body.addEventListener("click", (e) => {
    const target = e.target.closest("[data-action]");
    if (!target) return;
    const action = target.dataset.action;
    console.log("[kanban] action:", action);   // diagnostic
    if (action === "pick-folder") {
      e.preventDefault();
      pickFolder();
    } else if (action === "refresh-candidates") {
      e.preventDefault();
      loadPlanCandidates();
    }
  });

  // Project name → auto-slug
  $("#p-name").addEventListener("input", autoFillProjId);

  // Color palette
  $$(".swatch").forEach(s => s.addEventListener("click", () => {
    $$(".swatch").forEach(x => x.classList.remove("is-active"));
    s.classList.add("is-active");
  }));

  // Archive section toggle
  $("#btn-archive-toggle").addEventListener("click", () => {
    const box = $("#proj-archive");
    const list = $("#proj-archive-list");
    const open = !box.classList.contains("is-open");
    box.classList.toggle("is-open", open);
    list.hidden = !open;
    localStorage.setItem(LS.ARCHIVE_OPEN, open ? "1" : "0");
  });

  // Search
  $("#search").addEventListener("input", onSearchInput);
  $("#btn-search-clear").addEventListener("click", clearSearch);

  // Filter chips
  $$("#filters .filter-chip").forEach(b => {
    b.addEventListener("click", () => toggleFilter(b.dataset.filter));
  });

  // Keyboard
  document.addEventListener("keydown", (e) => {
    // skip if typing in input/textarea
    const tag = (e.target.tagName || "").toLowerCase();
    const inField = tag === "input" || tag === "textarea" || tag === "select";
    if (e.key === "Escape") {
      closeModal();
      closeNewModal();
      closeNewProj();
      if (state.search) clearSearch();
      return;
    }
    if (inField) return;
    if (e.key === "/" || e.key === "?") {
      e.preventDefault();
      $("#search").focus();
    } else if (e.key === "n") {
      e.preventDefault();
      openNewModal();
    } else if (e.key === "d") {
      toggleDensity();
    } else if (e.key === "t") {
      toggleTheme();
    } else if (e.key === "p") {
      toggleProfile();
    } else if (e.key === "\\") {
      toggleSidebar();
    } else if (e.key === "r") {
      loadBoard();
    }
  });

  // Middle-click panning: hold the wheel on the board and drag left/right.
  // Does not conflict with SortableJS drag-drop (which reacts to left-click).
  initMiddleClickPan();

  // Browser back/forward
  window.addEventListener("popstate", () => {
    const next = parseRoute();
    if (next) {
      state.projectId = next;
      loadBoard();
      renderSidebar();
    }
  });

  // Initial load: projects first, then select + board
  await loadProjects();
  if (!state.projectId && state.projects.length > 0) {
    // URL without /p/X — priority:
    // 1) last opened from localStorage (if it still exists and isn't archived)
    // 2) first active project
    const last = localStorage.getItem(LS.LAST_PROJECT);
    const lastP = last && state.projects.find(p => p.id === last && !p.archived);
    const first = lastP || state.projects.find(p => !p.archived) || state.projects[0];
    state.projectId = first.id;
    navigateTo(state.projectId, { replace: true });
  }
  if (state.projectId) {
    await loadBoard();
    renderSidebar();
  }

  // Polling
  setInterval(() => { loadBoard(); loadProjects(); }, 10000);
});
