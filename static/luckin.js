const basePath = new URL("..", document.currentScript.src).pathname.replace(/\/$/, "");
const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[ch]));

const PAY_MARKERS = ["wxpay", "weixin://", "pay.weixin", "alipay", "支付"];

function resultToken() {
  const match = location.pathname.match(/\/luckin\/r\/([^/]+)\/?$/);
  return match ? decodeURIComponent(match[1]) : "";
}

async function api(path, body) {
  const response = await fetch(basePath + path, {
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? {} : {"Content-Type": "application/json"},
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = {};
  try { data = await response.json(); } catch (error) { data = {}; }
  if (!response.ok) {
    const failure = new Error(data.error || "请求失败");
    failure.status = response.status;
    throw failure;
  }
  return data;
}

function formHref() {
  return (basePath || "") + "/luckin/";
}

function safeQr(value) {
  const text = String(value || "");
  const folded = text.toLowerCase();
  if (!text || PAY_MARKERS.some((marker) => folded.includes(marker))) return "";
  if (/^https?:\/\//i.test(text)) return text;
  if (/^data:image\/[a-z0-9.+-]+;base64,/i.test(text)) return text;
  return "";
}

function renderForm() {
  $("#app").innerHTML = `<article class="panel">
    <p class="eyebrow">平台推广</p>
    <h1>每天请人喝两杯瑞幸</h1>
    <p class="lede">先看现在券能请的饮品，选一杯并填写取餐门店。提交后会同步下单，结果里直接给出取餐码或取餐二维码。本页不连接你的瑞幸账号，也不收集券码，也不会出现支付码。按北京时间计算，每天全平台两杯，每人一杯，送完即止。</p>
    <p class="quota" id="quota">正在查看今日可请饮品…</p>
    <form id="claim-form">
      <div class="pair">
        <label><span>城市</span><input name="city" required maxlength="40" autocomplete="address-level2" placeholder="例如：上海"></label>
        <label><span>取餐门店</span><input name="shopName" required maxlength="80" placeholder="例如：瑞幸咖啡（陆家嘴店）"></label>
      </div>
      <div class="pair">
        <label><span>手机号（优先）</span><input name="phone" inputmode="numeric" autocomplete="tel" maxlength="20" placeholder="11 位手机号"></label>
        <label><span>邮箱（没有手机号时填写）</span><input name="email" type="email" autocomplete="email" maxlength="254" placeholder="name@example.com"></label>
      </div>
      <p class="help">联系方式至少填一个。页面和公开结果只显示打码后的联系方式。</p>
      <label><span>可请饮品</span><select name="drink_id" aria-label="可请饮品" required disabled><option value="">正在读取清单…</option></select>
        <span class="help" id="drink-spec">只列出当前券能完全覆盖的饮品。</span></label>
      <label><span>定位描述（选填）</span><textarea name="locationNote" maxlength="120" placeholder="例如：近地铁 2 号口"></textarea></label>
      <div class="row">
        <button type="button" id="locate">附上大致位置（选填）</button>
        <span class="help" id="locate-note">不附位置也可以提交。</span>
      </div>
      <input type="hidden" name="lat" value="">
      <input type="hidden" name="lng" value="">
      <label class="consent"><input name="consent" type="checkbox" required>我了解这是到店自取的推广体验，不是无条件福利；每天全平台两杯、每人限一杯；提交后会自动下单，且与 AgentReins Hub 推广相关。</label>
      <p class="form-error" id="form-error" role="alert"></p>
      <button class="primary" type="submit">提交领取</button>
    </form>
  </article>`;
  loadMenu();
  $("#locate").onclick = locate;
  $("#claim-form").onsubmit = submitClaim;
}

function applyMenu(menu) {
  const drinks = Array.isArray(menu.drinks) ? menu.drinks : [];
  const quota = menu.quota || {};
  const line = $("#quota");
  const select = $("#claim-form [name=drink_id]");
  const spec = $("#drink-spec");
  const button = $("#claim-form button.primary");
  if (!select) return;
  if (line) {
    const cups = Number.isInteger(quota.remaining)
      ? `今日剩余 ${quota.remaining} / ${quota.limit} 杯（北京时间 ${quota.day}）。`
      : "";
    line.textContent = (menu.message ? menu.message + " " : "") + cups;
  }
  if (!drinks.length || quota.remaining === 0) {
    select.innerHTML = `<option value="">现在没有可请的饮品</option>`;
    select.disabled = true;
    if (button) button.disabled = true;
    if (spec) spec.textContent = menu.message || "现在没有可用的券，暂时不能请客。";
    return;
  }
  select.innerHTML = drinks.map((drink) => {
    const left = Number.isInteger(drink.remaining) ? `（剩 ${drink.remaining}）` : "";
    return `<option value="${esc(drink.id)}">${esc(drink.name)}${esc(left)}</option>`;
  }).join("");
  select.disabled = false;
  if (button) button.disabled = false;
  const showSpec = () => {
    const chosen = drinks.find((drink) => drink.id === select.value);
    if (spec) spec.textContent = chosen && chosen.spec ? chosen.spec : "规格以清单为准。不提供糖度、冰量等定制。";
  };
  select.onchange = showSpec;
  showSpec();
}

async function loadMenu() {
  try {
    applyMenu(await api("/v1/luckin/menu"));
  } catch (error) {
    const line = $("#quota");
    if (line) line.textContent = "现在没有可用的券，暂时不能请客。";
    const select = $("#claim-form [name=drink_id]");
    const button = $("#claim-form button.primary");
    if (select) {
      select.innerHTML = `<option value="">现在没有可请的饮品</option>`;
      select.disabled = true;
    }
    if (button) button.disabled = true;
  }
}

function locate() {
  const note = $("#locate-note");
  if (!navigator.geolocation) {
    note.textContent = "这个浏览器不能提供位置，仍可提交。";
    return;
  }
  note.textContent = "正在请求位置…";
  navigator.geolocation.getCurrentPosition((position) => {
    $("#claim-form [name=lat]").value = String(position.coords.latitude);
    $("#claim-form [name=lng]").value = String(position.coords.longitude);
    note.textContent = "已附上大致位置。";
  }, () => {
    note.textContent = "没有取得位置，仍可提交。";
  }, {enableHighAccuracy: false, timeout: 8000, maximumAge: 60000});
}

async function submitClaim(event) {
  event.preventDefault();
  const form = event.target;
  const button = form.querySelector("button.primary");
  const error = $("#form-error");
  error.textContent = "";
  const data = new FormData(form);
  const phone = String(data.get("phone") || "").trim();
  const email = String(data.get("email") || "").trim();
  if (!phone && !email) {
    error.textContent = "请填写手机号或邮箱。";
    return;
  }
  if (!String(data.get("drink_id") || "")) {
    error.textContent = "请先从当前可请清单里选择饮品。";
    return;
  }
  if (!data.get("consent")) {
    error.textContent = "请先勾选同意说明。";
    return;
  }
  const body = {
    city: String(data.get("city") || "").trim(),
    shopName: String(data.get("shopName") || "").trim(),
    drink_id: String(data.get("drink_id") || ""),
    consent: true,
  };
  if (phone) body.phone = phone;
  if (email) body.email = email;
  const locationNote = String(data.get("locationNote") || "").trim();
  if (locationNote) body.locationNote = locationNote;
  const lat = String(data.get("lat") || "");
  const lng = String(data.get("lng") || "");
  if (lat && lng) {
    body.lat = Number(lat);
    body.lng = Number(lng);
  }
  button.disabled = true;
  try {
    const claim = await api("/v1/luckin/claims", body);
    form.reset();
    showSubmitted(claim);
  } catch (failure) {
    error.textContent = failure.message;
    button.disabled = false;
  }
}

function showSubmitted(claim) {
  const ready = claim.status === "ready";
  const failed = claim.status === "failed";
  const eyebrow = ready ? "可取餐" : (failed ? "这次没有下成" : "正在下单");
  const title = ready ? (claim.pickupCode ? "取餐码" : "取餐二维码") : (failed ? "今日名额已释放" : "正在下单");
  const pickup = ready ? pickupBlock(claim) : "";
  const story = failed
    ? esc(claim.failReason || claim.message || "下单未成功，可以再提交一次。")
    : esc(claim.message || "正在下单。请保存结果链接，取餐码或二维码会写在这一页。");
  $("#app").innerHTML = `<article class="panel">
    <p class="eyebrow">${eyebrow}</p>
    ${pickup}
    <h1>${title}</h1>
    <p>${story}</p>
    <p>领取编号 ${esc(claim.claimId)} · ${esc(claim.drink)} · ${esc(claim.contactMasked)}</p>
    <p><a class="result-link" href="${esc(claim.resultUrl)}">${esc(claim.resultUrl)}</a></p>
    <p class="muted">链接就是查看凭证。不要发到公开场合。</p>
  </article>`;
}

function statusClass(status) {
  if (status === "ready") return "ready";
  if (status === "failed" || status === "rejected" || status === "cancelled") return "bad";
  if (status === "ordering") return "wait";
  return "";
}

function pickupBlock(claim) {
  const code = claim.pickupCode
    ? `<div class="pickup"><span>取餐码</span><b>${esc(claim.pickupCode)}</b></div>`
    : "";
  const qr = safeQr(claim.qrUrl) || safeQr(claim.qrData);
  const image = qr ? `<img class="pickup-qr" alt="取餐二维码" src="${esc(qr)}">` : "";
  return code + image;
}

function resultBody(claim) {
  const when = claim.orderTime ? new Date(claim.orderTime).toLocaleString("zh-CN", {hour12: false, timeZone: "Asia/Shanghai"}) : "";
  const ready = claim.status === "ready";
  const ordering = claim.status === "ordering" || claim.status === "pending";
  const failed = claim.status === "failed" || claim.status === "rejected" || claim.status === "cancelled";
  const pickup = ready ? pickupBlock(claim) : "";
  const hint = claim.readyHint ? `<p>${esc(claim.readyHint)}</p>` : "";
  const eyebrow = ready ? "可取餐" : (ordering ? "正在下单" : "领取进度");
  const title = ready ? esc(claim.drink || "瑞幸请客") : (ordering ? "正在下单" : esc(claim.drink || "瑞幸请客"));
  const story = ordering
    ? `<p>正在向瑞幸下单。这一页会刷新出取餐码。</p>`
    : (claim.status === "failed" && claim.failReason ? `<p>${esc(claim.failReason)}</p>` : "");
  const again = failed ? `<p><a href="${esc(formHref())}">返回领取页</a></p>` : "";
  return `<article class="panel">
    <p class="eyebrow">${eyebrow}</p>
    ${pickup}
    <h1>${title}</h1>
    <p class="status ${statusClass(claim.status)}">${esc(claim.statusLabel || claim.status)}</p>
    ${story}
    <p>门店 ${esc(claim.storeName || claim.shopName || "")}${claim.city ? " · " + esc(claim.city) : ""}</p>
    ${when ? `<p>下单时间 ${esc(when)}</p>` : ""}
    ${hint}
    <p class="muted">联系方式 ${esc(claim.contactMasked || "")} · 编号 ${esc(claim.claimId || "")}</p>
    <p class="muted">到店自取。这是 AgentReins Hub 的推广体验，不是无条件福利。</p>
    ${again}
    <p class="row"><button type="button" id="refresh-result">刷新状态</button></p>
  </article>`;
}

async function renderResult(token) {
  const box = $("#app");
  try {
    const claim = await api("/v1/luckin/result/" + encodeURIComponent(token));
    box.innerHTML = resultBody(claim);
    $("#refresh-result").onclick = () => renderResult(token);
    if (claim.status === "pending" || claim.status === "ordering") {
      window.setTimeout(() => {
        if (resultToken() === token) renderResult(token);
      }, 15000);
    }
  } catch (error) {
    box.innerHTML = `<article class="panel"><h1>没有这笔领取</h1><p>链接不完整或已失效。领取结果只对持有链接的人显示。</p><p><a href="${esc(formHref())}">返回领取页</a></p></article>`;
  }
}

if (resultToken()) renderResult(resultToken());
else renderForm();
