// Vanilla-JS SPA for the demo issue tracker. History routing, accessible names on every control.
const app = document.getElementById("app");
const STATUSES = ["open", "in_progress", "closed"];
const PRIORITIES = ["low", "medium", "high"];

// Safe element builder: text is always set through textContent, never as HTML.
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key === "className") el.className = value;
    else if (value !== undefined && value !== null && value !== false) el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

async function api(method, url, body) {
  const res = await fetch(url, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
    credentials: "same-origin",
  });
  if (res.status === 401 && url !== "/api/login") {
    renderLogin();
    throw new ApiError("Sign in required", 401);
  }
  const data = res.status === 204 ? null : await res.json();
  if (!res.ok) throw new ApiError(data?.error ?? "Request failed", res.status);
  return data;
}

function navigate(path) {
  history.pushState({}, "", path);
  route();
}

document.addEventListener("click", (event) => {
  const link = event.target.closest("a[data-link]");
  if (link && !event.metaKey && !event.ctrlKey) {
    event.preventDefault();
    navigate(link.getAttribute("href"));
  }
});
window.addEventListener("popstate", route);

function show(...nodes) {
  app.replaceChildren(...nodes);
}

function options(values, selected) {
  return values.map((value) => h("option", { value, selected: value === selected }, value));
}

function formDialog({ title, fields, submitLabel, onSubmit }) {
  const error = h("p", { className: "error", role: "alert" });
  const titleId = `dlg-${title.toLowerCase().replace(/\W+/g, "-")}`;
  const form = h("form", {},
    ...fields,
    error,
    h("div", { className: "toolbar" },
      h("button", { type: "submit", className: "primary" }, submitLabel),
      h("button", { type: "button", onclick: () => dialog.close() }, "Cancel")));
  const dialog = h("dialog", { "aria-labelledby": titleId }, h("h2", { id: titleId }, title), form);
  dialog.addEventListener("close", () => dialog.remove());
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    error.textContent = "";
    try {
      await onSubmit(new FormData(form));
      dialog.close();
    } catch (err) {
      error.textContent = err.message;
    }
  });
  document.body.append(dialog);
  dialog.showModal();
  return dialog;
}

async function renderProjects() {
  document.title = "Projects · Issue Tracker";
  const { items } = await api("GET", "/api/projects");
  const list = items.length
    ? h("ul", { className: "cards", "aria-label": "Projects" },
        items.map((project) => h("li", {},
          h("a", { href: `/projects/${project.id}`, "data-link": true }, project.name),
          h("button", {
            className: "danger",
            onclick: async () => { await api("DELETE", `/api/projects/${project.id}`); renderProjects(); },
          }, "Delete"))))
    : h("p", { className: "empty" }, "No projects yet.");
  show(
    h("h1", {}, "Projects"),
    h("div", { className: "toolbar" },
      h("button", {
        className: "primary",
        onclick: () => formDialog({
          title: "New project",
          submitLabel: "Create project",
          fields: [h("label", {}, "Name", h("input", { name: "name", autocomplete: "off" }))],
          onSubmit: async (data) => { await api("POST", "/api/projects", { name: data.get("name") }); renderProjects(); },
        }),
      }, "New project")),
    list);
}

async function renderProject(id) {
  const [project, { items }] = await Promise.all([
    api("GET", `/api/projects/${id}`),
    api("GET", `/api/projects/${id}/issues`),
  ]);
  document.title = `${project.name} · Issue Tracker`;
  const table = items.length
    ? h("table", { "aria-label": "Issues" },
        h("thead", {}, h("tr", {}, h("th", {}, "Title"), h("th", {}, "Status"), h("th", {}, "Priority"), h("th", {}, "Actions"))),
        h("tbody", {}, items.map((issue) => h("tr", {},
          h("td", {}, h("a", { href: `/issues/${issue.id}`, "data-link": true }, issue.title)),
          h("td", {}, h("span", { className: "pill" }, issue.status)),
          h("td", {}, issue.priority),
          h("td", { className: "row-actions" },
            issue.status !== "closed"
              ? h("button", { onclick: async () => { await api("POST", `/api/issues/${issue.id}/close`); renderProject(id); } }, "Close")
              : null,
            h("button", { className: "danger", onclick: async () => { await api("DELETE", `/api/issues/${issue.id}`); renderProject(id); } }, "Delete"))))))
    : h("p", { className: "empty" }, "No issues yet.");
  show(
    h("a", { href: "/", "data-link": true }, "All projects"),
    h("h1", {}, project.name),
    h("div", { className: "toolbar" },
      h("button", {
        className: "primary",
        onclick: () => formDialog({
          title: "New issue",
          submitLabel: "Create issue",
          fields: [
            h("label", {}, "Title", h("input", { name: "title", autocomplete: "off" })),
            h("label", {}, "Status", h("select", { name: "status" }, options(STATUSES, "open"))),
            h("label", {}, "Priority", h("select", { name: "priority" }, options(PRIORITIES, "medium"))),
          ],
          onSubmit: async (data) => {
            await api("POST", `/api/projects/${id}/issues`, {
              title: data.get("title"), status: data.get("status"), priority: data.get("priority"),
            });
            renderProject(id);
          },
        }),
      }, "New issue")),
    table);
}

async function renderIssue(id) {
  const issue = await api("GET", `/api/issues/${id}`);
  document.title = `${issue.title} · Issue Tracker`;
  const status = h("p", { className: "status", role: "status" });
  const error = h("p", { className: "error", role: "alert" });
  const heading = h("h1", {}, issue.title);
  const form = h("form", { "aria-label": "Edit issue" },
    h("label", {}, "Title", h("input", { name: "title", value: issue.title, autocomplete: "off" })),
    h("label", {}, "Status", h("select", { name: "status" }, options(STATUSES, issue.status))),
    h("label", {}, "Priority", h("select", { name: "priority" }, options(PRIORITIES, issue.priority))),
    error,
    h("div", { className: "toolbar" }, h("button", { type: "submit", className: "primary" }, "Save changes")));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    error.textContent = "";
    status.textContent = "";
    const data = new FormData(form);
    try {
      const saved = await api("PATCH", `/api/issues/${id}`, {
        title: data.get("title"), status: data.get("status"), priority: data.get("priority"),
      });
      heading.textContent = saved.title;
      document.title = `${saved.title} · Issue Tracker`;
      status.textContent = "Saved";
    } catch (err) {
      error.textContent = err.message;
    }
  });
  show(
    h("a", { href: `/projects/${issue.project_id}`, "data-link": true }, "Back to project"),
    heading,
    form,
    status,
    h("div", { className: "toolbar" },
      h("button", {
        className: "danger",
        onclick: async () => { await api("DELETE", `/api/issues/${id}`); navigate(`/projects/${issue.project_id}`); },
      }, "Delete issue")));
}

function renderLogin() {
  document.title = "Sign in · Issue Tracker";
  const error = h("p", { className: "error", role: "alert" });
  const form = h("form", { "aria-label": "Sign in" },
    h("label", {}, "Email", h("input", { name: "email", type: "email", autocomplete: "username" })),
    h("label", {}, "Password", h("input", { name: "password", type: "password", autocomplete: "current-password" })),
    error,
    h("button", { type: "submit", className: "primary" }, "Sign in"));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(form);
    try {
      await api("POST", "/api/login", { email: data.get("email"), password: data.get("password") });
      renderSession();
      route();
    } catch (err) {
      error.textContent = err.message;
    }
  });
  show(h("h1", {}, "Sign in"), form);
}

async function renderSession() {
  const slot = document.getElementById("session");
  const config = await api("GET", "/api/config");
  if (!config.auth) return slot.replaceChildren();
  try {
    const me = await api("GET", "/api/me");
    slot.replaceChildren(
      h("span", {}, me.email, " "),
      h("button", { onclick: async () => { await api("POST", "/api/logout"); slot.replaceChildren(); renderLogin(); } }, "Sign out"));
  } catch {
    slot.replaceChildren();
  }
}

async function route() {
  const path = location.pathname;
  let match;
  try {
    if (path === "/") return await renderProjects();
    if ((match = path.match(/^\/projects\/(\d+)$/))) return await renderProject(Number(match[1]));
    if ((match = path.match(/^\/issues\/(\d+)$/))) return await renderIssue(Number(match[1]));
    show(h("h1", {}, "Not found"), h("a", { href: "/", "data-link": true }, "All projects"));
  } catch (err) {
    if (err.status === 401) return;
    show(h("h1", {}, "Something went wrong"), h("p", { role: "alert" }, err.message));
  }
}

renderSession();
route();
