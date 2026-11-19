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
      elements.submitResult.textContent = error.message || "제출에 실패했어요.";
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
    elements.emptyReset.addEventListener("click", resetFilters);
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
      state.packet = await response.json();
      state.activeSection = activePacketSections()[0]?.key || "";
      setTripSummary();
      render();
      elements.status.textContent = "읽기 전용 후보 검토";
    } catch (error) {
      elements.title.textContent = error.message || "결과를 불러오지 못했어요.";
      elements.empty.hidden = false;
      elements.status.textContent = "후보 패킷 확인 필요";
    }
  }

  boot();
}());
