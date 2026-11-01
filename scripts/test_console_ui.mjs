#!/usr/bin/env node
/** Read-only checks against local data; live write controls use intercepted fixtures. No model calls. */
import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");
const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const options = {
  "base-url": "http://127.0.0.1:8000",
  run: "7b17d4bc-0019-46b9-92c9-88fc28a1f65c",
  "capacity-run": "cb6f3da3-b828-4c59-ba10-3a79d6386f8a",
  output: resolve(root, ".local/console-tests"),
};
for (let index = 2; index < process.argv.length; index += 2) {
  const key = process.argv[index].replace(/^--/, "");
  if (!(key in options) || !process.argv[index + 1]) {
    throw new Error("Use --base-url URL --run UUID --capacity-run UUID --output DIR.");
  }
  options[key] = process.argv[index + 1];
}
const base = new URL(options["base-url"]);
assert(["127.0.0.1", "localhost", "[::1]"].includes(base.hostname), "Use a loopback service.");
const run = options.run;
assert(/^[a-f0-9-]{36}$/i.test(run), "Use a run UUID.");
assert(/^[a-f0-9-]{36}$/i.test(options["capacity-run"]), "Use a capacity-run UUID.");
const output = resolve(options.output);
await mkdir(output, { recursive: true, mode: 0o700 });
const evidence = {
  run_id: run, capacity_run_id: options["capacity-run"], base_url: base.origin, started_at: new Date().toISOString(),
  checks: [], screenshots: [], page_errors: [], writes: [], fixture_writes: [], authorization_headers: [],
};
const browser = await chromium.launch({ headless: true });
const contexts = [];
let currentCheck = "setup";
const delay = milliseconds => new Promise(done => setTimeout(done, milliseconds));
async function check(name, action) {
  currentCheck = name;
  const started = Date.now();
  await action();
  evidence.checks.push({ name, passed: true, milliseconds: Date.now() - started });
}
async function page(viewport = { width: 1440, height: 900 }, { fixture = false } = {}) {
  const context = await browser.newContext({ viewport, reducedMotion: "reduce" });
  contexts.push(context);
  // Refuse all real console mutations. Per-page deterministic fixtures override this route.
  await context.route("**/api/v1/console/**", route => ["GET", "HEAD"].includes(route.request().method()) ? route.continue() : route.abort("blockedbyclient"));
  const tab = await context.newPage();
  tab.setDefaultTimeout(10000);
  tab.on("pageerror", error => evidence.page_errors.push(error.message));
  tab.on("request", request => {
    if (new URL(request.url()).origin === base.origin && request.headers().authorization) {
      evidence.authorization_headers.push(new URL(request.url()).pathname);
    }
    if (new URL(request.url()).origin === base.origin && !["GET", "HEAD"].includes(request.method())) {
      (fixture ? evidence.fixture_writes : evidence.writes).push({ method: request.method(), path: new URL(request.url()).pathname });
    }
  });
  return tab;
}
async function openConsole(tab, { turn = 0, runId = run } = {}) {
  const target = new URL("/console", base);
  if (runId) {
    target.searchParams.set("run", runId);
    target.searchParams.set("turn", String(turn));
  }
  await tab.goto(target.href);
}
async function ready(tab, turn) {
  await tab.waitForFunction(expected => {
    const content = document.querySelector("#run-content");
    return !document.querySelector("#workspace").hidden &&
      content.getAttribute("aria-busy") === "false" &&
      document.querySelector("#turn-heading").textContent === "Turn " + String(expected + 1).padStart(2, "0") &&
      document.querySelectorAll(".dialogue-card").length > 0;
  }, turn);
  assert.equal(new URL(tab.url()).searchParams.get("turn"), String(turn));
}
async function turn(tab, value) {
  await tab.locator(`[data-turn="${value}"]`).click();
  await ready(tab, value);
}
async function api(path) {
  const response = await fetch(new URL("/api/v1/observer" + path, base), {
    signal: AbortSignal.timeout(15000),
  });
  assert.equal(response.status, 200, "Read-only observer request must succeed.");
  return response.json();
}
async function noPrivateDOM(tab, title, ownerNames) {
  const text = await tab.locator("body").textContent();
  assert(!text.includes(title), "Denied access must remove the private run title from the DOM.");
  for (const name of ownerNames) {
    assert(!text.includes(name), "Denied access must remove private participant names from the DOM.");
  }
  assert.equal(await tab.locator("#detail-content").textContent(), "");
  assert.equal(await tab.locator("#token, #login-form, #logout").count(), 0);
  assert.equal(await tab.evaluate(() => localStorage.length + sessionStorage.length), 0);
}

try {
  const overview = await api("/runs/" + run);
  const turns = (await api("/runs/" + run + "/turns")).items;
  assert(turns.length >= 4, "This check needs a saved run with at least four turns.");
  const title = overview.run.scenario.title || overview.run.scenario.name;
  const ownerNames = overview.run.scenario.owners.map(owner => owner.name);
  const first = turns[0].window;
  const windows = turns.map(item => item.window);
  const tab = await page();

  await check("direct local access and two delivered dialogue cards", async () => {
    await openConsole(tab, { turn: first });
    await ready(tab, first);
    assert.equal(await tab.locator(".dialogue-card").count(), overview.participants.length);
    assert.equal(await tab.locator("#run-name").textContent(), title);
    assert((await tab.locator(".speech-owner").count()) >= 2);
    assert((await tab.locator(".speech-agent").count()) >= 1);
    assert.equal(await tab.locator("#token, #login-form, #logout").count(), 0);
    assert.equal(await tab.evaluate(() => localStorage.length + sessionStorage.length), 0);
  });
  await check("speaker jumps scroll one conversation and mark absent replies", async () => {
    const card = tab.locator(".dialogue-card").first();
    const otherCard = tab.locator(".dialogue-card").nth(1);
    const otherScroll = await otherCard.locator(".dialogue-scroll").evaluate(element => element.scrollTop);
    await card.locator('[data-speaker="agent"]').click();
    assert((await card.locator(".dialogue-scroll").evaluate(element => element.scrollTop)) > 0);
    assert.equal(await otherCard.locator(".dialogue-scroll").evaluate(element => element.scrollTop), otherScroll);
    const shown = await card.evaluate(element => {
      const region = element.querySelector(".dialogue-scroll").getBoundingClientRect();
      const reply = element.querySelector(".speech-agent").getBoundingClientRect();
      return reply.top >= region.top && reply.top < region.bottom;
    });
    assert(shown, "The saved agent reply must be visible after the jump.");
    await card.locator('[data-speaker="owner"]').click();
    assert((await card.locator(".dialogue-scroll").evaluate(element => element.scrollTop)) < 25);
    const missing = turns.find(item => item.counts.agent_chat < overview.participants.length);
    if (missing) {
      await turn(tab, missing.window);
      for (const pair of await tab.locator(".dialogue-card").all()) {
        if (await pair.locator(".speech-agent").count() === 0) {
          assert(await pair.locator('[data-speaker="agent"]').isDisabled());
        }
      }
      await turn(tab, first);
    }
  });
  await check("turn markers, slider, previous, next, and URL", async () => {
    await turn(tab, windows[2]);
    await tab.locator("#previous-turn").click();
    await ready(tab, windows[1]);
    await tab.locator("#next-turn").click();
    await ready(tab, windows[2]);
    await tab.locator("#turn-slider").fill("3");
    await ready(tab, windows[3]);
    assert.equal(await tab.locator("#turn-slider").getAttribute("aria-valuetext"), `Turn ${windows[3] + 1} of ${turns.length}`);
  });
  await check("play advances saved turns and pause stops", async () => {
    await turn(tab, first);
    await tab.locator("#speed").selectOption("1000");
    await tab.locator("#play").click();
    await ready(tab, windows[1]);
    await tab.locator("#play").click();
    const stopped = new URL(tab.url()).searchParams.get("turn");
    await delay(1300);
    assert.equal(new URL(tab.url()).searchParams.get("turn"), stopped);
    assert.equal(await tab.locator("#play").getAttribute("aria-pressed"), "false");
  });
  await check("keyboard navigation and focused-input protection", async () => {
    await turn(tab, first);
    await tab.locator("#dialogue").focus();
    await tab.keyboard.press("ArrowRight");
    await ready(tab, windows[1]);
    await tab.keyboard.press("ArrowLeft");
    await ready(tab, first);
    await tab.locator("#log-search").fill("saved");
    await tab.keyboard.press("ArrowRight");
    await tab.keyboard.press("Space");
    await delay(1200);
    assert.equal(new URL(tab.url()).searchParams.get("turn"), String(first));
    assert.equal(await tab.locator("#play").getAttribute("aria-pressed"), "false");
    await tab.locator("#log-search").fill("");
    await tab.locator("#dialogue").focus();
    await tab.keyboard.press("Space");
    assert.equal(await tab.locator("#play").getAttribute("aria-pressed"), "true");
    await tab.keyboard.press("Space");
    assert.equal(await tab.locator("#play").getAttribute("aria-pressed"), "false");
  });
  await check("participant focus and Persona, Agent, Resources inspection", async () => {
    const participant = tab.locator(".cast-button").nth(1);
    const selectedName = await participant.locator(".cast-name").textContent();
    await participant.click();
    assert.equal(await tab.locator("#inspector-title").textContent(), selectedName);
    await tab.locator("#focus-only").check();
    assert.equal(await tab.locator(".dialogue-card").count(), 1);
    assert.equal(await tab.locator(".dialogue-card h3").textContent(), selectedName);
    await tab.locator('[data-inspect="persona"]').click();
    assert((await tab.locator("#inspector-body").textContent()).includes("Director cue"));
    await tab.locator('[data-inspect="agent"]').click();
    assert((await tab.locator("#inspector-body").textContent()).includes("Saved activation result"));
    await tab.locator('[data-inspect="resources"]').click();
    assert((await tab.locator(".resource-card").count()) >= 1);
    await tab.locator(".resource-card summary").first().click();
    assert.equal(await tab.locator(".resource-card").first().getAttribute("open"), "");
    await tab.locator("#focus-only").uncheck();
  });
  await check("mail, tools, errors, and text-search filters", async () => {
    for (const channel of ["mail", "tools", "errors"]) {
      await tab.locator(`[data-channel="${channel}"]`).click();
      assert.equal(await tab.locator(`[data-channel="${channel}"]`).getAttribute("aria-pressed"), "true");
      const kinds = await tab.locator("#log-list .log-kind").allTextContents();
      if (channel !== "errors") {
        assert(kinds.length > 0, "The saved first turn must have mail and tool records.");
        assert(kinds.every(kind => kind === channel));
      } else {
        assert.equal(await tab.locator("#log-list .log-row:not(.has-error)").count(), 0);
      }
    }
    await tab.locator('[data-channel="all"]').click();
    await tab.locator("#log-search").fill("unlikely-record-xyz-987654");
    assert.equal(await tab.locator("#log-list .log-row").count(), 0);
    assert((await tab.locator("#log-list").textContent()).includes("No records match"));
    await tab.locator("#log-search").fill(ownerNames[0]);
    assert((await tab.locator("#log-list .log-row").count()) > 0);
    await tab.locator("#log-search").fill("");
  });
  await check("saved evidence dialog and Escape", async () => {
    await tab.locator("#log-list .log-row").first().click();
    assert(await tab.locator("#detail-dialog").isVisible());
    assert((await tab.locator("#detail-content").textContent()).length > 20);
    await tab.keyboard.press("Escape");
    assert(!(await tab.locator("#detail-dialog").isVisible()));
    await tab.locator(".speech-meta button").first().click();
    assert.equal(await tab.locator("#detail-title").textContent(), "Delivered owner chat");
    await tab.locator("#close-detail").click();
  });
  await check("delayed old turn cannot overwrite a new turn", async () => {
    const pattern = `**/api/v1/observer/runs/${run}/turns/${windows[1]}?*`;
    let seen;
    const started = new Promise(resolveStarted => { seen = resolveStarted; });
    await tab.route(pattern, async route => {
      seen();
      try {
        const response = await route.fetch();
        await delay(600);
        await route.fulfill({ response });
      } catch { /* The newer navigation cancels this request. */ }
    });
    await tab.locator(`[data-turn="${windows[1]}"]`).click();
    await started;
    await tab.locator(`[data-turn="${windows[2]}"]`).click();
    await ready(tab, windows[2]);
    await delay(800);
    await ready(tab, windows[2]);
    await tab.unroute(pattern);
  });
  await check("delayed old run cannot overwrite the selected run", async () => {
    const other = await tab.locator(`#run-select option:not([value="${run}"])`).first().getAttribute("value");
    assert(other, "This check needs a second saved run.");
    const pattern = new URL(`/api/v1/observer/runs/${other}`, base).href;
    let seen;
    const started = new Promise(resolveStarted => { seen = resolveStarted; });
    await tab.route(pattern, async route => {
      seen();
      try {
        const response = await route.fetch();
        await delay(600);
        await route.fulfill({ response });
      } catch { /* The newer navigation cancels this request. */ }
    });
    await tab.locator("#run-select").selectOption(other);
    await started;
    await tab.locator("#run-select").selectOption(run);
    await ready(tab, first);
    await delay(800);
    assert.equal(await tab.locator("#run-select").inputValue(), run);
    assert.equal(await tab.locator("#run-name").textContent(), title);
    await ready(tab, first);
    await tab.unroute(pattern);
  });
  await check("server error and retry recover the selected turn", async () => {
    const pattern = `**/api/v1/observer/runs/${run}/turns/${windows[3]}?*`;
    await tab.route(pattern, route => route.fulfill({ status: 503, contentType: "application/json", body: '{"detail":"Test outage"}' }), { times: 1 });
    await tab.locator(`[data-turn="${windows[3]}"]`).click();
    await tab.locator("#error-banner").waitFor({ state: "visible" });
    await tab.locator("#retry-button").click();
    await ready(tab, windows[3]);
    assert(!(await tab.locator("#error-banner").isVisible()));
  });
  await check("follow latest and copy a turn link", async () => {
    await tab.locator("#live-follow").check();
    await ready(tab, windows.at(-1));
    await tab.locator("#live-follow").uncheck();
    await tab.context().grantPermissions(["clipboard-read", "clipboard-write"], { origin: base.origin });
    await tab.locator("#copy-link").click();
    const copied = await tab.evaluate(() => navigator.clipboard.readText());
    assert.equal(copied, tab.url());
    assert.equal(new URL(copied).searchParams.get("run"), run);
    assert.equal(new URL(copied).searchParams.get("turn"), String(windows.at(-1)));
    assert.deepEqual([...new URL(copied).searchParams.keys()].sort(), ["run", "turn"]);
  });
  await check("desktop screenshot and no horizontal overflow", async () => {
    await turn(tab, first);
    await tab.locator(".cast-button").first().click();
    await tab.locator('[data-inspect="persona"]').click();
    assert(await tab.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    const path = resolve(output, "desktop-1440x900.png");
    await tab.screenshot({ path });
    evidence.screenshots.push(path);
    for (const button of await tab.locator('[data-speaker="agent"]:enabled').all()) await button.click();
    const repliesPath = resolve(output, "desktop-agent-replies-1440x900.png");
    await tab.screenshot({ path: repliesPath });
    evidence.screenshots.push(repliesPath);
  });
  await check("denied local access clears private records and Retry restores the selected turn", async () => {
    await tab.route(`**/api/v1/observer/runs/${run}/turns/${first}?*`, route => route.fulfill({status: 403, json: {detail: "Local access denied"}}), {times: 1});
    await tab.locator("#refresh-turn").click();
    await tab.locator("#error-banner").waitFor({state: "visible"});
    assert((await tab.locator("#error-message").textContent()).includes("Local access was denied"));
    assert(!(await tab.locator("#run-content").isVisible()));
    await noPrivateDOM(tab, title, ownerNames);
    await tab.locator("#retry-button").click();
    await ready(tab, first);
  });

  const unavailable = await page();
  await check("initial service failure shows Retry without a login form", async () => {
    await unavailable.route("**/api/v1/observer/runs?*", route => route.fulfill({status: 503, json: {detail: "Test service unavailable"}}), {times: 1});
    await openConsole(unavailable, {turn: windows[1]});
    await unavailable.locator("#error-banner").waitFor({state: "visible"});
    assert.equal(await unavailable.locator("#token, #login-form, #logout").count(), 0);
    assert.equal(await unavailable.locator(".dialogue-card").count(), 0);
    await unavailable.locator("#retry-button").click();
    await ready(unavailable, windows[1]);
  });
  const empty = await page();
  await check("empty run list and refresh", async () => {
    const pattern = "**/api/v1/observer/runs?*";
    await empty.route(pattern, route => route.fulfill({ json: { items: [], next_offset: null } }), { times: 1 });
    await openConsole(empty, { runId: "" });
    await empty.locator("#empty-runs").waitFor({ state: "visible" });
    assert(!(await empty.locator("#run-content").isVisible()));
    await empty.locator("#refresh-runs").click();
    await empty.locator(".dialogue-card").first().waitFor();
    assert(!(await empty.locator("#empty-runs").isVisible()));
  });
  const linked = await page();
  await check("turn link opens the selected saved turn", async () => {
    await openConsole(linked, { turn: windows[3] });
    await ready(linked, windows[3]);
  });
  await check("saved message text cannot become executable HTML", async () => {
    const pattern = `**/api/v1/observer/runs/${run}/turns/${first}?*`;
    const markup = '<img src=x onerror="window.consoleInjected=true"><script>window.consoleInjected=true</script>';
    await linked.route(pattern, async route => {
      const response = await route.fetch();
      const payload = await response.json();
      assert(payload.chat.length > 0);
      payload.chat[0].content = markup;
      await route.fulfill({ response, json: payload });
    }, { times: 1 });
    await turn(linked, first);
    assert((await linked.locator("#dialogue").textContent()).includes(markup));
    assert.equal(await linked.locator("#dialogue img, #dialogue script").count(), 0);
    assert.equal(await linked.evaluate(() => window.consoleInjected), undefined);
  });
  await check("rejected turn access clears private data", async () => {
    const pattern = `**/api/v1/observer/runs/${run}/turns/${windows[2]}?*`;
    await linked.route(pattern, route => route.fulfill({ status: 401, json: { detail: "Local access denied" } }), { times: 1 });
    await linked.locator(`[data-turn="${windows[2]}"]`).click();
    await linked.locator("#error-banner").waitFor({ state: "visible" });
    await noPrivateDOM(linked, title, ownerNames);
  });
  const mobile = await page({ width: 390, height: 844 });
  await check("mobile layout, inspector, and playback controls", async () => {
    await openConsole(mobile, { turn: first });
    await ready(mobile, first);
    assert(await mobile.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await mobile.locator("#next-turn").click();
    await ready(mobile, windows[1]);
    await mobile.locator("#previous-turn").click();
    await ready(mobile, first);
    await mobile.locator(".cast-button").nth(1).click();
    await mobile.locator('[data-inspect="resources"]').click();
    assert((await mobile.locator(".resource-card").count()) > 0);
    await mobile.locator('[data-inspect="persona"]').click();
    await mobile.locator("#run-name").scrollIntoViewIfNeeded();
    assert(await mobile.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    const path = resolve(output, "mobile-390x844.png");
    await mobile.screenshot({ path });
    const fullPath = resolve(output, "mobile-full-page.png");
    await mobile.screenshot({ path: fullPath, fullPage: true });
    evidence.screenshots.push(path, fullPath);
  });
  await check("all 15 pairs retain their identities and are selectable on desktop and mobile", async () => {
    const capacityRun = options["capacity-run"];
    const saved = await api("/runs/" + capacityRun);
    const savedTurns = (await api("/runs/" + capacityRun + "/turns")).items;
    const participants = saved.participants.toSorted((left, right) => left.owner_id.localeCompare(right.owner_id, undefined, { numeric: true }));
    assert.equal(participants.length, 15, "The capacity check requires a saved 15-pair run.");
    const names = new Map(saved.run.scenario.owners.map(owner => [owner.id, owner.name]));
    for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
      const capacity = await page(viewport);
      await openConsole(capacity, { runId: capacityRun, turn: savedTurns[0].window });
      await ready(capacity, savedTurns[0].window);
      assert.equal(await capacity.locator(".cast-button").count(), 15);
      assert.equal(await capacity.locator(".dialogue-card").count(), 15);
      assert.deepEqual(await capacity.locator(".cast-button").evaluateAll(buttons => buttons.map(button => button.dataset.owner)), participants.map(item => item.owner_id));
      assert(await capacity.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await capacity.locator("#focus-only").check();
      for (const participant of participants) {
        const cast = capacity.locator(`.cast-button[data-owner="${participant.owner_id}"]`);
        assert.equal(await cast.locator(".cast-name").textContent(), names.get(participant.owner_id));
        assert.equal(await cast.locator(".cast-meta").textContent(), participant.owner_id);
        assert.equal(await cast.locator(".cast-agent").textContent(), participant.agent_name);
        await cast.click();
        assert.equal(await cast.getAttribute("aria-pressed"), "true");
        assert.equal(await capacity.locator(".dialogue-card").count(), 1);
        assert.equal(await capacity.locator(".dialogue-card h3").textContent(), names.get(participant.owner_id));
        assert((await capacity.locator(".dialogue-card-head").textContent()).includes(participant.agent_name));
        assert.equal(await capacity.locator("#inspector-title").textContent(), names.get(participant.owner_id));
        assert(await capacity.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
        if (viewport.width === 1440) {
          await capacity.locator('[data-inspect="agent"]').click();
          await capacity.locator("#inspector-body button").filter({ hasText: /^Inspect activation input$/ }).click();
          const input = JSON.parse(await capacity.locator("#detail-content").textContent());
          assert.equal(input.participant_id, participant.agent_id);
          assert.equal(input.run_id, capacityRun);
          await capacity.keyboard.press("Escape");
        }
      }
      if (viewport.width === 1440) {
        const path = resolve(output, "fifteen-pairs-1440x900.png");
        await capacity.screenshot({ path });
        evidence.screenshots.push(path);
      }
    }
  });
  const controlled = await page({ width: 1440, height: 1000 }, { fixture: true });
  const child = '11111111-1111-4111-8111-111111111111';
  const fixture = {
    current: 3, execution: null, prompts: [], requests: [], loseNextPrompt: false, lastBranch: null,
    state(id = run) {
      const running = ['queued', 'running'].includes(this.execution?.status);
      return { run_id: id, status: running ? 'running' : 'paused', phase: 'actors', current_window: id === child ? this.lastBranch.window : this.current,
        max_windows: 8, execution: this.execution,
        capabilities: { can_execute: !running, can_pause: running, can_intervene: true, reason: null },
        next_intervention_window: id === child ? this.lastBranch.window : this.current,
        budget: {campaign_id: 'test-campaign', limit_usd: 30, spent_usd: 21.43, reserved_usd: 0, available_usd: 8.57},
        interventions: this.prompts };
    },
  };
  const installControls = async target => {
    await target.route('**/api/v1/console/**', async route => {
      const request = route.request(); const url = new URL(request.url());
      const id = url.pathname.split('/')[5];
      const body = request.method() === 'POST' ? request.postDataJSON() : null;
      if (body) fixture.requests.push({ path: url.pathname, body });
      if (url.pathname.endsWith('/boundaries')) return route.fulfill({json: {
        run_id: id, parent_run_id: id === child ? run : null, parent_window: id === child ? fixture.lastBranch.window : null,
        items: windows.map(window => ({window, available: window === 0 || window === 2, source: window === 0 ? 'initial' : 'checkpoint', reason: window === 0 || window === 2 ? null : 'This old run has no complete checkpoint before this turn.'})),
      }});
      if (url.pathname.endsWith('/interventions')) {
        let saved = fixture.prompts.find(prompt => prompt.request_id === body.request_id);
        if (!saved) { saved = {id: crypto.randomUUID(), ...body, effective_window: fixture.current, status: 'pending'}; fixture.prompts.push(saved); }
        if (fixture.loseNextPrompt) { fixture.loseNextPrompt = false; return route.abort('failed'); }
        return route.fulfill({status: 201, json: saved});
      }
      if (url.pathname.endsWith('/branches')) {
        fixture.lastBranch = body; fixture.execution = null;
        return route.fulfill({status: 201, json: {run_id: child, parent_run_id: run, window: body.window, replayed: false}});
      }
      if (body) fixture.execution = {id: crypto.randomUUID(), request_id: body.request_id, mode: body.mode || 'step', status: url.pathname.endsWith('/pause') ? 'paused' : body.mode === 'continue' ? 'running' : 'queued'};
      return route.fulfill({json: fixture.state(id)});
    });
    await target.route(`**/api/v1/observer/runs/${child}**`, async route => {
      const url = new URL(route.request().url()); const suffix = url.pathname.split(child)[1];
      if (!suffix) {
        const data = structuredClone(overview); data.run.id = child; data.run.status = 'paused'; data.run.current_window = fixture.lastBranch.window;
        return route.fulfill({json: data});
      }
      if (suffix === '/turns') return route.fulfill({json: {run_id: child, items: turns.filter(item => item.window <= fixture.lastBranch.window)}});
      const source = await api('/runs/' + run + suffix + url.search);
      for (const key of ['chat', 'mail', 'board', 'events', 'jobs', 'perspectives']) source[key] = [];
      source.run_id = child; source.pagination.next_offset = null;
      return route.fulfill({json: source});
    });
  };
  await installControls(controlled);
  await check('Live controls show saved cursor, cost, and safe branch boundaries', async () => {
    await openConsole(controlled, {turn: 2}); await ready(controlled, 2);
    await controlled.locator('#mode-live').click();
    await controlled.locator('#live-step').waitFor({state: 'visible'});
    await controlled.waitForFunction(() => !document.querySelector('#live-step').disabled);
    assert((await controlled.locator('#execution-caption').textContent()).includes('Live cursor: Turn 04'));
    assert((await controlled.locator('#execution-budget').textContent()).includes('$8.57 available'));
    assert.equal(await controlled.locator('#branch-turn').textContent(), 'Branch before Turn 03');
    assert(!(await controlled.locator('#branch-turn').isDisabled()));
    await turn(controlled, 3);
    assert(await controlled.locator('#branch-turn').isDisabled());
    assert((await controlled.locator('#branch-reason').textContent()).includes('no complete checkpoint'));
    assert(await controlled.locator('#restart-scenario').isVisible());
    await turn(controlled, 2);
  });
  await check('Actor prompt is queued for selected owner without fabricated dialogue', async () => {
    const before = await controlled.locator('.speech').count();
    await controlled.locator('.cast-button').nth(1).click();
    const owner = await controlled.locator('.cast-button').nth(1).getAttribute('data-owner');
    await controlled.locator('#operator-prompt').fill('Ask whether a calm meeting is possible.');
    await controlled.locator('#send-prompt').click();
    await controlled.waitForFunction(() => document.querySelector('#operation-receipt').textContent.includes('Prompt queued'));
    const submitted = fixture.requests.at(-1).body;
    assert.equal(submitted.owner_id, owner); assert.equal(submitted.target, 'actor'); assert.equal(submitted.expected_window, 3);
    assert.equal(await controlled.locator('.speech').count(), before);
    assert((await controlled.locator('#operation-receipt').textContent()).includes('Turn 04'));
    assert.equal(await controlled.locator('#operator-prompt').inputValue(), '');
    fixture.prompts.at(-1).status = 'applied';
    await controlled.locator('#refresh-controls').click();
    await controlled.waitForFunction(() => document.querySelector('#operation-receipt').textContent.includes('Prompt applied'));
  });
  await check('Step, Continue, and Pause use bounded backend operations', async () => {
    await controlled.locator('#live-step').click();
    await controlled.waitForFunction(() => document.querySelector('#execution-status').textContent === 'queued');
    assert(await controlled.locator('#live-step').isDisabled());
    assert(!(await controlled.locator('#live-pause').isDisabled()));
    await controlled.locator('#live-pause').click();
    await controlled.waitForFunction(() => document.querySelector('#execution-status').textContent === 'paused');
    await controlled.locator('#live-continue').click();
    await controlled.waitForFunction(() => document.querySelector('#execution-status').textContent === 'running');
    await turn(controlled, 0);
    await controlled.locator('#refresh-controls').click();
    await controlled.waitForFunction(() => !document.querySelector('#refresh-controls').disabled);
    assert.equal(new URL(controlled.url()).searchParams.get('turn'), '0', 'Status polling must retain the inspected historical turn.');
    await controlled.locator('#live-pause').click();
    await controlled.waitForFunction(() => document.querySelector('#execution-status').textContent === 'paused');
    assert(fixture.requests.some(item => item.body.mode === 'step'));
    assert(fixture.requests.some(item => item.body.mode === 'continue'));
  });
  await check('A stale control response cannot overwrite an accepted live operation', async () => {
    const snapshot = fixture.state();
    let opened, release;
    const started = new Promise(done => { opened = done; });
    const gate = new Promise(done => { release = done; });
    await controlled.route(`**/api/v1/console/runs/${run}/execution`, async route => {
      opened(); await gate; await route.fulfill({json: snapshot});
    }, {times: 1});
    await controlled.locator('#refresh-controls').click(); await started;
    await controlled.locator('#live-step').click();
    await controlled.waitForFunction(() => document.querySelector('#execution-status').textContent === 'queued');
    release(); await delay(300);
    assert.equal(await controlled.locator('#execution-status').textContent(), 'queued');
    await controlled.locator('#live-pause').click();
    await controlled.waitForFunction(() => document.querySelector('#execution-status').textContent === 'paused');
  });
  await check('Stale turn rejection preserves prompt text and refreshes its target window', async () => {
    await controlled.locator('#operator-prompt').fill('Wait until the new turn.');
    fixture.current = 4;
    await controlled.route(`**/api/v1/console/runs/${run}/interventions`, route => route.fulfill({status: 409, json: {detail: 'The live window changed. Refresh and send again.'}}), {times: 1});
    await controlled.locator('#send-prompt').click();
    await controlled.waitForFunction(() => document.querySelector('#operation-receipt').textContent.includes('live window changed'));
    assert.equal(await controlled.locator('#operator-prompt').inputValue(), 'Wait until the new turn.');
    await controlled.waitForFunction(() => document.querySelector('#send-prompt').textContent.includes('Turn 05'));
    assert(!(await controlled.locator('#retry-operation').isVisible()));
    fixture.current = 3;
    await controlled.locator('#refresh-controls').click();
    await controlled.waitForFunction(() => document.querySelector('#send-prompt').textContent.includes('Turn 04'));
  });
  await check('Uncertain prompt retries preserve request IDs; changed text starts a new operation', async () => {
    await controlled.locator('#prompt-target').selectOption('agent');
    await controlled.locator('#operator-prompt').fill('Explain your options before acting.');
    fixture.loseNextPrompt = true;
    await controlled.locator('#send-prompt').click();
    await controlled.locator('#retry-operation').waitFor({state: 'visible'});
    const firstRequest = fixture.requests.at(-1).body;
    const beforeRetry = fixture.prompts.length;
    await controlled.locator('#retry-operation').click();
    await controlled.waitForFunction(() => document.querySelector('#operation-receipt').textContent.includes('Prompt queued'));
    assert.equal(fixture.requests.at(-1).body.request_id, firstRequest.request_id);
    assert.equal(fixture.prompts.length, beforeRetry);
    assert.equal(fixture.requests.at(-1).body.target, 'agent');
    fixture.loseNextPrompt = true;
    await controlled.locator('#operator-prompt').fill('An uncertain request.');
    await controlled.locator('#send-prompt').click();
    await controlled.locator('#retry-operation').waitFor({state: 'visible'});
    const prior = fixture.requests.at(-1).body.request_id;
    await controlled.locator('#operator-prompt').fill('A corrected request.');
    await controlled.locator('#send-prompt').click();
    await controlled.waitForFunction(() => document.querySelector('#operation-receipt').textContent.includes('Prompt queued'));
    assert.notEqual(fixture.requests.at(-1).body.request_id, prior);
  });
  await check('Historical prompt creates a paused child with parent link and no automatic execution', async () => {
    await turn(controlled, 2);
    await controlled.locator('#operator-prompt').fill('Change the plan before this turn begins.');
    const executions = fixture.requests.filter(item => item.path.endsWith('/execution')).length;
    await controlled.locator('#branch-turn').click();
    await controlled.waitForFunction(id => new URL(location.href).searchParams.get('run') === id && document.querySelector('#operation-receipt').textContent.includes('paused branch'), child);
    assert.equal(fixture.lastBranch.window, 2); assert.equal(fixture.lastBranch.prompt, 'Change the plan before this turn begins.');
    assert.equal(fixture.requests.filter(item => item.path.endsWith('/execution')).length, executions);
    assert((await controlled.locator('#parent-run-link').getAttribute('href')).includes(run));
    assert((await controlled.locator('#parent-run-link').textContent()).includes('before Turn 03'));
    assert.equal(await controlled.locator('.speech').count(), 0);
    assert.equal(await controlled.evaluate(() => localStorage.length + sessionStorage.length), 0);
    assert(await controlled.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  });
  await check('Unavailable historical checkpoint can restart the initial scenario', async () => {
    await controlled.locator('#run-select').selectOption(run); await ready(controlled, 0);
    await turn(controlled, 3);
    await controlled.waitForFunction(() => !document.querySelector('#restart-scenario').hidden);
    await controlled.locator('#restart-scenario').click();
    await controlled.waitForFunction(id => new URL(location.href).searchParams.get('run') === id && document.querySelector('#operation-receipt').textContent.includes('paused branch'), child);
    assert.equal(fixture.lastBranch.window, 0);
  });
  await check('Live console desktop and mobile controls fit the viewport', async () => {
    await controlled.locator('#run-select').selectOption(run); await ready(controlled, 0);
    await turn(controlled, 2);
    await controlled.locator('#operator-prompt').fill('Ask your agent to consider another path.');
    const desktopPath = resolve(output, 'live-controls-desktop.png');
    await controlled.screenshot({path: desktopPath}); evidence.screenshots.push(desktopPath);
    await controlled.setViewportSize({width: 390, height: 844});
    assert(await controlled.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await controlled.locator('#live-controls').scrollIntoViewIfNeeded();
    const mobilePath = resolve(output, 'live-controls-mobile.png');
    await controlled.screenshot({path: mobilePath}); evidence.screenshots.push(mobilePath);
    await controlled.locator('#mode-replay').click();
    assert(!(await controlled.locator('#live-controls').isVisible()));
    assert.equal(await controlled.locator('#mode-replay').getAttribute('aria-pressed'), 'true');
    await controlled.locator('#mode-live').click();
    await controlled.route(`**/api/v1/console/runs/${run}/interventions`, route => route.fulfill({status: 403, json: {detail: 'Local access denied'}}), {times: 1});
    await controlled.locator('#send-prompt').click();
    await controlled.locator('#error-banner').waitFor({state: 'visible'});
    assert.equal(await controlled.locator('#operator-prompt').inputValue(), '');
    assert.equal(await controlled.locator('#operation-receipt').textContent(), '');
    assert.equal(await controlled.locator('#parent-run-link').textContent(), '');
  });

  const observer = await page();
  await check("data observer opens directly without credentials", async () => {
    await observer.goto(new URL("/runs", base).href);
    await observer.locator(".record").first().waitFor();
    await observer.locator("#live").uncheck();
    await observer.locator("#run-select").selectOption(run);
    await observer.waitForFunction(id => document.querySelector("#run-id").textContent === id && document.querySelectorAll(".record").length > 0, run);
    assert.equal(await observer.locator("#token, #login-form, #logout").count(), 0);
    assert.equal(await observer.locator("#run-title").textContent(), title);
    const path = resolve(output, "observer-desktop.png");
    await observer.screenshot({path}); evidence.screenshots.push(path);
  });
  await check("data observer clears private records after denied access and can retry", async () => {
    await observer.route(`**/api/v1/observer/runs/${run}/events?*`, route => route.fulfill({status: 403, json: {detail: "Local access denied"}}), {times: 1});
    await observer.locator("#refresh").click();
    await observer.locator("#error").waitFor({state: "visible"});
    assert(!(await observer.locator("#run-content").isVisible()));
    const text = await observer.locator("body").textContent();
    assert(!text.includes(title));
    for (const name of ownerNames) assert(!text.includes(name));
    assert.equal(await observer.locator(".record").count(), 0);
    await observer.locator("#retry").click();
    await observer.locator(".record").first().waitFor();
    assert(!(await observer.locator("#error").isVisible()));
  });
  const observerMobile = await page({width: 390, height: 844});
  await check("data observer startup failure and mobile recovery", async () => {
    await observerMobile.route("**/api/v1/observer/runs?*", route => route.fulfill({status: 503, json: {detail: "Test service unavailable"}}), {times: 1});
    await observerMobile.goto(new URL("/runs", base).href);
    await observerMobile.locator("#error").waitFor({state: "visible"});
    assert.equal(await observerMobile.locator("#token, #login-form, #logout").count(), 0);
    await observerMobile.locator("#retry").click();
    await observerMobile.locator(".record").first().waitFor();
    assert(!(await observerMobile.locator("#error").isVisible()));
    const selected = await api("/runs/" + await observerMobile.locator("#run-select").inputValue());
    if (selected.run.status === "paused" && selected.run.result?.reason === "window_limit") {
      assert((await observerMobile.locator("#run-notice").textContent()).includes("after the requested turns"));
    }
    assert(await observerMobile.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    const path = resolve(output, "observer-mobile.png");
    await observerMobile.screenshot({path}); evidence.screenshots.push(path);
  });

  await check("no script exceptions or real write requests", async () => {
    assert.deepEqual(evidence.page_errors, []);
    assert.deepEqual(evidence.writes, []);
    assert.deepEqual(evidence.authorization_headers, []);
  });
  evidence.passed = true;
} catch (error) {
  evidence.passed = false;
  // Never include headers, form values, browser storage, or a Playwright trace.
  evidence.checks.push({ name: currentCheck, passed: false, error: String(error.message) });
  process.exitCode = 1;
} finally {
  await Promise.allSettled(contexts.map(context => context.close()));
  await browser.close();
  evidence.finished_at = new Date().toISOString();
  await writeFile(resolve(output, "results.json"), JSON.stringify(evidence, null, 2) + "\n", { mode: 0o600 });
  console.log(JSON.stringify(evidence, null, 2));
}
