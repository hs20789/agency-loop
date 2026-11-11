// UI smoke test: runs agency_loop/static/app.js in a small stub DOM parsed from the real index.html and drives the
// whole flow against a live server with mock providers (no npm packages).
//
//   AGENCY_PORT=8767 .venv/bin/python -m agency_loop &      # all providers default to mock
//   node tests/ui/ui_smoke.mjs http://127.0.0.1:8767
//
// Uploads count toward the server's per-IP limit (10 per 10 minutes): restart the server between repeated runs.
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const base = (process.argv[2] || "").replace(/\/$/, "");
if (!base) {
  console.error("usage: node tests/ui/ui_smoke.mjs <base url, e.g. http://127.0.0.1:8767>");
  process.exit(2);
}
const read = (rel) => fs.readFileSync(path.join(root, rel));
const html = read("agency_loop/static/index.html").toString();
const css = read("agency_loop/static/app.css").toString();
const appSource = read("agency_loop/static/app.js").toString();
const ANSWER_FIELDS = ["expected_behavior", "potential_issue", "ai_instruction", "verification"];

// ---------- stub DOM ----------

const VOID = new Set(["META", "LINK", "INPUT", "BR", "IMG", "HR"]);
const BOOLEAN = new Set(["hidden", "disabled", "checked", "open"]);
const decode = (s) => s.replace(/&(amp|lt|gt|quot|#39);/g, (_, e) => ({ amp: "&", lt: "<", gt: ">", quot: '"', "#39": "'" })[e]);

class Text {
  constructor(data) {
    this.data = String(data);
    this.parent = null;
  }

  get textContent() {
    return this.data;
  }
}

class Element {
  constructor(doc, tag) {
    Object.assign(this, { doc, tagName: tag.toUpperCase(), childNodes: [], attrs: {}, listeners: {}, parent: null, dataset: {} });
    Object.assign(this, { hidden: false, disabled: false, checked: false, open: false, value: "", returnValue: "", files: [], style: {}, offsetHeight: 0 });
  }

  get id() { return this.attrs.id || ""; }
  get name() { return this.attrs.name || ""; }
  get type() { return (this.attrs.type || (this.tagName === "BUTTON" ? "submit" : "")).toLowerCase(); }
  get className() { return this.attrs.class || ""; }
  set className(value) { this.attrs.class = String(value); }
  get classList() {
    const names = () => this.className.split(/\s+/).filter(Boolean);
    return {
      contains: (c) => names().includes(c),
      add: (c) => { if (!names().includes(c)) this.className = [...names(), c].join(" "); },
      remove: (c) => { this.className = names().filter((n) => n !== c).join(" "); },
      toggle: (c, on = !names().includes(c)) => { if (on) this.classList.add(c); else this.classList.remove(c); },
    };
  }

  get children() { return this.childNodes.filter((n) => n instanceof Element); }
  get parentElement() { return this.parent instanceof Element ? this.parent : null; }
  get isConnected() {
    let n = this;
    while (n.parent) n = n.parent;
    return n === this.doc.top;
  }

  get textContent() { return this.childNodes.map((n) => n.textContent).join(""); }
  set textContent(value) { this.replaceChildren(String(value)); }

  setAttribute(name, value) {
    this.attrs[name] = String(value);
    if (BOOLEAN.has(name)) this[name] = true;
    if (name === "value") this.value = String(value);
    if (name.startsWith("data-")) this.dataset[name.slice(5)] = String(value);
  }

  getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; }
  removeAttribute(name) {
    delete this.attrs[name];
    if (BOOLEAN.has(name)) this[name] = false;
  }

  append(...nodes) {
    for (const item of nodes) {
      const node = typeof item === "string" ? new Text(item) : item;
      if (!(node instanceof Element || node instanceof Text)) throw new Error(`append of a non-node: ${item}`);
      if (node.parent) node.parent.childNodes.splice(node.parent.childNodes.indexOf(node), 1);
      node.parent = this;
      this.childNodes.push(node);
    }
  }

  replaceChildren(...nodes) {
    for (const node of this.childNodes) node.parent = null;
    this.childNodes = [];
    this.append(...nodes);
  }

  * walk() {
    for (const child of this.children) {
      yield child;
      yield* child.walk();
    }
  }

  matches(selector) {
    const token = /([a-zA-Z][\w-]*)|\.([\w-]+)|#([\w-]+)|\[([\w-]+)(?:="([^"]*)")?\]|(:checked)/y;
    let ok = true;
    for (let i = 0; i < selector.length; i = token.lastIndex) {
      token.lastIndex = i;
      const m = token.exec(selector);
      if (!m) throw new Error(`stub DOM: unsupported selector ${selector}`);
      if (m[1]) ok &&= this.tagName === m[1].toUpperCase();
      else if (m[2]) ok &&= this.classList.contains(m[2]);
      else if (m[3]) ok &&= this.id === m[3];
      else if (m[4]) ok &&= m[5] === undefined ? m[4] in this.attrs : this.attrs[m[4]] === m[5];
      else ok &&= this.checked;
    }
    return ok;
  }

  closest(selector) {
    for (let n = this; n instanceof Element; n = n.parent) if (n.matches(selector)) return n;
    return null;
  }

  querySelectorAll(selector) { return [...this.walk()].filter((n) => n.matches(selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  contains(node) {
    for (let n = node; n; n = n.parent) if (n === this) return true;
    return false;
  }

  addEventListener(type, fn, options) { (this.listeners[type] ||= []).push({ fn, once: Boolean(options && options.once) }); }

  dispatchEvent(event) {
    event.target ||= this;
    event.preventDefault ||= () => { event.defaultPrevented = true; };
    for (let n = this; n; n = event.bubbles ? n.parent : null) {
      const list = (n.listeners && n.listeners[event.type]) || [];
      n.listeners && (n.listeners[event.type] = list.filter((l) => !l.once));
      for (const l of list) l.fn(event);
    }
    return !event.defaultPrevented;
  }

  click() {
    if (this.disabled) return;
    if (this.tagName === "INPUT" && this.type === "checkbox") this.checked = !this.checked;
    if (this.tagName === "INPUT" && this.type === "radio") {
      for (const other of this.doc.querySelectorAll(`input[name="${this.name}"]`)) other.checked = false;
      this.checked = true;
    }
    const proceed = this.dispatchEvent({ type: "click", bubbles: true });
    const form = this.tagName === "BUTTON" && this.type === "submit" ? this.closest("form") : null;
    if (!form || !proceed) return;
    if (form.getAttribute("method") === "dialog") form.closest("dialog").close(this.value);
    else form.dispatchEvent({ type: "submit", bubbles: true });
  }

  focus() { this.doc.activeElement = this; }
  scrollIntoView() {}
  getBoundingClientRect() { return { top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 }; }

  show() { this.open = true; }

  showModal() {
    if (this.open) throw new Error("InvalidStateError: dialog already open");
    this.open = true;
  }

  close(value) {
    if (!this.open) return;
    this.open = false;
    if (value !== undefined) this.returnValue = value;
    this.dispatchEvent({ type: "close" });
  }

  reset() {
    for (const n of this.walk()) {
      if (n.tagName === "TEXTAREA") n.value = "";
      if (n.tagName === "INPUT") n.checked = "checked" in n.attrs;
    }
  }
}

function makeDocument() {
  const doc = { activeElement: null };
  doc.createElement = (tag) => new Element(doc, tag);
  doc.top = new Element(doc, "#document");
  const stack = [doc.top];
  const tokens = /<!--[\s\S]*?-->|<!doctype[^>]*>|<\/([\w-]+)\s*>|<([\w-]+)((?:\s+[^\s=>/]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+))?)*)\s*\/?>|([^<]+)/gi;
  for (const m of html.matchAll(tokens)) {
    const current = stack[stack.length - 1];
    if (m[1]) {
      const name = m[1].toUpperCase();
      while (stack.length > 1 && stack.pop().tagName !== name);
    } else if (m[2]) {
      const node = new Element(doc, m[2]);
      for (const a of m[3].matchAll(/([^\s=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g)) {
        node.setAttribute(a[1].toLowerCase(), decode(a[2] ?? a[3] ?? a[4] ?? ""));
      }
      current.append(node);
      if (!VOID.has(node.tagName)) stack.push(node);
    } else if (m[4] !== undefined) {
      current.append(decode(m[4]));
    }
  }
  doc.documentElement = doc.top.querySelector("html");
  doc.body = doc.top.querySelector("body");
  doc.activeElement = doc.body;
  doc.getElementById = (id) => [...doc.top.walk()].find((n) => n.id === id) || null;
  doc.querySelectorAll = (selector) => doc.top.querySelectorAll(selector);
  doc.querySelector = (selector) => doc.top.querySelector(selector);
  return doc;
}

// ---------- browser stand-ins: cookie jar, fetch hooks, page reloads ----------

let cookie = "";
let currentPage = 0;
let currentDoc = null;
const requests = [];
const hooks = []; // one-shot {prefix, before?: async fn, rewrite?: fn(json) -> json}
const errors = [];
process.on("unhandledRejection", (err) => errors.push(err));

async function liveFetch(p, init = {}) {
  const line = `${init.method || "GET"} ${p}`;
  requests.push(line);
  const hook = hooks.find((h) => line.startsWith(h.prefix));
  if (hook) hooks.splice(hooks.indexOf(hook), 1);
  if (hook && hook.before) await hook.before();
  const headers = { ...(init.headers || {}), Origin: base };
  if (cookie) headers.Cookie = cookie;
  let response = await fetch(base + p, { ...init, headers, credentials: undefined });
  const setCookie = response.headers.get("set-cookie");
  if (setCookie) cookie = setCookie.split(";")[0].replace(/^al_ws=""$/, "");
  if (hook && hook.rewrite) {
    const data = hook.rewrite(await response.json());
    response = new Response(JSON.stringify(data), { status: response.status, headers: { "content-type": "application/json" } });
  }
  return response;
}

function boot() {
  const page = ++currentPage;
  const doc = makeDocument();
  currentDoc = doc;
  const window = {
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    localStorage: { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); } },
    setTimeout, clearTimeout, addEventListener() {}, innerWidth: 1280, innerHeight: 900,
  };
  // A reloaded page is gone: its pending pollers never hear back and never consume hooks.
  const pageFetch = (p, init) => (page === currentPage ? liveFetch(p, init) : new Promise(() => {}));
  vm.runInContext(appSource, vm.createContext({ document: doc, window, fetch: pageFetch, Blob, File, console }), { filename: "app.js" });
  return { doc, $: (id) => doc.getElementById(id) };
}

// ---------- checks ----------

let passed = 0;
let failed = 0;
const sleep = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); });
const norm = (s) => s.replace(/\s+/g, " ").trim();
const text = (node) => (node ? norm(node.textContent) : "");

function check(ok, label) {
  if (ok) passed += 1;
  else failed += 1;
  console.log(`${ok ? "ok  " : "FAIL"} ${label}`);
  if (!ok && currentDoc) {
    console.log(`     status: ${text(currentDoc.getElementById("status"))}`);
    console.log(`     last requests: ${requests.slice(-6).join(" | ")}`);
  }
  return ok;
}

async function until(fn, label, ms = 10000) {
  const end = Date.now() + ms;
  for (;;) {
    let ok = false;
    try {
      ok = Boolean(fn());
    } catch {
      ok = false;
    }
    if (ok || Date.now() > end) return check(ok, label);
    await sleep(25);
  }
}

const isHidden = (node) => {
  for (let n = node; n; n = n.parent) if (n.hidden) return true;
  return false;
};
const visible = (page, id) => Boolean(page.$(id)) && !isHidden(page.$(id));
const button = (page, label) => page.doc.querySelectorAll("button").find((b) => text(b) === label && !isHidden(b)) || null;
const dialogButton = (page, dialogId, value) => page.$(dialogId).querySelectorAll("button").find((b) => b.value === value);
const typeInto = (area, value) => {
  area.value = value;
  area.dispatchEvent({ type: "input", bubbles: true });
};
const hasTag = (node, label, tone) => node.querySelectorAll(".tag").some((t) => text(t) === label && t.classList.contains(tone));
const ordered = (lines, from = 0) => {
  let i = from;
  return lines.every((line) => (i = requests.indexOf(line, i)) !== -1);
};
const substep = (page, test) => page.$("substeps").children.filter(test).map((li) => text(li));

function upload(page, name, bytes) {
  const input = page.$("file-input");
  input.files = [new File([bytes], name)];
  input.dispatchEvent({ type: "change" });
  page.$("upload-btn").click();
}

async function selectCandidate(page, id, ratings) {
  page.$(`cand-${id}`).click();
  for (const [axis, value] of Object.entries(ratings)) page.$(`sa-${axis}-${value}`).click();
  page.$("sa-submit").click();
  await until(() => page.$("select-dialog").open, `select ${id}: confirmation dialog`);
  dialogButton(page, "select-dialog", "confirm").click();
  await until(() => visible(page, "selection"), `select ${id}: selection shown`);
}

// ---------- static checks ----------

check(!/innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function/.test(appSource), "app.js has no HTML or code sinks");
check(!/https?:\/\//.test(appSource) && !/src="https?:|<link[^>]*href="https?:/.test(html) && !/url\(|@import/.test(css), "no external requests");
const outLinks = [...html.matchAll(/<a [^>]*href="https?:[^>]*>/g)].map((m) => m[0]);
check(outLinks.length === 1 && outLinks[0].includes('href="https://code.claude.com/docs/en/sessions"') && outLinks[0].includes('target="_blank"')
  && outLinks[0].includes('rel="noopener noreferrer"'), "the only outside address is the official sessions docs link, opened in a new tab without referrer");

// ---------- 1. fresh page ----------

let page = boot();
await until(() => requests.includes("GET /api/workspace"), "fresh page asks for its workspace");
await sleep(100);
check(isHidden(page.$("step-mirror")) && page.$("step-whatif").classList.contains("is-pending"), "no workspace: upload step, step 3 pending");
await until(() => text(page.$("modes")).startsWith("제공자 모드:"), "footer: provider modes from /api/health");
// Storage is blocked in this stub, so every boot counts as a first visit and opens the tour.
check(page.$("tour").open && text(page.$("tour-count")) === "1 / 6" && page.doc.activeElement === page.$("tour-title") && page.$("tour-prev").disabled,
  "tour: opens on a first visit, focused, step 1 of 6");
page.$("tour-next").click();
check(text(page.$("tour-count")) === "2 / 6" && text(page.$("tour-body")).includes("의사결정 후보") && page.$("upload-title").classList.contains("tour-target"),
  "tour: 다음 moves on and marks its target");
page.$("tour").dispatchEvent({ type: "keydown", key: "Escape" });
check(!page.$("tour").open && !page.$("upload-title").classList.contains("tour-target"), "tour: Escape closes it");
page.$("tour-open").click();
check(page.$("tour").open && text(page.$("tour-count")) === "1 / 6", "tour: 사용 안내 reopens it at step 1");
page.$("tour").querySelector("[data-tour-close]").click();
check(!page.$("tour").open, "tour: 안내 닫기 closes it");
const modeTags = page.$("modes").querySelectorAll(".tag");
check(modeTags.length === 5 && modeTags.every((t) => text(t) === "모의" && t.classList.contains("tag-mock")) && text(page.$("modes")).includes("DeepSeek 모의"), "footer: every provider shown as mock");

// ---------- 1b. upload help, size pre-check ----------

const helps = page.doc.querySelectorAll("details").filter((d) => d.classList.contains("help"));
check(helps.map((d) => text(d.querySelector("summary"))).join(",") === "Claude Code 세션이란?,Claude Code 세션 파일 찾는 방법,파일을 찾지 못했다면,파일 크기와 보안 안내"
  && helps.every((d) => !d.open), "help: four sections, all collapsed by default");
const what = text(page.$("help-what"));
check(["Anthropic", "터미널이나 IDE", "내가 AI에게 한 요청", "AI가 고친 코드", "실행한 명령과 그 결과", "이해", "검증", "통제", "claude.ai 웹사이트나 앱에서 나눈 일반 채팅 기록은"].every((s) => what.includes(s)),
  "help: what Claude Code and a session are, what is analyzed, claude.ai chat is different");
const [unixTab, winTab] = page.$("os-tabs").querySelectorAll('[role="tab"]');
check(page.$("os-tabs").getAttribute("role") === "tablist" && unixTab.getAttribute("aria-selected") === "true" && winTab.getAttribute("aria-selected") === "false"
  && winTab.getAttribute("tabindex") === "-1" && !isHidden(page.$("os-panel-unix")) && page.$("os-panel-windows").hidden
  && page.$("os-panel-windows").getAttribute("aria-labelledby") === "os-tab-windows", "OS switch: Linux · macOS selected by default, Windows panel hidden");
winTab.click();
check(winTab.getAttribute("aria-selected") === "true" && winTab.getAttribute("tabindex") === "0" && unixTab.getAttribute("tabindex") === "-1"
  && page.$("os-panel-unix").hidden && !page.$("os-panel-windows").hidden, "OS switch: clicking Windows shows its panel");
winTab.dispatchEvent({ type: "keydown", key: "ArrowRight" });
check(unixTab.getAttribute("aria-selected") === "true" && page.doc.activeElement === unixTab && !page.$("os-panel-unix").hidden, "OS switch: arrow keys move and focus (wrapping)");
unixTab.dispatchEvent({ type: "keydown", key: "End" });
check(page.doc.activeElement === winTab && !page.$("os-panel-windows").hidden, "OS switch: End selects the last tab");
unixTab.dispatchEvent({ type: "keydown", key: "Home" });
const cmd = (id) => page.$(id).textContent;
check(cmd("cmd-unix-dir") === "~/.claude/projects/" && cmd("cmd-unix-find") === "find ~/.claude/projects -type f -name '*.jsonl'"
  && cmd("cmd-unix-recent") === "ls -lt ~/.claude/projects/*/*.jsonl | head -20", "help: exact Linux · macOS path and commands");
check(cmd("cmd-win-dir") === "%USERPROFILE%\\.claude\\projects\\" && cmd("cmd-win-find") === 'Get-ChildItem "$env:USERPROFILE\\.claude\\projects" -Recurse -File -Filter *.jsonl'
  && cmd("cmd-win-recent").includes("| Sort-Object LastWriteTime -Descending"), "help: exact Windows path and PowerShell commands");
const find = text(page.$("help-find"));
check(find.includes("복사본을 만드세요") && find.includes("scp") && find.includes("subagents") && find.includes("CLAUDE_CONFIG_DIR") && find.includes("30일"),
  "help: copy-not-edit, remote server, subagent files, config dir and cleanup");
check(find.includes("/export") && find.includes("일반 텍스트라 분석할 수 없습니다"), "help: /export files are not the JSONL input");
check(page.$("docs-sessions").getAttribute("target") === "_blank" && page.$("docs-sessions").getAttribute("rel") === "noopener noreferrer"
  && text(page.$("docs-sessions")).includes("새 탭에서 열림"), "help: docs link opens a new tab and says so");
const copyButtons = page.doc.querySelectorAll("button").filter((b) => b.getAttribute("data-copy"));
check(copyButtons.length === 9 && copyButtons.every((b) => page.$(b.getAttribute("data-copy")) && b.getAttribute("aria-label").includes("복사")), "help: a labelled copy button per command");
copyButtons[1].click(); // the stub has no Clipboard API, like plain HTTP on a public address
await until(() => text(page.$("status")).includes("복사하지 못했습니다") && text(copyButtons[1]) === "복사 안 됨", "copy: fails gracefully without the Clipboard API");
const safety = text(page.$("help-safety"));
check(["32MiB 이하", "API 키, 비밀번호", "모든 민감 정보가 지워진다고 보장하지 않습니다", "로그인이 없고", "HTTP", "안전한 서비스가 아닙니다", "공개 세션이나 합성 연습 세션만",
  "내 컴퓨터를 검색하거나 파일을 모으지 않습니다"].every((s) => safety.includes(s)), "help: size and security notes");
const missing = text(page.$("help-missing"));
check(missing.includes("fixtures/sessions/demo_weather.jsonl") && missing.includes("fixtures/sessions/many_requests.jsonl") && missing.includes("echo $CLAUDE_CONFIG_DIR"),
  "help: when the file is missing, and where the demo files are");
const MIB = 1024 * 1024;
const pick = (file) => {
  page.$("file-input").files = file ? [file] : [];
  page.$("file-input").dispatchEvent({ type: "change" });
};
pick(new File([new Uint8Array(32 * MIB + 1)], "big.jsonl"));
check(text(page.$("upload-error")).includes("32MiB를 넘습니다") && page.$("upload-btn").disabled, "pre-check: 33,554,433 bytes refused before sending");
pick(new File([new Uint8Array(32 * MIB)], "edge.jsonl"));
check(isHidden(page.$("upload-error")) && !page.$("upload-btn").disabled, "pre-check: exactly 33,554,432 bytes may be sent");
pick(null);

// ---------- 2. upload (the demo button) and select ----------

page.$("demo-btn").click();
await until(() => page.$("candidate-list").children.length === 3, "demo button: 3 candidate cards");
check(ordered(["GET /api/demo-session", "POST /api/workspaces"]) && text(page.$("file-info")).startsWith("demo_weather.jsonl"), "demo button: fetches the demo file, then the normal upload");
const guide = text(page.$("candidate-guide"));
check(visible(page, "candidate-guide") && ["무엇을 고르는 건가요", "중요한 기술적 선택", "그다음 모든 단계가 그 결정 하나", "기록 확인 예측", "결과 공개",
  "Claude 심층 분석 + 개발 과정 리포트", "독립 검토", "가정 상황(What-if)", "피드백과 재제출", "선택은 바꿀 수 없어요", "다시 올려"].every((s) => guide.includes(s)),
  "2-1: the candidate explainer says what a candidate is, what follows, and why the choice is locked");
check(["understanding", "verification", "control"].every((a) => text(page.$(`sa-${a}-help`)).endsWith("?")), "2-1: each self-assessment axis has a plain question");
const card1 = page.$("candidate-list").children[0];
check(text(card1).includes("외부 API·네트워크를 어떻게 부르고") && card1.querySelector(".badge-meaning") && card1.querySelector("details.ev-meta")
  && !card1.querySelector("details.ev-meta").open && text(card1.querySelector("details.ev-meta")).includes("JSONL"),
  "2-1: card explains its category and badges; the source position is folded into 상세 정보");
await selectCandidate(page, "c1", { understanding: 2, verification: 1, control: 1 });
check(isHidden(page.$("candidate-guide")), "2-1: the explainer folds away once a candidate is locked");
await until(() => visible(page, "predict-form"), "2-2: prediction form after selecting a retro candidate");
const pre = text(page.$("retro-block"));
check(pre.includes("사전 맥락") && pre.includes("E0010") && pre.includes("pytest -q") && !pre.includes("1 failed"), "2-2: pre-context without the result");
const preHeads = page.$("retro-block").querySelectorAll(".evidence-head").map(text);
check(preHeads.includes("내 요청") && preHeads.some((h) => h.startsWith("수정된 코드")) && preHeads.includes("실행한 명령") && preHeads.every((h) => !/E00|턴|JSONL/.test(h)),
  "2-2: records named in human words; IDs and positions only in 상세 정보");
// Blinding: nothing about the recorded result anywhere on the page before the prediction.
const whole = text(page.doc.top);
check(!/1 failed|3 passed|TypeError|Exit code|종료 코드|기록된 실패|일치/.test(whole), "2-2: no test result text anywhere in the DOM before the prediction");
check(text(page.$("predict-question")).includes("어떤 결과였을까요"), "2-2: question from the server");
check(isHidden(page.$("reveal-section")) && isHidden(page.$("analysis-section")), "2-3 and 2-4 hidden while blinded");
check(substep(page, (li) => li.getAttribute("aria-current") === "step")[0] === "2-2 기록 확인 예측", "substep 2-2 current");

// ---------- 3. 2-2 prediction and 2-3 reveal ----------

page.$("predict-submit").click();
await until(() => text(page.$("predict-error")).includes("하나를 골라"), "2-2: a choice is required");
page.$("predict-fail").click();
const reason = "시간 초과면 None이 넘어가 형식화에서 실패할 것 같습니다.";
typeInto(page.$("predict-reason"), reason);
check(text(page.$("predict-reason-count")) === `${reason.length} / 1,000자`, "2-2: reason counter");
page.$("predict-submit").click();
await until(() => page.$("predict-dialog").open, "2-2: confirm step opens");
check(text(page.$("predict-dialog")).includes("바꿀 수 없습니다") && text(page.$("predict-dialog-choice")) === "내 예측: 실패", "2-2: confirm says the answer cannot be changed");
dialogButton(page, "predict-dialog", "confirm").click();
await until(() => visible(page, "reveal-section"), "2-3: reveal shown");
const reveal = text(page.$("reveal-block"));
check(hasTag(page.$("reveal-block"), "기록과 비교: 일치", "tag-ok"), "2-3: label badge with text and colour");
check(reveal.includes("pytest -q") && reveal.includes("1 (명시)") && reveal.includes("1 failed, 3 passed in 0.42s"), "2-3: command, exit code with source, summary line");
check(reveal.includes("종료 코드: 1 (명시)") && reveal.includes("요약 줄: 1 failed, 3 passed in 0.42s"), "2-3: checkpoints");
check(Boolean(page.$("reveal-block").querySelector(".code-viewer")) && reveal.includes("TypeError"), "2-3: output excerpt in the log viewer");
check(reveal.includes(reason), "2-3: own reason shown back");
check(reveal.includes("검증 능력이 높다는 뜻은 아니며"), "2-3: matching is not a measure of verification ability");
check(isHidden(page.$("predict-form")) && visible(page, "analysis-section"), "2-3: form gone, 2-4 open");
check(text(page.$("evidence-list")).includes("이후 수정 있음(정답 아님)"), "2-4: later-fix tag on evidence");
const chapters = page.$("evidence-list").querySelectorAll("h5").map((h) => text(h.querySelector(".chapter-title")));
check(chapters.join(",") === "무엇을 시켰나,어떻게 고쳤나,어떻게 확인했나" && text(page.$("evidence-plot")).includes("흐름 한눈에"),
  "2-4: evidence told as a story in session order, with a plot strip");
const heads = page.$("evidence-list").querySelectorAll(".evidence-head").map(text);
check(["내 요청", "AI의 설명", "수정된 코드", "실행한 명령", "명령 결과"].every((n) => heads.some((h) => h.startsWith(n))) && heads.every((h) => !/E00|턴 \d|JSONL/.test(h)),
  "2-4: human record names, no IDs or line numbers by default");
const meta = page.$("ev-E0010").querySelector("details.ev-meta");
check(meta && !meta.open && meta.querySelectorAll("dd").map(text).join(" | ") === "E0010 | 턴 2 · JSONL 16번째 줄 | 사용자 요청",
  "2-4: ID, position and the original kind label folded into 상세 정보");
const order = page.$("evidence-list").querySelectorAll("article").map((a) => a.id);
check(order.join(",") === [...order].sort().join(",") && order.length === 10, "2-4: every visible record kept, in session order");

// ---------- 4. consent dialog and 2-4 analysis ----------

button(page, "심층 분석 시작").click();
await until(() => page.$("consent-dialog").open, "2-4: consent dialog opens");
const dialog = page.$("consent-dialog");
const consentText = text(dialog);
let boxes = dialog.querySelectorAll('input[type="checkbox"]');
check(text(page.$("consent-stage")) === "심층 분석", "consent: stage label");
check(page.$("consent-list").children.length === 4 && consentText.includes("6개 미만이라 검색을 생략합니다"), "consent: every provider, skipped ones with the reason");
check(boxes.length === 2 && boxes.every((b) => !b.checked), "consent: one unchecked box per applicable provider");
check(consentText.includes("모의 응답(실제로 보내지 않음)") && hasTag(dialog, "모의 응답(개발용)", "tag-mock"), "consent: destination and mock badge");
check(consentText.includes("API 데이터는 모델 학습에 쓰지 않음(공식 자료)") && consentText.includes("보관 여부 미확인"), "consent: policy lines");
check(/[\d,]+자 · 항목 \d+개/.test(consentText) && consentText.includes("화면 표시용 맥락 요약"), "consent: chars and purpose");
check(dialog.querySelectorAll("details").length === 2 && dialog.querySelectorAll("pre").some((p) => p.textContent.includes("<session_evidence>")), "consent: collapsible exact payload text");
page.$("consent-all").click();
check(boxes.every((b) => b.checked), "consent: 모두 선택");
hooks.push({ prefix: "POST /api/analysis", before: async () => check(page.$("main").getAttribute("aria-busy") === "true" && page.$("predict-submit").disabled, "buttons disabled while a request runs") });
hooks.push({
  prefix: "GET /api/jobs/",
  before: async () => {
    await until(() => page.$("main").getAttribute("aria-busy") === "false", "request finished before the first poll");
    check(text(page.$("job-analysis")).includes("대기"), "job line shows the queued job");
    check(page.doc.querySelectorAll(".js-lock").every((b) => b.disabled), "action buttons disabled while a job runs");
  },
});
dialogButton(page, "consent-dialog", "confirm").click();
await until(() => page.$("analysis-result").children.length > 0, "2-4: analysis result rendered", 15000);
check(ordered(["POST /api/preview", "POST /api/consent", "POST /api/analysis"], requests.lastIndexOf("POST /api/preview")), "2-4: preview, consent, then analysis");
const analysis = text(page.$("analysis-result"));
check(page.$("analysis-result").querySelectorAll("h5").map(text).join(",") === "결정 이유,대안,검증되지 않은 가정,잠재 위험,확인할 점", "2-4: five sections with server labels");
check(hasTag(page.$("analysis-result"), "확인된 기록", "tag-ok") && hasTag(page.$("analysis-result"), "모델 추정", "tag-info") && hasTag(page.$("analysis-result"), "검증 필요", "tag-warn"), "2-4: grade badges with text and colour");
check(page.$("analysis-result").querySelectorAll("blockquote").some((q) => text(q).includes("API가 느리면 멈추지 않게")), "2-4: quotes");
check(analysis.includes("진술 6개") && analysis.includes("[모의 응답]") && analysis.includes("모의 응답(개발용)"), "2-4: decision summary, stats, mock badge");
check(text(page.$("search-state")) === "관련 요청 검색: 검색 생략", "2-4: search state");
const jobLine = text(page.$("job-analysis"));
check(jobLine.includes("완료") && jobLine.includes("mock-claude-opus-5-5") && jobLine.includes("모의 응답(개발용)"), "2-4: job line with status, model and mock badge");
check(page.$("job-analysis").getAttribute("aria-live") === "polite", "2-4: job line is an aria-live region");

// ---------- 4a. the development-process report: auto-started with the same consent, five sections ----------

await until(() => requests.includes("POST /api/report"), "2-4: the report starts right after the analysis");
await until(() => text(page.$("job-report")).includes("완료") && text(page.$("job-report")).includes("개발 과정 리포트"),
  "2-4: report job done with its name", 15000);
await until(() => Boolean(button(page, "가정 상황 만들기")), "2-4: the What-if is offered once the report settles");
const sectionOrder = page.$("analysis-section").children.map((n) => n.id).filter(Boolean);
const orderIndex = (id) => sectionOrder.indexOf(id);
check(orderIndex("report-summary-card") < orderIndex("analysis-result") && orderIndex("analysis-result") < orderIndex("report-prompt-card")
  && orderIndex("report-prompt-card") < orderIndex("report-gaps-card") && orderIndex("report-gaps-card") < orderIndex("report-actions-card"),
  "리포트 순서: 세션 요약 → 심층 분석 → 프롬프트 개선 → 보완 포인트 → 개선 행동");
check(visible(page, "report-summary-card") && text(page.$("report-summary-head")).includes("개발 세션 요약")
  && hasTag(page.$("report-summary-card"), "모의 응답(개발용)", "tag-mock"), "리포트 카드: 개발 세션 요약, 모의 배지");
const reportSummary = text(page.$("report-summary-body"));
check(["요청한 주요 작업", "기록된 코드 변경", "중요한 기술적 의사결정", "수행된 테스트와 검증", "기록만으로 확인할 수 없는 부분"]
  .every((s) => reportSummary.includes(s)) && page.$("report-summary-body").querySelectorAll(".chip-ev").length > 0
  && hasTag(page.$("report-summary-body"), "확인된 기록", "tag-ok") && reportSummary.includes("진술"),
  "리포트 요약: 네 진술 그룹 + 확인 못 하는 부분, 근거 칩과 등급");
check(text(page.$("report-prompt-body")).includes("요구사항 명확성") && text(page.$("report-prompt-body")).includes("제안:")
  && text(page.$("report-prompt-card")).includes("맥락이 이미 충분하다면 짧은 요청을 문제 삼지 않습니다"),
  "리포트 프롬프트: 세부 항목, 관찰(근거 인용), 제안, 맥락 고려 안내");
check(text(page.$("report-gaps-body")).includes("예외·검증 기록 부족")
  && text(page.$("report-gaps-body")).includes("기록에 없다는 뜻은 아닙니다") && hasTag(page.$("report-gaps-body"), "모델 추정", "tag-info"),
  "리포트 보완 포인트: 종류, 가능성 표현(기록 없음≠안 함), 등급 배지");
check(text(page.$("report-actions-body")).includes("AI에게 추가로 확인 요청")
  && text(page.$("report-actions-body")).includes("테스트·검증 방법") && page.$("report-actions-body").querySelectorAll(".chip-ev").length > 0,
  "리포트 개선 행동: 종류 두 가지 이상과 근거 칩");
check(text(page.$("report-summary-card")).includes("세션 개발자와 현재 사용자를 다른 사람으로 다루며")
  && !/\b\d+\s*점\b|정답률|등급:/.test(text(page.$("report-summary-card")) + text(page.$("report-gaps-card"))),
  "리포트 원칙 문구, 점수·등급 없음");
await until(() => text(page.$("summary-body")).includes("[사용자 요청]"), "2-4: Local LLM summary card");
check(text(page.$("summary-card")).includes("화면 표시용"), "2-4: summary labelled display-only");
const chip = page.$("analysis-result").querySelector(".chip-ev");
const chipId = chip.getAttribute("data-ev");
check(!/E00/.test(text(chip)) && text(chip) === "내 요청" && chip.getAttribute("aria-label").includes("원본 기록으로 이동"), "evidence chip names the record, not its ID");
chip.click();
check(page.$(`ev-${chipId}`).classList.contains("is-target") && page.doc.activeElement === page.$(`ev-${chipId}`) && visible(page, "ev-back") && !page.$("evidence-dialog").open,
  "evidence chip jumps to and highlights the original below");
page.$("ev-back").click();
check(page.doc.activeElement === chip && isHidden(page.$("ev-back")), "돌아가기 returns focus to the statement's chip");
// Hover link: a statement previews its records and marks them below.
const stmt = page.$("analysis-result").querySelectorAll("li").find((li) => li.classList.contains("is-linked") && li.querySelector(".chip-ev").getAttribute("data-ev") === "E0012");
stmt.dispatchEvent({ type: "mouseenter" });
check(visible(page, "ev-preview") && text(page.$("ev-preview")).includes("수정된 코드") && text(page.$("ev-preview")).includes("근거 ID E0012")
  && page.$("ev-E0012").classList.contains("is-cited") && stmt.getAttribute("aria-describedby") === "ev-preview", "hover: a statement previews its cited record and marks it below");
stmt.dispatchEvent({ type: "mouseleave" });
check(isHidden(page.$("ev-preview")) && !page.$("ev-E0012").classList.contains("is-cited"), "hover: leaving hides the preview");
stmt.querySelector(".statement-text").click();
check(page.$("ev-E0012").classList.contains("is-target"), "click: clicking a statement jumps to its record");
page.$("ev-back").click();
// Badges: every grade badge carries its meaning.
const gradeTags = page.$("analysis-result").querySelectorAll(".tag").filter((t) => ["확인된 기록", "모델 추정", "검증 필요", "인용 미확인"].includes(text(t)));
check(gradeTags.length >= 6 && gradeTags.every((t) => t.getAttribute("title") && text(t.parent.querySelector(".badge-meaning")).length > 10),
  "badges: every grade badge has a one-line meaning (title and screen-reader text)");
check(page.$("grade-legend").children.length === 4 && text(page.$("grade-guide")).includes("확인이 이 화면의 핵심"), "badges: a first-time legend above the analysis");
// 설명 더 보기
check(!page.doc.body.classList.contains("explain-on") && page.$("explain-toggle").getAttribute("aria-pressed") === "false", "explain: off by default");
page.$("explain-toggle").click();
check(page.doc.body.classList.contains("explain-on") && page.$("explain-toggle").getAttribute("aria-pressed") === "true" && text(page.$("explain-toggle")) === "설명 접기",
  "explain: the toggle turns guidance on");
check(page.$("analysis-result").querySelectorAll("li").filter((li) => li.classList.contains("statement")).every((li) => li.querySelector(".explain"))
  && text(page.$("report-actions-body")).includes("이건 제안이지 정답이 아닙니다"), "explain: every statement and suggestion has guidance");
page.$("explain-toggle").click();
check(!page.doc.body.classList.contains("explain-on"), "explain: and off again");

// ---------- 4b. independent reviews: a failed review and its retry, both reviews, the comparison ----------

check(visible(page, "review-section") && Boolean(button(page, "HCX-007 독립 검토")) && Boolean(button(page, "DeepSeek 독립 검토")), "review: both review buttons after the analysis");
check(isHidden(page.$("comparison")), "review: no comparison before a review");
const claudeSummary = text(page.$("analysis-result").querySelector(".lead-strong"));
// Mock reviews never fail: fake a rate-limited HCX-007 job in its poll and in the next workspace view. The retry then
// reaches the real server, whose job did finish, so it answers 409 already_submitted and the stored review loads.
let reviewJobId = "";
const rateLimited = (job) => Object.assign(job, {
  status: "failed", status_label: "실패", result: null,
  error: { code: "provider_rate_limited", message: "제공자 요청 한도에 걸렸습니다. 같은 요청을 자동으로 반복하지 않습니다.", retryable: true },
});
hooks.push({ prefix: "GET /api/jobs/", rewrite: (job) => { reviewJobId = job.id; return rateLimited(job); } });
hooks.push({
  prefix: "GET /api/workspace",
  rewrite: (view) => {
    rateLimited(view.jobs.find((j) => j.id === reviewJobId));
    Object.assign(view.results, { reviews: null, comparison: null });
    return view;
  },
});
const reviewAt = requests.length;
button(page, "HCX-007 독립 검토").click();
await until(() => page.$("consent-dialog").open, "review: HCX-007 consent dialog");
boxes = dialog.querySelectorAll('input[type="checkbox"]');
check(text(page.$("consent-stage")) === "HCX-007 독립 검토" && boxes.length === 1 && !boxes[0].checked, "review: HCX-007 stage, one unchecked box");
const reviewPayload = dialog.querySelector("pre").textContent;
check(reviewPayload.includes("<session_evidence>") && !reviewPayload.includes(claudeSummary), "review: the analysis bundle without Claude's output");
check(text(dialog).includes("데이터 정책 미확인") && !dialog.querySelector(".policy-warn"), "review: HCX-007 policy line, no Preview warning");
dialogButton(page, "consent-dialog", "confirm").click(); // nothing checked: declined
await until(() => text(page.$("review-actions")).includes("HCX-007 전송에 동의하지 않아 검토를 요청하지 않았습니다."), "review declined: note shown");
check(!requests.slice(reviewAt).includes("POST /api/reviews") && Boolean(button(page, "HCX-007 독립 검토")), "review declined: no review request, the preview can be reopened");
button(page, "HCX-007 독립 검토").click();
await until(() => page.$("consent-dialog").open, "review: HCX-007 consent dialog reopened");
page.$("consent-all").click();
dialogButton(page, "consent-dialog", "confirm").click();
await until(() => text(page.$("job-review-hcx")).includes("제공자 요청 한도에 걸렸습니다."), "failed review: server message on its job line");
check(hasTag(page.$("job-review-hcx"), "실패", "tag-danger") && text(page.$("job-review-hcx")).includes("1분쯤 기다린 뒤 직접 다시 시도해 주세요."), "failed review: status badge and next step");
check(page.$("analysis-result").children.length === 1 && page.$("review-list").children.length === 0 && isHidden(page.$("comparison")), "failed review: the analysis stays, no review result");
check(!button(page, "HCX-007 독립 검토") && Boolean(button(page, "DeepSeek 독립 검토")), "failed review: the other review is still offered");
const retryReview = page.$("job-review-hcx").querySelector("button");
check(text(retryReview) === "다시 시도" && retryReview.getAttribute("aria-label") === "HCX-007 독립 검토 다시 시도", "failed review: retry button names its job");
retryReview.click();
await until(() => text(page.$("review-error")).includes("이미 제출했습니다"), "review retry: server message shown");
await until(() => page.$("review-list").children.length === 1, "review retry: the stored review is loaded");
check(requests.slice(reviewAt).filter((r) => r === "POST /api/reviews").length === 2, "review: one request, one retry");
const hcxCard = page.$("review-list").children[0];
check(hasTag(hcxCard, "모델 의견(해석)", "tag-info") && hasTag(hcxCard, "모델 mock-hcx-007", "tag-muted") && hasTag(hcxCard, "모의 응답(개발용)", "tag-mock"), "HCX-007 card: 해석 label, model name, mock badge");
check(text(hcxCard).includes("HCX-007 독립 검토") && text(hcxCard).includes("[모의 응답] CLOVA HCX-007 독립 검토 형식의 예시"), "HCX-007 card: title and summary");
check(hasTag(hcxCard, "문제", "tag-muted") && hasTag(hcxCard, "판단", "tag-muted") && hasTag(hcxCard, "확인된 기록", "tag-ok"), "HCX-007 card: point types and grade badges");
check(hcxCard.querySelectorAll(".chip-ev").map((c) => c.getAttribute("data-ev")).join(",") === "E0012,E0010" && hcxCard.querySelectorAll("blockquote").length === 2, "HCX-007 card: evidence chips and quotes");
const groupTitles = () => page.$("comparison-body").querySelectorAll("h6").map(text).join(" | ");
check(visible(page, "comparison") && groupTitles() === "같은 근거를 지적 · 근거 2개 | 한쪽만 지적 · 근거 2개 | 비교 불가(유효한 근거 ID 없음) · 의견 1개", "comparison: three groups for Claude and HCX-007");
check(text(page.$("comparison")).includes("의견이 같아도 사실로 확정하지 않습니다."), "comparison: agreement is not truth");

button(page, "DeepSeek 독립 검토").click();
await until(() => page.$("consent-dialog").open, "review: DeepSeek consent dialog");
const warn = dialog.querySelector(".policy-warn");
check(text(page.$("consent-stage")) === "DeepSeek 독립 검토" && Boolean(warn) && dialog.querySelectorAll(".policy").length === 1, "review: DeepSeek stage, policy shown as a warning");
check(text(warn).includes("Public Preview: 입력·출력이 학습에 쓰일 수 있음(약관 §4.3)") && text(warn).includes("학습 거부·ZDR 미적용"), "review: Preview training and no opt-out/ZDR lines");
page.$("consent-all").click();
dialogButton(page, "consent-dialog", "confirm").click();
await until(() => page.$("review-list").children.length === 2, "review: DeepSeek review rendered", 15000);
const deepseekCard = page.$("review-list").children[1];
check(text(deepseekCard).includes("DeepSeek 독립 검토") && hasTag(deepseekCard, "모델 mock-deepseek-v4-1-flash-1017", "tag-muted") && hasTag(deepseekCard, "모의 응답(개발용)", "tag-mock"), "DeepSeek card: model name and mock badge");
check(isHidden(page.$("review-error")) && !button(page, "HCX-007 독립 검토") && !button(page, "DeepSeek 독립 검토"), "review: one review per provider");
check(groupTitles() === "같은 근거를 지적 · 근거 3개 | 한쪽만 지적 · 근거 1개 | 비교 불가(유효한 근거 ID 없음) · 의견 1개", "comparison: regrouped with DeepSeek");
const sharedByAll = page.$("comparison-body").querySelectorAll(".cmp-entry").find((e) => e.querySelector(".cmp-head").querySelector(".chip-ev").getAttribute("data-ev") === "E0012");
check(text(sharedByAll.querySelector(".cmp-head")).includes("Claude · HCX-007 · DeepSeek") && sharedByAll.querySelectorAll("dt").map(text).join(",") === "Claude,HCX-007,DeepSeek", "comparison: providers and their statements per evidence id");

// ---------- 5. step 3: What-if ----------

check(!page.$("step-whatif").classList.contains("is-pending") && isHidden(page.$("whatif-empty")), "3: step active after the analysis");
await until(() => Boolean(button(page, "가정 상황 만들기")), "3: the What-if button after the settled report");
button(page, "가정 상황 만들기").click();
await until(() => page.$("whatif-card").children.length === 1, "3: What-if card rendered", 15000);
const card = page.$("whatif-card");
check(hasTag(card, "가정 상황", "tag-hypo") && !card.querySelector(".tag-retro"), "3: 가정 상황 badge differs from the 기록 확인 badge");
check(hasTag(card, "난이도 표준", "tag-category") && text(card).includes("외부 응답이 평소보다 늦게 오는 상황"), "3: tier badge, title, scenario, changes");
check(card.querySelectorAll(".chip-ev").map((c) => c.getAttribute("data-ev")).join(",") === "E0010,E0012,E0015", "3: related evidence chips");
check(ANSWER_FIELDS.every((f) => text(page.$(`q-${f}`)).length > 0), "3: the four questions");
check(visible(page, "answer-form") && text(page.$("answer-submit")) === "피드백 받기", "3: answer form");
check(page.$("stepper").children.find((li) => li.getAttribute("aria-current") === "step").dataset.step === "3", "stepper at step 3");

// ---------- 6. feedback 1 ----------

page.$("answer-submit").click();
await until(() => text(page.$("answer-error")).includes("네 항목을 모두"), "3: empty answers rejected before any request");
const answers = {
  expected_behavior: "응답이 5초 안에 오지 않으면 None을 반환합니다.",
  potential_issue: "None 때문에 format_forecast에서 TypeError가 날 수 있습니다.",
  ai_instruction: "시간 초과면 안내 문구를 출력하도록 고치고 pytest로 확인해 주세요.",
  verification: "timeout 테스트를 pytest로 실행해 통과하는지 봅니다.",
};
for (const f of ANSWER_FIELDS) typeInto(page.$(`ans-${f}`), answers[f]);
check(text(page.$("ans-verification-count")) === `${answers.verification.length} / 2,000자`, "3: answer counters");
hooks.push({
  prefix: "GET /api/jobs/",
  before: async () => {
    await until(() => page.$("main").getAttribute("aria-busy") === "false", "feedback request finished before the first poll");
    check(page.$("answer-submit").disabled, "3: 피드백 받기 disabled while the feedback job runs");
  },
});
page.$("answer-submit").click();
await until(() => page.$("consent-dialog").open, "3: feedback consent dialog");
boxes = dialog.querySelectorAll('input[type="checkbox"]');
check(text(page.$("consent-stage")) === "첫 피드백" && boxes.length === 1 && !boxes[0].checked, "3: feedback1 preview, Claude unchecked");
check(dialog.querySelector("pre").textContent.includes(answers.potential_issue), "3: payload shows the answers");
page.$("consent-all").click();
dialogButton(page, "consent-dialog", "confirm").click();
await until(() => page.$("fb-title-1"), "3: first feedback rendered", 15000);
let feedbackText = text(page.$("feedback-list"));
check(["잘한 점", "보완할 점", "힌트:", "AI 지시 점검", "요구 조건: 들어 있음", "규칙 엔진 확인", "기록된 실패", "코칭"].every((s) => feedbackText.includes(s)), "3: per-field feedback, instruction review and rule check, coaching");
check(Boolean(page.$("feedback-list").querySelector("table")) && page.$("feedback-list").querySelector("tbody").children.length === 3 && feedbackText.includes("이해: 자기평가는 '할 수 있다', 이번 답변에서는"), "3: axes table, self-rating vs observed with the neutral message");
check(isHidden(page.$("answer-form")) && Boolean(button(page, "다시 제출(1회)")), "3: 다시 제출(1회) offered");

// ---------- 7. feedback 2 ----------

button(page, "다시 제출(1회)").click();
check(visible(page, "answer-form") && ANSWER_FIELDS.every((f) => page.$(`ans-${f}`).value === answers[f]), "3: answers prefilled for the resubmission");
check(text(page.$("answer-submit")) === "다시 제출하고 최종 피드백 받기", "3: resubmit label");
typeInto(page.$("ans-verification"), `${answers.verification} 실패하면 출력의 요약 줄을 확인합니다.`);
page.$("answer-submit").click();
await until(() => page.$("consent-dialog").open, "3: feedback2 consent dialog");
check(text(page.$("consent-stage")) === "최종 피드백", "3: feedback2 stage label");
page.$("consent-all").click();
dialogButton(page, "consent-dialog", "confirm").click();
await until(() => page.$("fb-title-2"), "3: final feedback rendered", 15000);
feedbackText = text(page.$("feedback-list"));
check(feedbackText.includes("첫 피드백과 비교") && feedbackText.includes("다음 난이도 제안") && feedbackText.includes("같게: 다음에도 같은 난이도로"), "3: final view with explanation and next difficulty");
check(isHidden(page.$("answer-form")) && !button(page, "다시 제출(1회)"), "3: no third submission");
// App and model text only: recorded session output (e.g. pytest's "[100%]") is evidence, not a score.
const generated = page.$("reveal-block").querySelectorAll(".code-viewer").map(text).reduce((all, recorded) => all.replace(recorded, ""),
  ["reveal-block", "analysis-result", "review-list", "comparison-body", "whatif-card", "feedback-list"].map((id) => text(page.$(id))).join(" "));
check(!/\d\s*(점|%)|점수:|정답률/.test(generated), "no scores or rates");
check(errors.length === 0, `no async errors (${errors.map(String).join("; ")})`);

// ---------- 8. reload: restore every step and resume a running job ----------

let summaryId = "";
const rawReply = "<b>검토</b> 형식이 깨진 {JSON";
hooks.push({
  prefix: "GET /api/workspace",
  rewrite: (view) => {
    // Pretend the page reloads while the summary job still runs, and HCX-007 replied in an unchecked format.
    const job = view.jobs.find((j) => j.kind === "summary");
    summaryId = job.id;
    Object.assign(job, { status: "running", status_label: "진행", progress: "Local LLM 요약 중" });
    view.results.summary = null;
    Object.assign(view.results.reviews.hcx, { format_ok: false, summary: "", points: [], text: rawReply });
    return view;
  },
});
const reloadAt = requests.length;
page = boot();
await until(() => requests.slice(reloadAt).includes(`GET /api/jobs/${summaryId}`), "restore: resumes polling of a running job");
await until(() => text(page.$("summary-body")).includes("[사용자 요청]"), "restore: polled job result rendered");
check(text(page.$("reveal-block")).includes("기록과 비교: 일치") && isHidden(page.$("predict-form")), "restore: prediction and reveal");
check(page.$("analysis-result").children.length === 1 && page.$("evidence-list").children.length > 0, "restore: analysis and evidence");
check(visible(page, "report-summary-card") && text(page.$("report-summary-head")).includes("개발 세션 요약")
  && text(page.$("report-gaps-body")).includes("예외·검증 기록 부족"), "restore: the report cards");
check(page.$("whatif-card").children.length === 1 && Boolean(page.$("fb-title-1")) && Boolean(page.$("fb-title-2")), "restore: What-if and both feedbacks");
check(isHidden(page.$("answer-form")) && !button(page, "다시 제출(1회)"), "restore: nothing left to submit");
check(substep(page, (li) => li.classList.contains("is-done")).length === 4, "restore: all four sub-steps done");
check(page.$("review-list").children.length === 2 && text(page.$("comparison-body")).includes("Claude · HCX-007 · DeepSeek") && !button(page, "HCX-007 독립 검토"), "restore: both reviews and the comparison");
const unchecked = page.$("review-list").children[0];
check(hasTag(unchecked, "형식 미확인", "tag-warn") && unchecked.querySelector("pre").textContent === rawReply && !unchecked.querySelector("b") && !unchecked.querySelector(".statement"), "restore: 형식 미확인 shows the raw reply as text");

// ---------- 8b. a failed summary: no retry when the server says so, else POST /api/summary ----------

// Faked in the workspace view (mock jobs never fail); the real summary finished, so the retry answers 409.
const failSummary = (error) => hooks.push({
  prefix: "GET /api/workspace",
  rewrite: (view) => {
    Object.assign(view.jobs.find((j) => j.kind === "summary"), { status: "failed", status_label: "실패", error });
    view.results.summary = { error };
    return view;
  },
});
failSummary({ code: "cap_exceeded", message: "이 흐름의 호출 상한에 도달했습니다.", retryable: false });
page = boot();
await until(() => text(page.$("job-summary")).includes("이 흐름의 호출 상한에 도달했습니다."), "failed summary: server message");
check(text(page.$("job-summary")).includes("다시 시도할 수 없습니다") && !page.$("job-summary").querySelector("button"), "failed summary, not retryable: says so, no retry button");
failSummary({ code: "concurrency", message: "다른 분석이 진행 중입니다. 잠시 뒤 다시 시도해 주세요.", retryable: true });
page = boot();
await until(() => Boolean(page.$("job-summary").querySelector("button")), "failed summary, retryable: retry button");
check(text(page.$("job-summary")).includes("요약이 없어도 다른 단계는 그대로 진행됩니다.") && text(page.$("summary-body")) === "", "failed summary: the flow goes on without it");
page.$("job-summary").querySelector("button").click();
await until(() => text(page.$("summary-error")).includes("이미 제출했습니다"), "summary retry: POST /api/summary answered by the server");
await until(() => text(page.$("summary-body")).includes("[사용자 요청]"), "summary retry: the stored summary is loaded");

// ---------- 9. Claude declined, a failed job and its retry, a lost workspace ----------

const declinedAt = requests.length;
upload(page, "no_tests.jsonl", read("fixtures/sessions/no_tests.jsonl"));
await until(() => page.$("candidate-list").children.length === 1, "no_tests upload");
await selectCandidate(page, "c1", { understanding: 0, verification: 0, control: 0 });
await until(() => visible(page, "analysis-section"), "ineligible candidate goes to 2-4");
check(text(page.$("retro-block")).includes("예측 없이 분석으로 넘어갑니다. 이유: 테스트 기록 없음"), "ineligible: reason shown");
check(substep(page, (li) => li.classList.contains("is-skipped")).join(",") === "2-2 기록 확인 예측 생략,2-3 결과 공개 생략", "ineligible: 2-2 and 2-3 greyed out as skipped");
button(page, "심층 분석 시작").click();
await until(() => page.$("consent-dialog").open, "declined: consent dialog");
page.$("consent-local_llm").click();
dialogButton(page, "consent-dialog", "confirm").click();
await until(() => text(page.$("analysis-intro")).includes("전송 동의가 없어 호출하지 않았습니다"), "declined: rule-engine-only notice");
check(Boolean(button(page, "전송 미리보기 다시 열기")) && page.$("analysis-result").children.length === 0, "declined: no analysis, consent can be reopened");
await until(() => text(page.$("summary-body")).length > 0, "declined: the consented Local LLM summary still runs");
check(text(page.$("whatif-empty")).includes("Claude 전송에 동의하지 않아"), "declined: step 3 explains");
check(isHidden(page.$("report-summary-card")) && !requests.slice(declinedAt).includes("POST /api/report"),
  "declined: no report without an Anthropic consent");

// Mock jobs never fail: fake one failed analysis job in its poll and in the next workspace view. The retry then
// reaches the real server, whose job did finish, so it answers 409 already_submitted.
const retryAt = requests.length;
let failedId = "";
const fail = (job) => Object.assign(job, {
  status: "failed", status_label: "실패", result: null,
  error: { code: "timeout", message: "응답 시간이 초과되었습니다.", retryable: true },
});
hooks.push({ prefix: "GET /api/jobs/", rewrite: (job) => { failedId = job.id; return fail(job); } });
hooks.push({ prefix: "GET /api/workspace", rewrite: (view) => { fail(view.jobs.find((j) => j.id === failedId)); view.results.analysis = null; return view; } });
button(page, "전송 미리보기 다시 열기").click();
await until(() => page.$("consent-dialog").open, "reopened consent dialog");
page.$("consent-anthropic").click();
dialogButton(page, "consent-dialog", "confirm").click();
await until(() => text(page.$("job-analysis")).includes("응답 시간이 초과되었습니다.") && text(page.$("job-analysis")).includes("다시 시도할 수 있습니다."), "failed job: error message and retry advice");
check(hasTag(page.$("job-analysis"), "실패", "tag-danger") && page.$("analysis-result").children.length === 0, "failed job: status badge, no result");
button(page, "다시 시도").click();
await until(() => text(page.$("analysis-error")).includes("이미 제출했습니다"), "retry: server error message shown");
await until(() => page.$("analysis-result").children.length === 1, "retry: the stored result is loaded");
// The recovered analysis is a consented one again, so the report auto-starts and must settle before step 3.
await until(() => requests.slice(retryAt).includes("POST /api/report"), "recovered analysis: the report auto-starts");
await until(() => text(page.$("report-summary-body")).includes("요청한 주요 작업"), "recovered analysis: report card", 15000);
await until(() => Boolean(button(page, "가정 상황 만들기")), "recovered analysis: the What-if button after the report");

await fetch(`${base}/api/workspace`, { method: "DELETE", headers: { Origin: base, Cookie: cookie } });
button(page, "가정 상황 만들기").click();
await until(() => text(page.$("upload-error")).includes("작업 공간이 없거나 만료되었습니다"), "lost workspace: server message shown");
check(isHidden(page.$("step-mirror")), "lost workspace: back to step 1");

// ---------- 10. insufficient evidence: no consent dialog, no model ----------

const thin = [
  { type: "user", message: { role: "user", content: "고쳐" } },
  { type: "assistant", message: { role: "assistant", content: [{ type: "tool_use", id: "t1", name: "Edit", input: { file_path: "a.py", old_string: "x", new_string: "y" } }] } },
].map((r) => JSON.stringify(r)).join("\n");
upload(page, "thin.jsonl", `${thin}\n`);
await until(() => text(page.$("candidate-list")).includes("근거 부족"), "insufficient: card shows the evidence level");
await selectCandidate(page, "c1", { understanding: 1, verification: 1, control: 1 });
await until(() => Boolean(button(page, "심층 분석 시작")), "insufficient: 2-4 start button");
const thinAt = requests.length;
button(page, "심층 분석 시작").click();
await until(() => text(page.$("analysis-intro")).includes("근거가 부족해 모델을 호출하지 않았습니다"), "insufficient: rule-engine-only notice");
check(requests.slice(thinAt).includes("POST /api/analysis") && !requests.slice(thinAt).some((r) => /preview|consent/.test(r)) && !page.$("consent-dialog").open, "insufficient: POST /api/analysis directly, no consent dialog");
check(text(page.$("search-state")).includes("근거 부족") && text(page.$("job-analysis")) === "" && isHidden(page.$("summary-card")), "insufficient: search state, no job");
check(text(page.$("whatif-empty")).includes("근거 수준이 '부족'"), "insufficient: step 3 explains");
check(errors.length === 0, `no async errors (${errors.map(String).join("; ")})`);

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
