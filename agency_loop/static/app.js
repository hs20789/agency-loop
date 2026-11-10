"use strict";

// Agency Loop UI. Every session or model string is set as text (append / textContent), never as HTML.
(() => {
  const MAX_UPLOAD_BYTES = 32 * 1024 * 1024; // the server cap (config.max_upload_bytes)
  const AXES = [
    ["understanding", "이해"],
    ["verification", "검증"],
    ["control", "통제"],
  ];
  const RATINGS = ["아직 어렵다", "부분적으로", "할 수 있다"];
  const CODE_KINDS = new Set(["command", "command_output", "tool_other"]);
  const FALLBACK_ERRORS = {
    0: "서버에 연결할 수 없습니다.",
    400: "Claude Code 세션 JSONL 형식이 아닙니다.",
    404: "작업 공간이 없거나 만료되었습니다. 세션을 다시 올려 주세요.",
    409: "후보는 이미 선택되어 바꿀 수 없습니다. 다른 후보는 세션을 다시 올려 분석해 주세요.",
    413: "파일이 32MiB를 넘습니다. 더 작은 세션 파일을 올려 주세요.",
    422: "분석할 수 있는 대화·코드 기록이 없습니다.",
    429: "요청이 너무 많습니다. 잠시 뒤 다시 시도해 주세요.",
    503: "서버 사용량 제한에 도달했습니다. 잠시 뒤 다시 시도해 주세요.",
  };
  const UPLOAD_HINTS = {
    400: "Claude Code가 남긴 세션 JSONL 파일인지 확인해 주세요. 읽을 수 있는 줄이 하나도 없으면 분석할 수 없습니다.",
    413: "32MiB 이하의 세션 파일(.jsonl) 1개만 올릴 수 있습니다.",
    422: "사람 요청이나 코드 변경이 기록된 세션인지 확인해 주세요.",
    429: "10분 동안 올릴 수 있는 횟수를 넘었습니다.",
    503: "서버의 작업 공간 수가 가득 찼습니다.",
  };
  const ANSWER_FIELDS = ["expected_behavior", "potential_issue", "ai_instruction", "verification"];
  const CHOICES = { pass: "통과", fail: "실패", unknown: "판단 불가" };
  const SUBSTEPS = ["2-1 후보 선택", "2-2 기록 확인 예측", "2-3 결과 공개", "2-4 심층 분석"];
  const POLL_MS = 1000;
  const FINAL = new Set(["done", "failed"]);
  const MOCK = "모의 응답(개발용)";
  const GRADE_TONE = { recorded: "ok", inferred: "info", needs_verification: "warn", citation_unverified: "danger" };
  const LABEL_TONE = { match: "ok", mismatch: "warn", withheld: "muted" };
  const STATS = [
    ["recorded", "확인된 기록"],
    ["inferred", "모델 추정"],
    ["needs_verification", "검증 필요"],
    ["citation_unverified", "인용 미확인"],
  ];
  // slot: the job line (#job-<slot>) that shows the job; heading: where focus goes when a finished job left it nowhere.
  const JOBS = {
    analysis: { name: "심층 분석", slot: "analysis", done: "심층 분석을 마쳤습니다.", heading: "analysis-title" },
    report: { name: "개발 과정 리포트", slot: "report", done: "개발 과정 분석 리포트를 마쳤습니다.", heading: "report-summary-title" },
    summary: { name: "Local LLM 요약", slot: "summary", done: "Local LLM 요약을 마쳤습니다." },
    "review:hcx": { name: "HCX-007 독립 검토", slot: "review-hcx", done: "HCX-007 독립 검토를 마쳤습니다.", heading: "review-title" },
    "review:deepseek": { name: "DeepSeek 독립 검토", slot: "review-deepseek", done: "DeepSeek 독립 검토를 마쳤습니다.", heading: "review-title" },
    whatif: { name: "가정 상황 생성", slot: "whatif", done: "가정 상황을 만들었습니다.", heading: "whatif-title" },
    feedback1: { name: "첫 피드백", slot: "feedback", done: "첫 피드백을 받았습니다.", heading: "fb-title-1" },
    feedback2: { name: "최종 피드백", slot: "feedback", done: "최종 피드백을 받았습니다.", heading: "fb-title-2" },
  };
  // Offered only for errors the server marks retryable; the server allows a new job only after a failed one.
  const RETRY = {
    analysis: () => startAnalysis(true),
    report: () => retryReport(),
    summary: () => retrySummary(),
    "review:hcx": () => startReview("hcx", true),
    "review:deepseek": () => startReview("deepseek", true),
    whatif: () => startWhatif(),
    feedback1: () => submitAnswers(),
    feedback2: () => submitAnswers(),
  };
  // The next step where the server's message leaves it out; null where the message already says it.
  const ADVICE = {
    budget_exceeded: "비용 상한은 운영자만 바꿀 수 있어 다시 시도할 수 없습니다.",
    ledger_missing: "장부가 준비되기 전에는 다시 시도할 수 없습니다.",
    halted: "운영자가 장부를 확인하기 전에는 다시 시도할 수 없습니다.",
    daily_cap: "하루 상한은 서버 시각 자정에 초기화되며, 그 전에는 다시 시도할 수 없습니다.",
    cap_exceeded: "이 작업 공간에서는 이 호출을 더 할 수 없어 다시 시도할 수 없습니다.",
    provider_rate_limited: "1분쯤 기다린 뒤 직접 다시 시도해 주세요.",
    refusal: "같은 내용으로 다시 요청해도 처리되지 않을 수 있어 다시 시도 버튼을 두지 않습니다.",
    concurrency: null,
    incomplete: null,
  };
  const REVIEWERS = ["hcx", "deepseek"]; // the server's comparison order after Claude
  const PROVIDER_NAMES = { anthropic: "Claude", hcx: "HCX-007", deepseek: "DeepSeek" };
  const MODE_NAMES = [["anthropic", "Claude"], ["clova", "CLOVA(HCX-007·리랭커)"], ["mistral", "DeepSeek"], ["embed", "BGE-M3 임베딩"], ["local_llm", "Local LLM 요약"]];
  const MODES = { mock: ["모의", "mock"], live: ["실제", "info"], off: ["꺼짐", "muted"] };
  const NOTICE = {
    insufficient: "근거가 부족해 모델을 호출하지 않았습니다. 규칙 엔진이 찾은 아래 근거만 보여 주며, 개발 과정 리포트·가정 상황·피드백도 만들지 않습니다.",
    declined: "전송 동의가 없어 호출하지 않았습니다. Claude 전송에 동의하지 않으면 심층 분석·개발 과정 리포트·가정 상황을 진행할 수 없어, 규칙 엔진이 찾은 아래 근거만 보여 줍니다.",
  };
  // Human names for evidence kinds (the server's kind_label stays in each item's 상세 정보 and in what models see).
  const KIND_NAME = {
    user_request: "내 요청",
    assistant_text: "AI의 설명",
    code_edit: "수정된 코드",
    command: "실행한 명령",
    command_output: "명령 결과",
    tool_other: "도구 알림",
  };
  // One-line meaning of every grade badge (ARCHITECTURE: grade_statements).
  const GRADE_HELP = {
    recorded: "AI가 인용한 문장이 원본 기록에 그대로 있음을 프로그램이 확인한 사실입니다.",
    inferred: "기록을 근거로 AI가 해석한 내용입니다. 근거는 있지만 결론은 AI의 판단입니다.",
    needs_verification: "기록만으로는 맞는지 알 수 없어 직접 확인해 봐야 하는 내용입니다.",
    citation_unverified: "AI가 가리킨 근거나 인용을 원본에서 찾지 못했습니다. 그대로 믿지 마세요.",
  };
  // Extra guidance under each statement when 설명 더 보기 is on.
  const GRADE_GUIDE = {
    recorded: "원본에 있는 내용이에요. 이 문장을 누르면 아래 원본에서 같은 부분을 직접 볼 수 있어요.",
    inferred: "AI의 해석이에요. 사실로 확정된 것이 아니라 가능한 설명 하나로 읽고, 원본을 보고 동의하는지 스스로 판단해 보세요.",
    needs_verification: "AI도 확신하지 못한 내용이에요. 테스트를 돌리거나 코드를 직접 읽어 확인할 거리로 삼으세요.",
    citation_unverified: "근거가 확인되지 않은 문장이에요. 믿기 전에 원본 기록을 직접 찾아보거나 이 문장은 건너뛰세요.",
  };
  const LEVEL_HELP = {
    sufficient: "결정 앞뒤로 요청·설명·코드 기록이 충분해 AI 분석을 할 수 있습니다.",
    partial: "기록이 일부만 있어 분석할 수는 있지만 근거가 제한적입니다.",
    insufficient: "기록이 너무 적어 AI를 부르지 않고 원본 기록만 정리해 보여 줍니다.",
  };
  // Fixed per-category explanations (no session text, so nothing here can reveal a result).
  const CATEGORY_HELP = {
    error_handling: "실패하거나 예외가 생겼을 때 코드가 어떻게 반응할지 AI가 정한 순간입니다.",
    input_validation: "들어오는 값이 이상할 때 어떻게 걸러낼지 AI가 정한 순간입니다.",
    external_call: "외부 API·네트워크를 어떻게 부르고, 느리거나 실패할 때 어떻게 할지 AI가 정한 순간입니다.",
    dependency_config: "라이브러리나 설정 파일을 바꾸기로 AI가 정한 순간입니다.",
    assumption: "확인하지 않은 가정을 깔고 AI가 코드를 짠 순간입니다.",
    design_choice: "여러 구현 방법 중 하나를 AI가 고른 순간입니다.",
  };
  const RETRO_HELP = {
    yes: "결정 바로 뒤에 테스트 기록이 있어, 결과를 보기 전에 예측해 볼 수 있습니다.",
    no: "결과를 확정할 수 있는 테스트 기록이 없어 예측 없이 바로 분석으로 갑니다.",
  };
  const BADGE_HELP = {
    mock: "개발용 가짜 응답입니다. 실제 모델이 분석한 결과가 아닙니다.",
    opinion: "모델이 쓴 의견입니다. 사실이 아니라 해석으로 읽어 주세요.",
    later: "이 파일은 분석 범위 뒤에 다시 고쳐졌습니다. 나중 코드가 정답이라는 뜻은 아닙니다.",
    hypo: "실제로 일어난 일이 아니라 연습용으로 만든 상황입니다. 정해진 정답이 없습니다.",
    retro: "실제 세션 기록으로 결과를 확인할 수 있는 단계입니다.",
  };
  const SUGGESTION_NOTE = "이건 제안이지 정답이 아닙니다. 내 상황과 맞는지 판단해서 골라 쓰세요.";
  // The evidence list as a story: requests, edits and checks; AI text joins the step it leads into.
  const PHASE_OF = { user_request: "ask", code_edit: "fix", command: "check", command_output: "check" };
  const PHASES = {
    ask: ["무엇을 시켰나", "AI에게 무엇을 부탁했는지"],
    fix: ["어떻게 고쳤나", "AI가 어떻게 설명하고 코드를 고쳤는지"],
    check: ["어떻게 확인했나", "고친 뒤 무엇을 실행해 확인했는지"],
  };
  const TOUR = [
    ["Agency Loop에 오신 것을 환영해요", "AI와 함께 개발한 세션 기록을 다시 보며, 그때 AI가 내린 결정 하나를 이해하고 직접 검증해 보는 연습 도구입니다. 30초면 둘러볼 수 있어요.", "stepper"],
    ["① 세션 올리기", "Claude Code 세션 JSONL 파일을 올리면 비밀값·개인정보를 가리고, AI가 중요한 기술적 선택을 한 순간(의사결정 후보)을 최대 3개 찾아 줍니다. 파일 위치를 모르면 '세션 파일 찾는 방법'을, 파일이 없으면 '시연 파일로 바로 해 보기'를 눌러 보세요.", "upload-title"],
    ["② 결정 하나 되짚기", "후보 하나를 고르면 그 결정만 다룹니다. 테스트 결과를 예측해 보고, 결과를 확인한 뒤 Claude의 분석과 개발 과정 리포트를 읽습니다.", "stepper"],
    ["배지는 사실과 해석을 구분해요", "'확인된 기록'은 원본에 그대로 있는 사실, '모델 추정'은 AI의 해석, '검증 필요'는 직접 확인할 거리, '인용 미확인'은 근거를 찾지 못한 문장입니다.", "explain-toggle"],
    ["문장마다 원본을 확인하세요", "AI의 문장에 마우스를 올리면 어느 기록에서 왔는지 미리 보이고, 누르면 아래 원본 기록으로 이동합니다. '설명 더 보기'를 켜면 문장마다 읽는 법이 붙습니다.", "explain-toggle"],
    ["③ 가정 상황 연습", "마지막으로 '만약 ~라면?' 질문에 답하고 피드백을 받아 한 번 고쳐 낼 수 있습니다. 이 안내는 '사용 안내' 버튼으로 언제든 다시 볼 수 있어요.", "tour-open"],
  ];
  const REVEAL_NOTE = "일치 여부는 참고용 표시입니다. 결과를 맞혔다고 검증 능력이 높다는 뜻은 아니며, 점수나 능력 판정에 쓰지 않습니다. 예측과 이유는 어떤 모델에도 보내지 않습니다.";

  const $ = (id) => document.getElementById(id);
  const state = {
    file: null,
    view: null,
    busy: false,
    jobs: {}, // kind -> latest JobView
    polling: new Map(), // job id -> kind
    gen: 0, // bumped when the workspace goes away, so its pollers stop
    resubmit: false,
    formAttempt: 0, // feedback attempt the answer form currently holds
    consentBoxes: [],
    reportStarting: false, // one auto-start of the report at a time
    reportTried: false, // a failed or refused auto-start leaves retrying to the user
    jumpFrom: null, // the statement or chip a jump to the original came from
    previewAnchor: null,
    tourStep: 0,
    tourReturn: null,
  };

  // ---------- DOM helpers ----------

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [name, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (name === "class") node.className = value;
      else node.setAttribute(name, value === true ? "" : String(value));
    }
    node.append(...children.flat().filter((child) => child !== null && child !== undefined && child !== false));
    return node;
  }

  function tag(text, kind) {
    return el("span", { class: `tag tag-${kind}` }, text);
  }

  function fact(label, value) {
    return el("div", {}, el("dt", {}, label), el("dd", {}, value));
  }

  function chips(items, emptyText) {
    if (!items.length) return [el("li", { class: "chip chip-empty" }, emptyText)];
    return items.map((text) => el("li", { class: "chip" }, text));
  }

  function textList(items, className) {
    return el("ul", { class: className }, ...items.map((text) => el("li", {}, text)));
  }

  /** A badge with its one-line meaning: a hover title, and text that screen readers always hear and 설명 더 보기 shows. */
  function badge(text, tone, meaning, inline = false) {
    // inline: the meaning is always on screen (cards have room for it).
    const node = tag(text, tone);
    if (!meaning) return node;
    node.setAttribute("title", meaning);
    return el("span", { class: inline ? "badge badge-inline" : "badge" }, node, el("span", { class: "badge-meaning" }, ` ${meaning}`));
  }

  function gradeBadge(s) {
    return badge(s.grade_label, GRADE_TONE[s.grade] || "muted", GRADE_HELP[s.grade]);
  }

  function explain(text) {
    return text ? el("p", { class: "explain" }, el("span", { class: "explain-label" }, "읽는 법"), text) : null;
  }

  function kindName(item) {
    return KIND_NAME[item.kind] || item.kind_label;
  }

  function stored(key, value) {
    // Storage can be blocked (private mode, tests): reads fall back to null, writes are dropped.
    try {
      if (value === undefined) return window.localStorage.getItem(key);
      window.localStorage.setItem(key, value);
    } catch {
      // Not remembered.
    }
    return null;
  }

  function hint(text) {
    return el("p", { class: "hint" }, text);
  }

  function notice(text) {
    return el("div", { class: "alert alert-info" }, el("p", {}, text));
  }

  function count(n) {
    return Number(n).toLocaleString("ko-KR");
  }

  function formatSize(bytes) {
    if (bytes < 1024) return `${bytes}B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KiB`;
    return `${(bytes / 1024 / 1024).toFixed(1)}MiB`;
  }

  function announce(text) {
    const status = $("status");
    status.textContent = "";
    // A fresh text node lets screen readers repeat an identical message.
    window.setTimeout(() => { status.textContent = text; }, 50);
  }

  function focusHeading(id) {
    focusNode($(id), "start");
  }

  function focusNode(node, block) {
    node.focus({ preventScroll: true });
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    node.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block });
  }

  function showError(box, err, extra) {
    box.replaceChildren(...[err.message, extra].filter(Boolean).map((text) => el("p", {}, text)));
    box.hidden = false;
  }

  function confirmDialog(dialog) {
    return new Promise((resolve) => {
      dialog.returnValue = "";
      dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), { once: true });
      dialog.showModal();
    });
  }

  function actionButton(label, onClick, kind = "primary") {
    const button = el("button", { type: "button", class: `btn btn-${kind} js-lock` }, label);
    button.disabled = isLocked();
    button.addEventListener("click", onClick);
    return button;
  }

  function evidenceById(id) {
    return state.view ? state.view.evidence.find((e) => e.id === id) : undefined;
  }

  function chipLabel(id) {
    const item = evidenceById(id);
    if (!item) return "기록 보기";
    return item.file ? `${kindName(item)} · ${item.file}` : kindName(item);
  }

  function evidenceChips(ids, invalidIds = []) {
    if (!ids.length) return null;
    const invalid = new Set(invalidIds);
    return el("p", { class: "ev-chips" }, el("span", { class: "ev-chips-label" }, "근거"), ...ids.map((id) => {
      // The evidence ID is folded away: the chip names the record, its 상세 정보 and the preview carry the ID.
      if (invalid.has(id)) return badge("근거 확인할 수 없음", "danger", `AI가 가리킨 기록(${id})이 보낸 근거에 없습니다.`);
      const chip = el("button", { type: "button", class: "chip chip-ev", "data-ev": id,
        "aria-label": `근거: ${chipLabel(id)}. 누르면 원본 기록으로 이동` }, chipLabel(id));
      chip.addEventListener("click", () => jumpToEvidence(id, chip)); // a linked statement ignores clicks on buttons
      // Inside a linked statement the statement shows the preview; elsewhere the chip does.
      const own = () => !chip.closest(".is-linked");
      chip.addEventListener("mouseenter", () => { if (own()) showPreview(chip, [id]); });
      chip.addEventListener("mouseleave", () => { if (own()) hidePreview(); });
      chip.addEventListener("focus", () => { if (own()) showPreview(chip, [id]); });
      chip.addEventListener("blur", () => { if (own()) hidePreview(); });
      return chip;
    }));
  }

  // ---------- Statement -> original record: preview on hover/focus, jump on click ----------

  function markCited(ids, on) {
    for (const id of ids) {
      const node = $(`ev-${id}`);
      if (node) node.classList.toggle("is-cited", on);
    }
  }

  function showPreview(anchor, ids) {
    const box = $("ev-preview");
    box.replaceChildren(
      el("p", { class: "ev-preview-title" }, "이 문장이 가리키는 원본 기록"),
      ...ids.slice(0, 3).map((id) => {
        const item = evidenceById(id);
        if (!item) return el("p", { class: "ev-preview-item" }, "화면 목록에 없는 기록입니다. 누르면 따로 열어 봅니다.");
        const lines = item.text.split("\n");
        const snippet = lines.slice(0, 4).join("\n").slice(0, 240);
        return el("div", { class: "ev-preview-item" },
          el("p", { class: "ev-preview-head" }, el("strong", {}, kindName(item)), item.file ? el("code", {}, item.file) : null),
          el("pre", { class: "ev-preview-text" }, snippet + (snippet.length < item.text.length ? " …" : "")),
          el("p", { class: "ev-preview-meta" }, `근거 ID ${item.id} · 턴 ${item.turn} · JSONL ${item.line}번째 줄`));
      }),
      ...(ids.length > 3 ? [el("p", { class: "ev-preview-meta" }, `외 ${ids.length - 3}개`)] : []),
      el("p", { class: "ev-preview-foot" }, "누르면 아래 원본 기록으로 이동합니다."));
    box.hidden = false;
    anchor.setAttribute("aria-describedby", "ev-preview");
    const rect = anchor.getBoundingClientRect();
    const width = Math.min(380, window.innerWidth - 32);
    const left = Math.max(16, Math.min(rect.left, window.innerWidth - width - 16));
    const below = rect.bottom + 8;
    box.style.width = `${width}px`;
    box.style.left = `${left}px`;
    // Below the anchor when it fits, else above it.
    const height = box.offsetHeight || 0;
    box.style.top = `${below + height > window.innerHeight && rect.top > height + 16 ? rect.top - height - 8 : below}px`;
    state.previewAnchor = anchor;
  }

  function hidePreview() {
    $("ev-preview").hidden = true;
    if (state.previewAnchor) state.previewAnchor.removeAttribute("aria-describedby");
    state.previewAnchor = null;
  }

  function linkStatement(node, ids) {
    if (!ids.length) return node;
    node.classList.add("is-linked");
    const on = () => { showPreview(node, ids); markCited(ids, true); };
    const off = () => { hidePreview(); markCited(ids, false); };
    node.addEventListener("mouseenter", on);
    node.addEventListener("mouseleave", off);
    node.addEventListener("focusin", on);
    node.addEventListener("focusout", off);
    node.addEventListener("keydown", (event) => { if (event.key === "Escape") off(); });
    node.addEventListener("click", (event) => {
      // Clicking the statement itself (not a button, link or selected text) jumps to its first record.
      if (event.target.closest("button") || event.target.closest("a") || event.target.closest("summary")) return;
      if (window.getSelection && String(window.getSelection())) return;
      jumpToEvidence(ids[0], node.querySelector(".chip-ev") || node);
    });
    return node;
  }

  function jumpToEvidence(id, origin) {
    hidePreview();
    const target = $(`ev-${id}`);
    if (!target || lostFocus(target)) {
      openEvidence(id);
      return;
    }
    for (const node of document.querySelectorAll(".is-target")) node.classList.remove("is-target");
    target.classList.add("is-target");
    for (const details of target.querySelectorAll("details")) if (!details.classList.contains("ev-meta")) details.open = true;
    state.jumpFrom = origin;
    $("ev-back").hidden = false;
    focusNode(target, "center");
    announce(`원본 기록으로 이동했습니다: ${chipLabel(id)}. '보던 문장으로 돌아가기'로 돌아갈 수 있습니다.`);
  }

  function jumpBack() {
    $("ev-back").hidden = true;
    const origin = state.jumpFrom;
    state.jumpFrom = null;
    if (origin && !lostFocus(origin)) focusNode(origin, "center");
  }

  function modelTags(result) {
    return el("p", { class: "tags" },
      badge("모델 의견(해석)", "info", BADGE_HELP.opinion), tag(`모델 ${result.model}`, "muted"),
      result.mock ? badge(MOCK, "mock", BADGE_HELP.mock) : null);
  }

  // ---------- API ----------

  class ApiError extends Error {
    constructor(status, code, message) {
      super(message);
      this.status = status;
      this.code = code;
    }
  }

  async function api(method, path, body) {
    const init = { method, credentials: "same-origin", headers: { Accept: "application/json" } };
    if (body instanceof Blob) {
      init.body = body;
    } else if (body !== undefined) {
      init.body = JSON.stringify(body);
      init.headers["Content-Type"] = "application/json";
    }
    let response;
    try {
      response = await fetch(path, init);
    } catch {
      throw new ApiError(0, "connection", FALLBACK_ERRORS[0]);
    }
    if (response.status === 204) return null;
    const data = await response.json().catch(() => null);
    if (!response.ok) {
      const error = (data && data.error) || {};
      const message = error.message || FALLBACK_ERRORS[response.status] || `요청을 처리하지 못했습니다(${response.status}).`;
      throw new ApiError(response.status, error.code || "", message);
    }
    return data;
  }

  // ---------- Theme ----------

  function currentTheme() {
    const chosen = document.documentElement.dataset.theme;
    if (chosen) return chosen;
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  function updateThemeButton() {
    const dark = currentTheme() === "dark";
    const button = $("theme-toggle");
    button.textContent = dark ? "라이트 테마" : "다크 테마";
    button.setAttribute("aria-label", dark ? "라이트 테마로 바꾸기" : "다크 테마로 바꾸기");
  }

  function initTheme() {
    try {
      const saved = window.localStorage.getItem("al-theme");
      if (saved === "light" || saved === "dark") document.documentElement.dataset.theme = saved;
    } catch {
      // Storage can be blocked; the system theme still applies.
    }
    updateThemeButton();
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", updateThemeButton);
    $("theme-toggle").addEventListener("click", () => {
      const next = currentTheme() === "dark" ? "light" : "dark";
      document.documentElement.dataset.theme = next;
      try {
        window.localStorage.setItem("al-theme", next);
      } catch {
        // Not remembered when storage is blocked.
      }
      updateThemeButton();
    });
  }

  // ---------- Learning scaffolds: 설명 더 보기 and the guided tour ----------

  function setExplain(on) {
    document.body.classList.toggle("explain-on", on);
    const button = $("explain-toggle");
    button.setAttribute("aria-pressed", String(on));
    button.textContent = on ? "설명 접기" : "설명 더 보기";
  }

  function initExplain() {
    setExplain(stored("al-explain") === "1");
    $("explain-toggle").addEventListener("click", () => {
      const on = !document.body.classList.contains("explain-on");
      setExplain(on);
      stored("al-explain", on ? "1" : "0");
      announce(on ? "모든 진술과 배지 옆에 읽는 법을 보여 줍니다." : "읽는 법을 접었습니다.");
    });
    $("grade-legend").replaceChildren(...STATS.map(([grade, label]) => el("div", { class: "legend-row" },
      el("dt", {}, tag(label, GRADE_TONE[grade])), el("dd", {}, GRADE_HELP[grade]))));
    $("ev-back").addEventListener("click", jumpBack);
    window.addEventListener("keydown", (event) => { if (event.key === "Escape") hidePreview(); });
  }

  function showTourStep(step) {
    state.tourStep = step;
    const [title, body, target] = TOUR[step];
    $("tour-count").textContent = `${step + 1} / ${TOUR.length}`;
    $("tour-title").textContent = title;
    $("tour-body").textContent = body;
    $("tour-prev").disabled = step === 0;
    $("tour-next").textContent = step === TOUR.length - 1 ? "시작하기" : "다음";
    for (const node of document.querySelectorAll(".tour-target")) node.classList.remove("tour-target");
    const node = $(target);
    if (node && !lostFocus(node)) node.classList.add("tour-target");
    $("tour-title").focus();
  }

  function openTour() {
    const tour = $("tour");
    state.tourReturn = document.activeElement;
    if (!tour.open) tour.show(); // non-modal: the page stays readable behind it
    showTourStep(0);
  }

  function closeTour() {
    const tour = $("tour");
    if (!tour.open) return;
    tour.close();
    stored("al-tour-done", "1");
    for (const node of document.querySelectorAll(".tour-target")) node.classList.remove("tour-target");
    const back = state.tourReturn;
    if (back && !lostFocus(back) && back.focus) back.focus();
  }

  function initTour() {
    $("tour-open").addEventListener("click", openTour);
    $("tour-prev").addEventListener("click", () => showTourStep(Math.max(0, state.tourStep - 1)));
    $("tour-next").addEventListener("click", () => {
      if (state.tourStep === TOUR.length - 1) closeTour();
      else showTourStep(state.tourStep + 1);
    });
    $("tour").querySelector("[data-tour-close]").addEventListener("click", closeTour);
    $("tour").addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        closeTour();
      }
    });
    if (stored("al-tour-done") !== "1") openTour();
  }

  // ---------- Layout state ----------

  function setStep(step) {
    for (const item of $("stepper").children) {
      const n = Number(item.dataset.step);
      item.classList.toggle("is-done", n < step);
      if (n === step) item.setAttribute("aria-current", "step");
      else item.removeAttribute("aria-current");
    }
  }

  function isLocked() {
    // The Local LLM summary never blocks the flow, so only the other jobs lock the action buttons.
    return state.busy || [...state.polling.values()].some((kind) => kind !== "summary");
  }

  function setBusy(busy) {
    state.busy = busy;
    $("main").setAttribute("aria-busy", String(busy));
    $("upload-btn").disabled = busy || !state.file || state.file.size > MAX_UPLOAD_BYTES;
    $("demo-btn").disabled = busy;
    $("sa-submit").disabled = busy;
    $("delete-open").disabled = busy;
    const locked = isLocked();
    for (const button of document.querySelectorAll(".js-lock")) button.disabled = locked;
  }

  function resetToUpload() {
    state.view = null;
    state.gen += 1;
    state.jobs = {};
    state.polling = new Map();
    state.resubmit = false;
    state.formAttempt = 0;
    state.reportStarting = false;
    state.reportTried = false;
    $("scope").hidden = true;
    $("step-mirror").hidden = true;
    $("selection").hidden = true;
    $("candidate-list").replaceChildren();
    $("selection-summary").replaceChildren();
    for (const id of ["sa-form", "predict-form", "answer-form"]) $(id).reset();
    for (const area of document.querySelectorAll("textarea")) updateCounter(area);
    for (const id of ["sa-error", "predict-error", "analysis-error", "summary-error", "review-error", "whatif-error", "answer-error", "report-error"]) $(id).hidden = true;
    $("sa-form").hidden = true;
    $("delete-open").hidden = true;
    $("replace-hint").hidden = true;
    $("ev-back").hidden = true;
    hidePreview();
    renderFlow();
    setStep(1);
  }

  function handleLostWorkspace(err) {
    resetToUpload();
    showError($("upload-error"), err);
    announce(err.message);
    focusHeading("upload-title");
  }

  // ---------- Step 1: upload ----------

  function chooseFile(file) {
    state.file = file || null;
    $("upload-error").hidden = true;
    $("file-info").textContent = file ? `${file.name} · ${formatSize(file.size)}` : "선택한 파일이 없습니다";
    if (file && file.size > MAX_UPLOAD_BYTES) {
      showError($("upload-error"), new ApiError(413, "too_large", FALLBACK_ERRORS[413]), UPLOAD_HINTS[413]);
    }
    setBusy(state.busy);
  }

  function initUpload() {
    const input = $("file-input");
    const zone = $("dropzone");
    input.addEventListener("change", () => chooseFile(input.files[0]));
    zone.addEventListener("dragover", (event) => {
      event.preventDefault();
      zone.classList.add("is-over");
    });
    zone.addEventListener("dragleave", () => zone.classList.remove("is-over"));
    zone.addEventListener("drop", (event) => {
      event.preventDefault();
      zone.classList.remove("is-over");
      const files = event.dataTransfer ? event.dataTransfer.files : [];
      if (files.length !== 1) {
        showError($("upload-error"), new ApiError(400, "", "파일은 한 번에 1개만 올릴 수 있습니다."));
        return;
      }
      chooseFile(files[0]);
    });
    // A file dropped next to the zone must not make the browser open it.
    for (const type of ["dragover", "drop"]) {
      window.addEventListener(type, (event) => {
        if (!zone.contains(event.target)) event.preventDefault();
      });
    }
    $("upload-btn").addEventListener("click", startUpload);
    $("demo-btn").addEventListener("click", startDemo);
  }

  async function startDemo() {
    // The fixed synthetic session goes through the normal upload path, exactly like a file the user picked.
    if (state.busy) return;
    setBusy(true);
    let file = null;
    try {
      const response = await fetch("/api/demo-session", { credentials: "same-origin" });
      if (response.ok) file = new File([await response.blob()], "demo_weather.jsonl");
    } catch {
      // Reported below.
    }
    setBusy(false);
    if (!file) {
      showError($("upload-error"), new ApiError(0, "", "시연 파일을 불러오지 못했습니다."),
        "Agency Loop 저장소의 fixtures/sessions/demo_weather.jsonl을 '파일 선택'으로 직접 올려 주세요.");
      announce("시연 파일을 불러오지 못했습니다.");
      return;
    }
    chooseFile(file);
    await startUpload();
  }

  // ---------- Step 1: help (OS tabs and copy buttons) ----------

  function initHelp() {
    const tabs = $("os-tabs").querySelectorAll('[role="tab"]');
    const selectTab = (tab, focus) => {
      for (const other of tabs) {
        const on = other === tab;
        other.setAttribute("aria-selected", String(on));
        other.setAttribute("tabindex", on ? "0" : "-1");
        $(other.getAttribute("aria-controls")).hidden = !on;
      }
      if (focus) tab.focus();
    };
    for (const [i, tab] of tabs.entries()) {
      tab.addEventListener("click", () => selectTab(tab, false));
      tab.addEventListener("keydown", (event) => {
        const to = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1 }[event.key];
        if (to === undefined) return;
        event.preventDefault();
        selectTab(tabs[(to + tabs.length) % tabs.length], true);
      });
    }
    for (const button of document.querySelectorAll("[data-copy]")) {
      button.addEventListener("click", async () => {
        let copied = false;
        try { // the Clipboard API needs a secure context: absent over plain HTTP on a public address
          await window.navigator.clipboard.writeText($(button.dataset.copy).textContent);
          copied = true;
        } catch {
          // Said below.
        }
        button.textContent = copied ? "복사됨" : "복사 안 됨";
        window.setTimeout(() => { button.textContent = "복사"; }, 2000);
        announce(copied ? "복사했습니다. 터미널에 붙여 넣으세요." : "복사하지 못했습니다. 명령을 마우스로 직접 선택해 복사해 주세요.");
      });
    }
  }

  async function startUpload() {
    const file = state.file;
    if (!file || state.busy) return;
    $("upload-error").hidden = true;
    const progress = $("upload-progress");
    progress.textContent = `${file.name} (${formatSize(file.size)}) 올리는 중… 서버가 비밀값·개인정보를 가리고 의사결정 후보를 찾습니다.`;
    progress.hidden = false;
    announce("세션 파일을 올리고 분석하는 중입니다.");
    setBusy(true);
    try {
      const data = await api("POST", "/api/workspaces", file);
      resetToUpload();
      renderWorkspace({ ...data, selected: null });
      announce(`분석을 마쳤습니다. 의사결정 후보 ${data.candidates.length}개를 찾았습니다.`);
      focusHeading("mirror-title");
    } catch (err) {
      showError($("upload-error"), err, UPLOAD_HINTS[err.status]);
      announce(err.message);
    } finally {
      progress.hidden = true;
      setBusy(false);
    }
  }

  function renderScope(scope) {
    $("scope-facts").replaceChildren(
      fact("사람 요청", `${scope.human_requests}개`),
      fact("코드 변경 기록", scope.has_code_edits ? "있음" : "없음"),
      fact("테스트 실행 기록", scope.has_test_records ? "있음" : "없음"),
      fact("깨진 줄(제외)", `${scope.bad_lines}개`),
    );
    const masked = Object.entries(scope.masked).map(([code, n]) => `${scope.masked_labels[code] || code} ${n}개`);
    $("scope-masked").replaceChildren(...chips(masked, "가린 항목 없음"));
    $("scope-dropped").replaceChildren(...chips(scope.dropped, "제외한 기록 없음"));
    $("scope-notices").replaceChildren(...scope.notices.map((text) => el("li", {}, text)));
    $("scope").hidden = false;
  }

  // ---------- Step 2-1: candidates and self-assessment ----------

  function applyView(view) {
    // Upload and select responses carry no flow state yet.
    state.view = { consents: {}, jobs: [], results: { feedback: [] }, retro: null, evidence: [], ...view };
    state.jobs = {};
    for (const job of state.view.jobs) state.jobs[job.kind] = job; // start order: the last job of a kind is current
  }

  function renderWorkspace(view) {
    applyView(view);
    renderScope(view.scope);
    renderCandidates(view.candidates, view.selected);
    $("step-mirror").hidden = false;
    $("sa-form").hidden = !view.candidates.length || Boolean(view.selected);
    $("delete-open").hidden = false;
    $("replace-hint").hidden = false;
    setStep(2);
  }

  function renderCandidates(cards, selectedId) {
    $("candidate-list").replaceChildren(...cards.map((card) => candidateCard(card, selectedId)));
    $("no-candidates").hidden = cards.length > 0;
    $("candidate-guide").hidden = cards.length === 0 || Boolean(selectedId);
    $("candidate-fieldset").hidden = cards.length === 0;
  }

  function candidateCard(card, selectedId) {
    const inputId = `cand-${card.id}`;
    const locked = Boolean(selectedId);
    const chosen = card.id === selectedId;
    const radio = el("input", { type: "radio", name: "candidate", id: inputId, value: card.id, "aria-describedby": `${inputId}-body` });
    radio.checked = chosen;
    radio.disabled = locked;
    const lines = [...new Set(card.lines)].join(", ");
    const classes = ["card", locked ? "is-locked" : "", locked && !chosen ? "is-dimmed" : ""].filter(Boolean).join(" ");
    const node = el("article", { class: classes },
      el("div", { class: "card-head" },
        radio,
        el("label", { for: inputId, class: "card-title" }, el("span", { class: "card-rank" }, `후보 ${card.rank}`), card.title)),
      el("div", { id: `${inputId}-body`, class: "card-body" },
        el("p", { class: "card-help" }, CATEGORY_HELP[card.category] || ""),
        el("p", { class: "tags" },
          tag(card.category_label, "category"),
          chosen ? tag("선택됨", "selected") : null),
        el("ul", { class: "card-facts" },
          el("li", {}, badge(`근거 ${card.evidence_level_label}`, `level-${card.evidence_level}`, LEVEL_HELP[card.evidence_level], true)),
          el("li", {}, card.retro_available
            ? badge("기록 확인 예측 가능", "retro", RETRO_HELP.yes, true)
            : badge("예측 없이 분석", "muted", RETRO_HELP.no, true))),
        el("p", { class: "card-label" }, "코드에서 찾은 신호"),
        el("ul", { class: "chips", "aria-label": "코드에서 찾은 신호" }, ...chips(card.signals.map((s) => s.label), "신호 없음")),
        el("dl", { class: "meta" },
          el("dt", {}, "파일"), el("dd", {}, ...card.files.flatMap((file, i) => [i ? ", " : "", el("code", {}, file)])),
          el("dt", {}, "테스트 실행 기록"), el("dd", {}, card.has_test_record ? "결정 이후 있음" : "결정 이후 없음")),
        el("details", { class: "ev-meta" },
          el("summary", {}, "상세 정보"),
          el("dl", { class: "meta" }, el("dt", {}, "원문 위치"), el("dd", {}, `턴 ${card.turn} · JSONL ${lines}번째 줄`)))));
    if (!locked) {
      node.addEventListener("click", (event) => {
        if (event.target !== radio && !event.target.closest("label") && !event.target.closest("summary")) radio.click();
      });
    }
    return node;
  }

  function readSelfAssessment() {
    const result = {};
    for (const [axis] of AXES) {
      const checked = $("sa-form").querySelector(`input[name="${axis}"]:checked`);
      if (!checked) return null;
      result[axis] = Number(checked.value);
    }
    return result;
  }

  function initSelection() {
    $("sa-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      if (state.busy || !state.view) return;
      const radio = document.querySelector('input[name="candidate"]:checked');
      const sa = readSelfAssessment();
      const errorBox = $("sa-error");
      if (!radio || !sa) {
        const missing = !radio ? "분석할 후보를 하나 골라 주세요." : "이해·검증·통제 자기평가를 모두 골라 주세요.";
        showError(errorBox, new ApiError(0, "", missing));
        (radio ? $("sa-form").querySelector("input") : document.querySelector('input[name="candidate"]')).focus();
        return;
      }
      errorBox.hidden = true;
      const card = state.view.candidates.find((c) => c.id === radio.value);
      $("select-dialog-candidate").textContent = `후보 ${card.rank} · ${card.title}`;
      if (await confirmDialog($("select-dialog"))) await selectCandidate(card.id, sa);
    });
  }

  async function selectCandidate(candidateId, sa) {
    setBusy(true);
    announce("후보를 고정하는 중입니다.");
    try {
      const result = await api("POST", "/api/select", { candidate_id: candidateId, self_assessment: sa });
      state.view = { ...state.view, ...result, self_assessment: sa };
      renderCandidates(state.view.candidates, result.selected);
      $("sa-form").hidden = true;
      renderSelection();
      renderFlow();
      announce(result.retro.available ? "후보를 고정했습니다. 사전 맥락을 확인하세요." : "후보를 고정했습니다. 확인 가능한 근거를 보여 줍니다.");
      focusHeading("selection-title");
    } catch (err) {
      if (err.code === "no_workspace") return handleLostWorkspace(err);
      if (err.code === "locked") await restore();
      showError($("sa-error"), err);
      announce(err.message);
    } finally {
      setBusy(false);
    }
  }

  function selectedCard() {
    return state.view.candidates.find((c) => c.id === state.view.selected);
  }

  function renderSelection() {
    const view = state.view;
    const card = selectedCard();
    const ratings = AXES.map(([axis, label]) => `${label}: ${RATINGS[view.self_assessment[axis]]}`).join(" · ");
    $("selection-summary").replaceChildren(
      el("p", { class: "selection-title" }, `후보 ${card.rank} · ${card.title}`),
      el("dl", { class: "facts" },
        fact("자기평가", ratings),
        fact("What-if 난이도", `${view.tier_label} (자기평가 기준, 3단계에서 사용)`)));
    $("selection").hidden = false;
  }

  // ---------- Steps 2-2 to 3: rendered from state.view and state.jobs ----------

  function renderFlow() {
    const view = state.view;
    const selected = Boolean(view && view.selected);
    const retro = selected ? view.retro : null;
    const open = selected && (!retro.available || Boolean(retro.submitted)); // nothing past 2-2 while blinded
    renderSubsteps(selected, retro, open);
    renderRetro(retro);
    renderReveal(retro);
    $("analysis-section").hidden = !open;
    if (open) renderAnalysis();
    renderStep3();
    if (view) setStep(view.results.whatif || state.jobs.whatif ? 3 : 2);
    maybeStartReport();
  }

  function renderSubsteps(selected, retro, open) {
    const skipped = Boolean(retro && !retro.available);
    const results = open ? state.view.results : {};
    const started = open && Boolean(state.jobs.analysis || state.view.consents.analysis || results.retrieval);
    const current = !selected ? 1 : !open ? 2 : results.analysis ? 5 : skipped || started ? 4 : 3;
    $("substeps").replaceChildren(...SUBSTEPS.map((label, i) => {
      const n = i + 1;
      const skip = skipped && (n === 2 || n === 3);
      const done = n < current && !skip;
      return el("li", { class: skip ? "is-skipped" : done ? "is-done" : null, "aria-current": n === current ? "step" : null },
        label, skip ? " " : null, skip ? tag("생략", "muted") : null,
        done ? el("span", { class: "visually-hidden" }, " (완료)") : null);
    }));
  }

  function renderRetro(retro) {
    const block = $("retro-block");
    $("predict-form").hidden = !(retro && retro.available && !retro.submitted);
    if (!retro || retro.submitted) {
      block.replaceChildren();
      return;
    }
    if (!retro.available) {
      block.replaceChildren(notice(retro.message));
      return;
    }
    const byId = new Map(state.view.evidence.map((item) => [item.id, item]));
    const pick = (ids) => ids.map((id) => byId.get(id)).filter(Boolean).map((item) => evidenceItem(item));
    const pre = retro.precontext;
    block.replaceChildren(
      el("div", { class: "retro-head" }, badge("기록 확인 예측", "retro", BADGE_HELP.retro), el("h4", {}, "사전 맥락")),
      hint("결과를 보기 전에는 대상 테스트를 실행하기 전까지의 기록만 보여 줍니다. 테스트 결과와 이후 기록은 서버가 보내지 않습니다."),
      chapterHead(1, "ask", "내 요청"), ...pick([pre.request_id, pre.last_request_id].filter(Boolean)),
      chapterHead(2, "fix", "수정된 코드"), ...pick(pre.edit_ids),
      chapterHead(3, "check", `실행한 명령 · 실행기 ${pre.command.runner} · 결과는 예측한 뒤에 공개`), ...pick([pre.command.evidence_id]));
    $("predict-question").textContent = retro.question;
  }

  function renderReveal(retro) {
    const show = Boolean(retro && retro.submitted);
    $("reveal-section").hidden = !show;
    if (!show) {
      $("reveal-block").replaceChildren();
      return;
    }
    const r = retro.reveal;
    const p = retro.prediction;
    const source = r.exit_source === "explicit" ? "명시" : "is_error=false로 추정";
    $("reveal-block").replaceChildren(...[
      el("p", { class: "tags" }, badge("기록 확인 예측", "retro", BADGE_HELP.retro),
        badge(`기록과 비교: ${retro.label_text}`, LABEL_TONE[retro.label] || "muted", "내 예측과 기록된 결과가 같은지만 보여 주는 참고 표시입니다.")),
      el("dl", { class: "facts" },
        fact("내 예측", p.choice_label),
        fact("기록된 결과", r.result_label),
        fact("명령", el("code", {}, r.command)),
        fact("종료 코드", `${r.exit_code} (${source})`),
        fact("요약 줄", el("code", {}, r.summary_line || "없음"))),
      p.reason ? el("p", { class: "my-answer" }, el("strong", {}, "내 이유: "), p.reason) : null,
      el("h4", {}, "판단에 필요한 확인 지점"),
      textList(retro.checkpoints, "plain-list"),
      el("h4", {}, "명령 결과 (출력 발췌)"),
      el("article", { class: "evidence ev-kind-command_output" },
        el("header", { class: "evidence-head" }, el("span", { class: "ev-kind" }, KIND_NAME.command_output)),
        collapsible(textViewer({ id: r.evidence_id, kind_label: "명령 출력", text: r.output_excerpt }), r.output_excerpt.split("\n").length),
        metaDetails([["근거 ID", r.evidence_id], ["범위", "출력 마지막 3,000자"], ["원래 기록 종류", "명령 출력"]])),
      el("p", { class: "callout callout-info" }, REVEAL_NOTE),
      explain("기록된 결과는 사실, 일치·불일치는 참고 표시일 뿐입니다. 틀렸다면 위 확인 지점과 출력에서 무엇을 놓쳤는지 찾아보는 것이 이 단계의 목적이에요."),
    ].filter(Boolean));
  }

  function renderAnalysis() {
    const { results, consents, evidence } = state.view;
    const insufficient = selectedCard().evidence_level === "insufficient";
    const consent = consents.analysis;
    let intro = [];
    if (results.analysis || state.jobs.analysis) {
      intro = [];
    } else if (insufficient && results.retrieval) {
      intro = [notice(NOTICE.insufficient)];
    } else if (insufficient) {
      intro = [hint("근거 수준이 '부족'이라 전송 동의 단계 없이, 모델을 호출하지 않고 규칙 엔진 근거만 정리합니다."),
        actionButton("심층 분석 시작", () => startAnalysis(true))];
    } else if (consent && !consent.providers.anthropic) {
      intro = [notice(NOTICE.declined), actionButton("전송 미리보기 다시 열기", () => startAnalysis(false), "secondary")];
    } else {
      intro = [hint("Claude가 결과를 포함한 확인 가능 근거로 결정의 이유·대안·검증되지 않은 가정·위험·확인할 점을 분석하고, 규칙 엔진이 인용을 확인해 진술마다 근거 등급을 붙입니다. 심층 분석이 끝나면 같은 근거 묶음으로 개발 과정 분석 리포트(개발 세션 요약·프롬프트 개선 포인트·판단·검증 보완 포인트·개선 행동)를 자동으로 만듭니다. 보내기 전에 제공자별 전송 미리보기에서 보낼 곳을 고릅니다."),
        el("ol", { class: "mini-steps" },
          el("li", {}, "버튼을 누르면 보낼 내용을 먼저 보여 줍니다. 보낼 곳을 직접 고르세요."),
          el("li", {}, "Claude가 결정을 분석하고, 이어서 개발 과정 리포트를 자동으로 만듭니다."),
          el("li", {}, "결과의 문장마다 붙은 근거를 눌러 원본 기록과 직접 대조해 보세요.")),
        actionButton("심층 분석 시작", () => startAnalysis(false))];
    }
    $("analysis-intro").replaceChildren(...intro);
    renderJobLine("analysis");
    const retrieval = results.retrieval;
    $("search-state").hidden = !retrieval;
    if (retrieval) $("search-state").replaceChildren("관련 요청 검색: ", tag(retrieval.state_label, retrieval.state === "failed" ? "warn" : "muted"));
    renderReport();
    $("analysis-result").replaceChildren(...(results.analysis ? [analysisResult(results.analysis)] : []));
    renderReviews();
    renderSummary();
    renderStory(evidence);
  }

  // ---------- The development-process report (개발 과정 분석 리포트) ----------

  function reportEligible() {
    // A report can run when a Claude analysis could: sufficient evidence and a consented Anthropic transfer.
    const view = state.view;
    if (!view || !view.selected) return false;
    const card = selectedCard();
    if (!card || card.evidence_level === "insufficient") return false;
    const consent = view.consents.analysis;
    return Boolean(consent && consent.providers.anthropic);
  }

  function reportWait() {
    // The server refuses the What-if while a report job is in flight, and the report auto-starts right after the
    // analysis: the button appears only once the report is stored, failed or was never needed.
    const job = state.jobs.report;
    if (job) return !FINAL.has(job.status);
    return !state.view.results.report && Boolean(state.view.results.analysis) && reportEligible();
  }

  async function maybeStartReport() {
    // One report per flow, automatic under the consented analysis stage; a failure leaves the retry to the user.
    if (state.reportStarting || state.reportTried) return;
    const view = state.view;
    if (!view || !view.selected || !view.results.analysis || view.results.report || state.jobs.report) return;
    if (!reportEligible()) return;
    state.reportStarting = true;
    try {
      const r = await api("POST", "/api/report");
      if (r.job_id) {
        track("report", r.job_id);
        renderJobLine("report");
        renderStep3();
        announce("개발 과정 분석 리포트를 요청했습니다.");
      } else {
        state.reportTried = true;
      }
    } catch (err) {
      state.reportTried = true;
      if (err.code === "no_workspace") handleLostWorkspace(err);
    } finally {
      state.reportStarting = false;
    }
  }

  function retryReport() {
    return act($("report-error"), async () => {
      const r = await api("POST", "/api/report");
      if (r.job_id) track("report", r.job_id);
      announce("개발 과정 분석 리포트를 다시 요청했습니다.");
      renderJobLine("report");
      renderStep3();
    });
  }

  function renderReport() {
    const r = state.view.results.report;
    for (const id of ["report-summary-card", "report-prompt-card", "report-gaps-card", "report-actions-card"]) {
      $(id).hidden = !r;
    }
    renderJobLine("report");
    if (!r) {
      for (const id of ["report-summary-head", "report-summary-body", "report-prompt-body", "report-gaps-body",
                        "report-actions-body"]) $(id).replaceChildren();
      return;
    }
    const stats = STATS.map(([key, label]) => `${label} ${r.stats[key] || 0}`).join(" · ");
    $("report-summary-head").replaceChildren(
      el("h4", { id: "report-summary-title", tabindex: "-1" }, "개발 세션 요약 ", tag("개발 과정 분석 리포트", "muted"),
        el("span", { class: "subtitle" }, "이 세션에서 무엇을 시켰고, 어떻게 고쳤고, 어떻게 확인했는지의 줄거리")),
      modelTags(r));
    $("report-summary-body").replaceChildren(
      el("p", { class: "lead-strong" }, r.overview),
      ...Object.entries(r.summary_labels).map(([key, label]) => el("section", { class: "statements" },
        el("h5", {}, label),
        (r[key] || []).length
          ? el("ul", { class: "statement-list" }, ...r[key].map(statementItem))
          : hint("이 항목에 해당하는 진술이 없습니다."))),
      el("section", { class: "statements" },
        el("h5", {}, "기록만으로 확인할 수 없는 부분"),
        r.not_determined.length ? textList(r.not_determined, "plain-list") : hint("근거만으로 확인되지 않는 부분이 없다고 보고됩니다.")),
      el("p", { class: "stats" }, `진술 ${r.stats.total}개 · ${stats}`));
    $("report-prompt-body").replaceChildren(...(r.prompt_findings.length
      ? r.prompt_findings.map((f) => el("section", { class: "fb-item" },
        el("h5", {}, f.aspect_label),
        statementItem(f.observation),
        f.suggestion ? el("p", { class: "suggestion" }, el("strong", {}, "제안: "), f.suggestion) : null,
        f.suggestion ? explain(SUGGESTION_NOTE) : null))
      : [hint("이 요청 범위에서 구체화하면 좋은 부분이 확인되지 않았습니다.")]));
    $("report-gaps-body").replaceChildren(...(r.gaps.length
      ? [el("ul", { class: "statement-list" }, ...r.gaps.map((g) => statementItem({ ...g, type_label: g.concern_label })))]
      : [hint("기록에서 확인되는 보완 포인트가 없습니다.")]));
    $("report-actions-body").replaceChildren(...[
      ...r.actions.map((a) => el("section", { class: "fb-item" },
        el("h5", {}, a.kind_label),
        el("p", { class: "statement-text" }, " ", el("span", { class: "tags" }, tag(a.kind_label, "category")), " ", a.text),
        evidenceChips(a.evidence_ids),
        explain(SUGGESTION_NOTE))),
      r.dropped_id_count > 0 ? hint(`보낸 근거에 없는 근거 ID ${count(r.dropped_id_count)}개는 표시에서 뺐습니다.`) : null,
      ...(r.actions.length ? [] : [hint("실행 가능한 개선 행동이 만들어지지 않았습니다.")]),
    ].filter(Boolean));
  }

  function analysisResult(a) {
    const stats = STATS.map(([key, label]) => `${label} ${a.stats[key] || 0}`).join(" · ");
    return el("article", { class: "card-block" },
      el("header", { class: "result-head" }, el("h4", {}, "Claude 심층 분석",
        el("span", { class: "subtitle" }, "고른 결정 하나에 대한 이유·대안·가정·위험·확인할 점")), modelTags(a)),
      el("p", { class: "lead-strong" }, a.decision_summary),
      hint(a.scope_note),
      el("p", { class: "stats" }, `진술 ${a.stats.total}개 · ${stats}`),
      ...Object.entries(a.section_labels).map(([key, label]) => el("section", { class: "statements" },
        el("h5", {}, label),
        (a.sections[key] || []).length
          ? el("ul", { class: "statement-list" }, ...a.sections[key].map(statementItem))
          : hint("이 항목에 해당하는 진술이 없습니다."))));
  }

  function statementLine(s) {
    // Review points also carry a type (판단, 문제, 반례, ...); Claude's statements do not.
    return [s.type_label ? [tag(s.type_label, "muted"), " "] : null, gradeBadge(s), " ", s.text];
  }

  function statementItem(s) {
    const invalid = new Set(s.invalid_ids || []);
    return linkStatement(el("li", { class: `statement grade-${s.grade}` },
      el("p", { class: "statement-text" }, ...statementLine(s)),
      s.quote ? el("blockquote", { class: "quote" }, el("span", { class: "quote-label" }, "인용한 원문"), s.quote) : null,
      evidenceChips(s.evidence_ids, s.invalid_ids),
      explain(GRADE_GUIDE[s.grade])), s.evidence_ids.filter((id) => !invalid.has(id)));
  }

  function renderReviews() {
    const { results, consents } = state.view;
    const reviews = results.reviews || {};
    // One review per provider: its button goes once a job exists, and a failed job offers its retry on its line.
    const open = results.analysis ? REVIEWERS.filter((p) => !reviews[p] && !state.jobs[`review:${p}`]) : [];
    const declined = open.filter((p) => consents[`review:${p}`] && !consents[`review:${p}`].providers[p]);
    $("review-section").hidden = !results.analysis;
    $("review-actions").replaceChildren(
      ...(open.length ? [el("div", { class: "actions" }, ...open.map((p) => actionButton(JOBS[`review:${p}`].name, () => startReview(p, false), "secondary")))] : []),
      ...declined.map((p) => hint(`${PROVIDER_NAMES[p]} 전송에 동의하지 않아 검토를 요청하지 않았습니다. 버튼을 누르면 미리보기를 다시 엽니다.`)));
    for (const p of REVIEWERS) renderJobLine(`review-${p}`);
    $("review-list").replaceChildren(...REVIEWERS.filter((p) => reviews[p]).map((p) => reviewCard(p, reviews[p])));
    $("comparison").hidden = !results.comparison;
    $("comparison-body").replaceChildren(...(results.comparison ? comparisonGroups(results.comparison) : []));
  }

  function reviewCard(provider, r) {
    const name = `${PROVIDER_NAMES[provider]} 독립 검토`;
    const body = r.format_ok
      ? [el("p", { class: "lead-strong" }, r.summary),
        r.points.length ? el("ul", { class: "statement-list" }, ...r.points.map(statementItem)) : hint("지적한 점이 없습니다.")]
      : [el("p", { class: "tags" }, badge("형식 미확인", "warn", "약속한 형식이 아니라 근거 등급을 붙이지 못한 원문입니다.")),
        hint("약속한 JSON 형식이 아니어서 근거 등급과 비교 없이, 받은 글을 그대로 보여 줍니다."),
        el("pre", { class: "payload", tabindex: "0", "aria-label": `${name} 응답 원문` }, r.text || "")];
    return el("article", { class: "card-block" }, el("header", { class: "result-head" }, el("h5", {}, name), modelTags(r)), ...body);
  }

  function comparisonGroups(c) {
    const name = (p) => PROVIDER_NAMES[p] || p;
    const said = (providers, statements) => el("dl", { class: "cmp-said" }, ...providers.flatMap((p) => [
      el("dt", {}, name(p)), ...statements[p].map((s) => el("dd", {}, ...statementLine(s)))]));
    const cited = (entry) => el("li", { class: "cmp-entry" },
      el("div", { class: "cmp-head" }, evidenceChips([entry.evidence_id]), el("span", {}, entry.providers.map(name).join(" · "))),
      said(entry.providers, entry.statements));
    const group = (key, counted, items) => el("section", {},
      el("h6", {}, `${c.labels[key]} · ${counted}`),
      items.length ? el("ul", { class: "cmp-list" }, ...items) : hint("해당하는 의견이 없습니다."));
    const loose = c.different.reduce((n, d) => n + d.statements.length, 0);
    return [
      el("p", { class: "callout callout-info" }, `${c.note}.`),
      group("same_evidence", `근거 ${c.same_evidence.length}개`, c.same_evidence.map(cited)),
      group("only_one", `근거 ${c.only_one.length}개`, c.only_one.map(cited)),
      group("different", `의견 ${loose}개`, c.different.map((d) => el("li", { class: "cmp-entry" }, said([d.provider], { [d.provider]: d.statements })))),
    ];
  }

  function renderSummary() {
    const result = state.view.results.summary;
    $("summary-card").hidden = !result && !state.jobs.summary;
    renderJobLine("summary");
    $("summary-body").replaceChildren(...(result && result.summary ? [el("p", { class: "prose-block" }, result.summary)] : []));
  }

  function slotJob(slot) {
    // The last kind of the slot that has a job, so feedback2 takes over feedback1's line.
    return Object.keys(JOBS).filter((kind) => JOBS[kind].slot === slot).map((kind) => state.jobs[kind]).filter(Boolean).pop();
  }

  function renderJobLine(slot) {
    const box = $(`job-${slot}`);
    const job = slotJob(slot);
    // Unchanged content stays put, so the live region is not re-announced on every poll.
    const key = job ? `${job.id} ${job.status} ${job.progress} ${job.model}` : "";
    if (box.dataset.key === key) return;
    box.dataset.key = key;
    if (!job) {
      box.replaceChildren();
      return;
    }
    const failed = job.status === "failed";
    const error = job.error || { message: "작업을 마치지 못했습니다.", retryable: false };
    const advice = Object.hasOwn(ADVICE, error.code || "") ? ADVICE[error.code] : error.retryable ? "다시 시도할 수 있습니다." : null;
    const retry = failed && error.retryable ? actionButton("다시 시도", RETRY[job.kind], "secondary") : null;
    if (retry) retry.setAttribute("aria-label", `${JOBS[job.kind].name} 다시 시도`);
    box.replaceChildren(...[
      el("p", { class: "job-status" },
        el("span", { class: "job-name" }, JOBS[job.kind].name),
        tag(job.status_label, failed ? "danger" : job.status === "done" ? "ok" : "info"),
        job.progress && !FINAL.has(job.status) ? el("span", {}, job.progress) : null,
        job.model ? el("span", { class: "job-model" }, `모델 ${job.model}`) : null,
        job.mock ? tag(MOCK, "mock") : null),
      failed ? el("div", { class: "alert alert-error" }, el("p", {}, error.message), advice ? el("p", {}, advice) : null,
        job.kind === "summary" ? el("p", {}, "요약이 없어도 다른 단계는 그대로 진행됩니다.") : null) : null,
      retry,
    ].filter(Boolean));
  }

  function step3Reason(view) {
    const card = view && view.selected ? selectedCard() : null;
    if (card && card.evidence_level === "insufficient") {
      return "근거 수준이 '부족'이라 가정 상황과 피드백을 만들지 않습니다. 2-4의 규칙 엔진 근거를 확인해 주세요.";
    }
    const consent = card && view.consents.analysis;
    if (consent && !consent.providers.anthropic && !state.jobs.analysis) {
      return "Claude 전송에 동의하지 않아 가정 상황과 피드백을 만들 수 없습니다. 2-4에서 전송 미리보기를 다시 열 수 있습니다.";
    }
    return "심층 분석을 마치면 그 결정을 바탕으로 가정 상황을 만들고, 답변에 대한 피드백을 받을 수 있습니다.";
  }

  function renderStep3() {
    const view = state.view;
    const results = view && view.selected ? view.results : { feedback: [] };
    const { analysis, whatif } = results;
    const done = results.feedback;
    const attempt = done.length + 1;
    const job = state.jobs[`feedback${attempt}`];
    // While its job runs the form shows what was sent, which only this page session knows (not after a reload).
    const showForm = Boolean(whatif && whatif.can_generate && attempt <= 2 && (job
      ? job.status === "failed" || state.formAttempt === attempt
      : attempt === 1 || state.resubmit));
    $("step-whatif").classList.toggle("is-pending", !analysis);
    $("whatif-empty").hidden = Boolean(analysis);
    $("whatif-empty").textContent = step3Reason(view);
    $("whatif-intro").replaceChildren(...(analysis && !whatif && !state.jobs.whatif && !reportWait()
      ? [hint(`자기평가로 정한 난이도(${view.tier_label})에 맞춰 Claude가 가정 상황을 만듭니다. 심층 분석에서 동의한 근거 범위만 보내며, 개발 과정 리포트가 있으면 그 보완 포인트도 함께 참고합니다.`),
        actionButton("가정 상황 만들기", startWhatif)]
      : []));
    renderJobLine("whatif");
    $("whatif-card").replaceChildren(...(whatif ? [whatifCard(whatif)] : []));
    $("feedback-list").replaceChildren(...done.map(feedbackView));
    $("resubmit-box").replaceChildren(...(done.length === 1 && !showForm
      ? [hint("피드백을 참고해 답변을 고쳐 한 번 더 제출할 수 있습니다. 지금 답변을 미리 채워 둡니다."),
        actionButton("다시 제출(1회)", openResubmit, "secondary")]
      : []));
    $("answer-form").hidden = !showForm;
    if (showForm && state.formAttempt !== attempt) {
      state.formAttempt = attempt;
      if (attempt === 2) fillAnswers(done[0].answers);
    }
    $("answer-title").textContent = attempt === 2 ? "답변 고쳐 쓰기 (다시 제출 1회)" : "내 답변";
    $("answer-submit").textContent = attempt === 2 ? "다시 제출하고 최종 피드백 받기" : "피드백 받기";
    renderJobLine("feedback");
  }

  function whatifCard(w) {
    const head = el("p", { class: "tags" },
      badge("가정 상황", "hypo", BADGE_HELP.hypo), badge(`난이도 ${w.tier_label}`, "category", "처음에 고른 자기평가로 정한 난이도입니다."),
      tag(`모델 ${w.model}`, "muted"), w.mock ? badge(MOCK, "mock", BADGE_HELP.mock) : null);
    if (!w.can_generate) {
      return el("article", { class: "card-block whatif-card" }, head,
        el("h3", {}, "가정 상황을 만들지 않았습니다"),
        el("p", {}, w.reason),
        hint("근거가 부족하면 임의의 과제를 만들지 않습니다."));
    }
    return el("article", { class: "card-block whatif-card" }, head,
      el("h3", {}, w.title),
      el("p", { class: "prose-block" }, w.scenario),
      el("h4", {}, "가정한 변화"),
      textList(w.hypothetical_changes, "plain-list"),
      el("h4", {}, "관련 근거"),
      evidenceChips(w.related_evidence_ids) || hint("연결된 근거가 없습니다."),
      w.dropped_id_count ? hint(`보낸 근거에 없는 근거 ID ${w.dropped_id_count}개는 뺐습니다.`) : null,
      el("h4", {}, "질문"),
      el("dl", { class: "questions" }, ...ANSWER_FIELDS.flatMap((field) => [
        el("dt", {}, w.question_labels[field]), el("dd", { id: `q-${field}` }, w.questions[field])])),
      hint("실제로 실행된 적 없는 가정 상황이라 정해진 정답이 없고, 일치·불일치를 표시하지 않습니다."),
      explain("정답 맞히기가 아니라 생각을 글로 꺼내 보는 연습이에요. 관련 근거를 눌러 원래 결정을 다시 보고, 아래 네 칸에 내 말로 답해 보세요."));
  }

  function pointList(title, items, tone) {
    if (!items.length) return null;
    return el("div", { class: `points points-${tone}` }, el("p", { class: "points-title" }, title), textList(items, "plain-list"));
  }

  function feedbackView(fb) {
    const final = fb.attempt === 2;
    const review = fb.instruction_review;
    const check = fb.instruction_check;
    const has = (value) => (value ? "들어 있음" : "드러나지 않음");
    return el("article", { class: "card-block feedback" },
      el("header", { class: "result-head" },
        el("h3", { id: `fb-title-${fb.attempt}`, tabindex: "-1" }, final ? "최종 피드백" : "첫 피드백"), modelTags(fb)),
      ...fb.items.map((item) => el("section", { class: "fb-item" },
        el("h4", {}, item.field_label),
        el("p", { class: "my-answer" }, el("strong", {}, "내 답변: "), fb.answers[item.field] || "(비어 있음)"),
        pointList("잘한 점", item.strengths, "ok"),
        pointList("보완할 점", item.gaps, "warn"),
        item.hint ? el("p", {}, el("strong", {}, "힌트: "), item.hint) : null,
        evidenceChips(item.evidence_ids),
        explain("피드백도 AI의 의견입니다. 맞다고 생각하는 부분만 골라 다시 제출할 때 반영해 보세요."))),
      el("section", { class: "fb-item" },
        el("h4", {}, "AI 지시 점검"),
        el("p", { class: "tags" }, badge("모델 의견(해석)", "info", BADGE_HELP.opinion)),
        textList([`요구 조건: ${has(review.has_requirements)}`, `검증 단계: ${has(review.has_verification_steps)}`], "plain-list"),
        review.note ? el("p", {}, review.note) : null,
        el("p", { class: "tags" }, badge("규칙 엔진 확인", "category", "AI가 아니라 프로그램이 세션 기록과 직접 대조한 결과입니다.")),
        check.mentions.length ? el("ul", { class: "plain-list" }, ...check.mentions.map((m) => el("li", {},
          el("code", {}, m.text),
          ` ${m.kind === "file" ? "테스트 파일" : "테스트 실행기"} · 세션 기록 ${m.found ? "있음" : "없음"} · ${m.status_label}`))) : null,
        hint(check.note)),
      el("section", { class: "fb-item" },
        el("h4", {}, "자기평가와 이번 답변 비교"),
        hint("점수가 아닙니다. 답변에 드러난 정도는 모델 추정이며, 자기평가와 나란히 보여 줄 뿐입니다."),
        el("div", { class: "table-wrap", role: "region", tabindex: "0", "aria-label": "자기평가와 이번 답변 비교" },
          el("table", { class: "axes" },
            el("thead", {}, el("tr", {}, ...["축", "자기평가", "이번 답변에 드러난 정도(모델 추정)", "비교"].map((text) => el("th", { scope: "col" }, text)))),
            el("tbody", {}, ...fb.comparison.map((row) => el("tr", {},
              el("th", { scope: "row" }, row.axis_label), el("td", {}, row.self_label), el("td", {}, row.observed_label), el("td", {}, row.message))))))),
      el("section", { class: "fb-item" }, el("h4", {}, "코칭"), el("p", {}, fb.coaching)),
      final ? el("section", { class: "fb-item" }, el("h4", {}, "첫 피드백과 비교"), el("p", {}, fb.explanation)) : null,
      final && fb.next_difficulty ? el("section", { class: "fb-item" },
        el("h4", {}, "다음 난이도 제안"),
        el("p", {}, `${fb.next_difficulty.label}: ${fb.next_difficulty.message}`),
        hint("글로만 제안하며 다음 과제는 만들지 않습니다.")) : null);
  }

  function fillAnswers(answers) {
    for (const field of ANSWER_FIELDS) {
      const area = $(`ans-${field}`);
      area.value = answers[field] || "";
      updateCounter(area);
    }
  }

  function updateCounter(area) {
    $(`${area.id}-count`).textContent = `${count(area.value.length)} / ${count(area.getAttribute("maxlength"))}자`;
  }

  // ---------- Evidence and code viewer ----------

  /** One original record under a human name; its ID, source position and server label sit in 상세 정보. */
  function evidenceItem(item, anchor = false) {
    let body;
    if (item.diff) body = collapsible(diffViewer(item), item.diff.length);
    else if (CODE_KINDS.has(item.kind)) body = collapsible(textViewer(item), item.text.split("\n").length);
    else body = el("p", { class: "prose" }, item.text);
    return el("article", { class: `evidence ev-kind-${item.kind}`, id: anchor ? `ev-${item.id}` : null, tabindex: anchor ? "-1" : null,
      "aria-label": `${kindName(item)}${item.file ? ` ${item.file}` : ""}` },
      el("header", { class: "evidence-head" },
        el("span", { class: "ev-kind" }, kindName(item)),
        item.file ? el("code", { class: "ev-file" }, item.file) : null,
        item.later_fix ? badge("이후 수정 있음(정답 아님)", "warn", BADGE_HELP.later) : null),
      body,
      metaDetails([["근거 ID", item.id], ["원문 위치", `턴 ${item.turn} · JSONL ${item.line}번째 줄`], ["원래 기록 종류", item.kind_label]]));
  }

  function metaDetails(rows) {
    return el("details", { class: "ev-meta" },
      el("summary", {}, "상세 정보"),
      el("dl", { class: "meta" }, ...rows.flatMap(([label, value]) => [el("dt", {}, label), el("dd", {}, value)])));
  }

  function chapterHead(n, phase, detail, id) {
    const [title, help] = PHASES[phase];
    return el("h5", { class: `chapter chapter-${phase}`, id, tabindex: id ? "-1" : null },
      el("span", { class: "chapter-num", "aria-hidden": "true" }, String(n)),
      el("span", { class: "chapter-title" }, title),
      el("span", { class: "chapter-detail" }, detail || help));
  }

  /** Session-order chapters: requests, edits and checks; AI text joins the step it leads into, tool notes the step before. */
  function storyChapters(items) {
    const phases = items.map((item) => PHASE_OF[item.kind] || null);
    let next = null;
    for (let i = items.length - 1; i >= 0; i -= 1) {
      if (PHASE_OF[items[i].kind]) next = PHASE_OF[items[i].kind];
      else if (items[i].kind === "assistant_text") phases[i] = next;
    }
    let prev = "ask";
    for (let i = 0; i < items.length; i += 1) prev = phases[i] = phases[i] || prev;
    const chapters = [];
    items.forEach((item, i) => {
      const last = chapters[chapters.length - 1];
      if (last && last.phase === phases[i]) last.items.push(item);
      else chapters.push({ phase: phases[i], items: [item] });
    });
    return chapters;
  }

  function chapterSummary(items) {
    const counts = new Map();
    for (const item of items) counts.set(kindName(item), (counts.get(kindName(item)) || 0) + 1);
    return [...counts].map(([name, n]) => `${name} ${n}`).join(" · ");
  }

  function renderStory(evidence) {
    const chapters = storyChapters(evidence);
    $("evidence-plot").hidden = chapters.length === 0;
    $("evidence-plot").replaceChildren(...(chapters.length ? [
      el("p", { class: "plot-title" }, "흐름 한눈에"),
      el("ol", { class: "plot-list" }, ...chapters.map((c, i) => {
        const link = el("button", { type: "button", class: "plot-step" },
          el("span", { class: "chapter-num", "aria-hidden": "true" }, String(i + 1)),
          el("span", {}, el("strong", {}, PHASES[c.phase][0]), el("span", { class: "plot-count" }, chapterSummary(c.items))));
        link.addEventListener("click", () => focusHeading(`chapter-${i + 1}`));
        return el("li", { class: `plot-item chapter-${c.phase}` }, link);
      }))] : []));
    $("evidence-list").replaceChildren(...chapters.map((c, i) => el("section", { class: `story-chapter chapter-${c.phase}` },
      chapterHead(i + 1, c.phase, null, `chapter-${i + 1}`),
      ...c.items.map((item) => evidenceItem(item, true)))));
  }

  function collapsible(viewer, lineCount) {
    const details = el("details", {}, el("summary", {}, `${lineCount}줄 · 접기/펼치기`), viewer);
    details.open = lineCount <= 40;
    return details;
  }

  function codeTable(label, rows) {
    return el("div", { class: "code-viewer", role: "region", tabindex: "0", "aria-label": label },
      el("table", { class: "code" }, el("tbody", {}, ...rows)));
  }

  function diffViewer(item) {
    const signs = { "+": ["+", "추가된 줄"], "-": ["−", "삭제된 줄"], " ": ["", ""] };
    let n = 0;
    const rows = item.diff.map((row) => {
      if (row.op === "@") {
        n = 0;
        return el("tr", { class: "row-hunk" }, el("td", { class: "ln" }), el("td", { class: "op" }), el("td", { class: "src" }, row.text));
      }
      n += 1;
      const [sign, spoken] = signs[row.op] || signs[" "];
      const kind = row.op === "+" ? "row-add" : row.op === "-" ? "row-del" : "";
      return el("tr", { class: kind },
        el("td", { class: "ln" }, String(n)),
        el("td", { class: "op" }, el("span", { "aria-hidden": "true" }, sign), spoken ? el("span", { class: "visually-hidden" }, spoken) : null),
        el("td", { class: "src" }, row.text));
    });
    return codeTable(`${item.id} 코드 변경${item.file ? ` ${item.file}` : ""}`, rows);
  }

  function textViewer(item) {
    const rows = item.text.split("\n").map((line, i) =>
      el("tr", {}, el("td", { class: "ln" }, String(i + 1)), el("td", { class: "src" }, line)));
    return codeTable(`${item.id} ${item.kind_label}`, rows);
  }

  async function openEvidence(id) {
    let item = state.view && state.view.evidence.find((e) => e.id === id);
    if (!item) {
      try {
        item = await api("GET", `/api/evidence/${encodeURIComponent(id)}`);
      } catch (err) {
        if (err.code === "no_workspace") handleLostWorkspace(err);
        else announce(err.message);
        return;
      }
    }
    $("evidence-dialog-id").textContent = id;
    $("evidence-dialog-body").replaceChildren(evidenceItem(item));
    if (!$("evidence-dialog").open) $("evidence-dialog").showModal();
  }

  // ---------- Actions: prediction, consent, analysis, What-if, feedback ----------

  async function act(box, work) {
    if (isLocked()) return;
    box.hidden = true;
    setBusy(true);
    try {
      await work();
    } catch (err) {
      if (err.code === "no_workspace") {
        handleLostWorkspace(err);
        return;
      }
      showError(box, err);
      announce(err.message);
      // Another tab, or a response lost after the server acted, may have moved the workspace on.
      if (err.code === "already_submitted" || err.status === 0) await refresh().catch(() => {});
    } finally {
      setBusy(false);
    }
  }

  async function submitPrediction() {
    if (isLocked()) return;
    const checked = $("predict-form").querySelector('input[name="prediction"]:checked');
    if (!checked) {
      showError($("predict-error"), new ApiError(0, "", "통과·실패·판단 불가 중 하나를 골라 주세요."));
      $("predict-pass").focus();
      return;
    }
    $("predict-error").hidden = true;
    $("predict-dialog-choice").textContent = `내 예측: ${CHOICES[checked.value]}`;
    if (!(await confirmDialog($("predict-dialog")))) return;
    await act($("predict-error"), async () => {
      const r = await api("POST", "/api/prediction", { choice: checked.value, reason: $("predict-reason").value });
      const retro = { available: true, submitted: true, prediction: r.prediction, reveal: r.reveal, label: r.label, label_text: r.label_text, checkpoints: r.checkpoints };
      state.view = { ...state.view, retro, evidence: r.evidence };
      renderFlow();
      announce(`예측을 저장했습니다. 기록과 비교: ${r.label_text}`);
      focusHeading("reveal-title");
    });
  }

  function renderConsent(preview) {
    $("consent-stage").textContent = preview.stage_label;
    state.consentBoxes = [];
    $("consent-list").replaceChildren(...preview.providers.map((p) => {
      const id = `consent-${p.name}`;
      let head;
      if (p.applicable) {
        const input = el("input", { type: "checkbox", id, "aria-describedby": `${id}-info` });
        state.consentBoxes.push({ name: p.name, input });
        head = el("div", { class: "consent-check" }, input, el("label", { for: id }, p.label));
      } else {
        head = el("p", { class: "consent-check" }, p.label, " ", tag("보내지 않음", "muted"));
      }
      const policy = el("ul", { class: "policy", "aria-label": "데이터 정책" }, ...p.policy.map((line) => el("li", {}, line)));
      // DeepSeek is a Public Preview (inputs may train the model, no opt-out or ZDR): its policy comes first, as a warning.
      const warn = p.name === "deepseek";
      return el("article", { class: "consent-provider" },
        head,
        el("div", { id: `${id}-info` },
          warn ? el("div", { class: "callout policy-warn" }, el("p", {}, el("strong", {}, "데이터 정책 주의: 보내기 전에 확인하세요")), policy) : null,
          p.mock ? el("p", { class: "tags" }, tag(MOCK, "mock")) : null,
          el("dl", { class: "meta" },
            el("dt", {}, "목적지"), el("dd", {}, p.destination),
            el("dt", {}, "목적"), el("dd", {}, p.purpose),
            el("dt", {}, p.applicable ? "보낼 내용" : "보내지 않는 이유"),
            el("dd", {}, p.applicable ? `${count(p.chars)}자 · 항목 ${p.items.length}개` : p.skip_reason)),
          warn ? null : policy),
        p.applicable ? el("details", {},
          el("summary", {}, `보낼 내용 원문 보기 (${count(p.chars)}자)`),
          el("pre", { class: "payload", tabindex: "0", "aria-label": `${p.label}에 보낼 내용` }, p.payload_text)) : null);
    }));
    $("consent-all").hidden = state.consentBoxes.length === 0;
  }

  /** Preview -> dialog -> POST /api/consent. Returns {previewId, providers} or null when the user cancelled. */
  async function consentFlow(stage, answers) {
    const preview = await api("POST", "/api/preview", answers ? { stage, answers } : { stage });
    renderConsent(preview);
    if (!(await confirmDialog($("consent-dialog")))) {
      announce("전송하지 않았습니다.");
      return null;
    }
    const providers = Object.fromEntries(state.consentBoxes.map(({ name, input }) => [name, input.checked]));
    const r = await api("POST", "/api/consent", { preview_id: preview.preview_id, providers });
    state.view.consents[stage] = { providers: r.providers };
    return { previewId: preview.preview_id, providers: r.providers };
  }

  function startAnalysis(direct) {
    // direct: insufficient evidence (no consent step, no model call) or a retry under the recorded consent.
    return act($("analysis-error"), async () => {
      if (!direct && !(await consentFlow("analysis"))) return;
      const r = await api("POST", "/api/analysis");
      announce(r.notice || "심층 분석을 시작했습니다.");
      if (r.job_id) track("analysis", r.job_id);
      if (r.summary_job_id) track("summary", r.summary_job_id);
      if (r.job_id) renderFlow();
      else await refresh(); // the notice states (declined consent, insufficient evidence) come from the stored view
      focusHeading("analysis-title");
    });
  }

  function retrySummary() {
    return act($("summary-error"), async () => {
      track("summary", (await api("POST", "/api/summary")).job_id);
      renderSummary();
      announce("Local LLM 요약을 다시 요청했습니다.");
      focusHeading("summary-title");
    });
  }

  function startReview(provider, retry) {
    // retry: a new job under the consent already recorded for this review's stage.
    const stage = `review:${provider}`;
    return act($("review-error"), async () => {
      const consent = retry ? null : await consentFlow(stage);
      if (!retry && !consent) return;
      if (consent && !consent.providers[provider]) {
        announce(`${PROVIDER_NAMES[provider]} 전송에 동의하지 않아 검토를 요청하지 않았습니다.`);
      } else {
        track(stage, (await api("POST", "/api/reviews", { provider })).job_id);
        announce(`${JOBS[stage].name}를 요청했습니다.`);
      }
      renderReviews();
      focusHeading("review-title"); // the button that had focus is gone
    });
  }

  function startWhatif() {
    return act($("whatif-error"), async () => {
      track("whatif", (await api("POST", "/api/whatif")).job_id);
      renderFlow();
      announce("가정 상황을 만드는 중입니다.");
      focusHeading("whatif-title");
    });
  }

  function openResubmit() {
    state.resubmit = true;
    renderStep3();
    focusHeading("answer-title");
  }

  function submitAnswers() {
    const attempt = state.view.results.feedback.length + 1;
    const answers = Object.fromEntries(ANSWER_FIELDS.map((field) => [field, $(`ans-${field}`).value]));
    const empty = ANSWER_FIELDS.find((field) => !answers[field].trim());
    if (empty) {
      showError($("answer-error"), new ApiError(0, "", "네 항목을 모두 적어 주세요."));
      $(`ans-${empty}`).focus();
      return null;
    }
    return act($("answer-error"), async () => {
      const stage = `feedback${attempt}`;
      const consent = await consentFlow(stage, answers);
      if (!consent) return;
      if (!consent.providers.anthropic) {
        showError($("answer-error"), new ApiError(0, "", "Claude 전송에 동의하지 않아 피드백을 요청하지 않았습니다. 답변은 그대로 남아 있습니다."));
        return;
      }
      const r = await api("POST", "/api/feedback", { attempt, preview_id: consent.previewId });
      track(stage, r.job_id);
      renderStep3();
      announce(`${JOBS[stage].name}을 요청했습니다.`);
    });
  }

  // ---------- Jobs: polling and refresh ----------

  function track(kind, id) {
    state.jobs[kind] = { id, kind, status: "queued", status_label: "대기", progress: "", model: null, mock: false, error: null };
    poll(id, kind);
  }

  function resumePolls() {
    // Only the kinds this page renders (every kind the server has today).
    for (const job of Object.values(state.jobs)) if (JOBS[job.kind] && !FINAL.has(job.status)) poll(job.id, job.kind);
  }

  async function poll(id, kind) {
    if (state.polling.has(id)) return;
    const gen = state.gen;
    state.polling.set(id, kind);
    setBusy(state.busy);
    let job;
    try {
      for (;;) {
        job = await api("GET", `/api/jobs/${encodeURIComponent(id)}`);
        if (gen !== state.gen) return;
        state.jobs[job.kind] = job;
        renderJobLine(JOBS[job.kind].slot);
        if (FINAL.has(job.status)) break;
        await new Promise((resolve) => { window.setTimeout(resolve, POLL_MS); });
        if (gen !== state.gen) return;
      }
    } catch (err) {
      if (gen !== state.gen) return;
      if (err.code === "no_workspace") {
        handleLostWorkspace(err);
        return;
      }
      announce(`${JOBS[kind].name} 상태를 확인하지 못했습니다. ${err.message} 페이지를 새로 고치면 다시 확인합니다.`);
      return;
    } finally {
      state.polling.delete(id);
      setBusy(state.busy);
    }
    await jobFinished(job);
  }

  function lostFocus(node) {
    if (!node || node === document.body || !node.isConnected) return true;
    for (let n = node; n; n = n.parentElement) if (n.hidden) return true;
    return false;
  }

  async function jobFinished(job) {
    const info = JOBS[job.kind];
    announce(job.status === "done" ? info.done : `${info.name}에 실패했습니다. ${job.error ? job.error.message : ""}`);
    const gen = state.gen;
    const focused = document.activeElement;
    try {
      await refresh(job.kind === "summary");
    } catch (err) {
      if (err.code === "no_workspace") handleLostWorkspace(err);
      else announce(err.message);
      return;
    }
    // Move focus only when the re-render removed or hid what had it.
    if (gen === state.gen && info.heading && job.status === "done" && lostFocus(focused)) focusHeading(info.heading);
  }

  async function refresh(summaryOnly = false) {
    applyView(await api("GET", "/api/workspace"));
    if (summaryOnly) renderSummary();
    else renderFlow();
    resumePolls();
  }

  // ---------- Workspace lifecycle ----------

  async function restore() {
    let view;
    try {
      view = await api("GET", "/api/workspace");
    } catch (err) {
      resetToUpload();
      if (err.status !== 404) announce(err.message);
      return;
    }
    resetToUpload();
    renderWorkspace(view);
    if (!view.selected) return;
    // The workspace view carries every visible evidence item and stored result: one request per reload.
    renderSelection();
    renderFlow();
    resumePolls();
  }

  function initFlow() {
    $("predict-form").addEventListener("submit", (event) => {
      event.preventDefault();
      submitPrediction();
    });
    $("answer-form").addEventListener("submit", (event) => {
      event.preventDefault();
      submitAnswers();
    });
    $("consent-all").addEventListener("click", () => {
      for (const { input } of state.consentBoxes) input.checked = true;
    });
    for (const area of document.querySelectorAll("textarea")) area.addEventListener("input", () => updateCounter(area));
  }

  function initDelete() {
    $("delete-open").addEventListener("click", async () => {
      if (state.busy || !(await confirmDialog($("delete-dialog")))) return;
      setBusy(true);
      try {
        await api("DELETE", "/api/workspace");
        resetToUpload();
        $("file-input").value = "";
        chooseFile(null);
        announce("작업 공간을 삭제했습니다.");
        focusHeading("upload-title");
      } catch (err) {
        announce(err.message);
      } finally {
        setBusy(false);
      }
    });
  }

  async function renderModes() {
    // The server's mode per provider, so a mock run is never mistaken for a live one.
    try {
      const { modes } = await api("GET", "/api/health");
      $("modes").replaceChildren("제공자 모드:", ...MODE_NAMES.map(([key, name]) => {
        const [label, tone] = MODES[modes[key]] || ["미확인", "muted"];
        return el("span", { class: "mode" }, name, " ", tag(label, tone));
      }));
    } catch {
      $("modes").textContent = "제공자 모드를 확인하지 못했습니다. 모의 응답은 결과마다 붙는 '모의 응답(개발용)' 배지로 구분해 주세요.";
    }
  }

  initTheme();
  initExplain();
  initTour();
  initUpload();
  initHelp();
  initSelection();
  initFlow();
  initDelete();
  renderModes();
  restore();
})();
