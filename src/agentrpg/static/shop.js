/* Credential-free storefront. The private browser adapter supplies authentication. */
(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const params = new URLSearchParams(location.search);
  const run = params.get("run") || "";
  const observer = params.get("observer") === "1";
  let selectedOwner = params.get("owner") || "";
  let data = null, view = ["catalog", "orders"].includes(location.hash.slice(1)) ? location.hash.slice(1) : "catalog", detailId = null, query = "", selectedTask = "";
  let busy = false;
  const validRun = /^[0-9a-f-]{36}$/i.test(run);
  const api = `/api/v1/runs/${encodeURIComponent(run)}/shop`;
  const statusNames = { pending: "处理中", accepted: "已结算", rejected: "未成交", partial: "部分成交" };
  const art = id => `/assets/shop-${String(id).includes("mug") ? "mug" : String(id).includes("lamp") ? "lamp" : "notebook"}.svg`;
  const describe = id => String(id).includes("mug") ? "一只温润的陶瓷杯，陪伴每个喝水与咖啡的日常。" : String(id).includes("lamp") ? "为书桌留一束温暖的光。简洁、安静，刚刚好。" : "把想法写下来，让日常的小事有迹可循。";
  function el(tag, text, cls) {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = String(text);
    if (cls) node.className = cls;
    return node;
  }
  function image(sku, cls) { const img = el("img", null, cls); img.src = art(sku); img.alt = ""; return img; }
  function button(label, target, action, cls) {
    const node = el("button", label, cls);
    node.dataset.shopTarget = target;
    node.addEventListener("click", action);
    return node;
  }
  function message(text, success = false) {
    $("status").textContent = text;
    $("status").className = success ? "status success" : "status";
    $("status").hidden = !text;
  }
  function ready(value) { $("shop-main").dataset.ready = String(value); $("shop-main").setAttribute("aria-busy", String(!value)); }
  async function request(path, body) {
    const response = await fetch(path, { method: body ? "POST" : "GET", credentials: "omit",
      headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined });
    let result;
    try { result = await response.json(); } catch { throw new Error("商城暂时无法返回结果，请稍后刷新。"); }
    if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "操作未成功，请刷新并检查当前任务和授权。");
    return result;
  }
  async function refresh() {
    const path = observer ? `/api/v1/observer/runs/${run}/shop${selectedOwner ? `?owner=${encodeURIComponent(selectedOwner)}` : ""}` : `${api}/state`;
    data = await request(path);
    if (observer) selectedOwner = data.owner_id;
    render();
  }
  async function change(action) {
    if (busy || observer) return;
    busy = true; ready(false); message("");
    try { await action(); await refresh(); }
    catch (error) { message(error.message); }
    finally { busy = false; ready(true); renderButtons(); }
  }
  function show(next) {
    view = next;
    history.replaceState(null, "", `${location.pathname}${location.search}#${view}`);
    for (const name of ["catalog", "detail", "review", "orders"]) $(`${name}-view`).hidden = name !== view;
    $("catalog-nav").classList.toggle("active", next === "catalog" || next === "detail");
    $("orders-nav").classList.toggle("active", next === "orders");
    if (next === "review") renderReview();
    if (next === "orders") renderOrders();
  }
  function renderButtons() {
    if (!data) {
      $("review-cart").disabled = true;
      $("place-order").disabled = true;
      return;
    }
    const running = data.run.status === "running";
    const chosen = data.tasks.find(t => t.id === selectedTask && t.status === "open");
    for (const node of document.querySelectorAll(".add-button")) node.disabled = observer || busy || !running || !chosen || node.dataset.stock === "0";
    for (const node of document.querySelectorAll(".remove-button")) node.disabled = observer || busy || !running;
    $("review-cart").disabled = !data.cart.lines.length || busy;
    $("place-order").disabled = observer || busy || !running || !data.cart.lines.length;
  }
  async function addProduct(product, qty) {
    await change(() => request(`${api}/cart`, { action: "set_quantity", task_id: selectedTask || null, listing_id: product.id, quantity: qty }));
  }
  function productActions(product) {
    const actions = el("div", null, "product-actions");
    const qty = el("input", null, "quantity");
    Object.assign(qty, { type: "number", min: "1", max: String(Math.min(100, product.quantity)), value: "1" });
    qty.setAttribute("aria-label", `${product.title} 数量`);
    qty.dataset.shopTarget = `quantity-${product.id}`;
    const add = button("加入购物车", `add-${product.id}`, () => addProduct(product, Number(qty.value)), "add-button");
    add.dataset.stock = String(product.quantity);
    actions.append(qty, add);
    return actions;
  }
  function renderProducts() {
    const products = data.catalog.filter(p => `${p.title} ${p.origin_id}`.toLowerCase().includes(query.toLowerCase()));
    $("products").replaceChildren();
    $("product-count").textContent = `${products.length} 件好物`;
    $("products-heading").textContent = query ? `搜索「${query}」` : "日常精选";
    if (!products.length) $("products").append(el("p", query ? "没有找到匹配的商品，试试其他关键词。" : "店铺正在准备商品，稍后刷新看看。", "empty"));
    for (const product of products) {
      const card = el("article", null, "product-card"), frame = el("div", null, "product-image");
      frame.append(image(product.origin_id), el("span", "EVERYDAY GOODS", "product-tag"));
      card.append(frame, el("h3", product.title), el("p", describe(product.origin_id)));
      const bottom = el("div", null, "product-bottom"), price = el("span", product.unit_price, "price");
      price.append(el("small", "积分")); bottom.append(price, el("span", product.quantity > 0 ? `库存 ${product.quantity} 件` : "暂时缺货", "stock"));
      card.append(bottom, productActions(product));
      card.append(button("查看详情 ↗", `detail-${product.id}`, () => { detailId = product.id; renderDetail(); show("detail"); }, "details-button"));
      $("products").append(card);
    }
  }
  function renderDetail() {
    const product = data.catalog.find(p => p.id === detailId);
    if (!product) { show("catalog"); return; }
    const box = el("div", null, "detail-card"), text = el("div");
    text.append(el("p", "THE EVERYDAY EDIT", "eyebrow"), el("h2", product.title), el("p", describe(product.origin_id)),
      el("p", `店主：${product.seller_id} · 商品：${product.origin_id}`), el("p", `当前库存 ${product.quantity} 件`),
      el("strong", `${product.unit_price} 积分`, "price"), productActions(product));
    box.append(image(product.origin_id), text); $("product-detail").replaceChildren(box); renderButtons();
  }
  function lineView(line, remove = false) {
    const row = el("div", null, "cart-line"), info = el("div");
    info.append(el("h3", line.title), el("p", `${line.quantity} 件 × ${line.quoted_price ?? line.unit_price} 积分`));
    row.append(image(line.origin_id, "cart-thumb"), info);
    if (remove) {
      const removeButton = button("×", `remove-${line.listing_id}`, () => addProduct({ id: line.listing_id }, 0), "remove-button");
      removeButton.setAttribute("aria-label", `移除 ${line.title}`); row.append(removeButton);
    }
    return row;
  }
  function renderCart() {
    $("cart-lines").replaceChildren(...data.cart.lines.map(line => lineView(line, true)));
    if (!data.cart.lines.length) $("cart-lines").append(el("p", "购物车还空着。\n选一件心仪的好物吧。", "empty"));
    $("cart-count").textContent = `${data.cart.lines.reduce((sum, l) => sum + l.quantity, 0)} 件`;
    $("cart-total").textContent = `${data.cart.total} 积分`;
  }
  function renderTasks() {
    const tasks = data.tasks.filter(t => t.status === "open");
    if (!tasks.some(t => t.id === selectedTask)) selectedTask = tasks.some(t => t.id === data.cart.task_id) ? data.cart.task_id : tasks[0]?.id || "";
    $("shopping-task").replaceChildren();
    if (!tasks.length) { const option = el("option", "暂无待购买任务"); option.value = ""; $("shopping-task").append(option); }
    for (const task of tasks) { const option = el("option", task.description); option.value = task.id; $("shopping-task").append(option); }
    $("shopping-task").value = selectedTask;
    const task = tasks.find(t => t.id === selectedTask);
    $("task-summary").textContent = task ? `本次购买 ${task.quantity} 件，合计最多 ${task.max_total} 积分。任务 ${task.task_key}` : "新采购需要主人的新任务；已有订单可在“我的订单”查看。";
  }
  function renderReview() {
    $("review-lines").replaceChildren(...data.cart.lines.map(line => lineView(line)));
    if (!data.cart.lines.length) $("review-lines").append(el("p", "购物车已清空。请查看已有订单，或返回商城。", "empty"));
    $("review-total").textContent = `${data.cart.total} 积分`;
    renderButtons();
  }
  function renderOrders() {
    $("orders").replaceChildren();
    if (!data.orders.length) $("orders").append(el("p", "还没有订单。完成购买后，回执会保存在这里。", "empty"));
    for (const order of data.orders) {
      const box = el("article", null, "order-card");
      box.dataset.orderId = order.id;
      box.dataset.pendingIds = JSON.stringify(order.lines.filter(l => l.receipt.status === "pending").map(l => l.request_id));
      const head = el("div", null, "order-header"), identity = el("div");
      identity.append(el("b", `订单 ${order.id.slice(0, 8)}`), el("p", `任务 ${order.task_key} · 第 ${order.window_index + 1} 轮`));
      head.append(identity, el("span", statusNames[order.status] || order.status, `order-status ${order.status}`));
      const body = el("div", null, "order-body");
      for (const line of order.lines) { body.append(lineView(line)); if (line.receipt.reason) body.append(el("p", `未成交原因：${line.receipt.reason}`, "muted")); }
      body.append(el("p", `已扣款 ${order.paid_total} 积分 · 报价总计 ${order.quoted_total} 积分`, "order-total"), el("p", `订单 ID：${order.id}`, "receipt"));
      for (const line of order.lines) body.append(el("p", `回执 ${line.request_id} · ${statusNames[line.receipt.status] || line.receipt.status}`, "receipt"));
      box.append(head, body); $("orders").append(box);
    }
  }
  function render() {
    $("owner-name").textContent = data.owner_id;
    $("balance").textContent = data.balance;
    $("permission").textContent = `${data.consent.buy_budget} 积分`;
    $("run-status").textContent = `第 ${data.run.current_window + 1} 轮 · ${data.run.status === "running" ? "运行中" : data.run.status === "completed" ? "已结束" : "已暂停"}`;
    $("observer-banner").hidden = !observer;
    if (observer) {
      $("observer-owner").replaceChildren(...data.owners.map(owner => { const option = el("option", owner); option.value = owner; return option; }));
      $("observer-owner").value = selectedOwner;
      $("trajectory-link").href = `/console?run=${run}&turn=${Math.max(0, data.run.current_window - 1)}`;
    }
    renderTasks(); renderProducts(); renderCart(); renderOrders();
    if (view === "detail") renderDetail();
    show(view); renderButtons();
  }
  $("search-form").addEventListener("submit", event => { event.preventDefault(); query = $("search").value.trim(); if (data) { renderProducts(); show("catalog"); renderButtons(); } });
  $("shopping-task").addEventListener("change", () => { selectedTask = $("shopping-task").value; renderTasks(); renderButtons(); });
  $("catalog-nav").addEventListener("click", () => show("catalog"));
  for (const button of document.querySelectorAll('[data-shop-target="back-catalog"]')) button.addEventListener("click", () => show("catalog"));
  $("orders-nav").addEventListener("click", async () => { ready(false); try { await refresh(); show("orders"); } catch (error) { message(error.message); } finally { ready(true); } });
  $("refresh-orders").addEventListener("click", async () => { ready(false); try { await refresh(); } catch (error) { message(error.message); } finally { ready(true); } });
  $("review-cart").addEventListener("click", () => show("review"));
  $("place-order").addEventListener("click", () => change(async () => {
    await request(`${api}/checkout`, { cart_version: data.cart.version });
    view = "orders"; message("订单已提交。请以订单页面显示的实际结算回执为准。", true);
  }));
  $("observer-owner").addEventListener("change", async () => { selectedOwner = $("observer-owner").value; ready(false); try { await refresh(); } catch (error) { message(error.message); } finally { ready(true); } });
  ready(false);
  if (!validRun) { message("请从一个已启用商城的场景链接打开页面。当前链接缺少有效的场景 ID。"); ready(true); }
  else refresh().catch(error => message(error.message)).finally(() => ready(true));
})();
