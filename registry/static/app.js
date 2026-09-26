// Minimal vanilla-JS helpers shared across the registry UI. No build step,
// no framework — server-rendered pages with fetch() for the interactive bits
// (policy save/create, activity polling).

async function postJSON(url, method, body) {
  const resp = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const isJson = (resp.headers.get("content-type") || "").includes("application/json");
  const data = isJson ? await resp.json() : await resp.text();
  if (!resp.ok) {
    const detail = isJson && data && data.detail ? data.detail : resp.statusText;
    throw new Error(detail);
  }
  return data;
}

function showStatus(el, message, ok) {
  el.textContent = message;
  el.className = "status-msg " + (ok ? "ok" : "error");
}

// -- Policy editor (policy_detail.html, policy_new.html) --------------------

function initPolicyForm({ mode, policyName }) {
  const form = document.getElementById("policy-form");
  const status = document.getElementById("policy-status");
  if (!form) return;

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const nameInput = document.getElementById("policy-name");
    const name = mode === "create" ? nameInput.value.trim() : policyName;
    const raw = document.getElementById("policy-json").value;

    if (mode === "create" && !name) {
      showStatus(status, "Policy name is required.", false);
      return;
    }

    let parsed;
    try {
      parsed = JSON.parse(raw);
    } catch (err) {
      showStatus(status, "Invalid JSON: " + err.message, false);
      return;
    }

    try {
      if (mode === "create") {
        await postJSON(`/policies?name=${encodeURIComponent(name)}`, "POST", parsed);
        showStatus(status, "Created. Redirecting…", true);
        setTimeout(() => (window.location.href = `/ui/policies/${encodeURIComponent(name)}`), 600);
      } else {
        await postJSON(`/policies/${encodeURIComponent(name)}`, "PUT", parsed);
        showStatus(status, "Saved.", true);
      }
    } catch (err) {
      showStatus(status, err.message, false);
    }
  });
}

function initDeletePolicyButton({ policyName }) {
  const btn = document.getElementById("delete-policy-btn");
  if (!btn) return;

  btn.addEventListener("click", async () => {
    if (!confirm(`Delete policy "${policyName}"? This cannot be undone.`)) return;
    try {
      await postJSON(`/policies/${encodeURIComponent(policyName)}`, "DELETE");
      window.location.href = "/ui/policies";
    } catch (err) {
      alert("Could not delete: " + err.message);
    }
  });
}

// -- Activity feed (activity.html) ------------------------------------------

function fmtSeconds(s) {
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
}

function renderActivity(checkouts, traces) {
  const activeBody = document.getElementById("active-sessions-body");
  const traceBody = document.getElementById("recent-traces-body");
  if (!activeBody || !traceBody) return;

  activeBody.innerHTML = checkouts.length
    ? checkouts
        .map(
          (c) => `<tr>
            <td class="mono">${c.session_id}</td>
            <td>${c.policy_name}</td>
            <td>${c.client_id || '<span class="text-dim">—</span>'}</td>
            <td><span class="badge ${c.status}">${c.status}</span></td>
            <td class="text-dim">${fmtSeconds(c.elapsed_seconds)}</td>
          </tr>`
        )
        .join("")
    : `<tr><td colspan="5" class="empty-state">No sessions in flight.</td></tr>`;

  traceBody.innerHTML = traces.length
    ? traces
        .map(
          (t) => `<tr>
            <td><a href="/ui/traces/${encodeURIComponent(t.session_id)}" class="mono">${t.session_id}</a></td>
            <td>${t.policy}</td>
            <td>${t.summary.total_calls}</td>
            <td><span class="badge success">${t.summary.successful}</span> <span class="badge blocked">${t.summary.blocked}</span> <span class="badge response_withheld">${t.summary.response_withheld}</span></td>
            <td class="text-dim">${t.ended_at}</td>
          </tr>`
        )
        .join("")
    : `<tr><td colspan="5" class="empty-state">No traces yet.</td></tr>`;
}

async function pollActivity() {
  try {
    const [checkouts, traces] = await Promise.all([
      fetch("/checkouts").then((r) => r.json()),
      fetch("/traces?limit=15").then((r) => r.json()),
    ]);
    renderActivity(checkouts, traces);
  } catch (err) {
    console.error("activity poll failed", err);
  }
}

function initActivityFeed() {
  if (!document.getElementById("active-sessions-body")) return;
  pollActivity();
  setInterval(pollActivity, 3000);
}

document.addEventListener("DOMContentLoaded", initActivityFeed);
