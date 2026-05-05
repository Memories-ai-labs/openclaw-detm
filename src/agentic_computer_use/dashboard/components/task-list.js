/* task-list.js — Sidebar task list component */
"use strict";

const TaskList = (() => {
  let _container = null;
  let _tasks = [];
  let _selectedId = null;
  let _onSelect = null;
  let _onDelete = null;
  let _onDownload = null;

  // task_id → {session_id, started_at, instruction} for currently-running live_ui sessions
  let _activeSessions = {};

  async function _pollActiveSessions() {
    try {
      const r = await fetch("/api/live_sessions/active");
      if (r.ok) {
        const prev = JSON.stringify(_activeSessions);
        _activeSessions = (await r.json()).sessions || {};
        if (JSON.stringify(_activeSessions) !== prev) render();
      }
    } catch (e) {}
  }
  let _activeSessionsPollTimer = setInterval(_pollActiveSessions, 2000);
  _pollActiveSessions();

  function init(container, onSelect, onDelete, onDownload) {
    _container = container;
    _onSelect = onSelect;
    _onDelete = onDelete;
    _onDownload = onDownload;
  }

  function setTasks(tasks) {
    _tasks = tasks || [];
    render();
  }

  function getSelectedId() {
    return _selectedId;
  }

  function render() {
    if (!_container) return;
    _container.innerHTML = "";

    if (_tasks.length === 0) {
      const li = document.createElement("li");
      li.style.color = "var(--text-dim)";
      li.style.padding = "16px 12px";
      li.style.fontSize = "12px";
      li.textContent = "No tasks found.";
      _container.appendChild(li);
      return;
    }

    for (const task of _tasks) {
      const li = document.createElement("li");
      if (task.task_id === _selectedId) li.classList.add("selected");

      const statusIcon = _statusIcon(task.status);

      const agentBadge = task.agent_id
        ? `<span class="badge badge-agent">${_esc(task.agent_id)}</span>`
        : "";

      const liveSessionRec = _activeSessions[task.task_id];
      const liveSessionId = liveSessionRec && liveSessionRec.session_id;

      li.innerHTML = `
        <div class="task-list-row">
          <div class="task-list-info">
            <span class="task-name">${statusIcon} ${_esc(task.name)}</span>
            <div class="task-meta">
              <span class="badge badge-${task.status}">${task.status}</span>
              ${agentBadge}
              <span>${task.progress_pct || 0}%</span>
            </div>
            <div class="progress-bar">
              <div class="progress-fill" style="width:${task.progress_pct || 0}%"></div>
            </div>
          </div>
          ${liveSessionId ? `<button class="task-live-btn" data-session-id="${_esc(liveSessionId)}" title="Connect to live session">&#9679; Live</button>` : ""}
          ${(task.status === "completed" || task.status === "cancelled") ? `<button class="task-video-btn" title="Download recording" data-task-id="${_esc(task.task_id)}">⬇</button>` : ""}
          <button class="task-bundle-btn" title="Download bundle (task JSON + journal + screenshots + recordings)" data-task-id="${_esc(task.task_id)}">📦</button>
          <button class="task-delete-btn" title="Delete task" data-task-id="${_esc(task.task_id)}">&times;</button>
        </div>
      `;

      const liveBtn = li.querySelector(".task-live-btn");
      if (liveBtn) {
        liveBtn.addEventListener("click", e => {
          e.stopPropagation();
          LiveSessionViewer.openLive(liveBtn.dataset.sessionId);
        });
      }

      li.querySelector(".task-list-info").addEventListener("click", () => {
        _selectedId = task.task_id;
        render();
        if (_onSelect) _onSelect(task.task_id);
      });

      li.querySelector(".task-delete-btn").addEventListener("click", (e) => {
        e.stopPropagation();
        if (_onDelete) _onDelete(task.task_id, task.name);
      });

      const videoBtn = li.querySelector(".task-video-btn");
      if (videoBtn) {
        videoBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          if (_onDownload) _onDownload(task.task_id);
        });
      }

      const bundleBtn = li.querySelector(".task-bundle-btn");
      if (bundleBtn) {
        bundleBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          // Direct link — /api/tasks/{id}/export streams the zip straight to disk.
          const a = document.createElement("a");
          a.href = `/api/tasks/${encodeURIComponent(task.task_id)}/export`;
          a.download = `detm-task-${task.task_id}.zip`;
          document.body.appendChild(a);
          a.click();
          document.body.removeChild(a);
        });
      }

      _container.appendChild(li);
    }
  }

  function _statusIcon(status) {
    const icons = {
      active: "▶",
      completed: "✓",
      failed: "✗",
      paused: "⏸",
      pending: "○",
      cancelled: "⊘",
    };
    return icons[status] || "•";
  }

  function _esc(str) {
    const d = document.createElement("div");
    d.textContent = str || "";
    return d.innerHTML;
  }

  return { init, setTasks, getSelectedId, render };
})();
