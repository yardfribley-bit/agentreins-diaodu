const basePath = new URL("..", document.currentScript.src).pathname.replace(/\/$/, "");
const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[ch]));

const DRINKS = [
  ["coconut_latte", "生椰拿铁"],
  ["latte", "普通拿铁"],
  ["butter_latte", "小黄油拿铁"],
  ["butter_americano", "小黄油美式"],
  ["ethiopia_americano", "埃塞金烘美式"],
  ["lemon_tea", "手打鲜切柠檬茶"],
];

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

function renderForm() {
  const options = DRINKS.map(([id, name]) => (
    `<option value="${esc(id)}"${id === "coconut_latte" ? " selected" : ""}>${esc(name)}</option>`
  )).join("");
  $("#app").innerHTML = `<article class="panel">
    <p class="eyebrow">平台推广</p>
    <h1>每天请人喝两杯瑞幸</h1>
    <p class="lede">提交后会立刻自动下单。请保存结果链接，取餐码出现在结果页。本页不连接你的瑞幸账号，也不收集券码。饮品默认生椰拿铁，需到店自取。按北京时间计算，每天全平台两杯，每人一杯，送完即止。</p>
    <p class="quota" id="quota">正在查看今日剩余杯数…</p>
    <form id="claim-form">
      <div class="pair">
        <label><span>城市</span><input name="city" required maxlength="40" autocomplete="address-level2" placeholder="例如：上海"></label>
        <label><span>店名</span><input name="shopName" required maxlength="80" placeholder="例如：瑞幸咖啡（陆家嘴店）"></label>
      </div>
      <div class="pair">
        <label><span>手机号（优先）</span><input name="phone" inputmode="numeric" autocomplete="tel" maxlength="20" placeholder="11 位手机号"></label>
        <label><span>邮箱（没有手机号时填写）</span><input name="email" type="email" autocomplete="email" maxlength="254" placeholder="name@example.com"></label>
      </div>
      <p class="help">联系方式至少填一个。页面和公开结果只显示打码后的联系方式。</p>
      <label><span>饮品</span><select name="drink" aria-label="饮品">${options}</select>
        <span class="help">默认生椰拿铁，大杯、冰、不另外加糖。不提供糖度、冰量等定制。</span></label>
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
  loadQuota();
  $("#locate").onclick = locate;
  $("#claim-form").onsubmit = submitClaim;
}

async function loadQuota() {
  try {
    const quota = await api("/v1/luckin/quota");
    const line = $("#quota");
    if (!line) return;
    line.textContent = quota.remaining > 0
      ? `今日剩余 ${quota.remaining} / ${quota.limit} 杯（北京时间 ${quota.day}）`
      : "今日两杯已送完，请明天再来。";
    const button = $("#claim-form button.primary");
    if (button && quota.remaining <= 0) button.disabled = true;
  } catch (error) {
    const line = $("#quota");
    if (line) line.textContent = "今日杯数暂时无法显示，仍可尝试提交。";
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
  if (!data.get("consent")) {
    error.textContent = "请先勾选同意说明。";
    return;
  }
  const body = {
    city: String(data.get("city") || "").trim(),
    shopName: String(data.get("shopName") || "").trim(),
    drink: String(data.get("drink") || "coconut_latte"),
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
  const ready = claim.status === "ready" && claim.pickupCode;
  const failed = claim.status === "failed";
  const eyebrow = ready ? "可取餐" : (failed ? "这次没有下成" : "正在下单");
  const title = ready ? "取餐码" : (failed ? "今日名额已释放" : "正在下单");
  const pickup = ready ? `<div class="pickup"><span>取餐码</span><b>${esc(claim.pickupCode)}</b></div>` : "";
  const story = failed
    ? esc(claim.failReason || claim.message || "下单未成功，可以再提交一次。")
    : esc(claim.message || "正在下单。请保存结果链接，取餐码会写在这一页。");
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

function resultBody(claim) {
  const when = claim.orderTime ? new Date(claim.orderTime).toLocaleString("zh-CN", {hour12: false, timeZone: "Asia/Shanghai"}) : "";
  const ready = claim.status === "ready";
  const ordering = claim.status === "ordering" || claim.status === "pending";
  const failed = claim.status === "failed" || claim.status === "rejected" || claim.status === "cancelled";
  const pickup = ready ? `<div class="pickup"><span>取餐码</span><b>${esc(claim.pickupCode)}</b></div>` : "";
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
    <p>门店 ${esc(claim.shopName || "")}${claim.city ? " · " + esc(claim.city) : ""}</p>
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
