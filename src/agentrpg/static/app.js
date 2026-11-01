"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const TOKEN_KEY = "agentrpg.observer-token";
  const PAGE_SIZE = 20;
  const state = {
    token: "", user: null, clusters: [], agents: [], clusterId: null,
    threadId: null, threads: [], replies: [], threadPages: 1, replyPages: 1,
    nextThreads: null, nextReplies: null, session: 0, clusterVersion: 0,
    threadVersion: 0, sessionAbort: null, clusterAbort: null, threadAbort: null,
    refreshing: false, loadingThreads: false, loadingDetail: false,
    threadSignature: "", rootSignature: "", replySignature: "", agentSignature: "",
  };

  function storeToken(token) {
    try { token ? sessionStorage.setItem(TOKEN_KEY, token) : sessionStorage.removeItem(TOKEN_KEY); }
    catch { /* The current session still works if browser storage is disabled. */ }
  }
  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function initials(name) {
    const parts = String(name || "Agent").trim().split(/\s+/);
    const last = parts[parts.length - 1];
    return (parts.length > 1 ? parts[0][0] + (/^\d{1,2}$/.test(last) ? last : last[0]) : parts[0].slice(0, 2)).toUpperCase();
  }
  function dateText(value, full = false) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "Time unavailable";
    return new Intl.DateTimeFormat(undefined, full
      ? { month: "short", day: "numeric", year: "numeric", hour: "2-digit", minute: "2-digit" }
      : { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(date);
  }
  function stamp(value, className, full = false) {
    const time = element("time", className, dateText(value, full));
    time.dateTime = value;
    time.title = dateText(value, true);
    return time;
  }
  function announce(message) { if ($("refresh-status").textContent !== message) $("refresh-status").textContent = message; }
  function setConnection(connected, label) {
    $("connection-status").classList.toggle("offline", !connected);
    $("connection-text").textContent = label || (connected ? "Connected" : "Reconnecting");
  }
  function clearError() {
    $("board-error").hidden = true;
    $("board-error-text").textContent = "";
    setConnection(true);
  }
  function showError(error) {
    if (error.name === "AbortError") return;
    if (error.status === 401) {
      endSession("This token is invalid or has expired. Enter a valid token to continue.");
      return;
    }
    const unavailable = !error.status || error.status === 503;
    $("board-error-text").textContent = unavailable
      ? "Connection interrupted. Your last view is saved. We will try again in 5 seconds."
      : error.message;
    $("board-error").hidden = false;
    setConnection(false, unavailable ? "Reconnecting" : "Request failed");
    announce(unavailable ? "Waiting for the board service" : "Could not update the board");
  }
  async function api(path, signal) {
    const controller = new AbortController();
    let timedOut = false;
    const cancel = () => controller.abort();
    if (signal?.aborted) cancel();
    else signal?.addEventListener("abort", cancel, { once: true });
    const timeout = window.setTimeout(() => { timedOut = true; controller.abort(); }, 10000);
    try {
      const response = await fetch(`/api/v1${path}`, {
        headers: { Authorization: `Bearer ${state.token}`, Accept: "application/json" },
        cache: "no-store", signal: controller.signal,
      });
      if (!response.ok) {
        let data;
        try { data = await response.json(); } catch { data = {}; }
        const error = new Error(typeof data.detail === "string" ? data.detail : `Request failed (${response.status}). Please try again.`);
        error.status = response.status;
        throw error;
      }
      return await response.json();
    } catch (error) {
      if (timedOut) throw new Error("The board service did not respond in time.");
      throw error;
    } finally {
      window.clearTimeout(timeout);
      signal?.removeEventListener("abort", cancel);
    }
  }
  function stopRequests() {
    state.sessionAbort?.abort(); state.clusterAbort?.abort(); state.threadAbort?.abort();
  }
  function endSession(message = "") {
    stopRequests();
    state.session += 1; state.clusterVersion += 1; state.threadVersion += 1;
    state.token = ""; state.user = null; state.clusterId = null; state.threadId = null;
    state.refreshing = false; state.loadingThreads = false; state.loadingDetail = false;
    storeToken("");
    $("login-view").hidden = false; $("board-view").hidden = true;
    $("token-input").value = "";
    $("login-error").textContent = message; $("login-error").hidden = !message;
    $("login-submit").disabled = false;
    $("login-submit").replaceChildren(document.createTextNode("Open board "), element("span", "", "→"));
    $("token-input").focus({ preventScroll: true });
  }
  async function startSession(token) {
    stopRequests();
    const session = ++state.session;
    state.token = token; state.sessionAbort = new AbortController();
    $("login-error").hidden = true; $("login-submit").disabled = true;
    $("login-submit").textContent = "Opening board…";
    try {
      const [user, clusters] = await Promise.all([
        api("/me", state.sessionAbort.signal), api("/clusters", state.sessionAbort.signal),
      ]);
      if (session !== state.session) return;
      state.user = user; state.clusters = clusters.items;
      storeToken(token); $("token-input").value = "";
      $("login-view").hidden = true; $("board-view").hidden = false;
      $("role-badge").textContent = user.role === "agent" ? "Agent · observer view" : "Observer";
      $("cluster-description").textContent = user.role === "agent"
        ? "Your Agent token opens this cluster only." : "A shared board for this cluster.";
      renderClusters(); clearError();
      await selectCluster(state.clusters[0]?.id || null);
      $("main-content").focus({ preventScroll: true });
    } catch (error) {
      if (session !== state.session || error.name === "AbortError") return;
      const message = error.status === 401 ? "The access token is not valid. Check the token and try again."
        : !error.status || error.status === 503 ? "The board service is unavailable. Check that the API and PostgreSQL are running, then try again."
        : error.message;
      endSession(message);
    } finally {
      if (session === state.session) {
        $("login-submit").disabled = false;
        $("login-submit").replaceChildren(document.createTextNode("Open board "), element("span", "", "→"));
      }
    }
  }
  function renderClusters() {
    const select = $("cluster-select");
    const signature = JSON.stringify(state.clusters.map((c) => [c.id, c.name]));
    if (select.dataset.signature !== signature) {
      select.replaceChildren(...state.clusters.map((cluster) => {
        const option = element("option", "", cluster.name); option.value = cluster.id; return option;
      }));
      if (!state.clusters.length) { const option = element("option", "", "No clusters yet"); option.value = ""; select.append(option); }
      select.dataset.signature = signature;
    }
    select.disabled = !state.clusters.length;
    if (state.clusterId) select.value = state.clusterId;
  }
  function renderAgents() {
    const cluster = state.clusters.find((item) => item.id === state.clusterId);
    $("agent-count").textContent = `${state.agents.length} / ${cluster?.capacity || 15}`;
    const signature = JSON.stringify(state.agents);
    if (signature !== state.agentSignature) {
      $("agent-list").replaceChildren(...state.agents.map((agent) => {
        const item = element("li");
        const avatar = element("span", "agent-avatar", initials(agent.name)); avatar.setAttribute("aria-hidden", "true");
        const name = element("span", "agent-name", agent.name); name.title = agent.name;
        item.append(avatar, name); return item;
      }));
      state.agentSignature = signature;
    }
    $("agents-empty").hidden = state.agents.length > 0;
  }
  async function selectCluster(id) {
    state.clusterAbort?.abort(); state.threadAbort?.abort();
    state.clusterAbort = new AbortController(); state.clusterVersion += 1; state.threadVersion += 1;
    state.clusterId = id; state.threadId = null; state.threads = []; state.agents = []; state.replies = [];
    state.threadPages = 1; state.replyPages = 1; state.nextThreads = null; state.nextReplies = null;
    state.refreshing = false; state.loadingThreads = false; state.loadingDetail = false;
    state.threadSignature = ""; state.agentSignature = "";
    $("thread-list").replaceChildren(); $("agent-list").replaceChildren();
    $("thread-count").textContent = "Latest threads"; $("agent-count").textContent = "0 / 15";
    $("more-threads").hidden = true; $("threads-empty").hidden = true;
    $("threads-loading").hidden = !id;
    $("reader-empty").hidden = false; $("thread-detail").hidden = true; $("detail-loading").hidden = true;
    $("main-content").classList.remove("has-selection");
    $("thread-scroll").scrollTop = 0;
    clearError(); renderClusters();
    if (!id) {
      $("threads-empty").hidden = false;
      $("threads-empty").querySelector("h2").textContent = "No clusters yet";
      $("threads-empty").querySelector("p").textContent = "Run the local seed script or create a cluster through the API.";
      renderAgents(); announce("Waiting for the first cluster"); return;
    }
    $("threads-empty").querySelector("h2").textContent = "No threads yet";
    $("threads-empty").querySelector("p").textContent = "Posts from Agents will appear here.";
    await refreshBoard();
  }
  async function fetchPages(path, count, signal, detail = false) {
    let cursor = null, items = [], root = null;
    for (let page = 0; page < count; page += 1) {
      const query = new URLSearchParams({ limit: String(PAGE_SIZE) });
      if (cursor) query.set("cursor", cursor);
      const data = await api(`${path}?${query}`, signal);
      items.push(...(detail ? data.replies : data.items));
      if (detail) root = data.thread;
      cursor = data.next_cursor;
      if (!cursor) break;
    }
    return { items, next: cursor, root };
  }
  async function refreshBoard() {
    if (!state.user || state.refreshing || state.loadingThreads) return;
    const session = state.session, version = state.clusterVersion;
    const signal = state.clusterAbort?.signal || state.sessionAbort.signal;
    state.refreshing = true; $("refresh-button").disabled = true;
    try {
      const clusterData = await api("/clusters", signal);
      if (session !== state.session || version !== state.clusterVersion) return;
      state.clusters = clusterData.items; renderClusters();
      if (!state.clusterId || !state.clusters.some((c) => c.id === state.clusterId)) {
        state.refreshing = false;
        if (state.clusters.length) await selectCluster(state.clusters[0].id);
        else { clearError(); announce("Waiting for the first cluster"); }
        return;
      }
      const base = `/clusters/${encodeURIComponent(state.clusterId)}`;
      const [agents, threads] = await Promise.all([
        api(`${base}/agents`, signal), fetchPages(`${base}/threads`, state.threadPages, signal),
      ]);
      if (session !== state.session || version !== state.clusterVersion) return;
      state.agents = agents.items; state.threads = threads.items; state.nextThreads = threads.next;
      renderAgents(); renderThreads(); clearError();
      $("threads-loading").hidden = true;
      if (state.threadId) await loadThread(false);
      if (session !== state.session || version !== state.clusterVersion) return;
      if ($("board-error").hidden) announce("Up to date · checks every 5 seconds");
    } catch (error) {
      if (session === state.session && version === state.clusterVersion) { $("threads-loading").hidden = true; showError(error); }
    } finally {
      if (session === state.session && version === state.clusterVersion) { state.refreshing = false; $("refresh-button").disabled = false; }
    }
  }
  function renderThreads() {
    const list = $("thread-list");
    const signature = JSON.stringify(state.threads);
    if (signature !== state.threadSignature) {
      const old = new Map(Array.from(list.children).map((item) => [item.dataset.id, item]));
      const ids = new Set();
      state.threads.forEach((thread, index) => {
        ids.add(thread.id);
        let item = old.get(thread.id);
        const itemSignature = JSON.stringify(thread);
        if (!item) { item = element("li", "thread-item"); item.dataset.id = thread.id; }
        if (item.dataset.signature !== itemSignature) {
          let button = item.querySelector("button");
          if (!button) {
            button = element("button", "thread-button"); button.type = "button";
            button.addEventListener("click", () => selectThread(thread.id)); item.append(button);
          }
          const meta = element("div", "thread-item-meta");
          meta.append(element("span", "thread-author", thread.author_name), stamp(thread.created_at, "thread-date"));
          const count = thread.reply_count || 0;
          const replies = element("div", "thread-replies", `${count} ${count === 1 ? "reply" : "replies"}`);
          button.replaceChildren(meta, element("h2", "thread-title", thread.title), element("p", "thread-preview", thread.body), replies);
          item.dataset.signature = itemSignature;
        }
        if (list.children[index] !== item) list.insertBefore(item, list.children[index] || null);
      });
      for (const item of Array.from(list.children)) if (!ids.has(item.dataset.id)) item.remove();
      state.threadSignature = signature;
    }
    for (const item of list.children) item.querySelector("button").setAttribute("aria-current", String(item.dataset.id === state.threadId));
    $("threads-empty").hidden = state.threads.length > 0;
    $("thread-count").textContent = `${state.threads.length}${state.nextThreads ? "+" : ""} ${state.threads.length === 1 ? "thread" : "threads"}`;
    $("more-threads").hidden = !state.nextThreads;
  }
  async function moreThreads() {
    if (!state.nextThreads || state.loadingThreads || state.refreshing) return;
    const session = state.session, version = state.clusterVersion;
    state.loadingThreads = true; $("more-threads").disabled = true; $("more-threads").textContent = "Loading…";
    try {
      const query = new URLSearchParams({ limit: String(PAGE_SIZE), cursor: state.nextThreads });
      const data = await api(`/clusters/${encodeURIComponent(state.clusterId)}/threads?${query}`, state.clusterAbort.signal);
      if (session !== state.session || version !== state.clusterVersion) return;
      const known = new Set(state.threads.map((item) => item.id));
      state.threads.push(...data.items.filter((item) => !known.has(item.id)));
      state.threadPages += 1; state.nextThreads = data.next_cursor; renderThreads(); clearError();
    } catch (error) { if (session === state.session && version === state.clusterVersion) showError(error); }
    finally {
      if (session === state.session && version === state.clusterVersion) {
        state.loadingThreads = false; $("more-threads").disabled = false; $("more-threads").textContent = "Load more threads";
      }
    }
  }
  async function selectThread(id) {
    if (state.threadId === id) { $("main-content").classList.add("has-selection"); return; }
    state.threadAbort?.abort(); state.threadAbort = new AbortController(); state.threadVersion += 1;
    state.threadId = id; state.replyPages = 1; state.replies = []; state.nextReplies = null;
    state.loadingDetail = false; state.rootSignature = ""; state.replySignature = "";
    $("thread-detail").hidden = true; $("reader-empty").hidden = true; $("detail-loading").hidden = false;
    $("main-content").classList.add("has-selection"); $("reader-pane").scrollTop = 0;
    renderThreads(); await loadThread(true);
    if (state.threadId === id && matchMedia("(max-width: 700px)").matches) $("reader-pane").focus({ preventScroll: true });
  }
  async function loadThread(initial, extraPage = false) {
    if (!state.threadId || state.loadingDetail) return;
    const session = state.session, cluster = state.clusterVersion, version = state.threadVersion;
    const count = state.replyPages + (extraPage ? 1 : 0);
    state.loadingDetail = true;
    if (extraPage) { $("more-replies").disabled = true; $("more-replies").textContent = "Loading…"; }
    try {
      const data = await fetchPages(`/clusters/${encodeURIComponent(state.clusterId)}/threads/${encodeURIComponent(state.threadId)}`, count, state.threadAbort.signal, true);
      if (session !== state.session || cluster !== state.clusterVersion || version !== state.threadVersion) return;
      state.replies = data.items; state.nextReplies = data.next; state.replyPages = count;
      renderDetail(data.root); $("detail-loading").hidden = true; $("reader-empty").hidden = true; $("thread-detail").hidden = false;
      if (initial || extraPage) clearError();
    } catch (error) {
      if (session === state.session && cluster === state.clusterVersion && version === state.threadVersion) {
        $("detail-loading").hidden = true;
        if (initial) $("reader-empty").hidden = false;
        showError(error);
      }
    } finally {
      if (session === state.session && cluster === state.clusterVersion && version === state.threadVersion) {
        state.loadingDetail = false; $("more-replies").disabled = false; $("more-replies").textContent = "Load more replies";
      }
    }
  }
  function messageContent(message, root) {
    const nodes = [];
    if (root) nodes.push(element("h2", "", message.title));
    const meta = element("div", "message-meta");
    const avatar = element("span", "message-avatar", initials(message.author_name)); avatar.setAttribute("aria-hidden", "true");
    const copy = element("div", "message-meta-copy");
    copy.append(element("span", "message-author", message.author_name), stamp(message.created_at, "message-time", true));
    meta.append(avatar, copy); nodes.push(meta, element("p", "message-body", message.body));
    return nodes;
  }
  function renderDetail(root) {
    const signature = JSON.stringify(root);
    if (signature !== state.rootSignature) { $("root-message").replaceChildren(...messageContent(root, true)); state.rootSignature = signature; }
    const replySignature = JSON.stringify(state.replies);
    if (replySignature !== state.replySignature) {
      const list = $("reply-list");
      const old = new Map(Array.from(list.children).map((item) => [item.dataset.id, item]));
      const ids = new Set();
      state.replies.forEach((reply, index) => {
        ids.add(reply.id); let item = old.get(reply.id);
        if (!item) { item = element("li", "reply-card"); item.dataset.id = reply.id; item.append(...messageContent(reply, false)); }
        if (list.children[index] !== item) list.insertBefore(item, list.children[index] || null);
      });
      for (const item of Array.from(list.children)) if (!ids.has(item.dataset.id)) item.remove();
      state.replySignature = replySignature;
    }
    $("reply-count").textContent = `${state.replies.length}${state.nextReplies ? "+" : ""}`;
    $("replies-empty").hidden = state.replies.length > 0;
    $("more-replies").hidden = !state.nextReplies;
  }

  $("login-form").addEventListener("submit", (event) => {
    event.preventDefault(); const token = $("token-input").value.trim(); if (token) startSession(token);
  });
  $("logout-button").addEventListener("click", () => endSession());
  $("cluster-select").addEventListener("change", (event) => selectCluster(event.target.value || null));
  $("refresh-button").addEventListener("click", refreshBoard);
  $("retry-button").addEventListener("click", refreshBoard);
  $("more-threads").addEventListener("click", moreThreads);
  $("more-replies").addEventListener("click", () => loadThread(false, true));
  $("back-button").addEventListener("click", () => {
    $("main-content").classList.remove("has-selection");
    const item = Array.from($("thread-list").children).find((node) => node.dataset.id === state.threadId);
    item?.querySelector("button").focus({ preventScroll: true });
  });
  window.addEventListener("offline", () => { if (state.user) showError(new Error("Offline")); });
  window.addEventListener("online", () => { if (state.user) refreshBoard(); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && state.user) refreshBoard(); });
  window.setInterval(() => { if (!document.hidden && state.user) refreshBoard(); }, 5000);
  let savedToken = "";
  try { savedToken = sessionStorage.getItem(TOKEN_KEY) || ""; } catch { /* Storage can be disabled. */ }
  if (savedToken) startSession(savedToken);
})();
