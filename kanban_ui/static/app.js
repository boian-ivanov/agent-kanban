/* FinOps Kanban frontend v2.
 * Multi-project (URL /p/{slug}), drag-drop, search+filter, collapsible cols,
 * compact mode, quick-add, keyboard shortcuts, polling /api/board каждые 10 сек.
 */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const STATUS_TITLE = {
  backlog: "Бэклог",
  approved: "Согласовано",
  analyst: "Аналитика",
  in_progress: "В работе",
  testing: "Тестирование",
  uat: "Приёмка",
  done: "Закрыто",
  blocked: "Заблокировано",
  cancelled: "Отменено",
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
};

const PROFILES = ["standard", "cyberpunk", "horizon"];
const PROFILE_LABELS = {
  standard: "Стандарт",
  cyberpunk: "Киберпанк",
  horizon:   "Горизонт",
};

// ----------------------------------------------------------- State

const state = {
  projectId: null,                  // выбирается из URL или первого проекта
  project: null,                   // полные мета-данные текущего проекта
  projects: [],                    // все проекты (active + archived)
  board: { columns: [], tasks: {} },
  search: "",
  filters: new Set(),              // 'prio:high', 'blocked', 'assignee:claude', ...
  density: localStorage.getItem(LS.DENSITY) || "comfortable",
  theme: document.documentElement.getAttribute("data-theme") || "dark",
  sidebarCollapsed: localStorage.getItem(LS.SIDEBAR) === "collapsed",
  collapsedCols: _loadJSON(LS.COLLAPSED_COLS),
  expandedCols:  _loadJSON(LS.EXPANDED_COLS),
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
  return m ? m[1] : null;     // null = ещё не выбран, выберется первым проектом
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
    <button class="proj__menu" title="Редактировать" aria-label="Редактировать">⋯</button>
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
        <button class="col__quickadd" title="Быстро добавить (n)" aria-label="Добавить">+</button>
      </header>
      <div class="col__list" data-status="${col.id}"></div>
    `;
    root.appendChild(colEl);
    const listEl = colEl.querySelector(".col__list");
    for (const t of list) {
      const card = cardEl(t);
      if (!matchesFilters(t)) card.classList.add("is-hidden");
      else visible++;
      listEl.appendChild(card);
    }
    Sortable.create(listEl, {
      group: "tasks",
      animation: 140,
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
      // Если колонка свёрнута — сначала развернём, потом откроем quickadd
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
  // stats
  const filterLabel = (state.search || state.filters.size > 0)
    ? `${visible} / ${total}` : `${total}`;
  $("#stats").textContent = `${filterLabel} задач`;
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
  // hide/unhide карточки без полного re-render (быстрее)
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
  }
  const filterLabel = (state.search || state.filters.size > 0)
    ? `${visible} / ${total}` : `${total}`;
  $("#stats").textContent = `${filterLabel} задач`;
}

// ----------------------------------------------------------- Collapsed columns
//
// Логика «эффективно свёрнута»:
//   - manually collapsed (в collapsedCols) → ВСЕГДА свёрнута
//   - manually expanded (в expandedCols)   → ВСЕГДА развёрнута
//   - default: пустая колонка → свёрнута, непустая → развёрнута
//
// Это даёт автосворачивание пустых, но respect'ит явный выбор пользователя
// (клик по шапке).

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

  // удаляем colId из обоих списков (для чистоты)
  const cIdx = collapsedList.indexOf(colId);
  if (cIdx >= 0) collapsedList.splice(cIdx, 1);
  const eIdx = expandedList.indexOf(colId);
  if (eIdx >= 0) expandedList.splice(eIdx, 1);

  // добавляем в противоположный
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
  // Если уже есть quickadd-form — фокус
  let form = colEl.querySelector(".col__quickadd-form");
  if (form) { form.querySelector("input").focus(); return; }
  form = document.createElement("div");
  form.className = "col__quickadd-form";
  form.innerHTML = `<input type="text" placeholder="Заголовок (Enter — создать, Esc — отмена)" />`;
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
        toast("Создано");
        form.remove();
        await loadBoard();
        await loadProjects();
      } catch (err) {
        toast(`Ошибка: ${err.message}`, "err");
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
  // newDraggableIndex среди не-скрытых; но мы передаём newIndex относительно всех элементов
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
    toast(`Ошибка: ${e.message}`, "err");
    await loadBoard();
  }
}

// ----------------------------------------------------------- Task modal

async function openTaskModal(taskId) {
  let t;
  try {
    t = await api("GET", `/api/tasks/${taskId}`);
  } catch (e) {
    toast(`Не удалось загрузить ${taskId}`, "err");
    return;
  }
  $("#modal").dataset.taskId = t.id;
  $("#m-id").textContent = t.id;
  $("#m-status").textContent = STATUS_TITLE[t.status] || t.status;
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
  $("#modal").hidden = false;
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
    el.innerHTML = `<span class="h-text" style="color: var(--muted)">пусто</span>`;
  }
}

function closeModal() {
  $("#modal").hidden = true;
  delete $("#modal").dataset.taskId;
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
  };
  try {
    await api("PATCH", `/api/tasks/${taskId}`, payload);
    toast(`${taskId} сохранена`);
    closeModal();
    await loadBoard();
  } catch (e) {
    toast(`Ошибка: ${e.message}`, "err");
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
    toast(`Ошибка: ${e.message}`, "err");
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
    toast(`Ошибка: ${e.message}`, "err");
  }
}

// ----------------------------------------------------------- New task

function openNewModal() {
  $("#n-title").value = "";
  $("#n-description").value = "";
  $("#n-acceptance").value = "";
  $("#n-priority").value = "normal";
  $("#n-size").value = "M";
  $("#n-status").value = "backlog";
  $("#n-proj-chip").textContent = state.project ? state.project.name : state.projectId;
  $("#new-modal").hidden = false;
  $("#n-title").focus();
}

function closeNewModal() {
  $("#new-modal").hidden = true;
}

async function createTask() {
  const title = $("#n-title").value.trim();
  if (!title) {
    toast("Заголовок обязателен", "err");
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
  };
  try {
    const t = await api("POST", `/api/tasks`, payload);
    toast(`${t.id} создана`);
    closeNewModal();
    await loadBoard();
    await loadProjects();
  } catch (e) {
    toast(`Ошибка: ${e.message}`, "err");
  }
}

// ----------------------------------------------------------- Project modal (create + edit)

// editingProjId — null когда создаём новый, иначе id редактируемого проекта.
let editingProjId = null;
// activeSourceTab — какой таб источника задач выбран в wizard ('new'|'local'|'git').
let activeSourceTab = "new";
// selectedPlanFiles — Set путей выбранных plan-файлов в текущей сессии wizard'а.
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
  if (diff < 60)        return "только что";
  if (diff < 3600)      return `${Math.floor(diff / 60)} мин назад`;
  if (diff < 86400)     return `${Math.floor(diff / 3600)} ч назад`;
  if (diff < 86400 * 7) return `${Math.floor(diff / 86400)} дн назад`;
  return new Date(ts * 1000).toISOString().slice(0, 10);
}

async function loadPlanCandidates() {
  const box = $("#src-candidates");
  const path = $("#p-path").value.trim();
  if (!path) {
    box.innerHTML = `
      <div class="src-candidates__empty">
        Сначала укажи директорию проекта.<br>
        <button type="button" class="btn btn--ghost" data-action="pick-folder" style="margin-top:8px;">Указать директорию…</button>
      </div>`;
    return;
  }
  box.innerHTML = '<div class="src-candidates__empty">Сканирую…</div>';
  try {
    // Всегда используем path из input — в edit-режиме user мог только что
    // выбрать новый путь через picker, а в БД он ещё не сохранён.
    const r = await api("GET", `/api/system/list-md-files?path=${encodeURIComponent(path)}`);
    if (!r.items || r.items.length === 0) {
      box.innerHTML = `<div class="src-candidates__empty">Md-файлов в директории не нашёл. Используй «Выбрать другой файл…» ниже.</div>`;
      $("#src-local-file").value = "";
      return;
    }
    renderCandidates(r.items);
  } catch (e) {
    box.innerHTML = `<div class="src-candidates__empty">Ошибка: ${escapeHTML(e.message)}</div>`;
  }
}

function renderCandidates(items) {
  const box = $("#src-candidates");
  box.innerHTML = "";
  // Авто-выбор всех plan-файлов (prio ≤ 5: PLAN/BACKLOG/TASKS/TODO/ROADMAP).
  // CLAUDE.md/README.md/etc. — pruio 7+, не отмечены.
  selectedPlanFiles = new Set(
    items.filter(it => it.prio <= 5).map(it => it.file)
  );
  // Если ни один не подошёл (всё низкоприоритетное) — отметим первый чтоб не было пусто.
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
  if (counter) counter.textContent = `Выбрано: ${selectedPlanFiles.size}`;
}


async function showCurrentSource(projectId) {
  // Запросить текущий source проекта (если есть). 404 = нет.
  try {
    const src = await api("GET", `/api/projects/${projectId}/source`);
    let label;
    if (src.type === "plan_md") {
      const files = src.config.files || (src.config.file ? [src.config.file] : []);
      label = files.length > 0
        ? `Plan-файлы (${files.length}): ${files.join(", ")}`
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
  $("#proj-modal-title").textContent = "Новый проект";
  $("#btn-create-proj").textContent = "Создать";
  $("#p-name").value = "";
  $("#p-id").value = "";
  $("#p-id").disabled = false;
  $("#p-path").value = "";
  $$(".swatch").forEach(s => s.classList.toggle("is-active", s.dataset.color === "#F10D30"));
  resetSourceWizard();
  $("#proj-modal").hidden = false;
  $("#p-name").focus();
}

function openEditProj(p) {
  editingProjId = p.id;
  $("#proj-modal-title").textContent = `Проект: ${p.name}`;
  $("#btn-create-proj").textContent = "Сохранить";
  $("#p-name").value = p.name;
  $("#p-id").value = p.id;
  $("#p-id").disabled = true;     // id неизменяем (FK на tasks)
  $("#p-path").value = p.path || "";
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
  const colorEl = $(".swatch.is-active");
  const color = colorEl ? colorEl.dataset.color : "#F10D30";
  // Иконка автогенерируется из первой буквы имени; backend сделает то же
  // если icon пустой (см. Store.create_project).
  const icon = "";
  if (!name) { toast("Название обязательно", "err"); return; }

  // Pre-validation: для типов new/local нужен path. Иначе wizard ничего не сделает —
  // и пользователь получит пустой канбан, как было с Aizav2.
  if ((activeSourceTab === "new" || activeSourceTab === "local") && !path) {
    toast("Сначала укажи директорию проекта (поле «Директория проекта»)", "err");
    $("#p-path").focus();
    $("#p-path").classList.add("field--error");
    setTimeout(() => $("#p-path").classList.remove("field--error"), 2000);
    return;
  }
  if (activeSourceTab === "local") {
    if (selectedPlanFiles.size === 0) {
      toast("Отметь хотя бы один файл планов галочкой", "err");
      return;
    }
  }
  if (activeSourceTab === "git") {
    const url = $("#src-git-url").value.trim();
    if (!url) {
      toast("Укажи URL репозитория (или переключись на другой источник)", "err");
      $("#src-git-url").focus();
      return;
    }
  }

  // Save base project (create or edit)
  let savedId = editingProjId;
  if (editingProjId) {
    try {
      await api("PATCH", `/api/projects/${editingProjId}`, { name, color, icon, path });
    } catch (e) {
      toast(`Ошибка: ${e.message}`, "err");
      return;
    }
  } else {
    if (!id) { toast("ID обязателен", "err"); return; }
    if (!/^[a-z][a-z0-9-]{1,31}$/.test(id)) {
      toast("ID: latin lowercase, 2-32 символа", "err");
      return;
    }
    try {
      await api("POST", "/api/projects", { id, name, color, icon, path });
      savedId = id;
    } catch (e) {
      toast(`Ошибка: ${e.message}`, "err");
      return;
    }
  }

  // Setup source
  const sourceResult = await applySourceWizard(savedId, path);

  toast(editingProjId ? `Проект ${name} сохранён${sourceResult ? " · " + sourceResult : ""}` : `Проект ${name} создан${sourceResult ? " · " + sourceResult : ""}`);
  closeNewProj();
  await loadProjects();
  if (!editingProjId) navigateTo(savedId);
  await loadBoard();
  renderSidebar();
}

// Apply source-wizard outcome для проекта `projectId`. Возвращает короткое
// описание для toast или null если ничего не делали.
async function applySourceWizard(projectId, path) {
  const tab = activeSourceTab;
  // Для типов new/local нужен path. Для git — нет.
  if ((tab === "new" || tab === "local") && !path) {
    return null;          // пользователь не указал директорию — пропустим setup
  }
  try {
    if (tab === "new") {
      const r = await api("POST", `/api/projects/${projectId}/source/plan-new`);
      return `создан ${r.plan_md.split("/").pop()}`;
    }
    if (tab === "local") {
      const files = Array.from(selectedPlanFiles);
      if (files.length === 0) return null;
      const r = await api("POST", `/api/projects/${projectId}/source/plan-local`, { files });
      const c = r.imported || {};
      return `импорт из ${files.length} файл(а): создано ${c.created || 0}, пропущено ${c.skipped || 0}`;
    }
    if (tab === "git") {
      const repo_url = $("#src-git-url").value.trim();
      const token = $("#src-git-token").value;
      if (!repo_url) return null;
      await api("POST", `/api/projects/${projectId}/source/git`, { repo_url, token });
      return token ? "git подключён · токен сохранён" : "git подключён";
    }
  } catch (e) {
    toast(`Источник задач: ${e.message}`, "err");
  }
  return null;
}

// Открыть нативный picker папки (macOS Finder / Linux zenity / Windows FBD).
async function pickFolder() {
  const btn = $("#btn-pick-folder");
  if (btn) {
    btn.disabled = true;
    btn.dataset.oldText = btn.textContent;
    btn.textContent = "Открываю…";
  }
  try {
    const r = await api("POST", "/api/system/pick-folder");
    if (r.cancelled) return;
    if (r.path) {
      $("#p-path").value = r.path;
      $("#p-path").classList.remove("field--error");
      // Если активен local — перезагрузим candidates с новым path.
      if (activeSourceTab === "local") loadPlanCandidates();
    }
  } catch (e) {
    if (String(e.message).includes("HTTP 501")) {
      toast("Нативный picker недоступен — введите путь руками", "err");
    } else {
      toast(`Picker: ${e.message}`, "err");
    }
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = btn.dataset.oldText || "Выбрать…";
    }
  }
}

// Auto-fill id from name (только в режиме создания)
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
    toast(`Ошибка: ${e.message}`, "err");
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
      btn.title = "Claude CLI не найден — установите Claude Code";
    } else if (r.loggedIn) {
      btn.classList.add("claude-auth--ok");
      btn.title = `Claude CLI: залогинен (${r.authMethod || "ok"})`;
    } else {
      btn.classList.add("claude-auth--no");
      btn.title = "Claude CLI: НЕ залогинен — клик для входа";
    }
  } catch (e) {
    btn.classList.remove("claude-auth--ok", "claude-auth--unknown");
    btn.classList.add("claude-auth--no");
    btn.title = `Не удалось проверить: ${e.message}`;
  }
}

async function clickClaudeAuth() {
  const btn = $("#btn-claude-auth");
  if (!btn) return;
  // Если уже залогинен — клик ничего не делает (только тост со статусом).
  if (btn.classList.contains("claude-auth--ok")) {
    toast("Claude CLI залогинен");
    return;
  }
  btn.classList.add("is-busy");
  try {
    const r = await api("POST", "/api/system/claude-auth-login");
    toast(`Запущен браузерный логин (pid ${r.pid}). Заверши OAuth и подожди ~10 сек.`);
    // Через 10/30/60 сек повторно опрашиваем.
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
  toast(`Профиль: ${PROFILE_LABELS[next]}`);
}

// ----------------------------------------------------------- Loaders

async function loadProjects() {
  try {
    const r = await api("GET", "/api/projects?include_archived=true");
    state.projects = r.projects;
    renderSidebar();
  } catch (e) {
    toast(`Не удалось загрузить проекты: ${e.message}`, "err");
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
      // Проект не найден — переходим на первый доступный
      const fallback = state.projects.find(p => !p.archived);
      if (fallback && fallback.id !== state.projectId) {
        navigateTo(fallback.id, { replace: true });
        await loadBoard();
      } else {
        toast("Нет доступных проектов", "err");
      }
    } else {
      toast(`Сеть: ${e.message}`, "err");
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
    if (e.button !== 1) return;     // только средняя кнопка
    e.preventDefault();             // блокируем native autoscroll-cursor
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
  // Если фокус ушёл с окна — тоже отпускаем
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
  $("#btn-new").addEventListener("click", openNewModal);
  $("#btn-create").addEventListener("click", createTask);
  $("#btn-refresh").addEventListener("click", () => { loadBoard(); loadProjects(); });
  $("#btn-snapshot").addEventListener("click", snapshot);
  $("#btn-density").addEventListener("click", toggleDensity);
  $("#btn-theme").addEventListener("click", toggleTheme);
  $("#btn-profile").addEventListener("click", toggleProfile);
  $("#btn-claude-auth").addEventListener("click", clickClaudeAuth);
  $("#btn-sidebar").addEventListener("click", toggleSidebar);
  // Initial claude auth state + periodic refresh каждые 60 сек
  refreshClaudeAuth();
  setInterval(refreshClaudeAuth, 60000);
  $("#btn-new-proj").addEventListener("click", openNewProj);
  $("#btn-create-proj").addEventListener("click", saveProj);
  // Source-wizard tabs
  $$(".source-tab").forEach(t =>
    t.addEventListener("click", () => selectSourceTab(t.dataset.source))
  );
  // При смене path — перезапросить кандидатов (если активен local-режим)
  $("#p-path").addEventListener("change", () => {
    if (activeSourceTab === "local") loadPlanCandidates();
  });

  // Делегированный обработчик кликов по [data-action="..."] — работает
  // и для динамически вставленных кнопок (inline в src-candidates__empty),
  // и при перерисовках pane.
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

  // Middle-click panning: зажми колёсико на доске и таскай влево/вправо.
  // Не конфликтует с SortableJS drag-drop (тот реагирует на левую кнопку).
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

  // Initial load: сначала проекты, потом выбор + доска
  await loadProjects();
  if (!state.projectId && state.projects.length > 0) {
    // URL без /p/X — приоритет:
    // 1) последний открытый из localStorage (если ещё существует и не архивирован)
    // 2) первый активный проект
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
