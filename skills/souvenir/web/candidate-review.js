(function () {
  "use strict";

  const state = {
    packet: null,
    activeSection: "",
    query: "",
    maxPrice: "",
    sort: "recommended",
    verifiedOnly: false,
    refundableOnly: false,
    conditionSection: "",
    searching: false,
    refreshing: false,
    sessionExpired: false,
    selected: new Set(),
    expanded: new Set(),
  };

  const $ = (id) => document.getElementById(id);
  const elements = {
    tabs: $("section-tabs"),
    grid: $("candidate-grid"),
    empty: $("empty-state"),
    title: $("results-title"),
    label: $("active-section-label"),
    count: $("results-count"),
    chips: $("condition-chips"),
    search: $("candidate-search"),
    price: $("price-limit"),
    sort: $("sort-order"),
    verified: $("verified-only"),
    refundable: $("refundable-only"),
    reset: $("reset-filters"),
    emptyReset: $("empty-reset"),
    selectionCount: $("selection-count"),
    submit: $("submit-selection"),
    modal: $("submit-modal"),
    modalClose: $("modal-close"),
    modalCancel: $("modal-cancel"),
    modalConfirm: $("modal-confirm"),
    selectedList: $("selected-list"),
    submitResult: $("submit-result"),
    status: $("review-status"),
    sessionRefresh: $("session-refresh"),
    conditionFields: $("condition-editor-fields"),
    rerunSearch: $("rerun-search"),
    editorStatus: $("editor-status"),
    emptyTitle: $("empty-title"),
    emptyCopy: $("empty-copy"),
  };

  const defaultEmptyState = {
    title: "조건에 맞는 후보가 없어요.",
    copy: "검색어·금액 필터를 조금 느슨하게 하거나 다른 탭을 확인해보세요.",
    action: "필터 초기화",
  };

  const conditionDefinitions = {
    flights: [
      { key: "flyFrom", label: "출발 공항", type: "text", placeholder: "ICN", required: true },
      { key: "flyTo", label: "도착 공항", type: "text", placeholder: "KIX", required: true },
      { key: "departureDate", label: "가는 날", type: "date", required: true },
      { key: "returnDate", label: "오는 날", type: "date" },
      { key: "adults", label: "성인", type: "number", min: 1, step: 1 },
      { key: "price_to", label: "총액 상한", type: "number", min: 0, step: 1000, suffix: "원" },
    ],
    hotels: [
      { key: "location", label: "지역·역", type: "text", placeholder: "난바", required: true, full: true },
      { key: "check_in", label: "체크인", type: "date", required: true },
      { key: "check_out", label: "체크아웃", type: "date", required: true },
      { key: "guests", label: "투숙 인원", type: "number", min: 1, step: 1 },
      { key: "budget_per_night_krw", label: "1박 상한", type: "number", min: 0, step: 1000, suffix: "원" },
      { key: "bed_type", label: "침대", type: "text", placeholder: "트윈" },
      { key: "refundable_only", label: "취소 가능 객실", type: "checkbox", full: true },
    ],
    places: [
      { key: "destination", label: "여행지", type: "text", placeholder: "오사카", required: true },
      { key: "query", label: "검색어", type: "text", placeholder: "조용한 카페", required: true, full: true },
      { key: "included_type", label: "장소 유형", type: "text", placeholder: "cafe" },
      { key: "rank_preference", label: "정렬", type: "select", options: [["RELEVANCE", "추천순"], ["DISTANCE", "거리순"]] },
      { key: "open_now", label: "지금 영업 중", type: "checkbox", full: true },
    ],
  };

  function text(value, fallback) {
    if (value === null || value === undefined || value === "") return fallback || "";
    return String(value);
  }

  function number(value) {
    if (typeof value === "number" && Number.isFinite(value)) return value;
    const raw = String(value ?? "").trim();
    if (!raw) return null;
    const parsed = Number(raw.replace(/[^0-9.-]/g, ""));
    return Number.isFinite(parsed) ? parsed : null;
  }

  function escapeText(value) {
    return text(value).replace(/[&<>"']/g, (char) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[char]));
  }

  function activePacketSections() {
    return Object.values(state.packet.sections || {});
  }

  function sectionByKey(key) {
    return (state.packet.sections || {})[key] || null;
  }

  function inputDate(value) {
    const raw = text(value).trim();
    if (/^\d{4}-\d{2}-\d{2}/.test(raw)) return raw.slice(0, 10);
    const match = raw.match(/^(\d{2})\/(\d{2})\/(\d{4})$/);
    return match ? `${match[3]}-${match[2]}-${match[1]}` : "";
  }

  function conditionValue(section, definition) {
    const search = section.search || {};
    if (definition.key === "budget_per_night_krw") return section.budget_per_night_krw ?? "";
    if (definition.key === "bed_type") return section.room_filters?.bed_type || "";
    if (definition.key === "refundable_only") return section.room_filters?.refundable_only === true;
    if (definition.key === "destination") return search.destination || state.packet.trip?.destination || "";
    let value = search[definition.key];
    if (!value && ["departureDate", "check_in"].includes(definition.key)) value = state.packet.trip?.start_date || state.packet.trip?.start;
    if (!value && ["returnDate", "check_out"].includes(definition.key)) value = state.packet.trip?.end_date || state.packet.trip?.end;
    if (definition.type === "date") return inputDate(value);
    return value ?? "";
  }

  function makeConditionField(section, definition) {
    const wrapper = document.createElement("div");
    wrapper.className = `editor-field${definition.full ? " full" : ""}${definition.type === "checkbox" ? " checkbox" : ""}`;
    const inputId = `condition-${section.key}-${definition.key}`;
    const label = document.createElement("label");
    label.htmlFor = inputId;
    label.textContent = definition.label;
    const value = conditionValue(section, definition);
    let input;
    if (definition.type === "select") {
      input = document.createElement("select");
      (definition.options || []).forEach(([optionValue, optionLabel]) => {
        const option = document.createElement("option");
        option.value = optionValue;
        option.textContent = optionLabel;
        input.appendChild(option);
      });
      input.value = value || definition.options?.[0]?.[0] || "";
    } else {
      input = document.createElement("input");
      input.type = definition.type;
      if (definition.placeholder) input.placeholder = definition.placeholder;
      if (definition.min !== undefined) input.min = String(definition.min);
      if (definition.step !== undefined) input.step = String(definition.step);
      if (definition.required) input.required = true;
      if (definition.type === "checkbox") input.checked = Boolean(value);
      else input.value = text(value);
    }
    input.id = inputId;
    input.dataset.conditionKey = definition.key;
    input.dataset.conditionType = definition.type;
    if (definition.type === "checkbox") {
      wrapper.append(input, label);
    } else {
      wrapper.append(label, input);
      if (definition.suffix) {
        const suffix = document.createElement("span");
        suffix.className = "editor-suffix";
        suffix.textContent = definition.suffix;
        wrapper.appendChild(suffix);
      }
    }
    return wrapper;
  }

  function renderConditionEditor(section) {
    elements.conditionFields.replaceChildren();
    const definitions = conditionDefinitions[section.key] || [];
    definitions.forEach((definition) => elements.conditionFields.appendChild(makeConditionField(section, definition)));
    state.conditionSection = section.key;
    elements.editorStatus.textContent = "";
    elements.editorStatus.classList.remove("is-error");
  }

  function collectConditions() {
    const conditions = {};
    elements.conditionFields.querySelectorAll("[data-condition-key]").forEach((input) => {
      const key = input.dataset.conditionKey;
      const type = input.dataset.conditionType;
      if (type === "checkbox") {
        conditions[key] = input.checked;
        return;
      }
      if (type === "number") {
        conditions[key] = input.value === "" ? null : Number(input.value);
        return;
      }
      conditions[key] = input.value.trim();
    });
    if (state.activeSection === "hotels") {
      conditions.room_filters = {
        bed_type: conditions.bed_type || "",
        refundable_only: Boolean(conditions.refundable_only),
      };
      delete conditions.bed_type;
      delete conditions.refundable_only;
    }
    return conditions;
  }

  function setSearchBusy(busy) {
    state.searching = busy;
    elements.rerunSearch.disabled = busy;
    elements.rerunSearch.classList.toggle("is-loading", busy);
    elements.rerunSearch.textContent = busy ? "검색 중…" : "다시 검색";
  }

  function isNetworkError(error) {
    const message = text(error?.message).toLowerCase();
    return error?.name === "TypeError" || /load failed|failed to fetch|networkerror|network request failed/.test(message);
  }

  function friendlyError(error, fallback) {
    if (error?.name === "AbortError") {
      return {
        message: "검색 시간이 길어지고 있어요. 조건을 줄여 다시 시도해주세요.",
        status: "검색 지연 · 다시 시도 필요",
        session: false,
      };
    }
    if (isNetworkError(error)) {
      return {
        message: "검색 세션이 만료됐어요. 세션 새로고침을 눌러 현재 화면과 다시 연결해주세요.",
        status: "검색 세션 만료 · 새로고침 필요",
        session: true,
      };
    }
    return {
      message: error?.message || fallback,
      status: "검색 확인 필요",
      session: false,
      fallback: error?.fallback || "",
    };
  }

  function retryableSearchError(error) {
    return error?.retryable === true || error?.code === "SEARCH_FAILED";
  }

  function waitForRetry(milliseconds) {
    return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
  }

  async function requestSearch(section, conditions, signal) {
    let attempt = 0;
    while (true) {
      const response = await fetch("/api/search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ section: section.key, conditions }),
        signal,
      });
      let payload;
      try {
        payload = await response.json();
      } catch (error) {
        error.retryable = false;
        throw error;
      }
      if (response.ok) return payload;
      const providerError = new Error(payload.message || "검색을 완료하지 못했어요.");
      providerError.code = payload.error;
      providerError.retryable = payload.retryable === true;
      providerError.fallback = payload.fallback || "";
      if (!retryableSearchError(providerError) || attempt >= 1) throw providerError;
      attempt += 1;
      elements.editorStatus.classList.remove("is-error");
      elements.editorStatus.textContent = `${section.label} 검색 공급자가 응답하지 않아 자동으로 다시 시도하고 있어요…`;
      elements.status.textContent = `${section.label} 검색 재시도 중… (${attempt + 1}/2)`;
      await waitForRetry(2500);
    }
  }

  function setSessionRefreshVisible(visible) {
    state.sessionExpired = visible;
    elements.sessionRefresh.hidden = !visible;
  }

  function setEmptyState({ title, copy, action, sessionError = false }) {
    elements.emptyTitle.textContent = title;
    elements.emptyCopy.textContent = copy;
    elements.emptyReset.textContent = action;
    elements.emptyReset.dataset.action = sessionError ? "refresh" : "reset";
    elements.empty.classList.toggle("is-session-error", sessionError);
  }

  function restoreDefaultEmptyState() {
    setEmptyState({ ...defaultEmptyState });
  }

  function showSessionError(message, status) {
    elements.title.textContent = "검색 세션이 만료됐어요.";
    elements.label.textContent = "SESSION EXPIRED";
    elements.count.textContent = "";
    elements.chips.replaceChildren();
    elements.grid.replaceChildren();
    setEmptyState({
      title: "검색을 다시 시작해주세요.",
      copy: message || "세션 새로고침을 누르고, 연결이 되지 않으면 검색 서버를 다시 켠 뒤 이 탭을 새로고침해주세요.",
      action: "세션 새로고침",
      sessionError: true,
    });
    elements.empty.hidden = false;
    elements.status.textContent = status || "검색 세션 만료 · 새로고침 필요";
    setSessionRefreshVisible(true);
  }

  function resetResultState(packet) {
    state.packet = packet;
    state.activeSection = activePacketSections()[0]?.key || "";
    state.conditionSection = "";
    state.selected.clear();
    state.expanded.clear();
    state.query = "";
    state.maxPrice = "";
    state.sort = "recommended";
    state.verifiedOnly = false;
    state.refundableOnly = false;
    elements.search.value = "";
    elements.price.value = "";
    elements.sort.value = "recommended";
    elements.verified.checked = false;
    elements.refundable.checked = false;
    restoreDefaultEmptyState();
    setSessionRefreshVisible(false);
    setTripSummary();
  }

  async function refreshSession() {
    if (state.refreshing) return;
    state.refreshing = true;
    elements.sessionRefresh.disabled = true;
    elements.sessionRefresh.textContent = "연결 중…";
    elements.status.textContent = "검색 세션 확인 중…";
    try {
      const health = await fetch("/api/health", { cache: "no-store" });
      if (!health.ok) throw new Error("세션 상태를 확인하지 못했어요.");
      const response = await fetch("/api/results", { cache: "no-store" });
      if (!response.ok) throw new Error("후보 결과를 다시 불러오지 못했어요.");
      const packet = await response.json();
      if (!packet || !packet.sections) throw new Error("후보 결과 형식이 올바르지 않아요.");
      resetResultState(packet);
      render();
      elements.status.textContent = "세션 새로고침 완료 · 후보 검토";
      elements.editorStatus.textContent = "세션을 새로 확인했어요. 조건을 다시 검색할 수 있어요.";
    } catch (error) {
      const notice = friendlyError(error, "세션을 새로고침하지 못했어요. 검색 서버가 켜져 있는지 확인해주세요.");
      showSessionError(notice.session ? notice.message : "세션을 새로고침하지 못했어요. 검색 서버를 다시 켠 뒤 다시 눌러주세요.", notice.status);
    } finally {
      state.refreshing = false;
      elements.sessionRefresh.disabled = false;
      elements.sessionRefresh.textContent = "세션 새로고침";
    }
  }

  function selectedCandidates() {
    const candidates = [];
    activePacketSections().forEach((section) => {
      (section.candidates || []).forEach((candidate) => {
        if (state.selected.has(candidate.id)) candidates.push({ section, candidate });
      });
    });
    return candidates;
  }

  function setTripSummary() {
    const trip = state.packet.trip || {};
    $("trip-destination").textContent = text(trip.destination, "여행지 미정");
    $("trip-dates").textContent = text(trip.date_label, "여행 날짜 확인 필요");
    $("trip-travelers").textContent = text(trip.travelers_label, "인원 확인 필요");
    $("trip-budget").textContent = text(trip.budget_label, "예산 미정");
    document.title = `${text(trip.destination, "Souvenir")} · 후보 검토`;
  }

  function renderTabs() {
    elements.tabs.replaceChildren();
    activePacketSections().forEach((section) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `tab-button${section.key === state.activeSection ? " is-active" : ""}`;
      button.setAttribute("aria-selected", section.key === state.activeSection ? "true" : "false");
      button.innerHTML = `<span aria-hidden="true">${escapeText(section.icon || "•")}</span><span>${escapeText(section.label)}</span><span class="tab-count">${section.candidate_count || 0}</span>`;
      button.addEventListener("click", () => {
        state.activeSection = section.key;
        state.conditionSection = "";
        state.query = "";
        state.maxPrice = "";
        state.verifiedOnly = false;
        state.refundableOnly = false;
        elements.search.value = "";
        elements.price.value = "";
        elements.verified.checked = false;
        elements.refundable.checked = false;
        render();
      });
      elements.tabs.appendChild(button);
    });
  }

  function candidateMatches(candidate) {
    const haystack = [candidate.title, candidate.subtitle, candidate.location, ...(candidate.metadata || []), ...(candidate.tags || [])].join(" ").toLowerCase();
    if (state.query && !haystack.includes(state.query.toLowerCase())) return false;
    const limit = number(state.maxPrice);
    if (limit !== null && candidate.price !== null && candidate.price !== undefined && candidate.price > limit) return false;
    if (state.verifiedOnly && !["matched", "verified"].includes(candidate.verification)) return false;
    if (state.refundableOnly && !(candidate.tags || []).some((tag) => String(tag).includes("취소 가능"))) return false;
    return true;
  }

  function visibleCandidates() {
    const section = sectionByKey(state.activeSection);
    if (!section) return [];
    const candidates = (section.candidates || []).filter(candidateMatches).slice();
    if (state.sort === "price") candidates.sort((a, b) => (a.price ?? Infinity) - (b.price ?? Infinity));
    if (state.sort === "rating") candidates.sort((a, b) => (b.score ?? -Infinity) - (a.score ?? -Infinity));
    return candidates;
  }

  function renderChips(section) {
    elements.chips.replaceChildren();
    (section.filters || []).forEach((filter) => {
      const chip = document.createElement("span");
      chip.className = "condition-chip";
      chip.textContent = filter;
      elements.chips.appendChild(chip);
    });
    if (section.coverage === "provider_returned_results_only") {
      const chip = document.createElement("span");
      chip.className = "condition-chip";
      chip.textContent = "공급자가 반환한 결과 범위";
      elements.chips.appendChild(chip);
    }
  }

  function makeTag(label) {
    const tag = document.createElement("span");
    tag.className = `card-tag${String(label).includes("확인") || String(label).includes("필요") ? " warning" : ""}`;
    tag.textContent = label;
    return tag;
  }

  function makeDetailPanel(candidate) {
    const panel = document.createElement("div");
    panel.className = "detail-panel";
    Object.entries(candidate.details || {}).forEach(([key, value]) => {
      if (!value) return;
      const line = document.createElement("div");
      line.className = "detail-line";
      const label = document.createElement("b");
      label.textContent = key;
      const content = document.createElement("span");
      content.textContent = value;
      line.append(label, content);
      panel.appendChild(line);
    });
    if (!panel.children.length) panel.textContent = "상세 정보 확인 필요";
    return panel;
  }

  function makeCard(candidate, index, section) {
    const article = document.createElement("article");
    article.className = `candidate-card${state.selected.has(candidate.id) ? " is-selected" : ""}`;
    article.dataset.id = candidate.id;

    const visual = document.createElement("div");
    visual.className = "card-visual";
    if (candidate.image_url && /^https:\/\//i.test(candidate.image_url)) {
      visual.classList.add("has-image");
      visual.style.backgroundImage = `url("${candidate.image_url.replace(/"/g, "%22")}")`;
    }
    const icon = document.createElement("span");
    icon.className = "card-icon";
    icon.textContent = section.icon || "•";
    const indexLabel = document.createElement("span");
    indexLabel.className = "card-index";
    indexLabel.textContent = `후보 ${index + 1}`;
    visual.append(icon, indexLabel);

    const body = document.createElement("div");
    body.className = "card-body";
    const heading = document.createElement("div");
    heading.className = "card-heading";
    const title = document.createElement("h3");
    title.textContent = candidate.title;
    const select = document.createElement("button");
    select.type = "button";
    select.className = "select-toggle";
    select.setAttribute("aria-pressed", state.selected.has(candidate.id) ? "true" : "false");
    select.textContent = state.selected.has(candidate.id) ? "선택됨" : "선택";
    select.addEventListener("click", () => {
      if (state.selected.has(candidate.id)) state.selected.delete(candidate.id);
      else state.selected.add(candidate.id);
      render();
    });
    heading.append(title, select);
    body.appendChild(heading);

    const subtitle = document.createElement("p");
    subtitle.className = "card-subtitle";
    subtitle.textContent = candidate.subtitle || "상세 정보 확인 필요";
    body.appendChild(subtitle);

    const metadata = document.createElement("div");
    metadata.className = "card-metadata";
    (candidate.metadata || []).slice(0, 3).forEach((item) => {
      const span = document.createElement("span");
      span.textContent = item;
      metadata.appendChild(span);
    });
    if (metadata.children.length) body.appendChild(metadata);

    const tags = document.createElement("div");
    tags.className = "card-tags";
    (candidate.tags || []).slice(0, 3).forEach((tag) => tags.appendChild(makeTag(tag)));
    body.appendChild(tags);

    const priceRow = document.createElement("div");
    priceRow.className = "price-row";
    const price = document.createElement("strong");
    price.className = "price";
    price.textContent = candidate.price_label || "가격 확인 필요";
    const priceNote = document.createElement("span");
    priceNote.className = "price-note";
    priceNote.textContent = candidate.price_note || "조건 확인 필요";
    priceRow.append(price, priceNote);
    body.appendChild(priceRow);

    const actions = document.createElement("div");
    actions.className = "card-actions";
    const link = document.createElement("a");
    link.className = `detail-link${candidate.url ? "" : " is-disabled"}`;
    link.textContent = candidate.url ? "원문 보기" : "원문 링크 없음";
    link.href = candidate.url || "#";
    if (candidate.url) { link.target = "_blank"; link.rel = "noreferrer noopener"; }
    const details = document.createElement("button");
    details.type = "button";
    details.className = "detail-toggle";
    const key = `${section.key}:${candidate.id}`;
    details.textContent = state.expanded.has(key) ? "상세 접기" : "상세 보기";
    details.addEventListener("click", () => {
      if (state.expanded.has(key)) state.expanded.delete(key); else state.expanded.add(key);
      render();
    });
    actions.append(link, details);
    body.appendChild(actions);
    if (state.expanded.has(key)) body.appendChild(makeDetailPanel(candidate));

    article.append(visual, body);
    return article;
  }

  function renderCards(section, visible) {
    elements.grid.replaceChildren();
    visible.forEach((candidate, index) => elements.grid.appendChild(makeCard(candidate, index, section)));
    restoreDefaultEmptyState();
    elements.empty.hidden = visible.length !== 0;
  }

  function renderModalList() {
    elements.selectedList.replaceChildren();
    selectedCandidates().forEach(({ section, candidate }) => {
      const item = document.createElement("div");
      item.className = "selected-item";
      const icon = document.createElement("span");
      icon.className = "selected-item-icon";
      icon.textContent = section.icon || "•";
      const textWrap = document.createElement("div");
      textWrap.className = "selected-item-text";
      const title = document.createElement("strong");
      title.textContent = candidate.title;
      const label = document.createElement("span");
      label.textContent = section.label;
      textWrap.append(title, label);
      item.append(icon, textWrap);
      elements.selectedList.appendChild(item);
    });
  }

  function render() {
    if (!state.packet) return;
    const section = sectionByKey(state.activeSection) || activePacketSections()[0];
    if (!section) return;
    state.activeSection = section.key;
    renderTabs();
    if (state.conditionSection !== section.key) renderConditionEditor(section);
    const visible = visibleCandidates();
    elements.label.textContent = `${section.label} · ${section.provider || "검색 결과"}`;
    elements.title.textContent = `${section.label} 후보를 확인하세요`;
    elements.count.textContent = `${visible.length} / ${section.candidate_count || 0}개 표시`;
    renderChips(section);
    renderCards(section, visible);
    elements.selectionCount.textContent = state.selected.size;
    elements.submit.disabled = state.selected.size === 0;
    elements.refundable.closest(".hotel-only-filter").hidden = section.key !== "hotels";
  }

  function resetFilters() {
    state.query = ""; state.maxPrice = ""; state.sort = "recommended"; state.verifiedOnly = false; state.refundableOnly = false;
    elements.search.value = ""; elements.price.value = ""; elements.sort.value = "recommended"; elements.verified.checked = false; elements.refundable.checked = false;
    render();
  }

  function openModal() {
    if (!state.selected.size) return;
    renderModalList();
    elements.submitResult.textContent = "";
    elements.modal.hidden = false;
    elements.modalConfirm.focus();
  }

  function closeModal() {
    elements.modal.hidden = true;
    elements.submit.focus();
  }

  async function rerunSearch() {
    if (!state.packet || !state.activeSection || state.searching) return;
    const section = sectionByKey(state.activeSection);
    if (!section || !section.searchable) return;
    setSearchBusy(true);
    elements.editorStatus.classList.remove("is-error");
    elements.editorStatus.textContent = "같은 화면에서 검색 결과를 갱신하는 중…";
    elements.status.textContent = `${section.label} 재검색 중…`;
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 120000);
    try {
      const payload = await requestSearch(section, collectConditions(), controller.signal);
      resetResultState(payload.results);
      render();
      elements.editorStatus.textContent = "검색 결과를 갱신했어요. 이전 선택은 새 결과에 맞춰 초기화됐어요.";
      elements.status.textContent = "검색 완료 · 같은 화면에서 결과 갱신";
    } catch (error) {
      const notice = friendlyError(error, "검색을 완료하지 못했어요. 조건을 확인하고 다시 시도해주세요.");
      elements.editorStatus.textContent = notice.fallback === "host_web_search"
        ? "숙소 공급자가 계속 응답하지 않아 기존 결과를 유지했어요. 잠시 후 다시 검색해주세요."
        : notice.message;
      elements.editorStatus.classList.add("is-error");
      elements.status.textContent = notice.session
        ? notice.status
        : (notice.fallback === "host_web_search" ? "숙소 공급자 지연 · 기존 결과 유지" : "재검색 실패 · 기존 결과 유지");
      setSessionRefreshVisible(notice.session);
    } finally {
      window.clearTimeout(timeout);
      setSearchBusy(false);
    }
  }

  async function submitSelection() {
    const selected = selectedCandidates().map(({ section, candidate }) => ({ section: section.key, id: candidate.id }));
    elements.modalConfirm.disabled = true;
    elements.submitResult.textContent = "제출 내용을 기록하는 중…";
    try {
      const response = await fetch("/api/selection", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ selected }) });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || "제출에 실패했어요.");
      elements.submitResult.textContent = `${payload.selection_count}개 후보를 선택·예약 전 상태로 넘겼어요.`;
      elements.status.textContent = "선택 제출 완료 · 저장 전 검토";
      elements.modalConfirm.textContent = "제출 완료";
    } catch (error) {
      const notice = friendlyError(error, "제출에 실패했어요. 잠시 후 다시 시도해주세요.");
      elements.submitResult.textContent = notice.session ? "저장 세션이 만료됐어요. 세션 새로고침 후 다시 제출해주세요." : notice.message;
      elements.submitResult.style.color = "#a85d4a";
    } finally {
      elements.modalConfirm.disabled = false;
    }
  }

  function bind() {
    elements.search.addEventListener("input", (event) => { state.query = event.target.value.trim(); render(); });
    elements.price.addEventListener("input", (event) => { state.maxPrice = event.target.value; render(); });
    elements.sort.addEventListener("change", (event) => { state.sort = event.target.value; render(); });
    elements.verified.addEventListener("change", (event) => { state.verifiedOnly = event.target.checked; render(); });
    elements.refundable.addEventListener("change", (event) => { state.refundableOnly = event.target.checked; render(); });
    elements.reset.addEventListener("click", resetFilters);
    elements.emptyReset.addEventListener("click", () => {
      if (elements.emptyReset.dataset.action === "refresh") refreshSession();
      else resetFilters();
    });
    elements.sessionRefresh.addEventListener("click", refreshSession);
    elements.rerunSearch.addEventListener("click", rerunSearch);
    elements.submit.addEventListener("click", openModal);
    elements.modalClose.addEventListener("click", closeModal);
    elements.modalCancel.addEventListener("click", closeModal);
    elements.modalConfirm.addEventListener("click", submitSelection);
    elements.modal.addEventListener("click", (event) => { if (event.target === elements.modal) closeModal(); });
    document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !elements.modal.hidden) closeModal(); });
  }

  async function boot() {
    bind();
    try {
      const response = await fetch("/api/results", { cache: "no-store" });
      if (!response.ok) throw new Error("결과를 불러오지 못했어요.");
      resetResultState(await response.json());
      render();
      elements.status.textContent = "읽기 전용 후보 검토";
      setSessionRefreshVisible(false);
    } catch (error) {
      const notice = friendlyError(error, "결과를 불러오지 못했어요. 세션 새로고침을 눌러 다시 연결해주세요.");
      showSessionError(notice.session ? notice.message : "결과를 불러오지 못했어요. 세션 새로고침을 눌러 다시 연결해주세요.", notice.status);
    }
  }

  boot();
}());
