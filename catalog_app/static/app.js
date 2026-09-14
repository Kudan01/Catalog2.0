const LOCALE_REGISTRY_URL = "/static/i18n/locales.json";
const DIAGNOSTIC_TOKEN = new URLSearchParams(window.location.search).get("catalog2_diagnostics") || "";
const DIAGNOSTICS_ENABLED = /^[a-f0-9]{32}$/i.test(DIAGNOSTIC_TOKEN);
const diagnosticNativeFetch = window.fetch.bind(window);
const diagnosticState = {
  events: [],
  flushTimer: null,
  sequence: 0,
  mutationCount: 0,
  lastMutationCount: 0,
};

function diagnosticEvent(event, fields = {}) {
  if (!DIAGNOSTICS_ENABLED) return;
  diagnosticState.events.push({
    event,
    sequence: ++diagnosticState.sequence,
    browser_time_ms: Math.round(performance.now() * 1000) / 1000,
    active_locale: typeof activeLocale === "string" ? activeLocale : "",
    view: typeof state === "object" && state ? state.view : "",
    folder: typeof state === "object" && state ? state.folder : "",
    mutations_total: diagnosticState.mutationCount,
    ...fields,
  });
  if (diagnosticState.events.length >= 25) {
    flushDiagnosticEvents();
  } else if (!diagnosticState.flushTimer) {
    diagnosticState.flushTimer = window.setTimeout(flushDiagnosticEvents, 300);
  }
}

async function flushDiagnosticEvents() {
  if (!DIAGNOSTICS_ENABLED || !diagnosticState.events.length) return;
  if (diagnosticState.flushTimer) {
    window.clearTimeout(diagnosticState.flushTimer);
    diagnosticState.flushTimer = null;
  }
  const events = diagnosticState.events.splice(0, diagnosticState.events.length);
  try {
    await diagnosticNativeFetch(`/api/diagnostics/events?token=${encodeURIComponent(DIAGNOSTIC_TOKEN)}`, {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ events }),
      keepalive: true,
    });
  } catch (error) {
    console.error("Catalog diagnostics event flush failed:", error);
  }
}

function diagnosticOperationStart(event, fields = {}) {
  const operationId = ++diagnosticState.sequence;
  diagnosticEvent(`${event}.start`, { operation_id: operationId, ...fields });
  return { operationId, started: performance.now(), mutations: diagnosticState.mutationCount };
}

function diagnosticOperationEnd(event, operation, fields = {}) {
  diagnosticEvent(`${event}.end`, {
    operation_id: operation.operationId,
    duration_ms: Math.round((performance.now() - operation.started) * 1000) / 1000,
    mutations: diagnosticState.mutationCount - operation.mutations,
    ...fields,
  });
}

if (DIAGNOSTICS_ENABLED && "MutationObserver" in window) {
  const diagnosticMutationObserver = new MutationObserver(records => {
    diagnosticState.mutationCount += records.length;
  });
  diagnosticMutationObserver.observe(document.documentElement, {
    childList: true,
    subtree: true,
    attributes: true,
    characterData: true,
  });
}

window.addEventListener("beforeunload", () => {
  if (!DIAGNOSTICS_ENABLED || !diagnosticState.events.length) return;
  const body = JSON.stringify({ events: diagnosticState.events.splice(0) });
  navigator.sendBeacon(
    `/api/diagnostics/events?token=${encodeURIComponent(DIAGNOSTIC_TOKEN)}`,
    new Blob([body], { type: "application/json" }),
  );
});

window.addEventListener("load", () => {
  if (!DIAGNOSTICS_ENABLED) return;
  window.setTimeout(() => {
    const navigation = performance.getEntriesByType("navigation")[0];
    const resources = performance.getEntriesByType("resource");
    diagnosticEvent("frontend.performance.navigation", navigation ? {
      dom_content_loaded_ms: Math.round(navigation.domContentLoadedEventEnd * 1000) / 1000,
      load_event_ms: Math.round(navigation.loadEventEnd * 1000) / 1000,
      response_end_ms: Math.round(navigation.responseEnd * 1000) / 1000,
      transfer_size: navigation.transferSize || 0,
    } : {});
    diagnosticEvent("frontend.performance.resources", {
      count: resources.length,
      total_duration_ms: Math.round(resources.reduce((sum, item) => sum + item.duration, 0) * 1000) / 1000,
      total_transfer_size: resources.reduce((sum, item) => sum + (item.transferSize || 0), 0),
      slowest: resources
        .slice()
        .sort((a, b) => b.duration - a.duration)
        .slice(0, 20)
        .map(item => ({ name: item.name, duration_ms: Math.round(item.duration * 1000) / 1000, transfer_size: item.transferSize || 0 })),
      memory_used_js_heap: performance.memory?.usedJSHeapSize || null,
    });
  }, 1000);
});

const LOCALE_FILE_BASE_URL = "/static/i18n";
const EMERGENCY_LOCALE = "en";
const STARTUP_RUNTIME_API_RETRY_DELAYS_MS = [0, 150, 400];

function readStartupRuntimeSettingsFromHtml() {
  const element = document.getElementById("catalogRuntimeBootstrap");
  if (!element) return null;

  try {
    const payload = JSON.parse(element.textContent || "");
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      throw new Error("Startup runtime settings must be a JSON object.");
    }
    return payload;
  } catch (error) {
    console.error("Could not read runtime settings embedded in index.html:", error);
    return null;
  }
}

const STARTUP_RUNTIME_SETTINGS = readStartupRuntimeSettingsFromHtml();

let localeRegistry = {
  default: EMERGENCY_LOCALE,
  fallback: EMERGENCY_LOCALE,
  locales: [{ code: EMERGENCY_LOCALE, name: "English" }],
};
let supportedLocales = new Set([EMERGENCY_LOCALE]);
let publicDefaultLocale = EMERGENCY_LOCALE;
let fallbackLocale = EMERGENCY_LOCALE;
const localeMessages = new Map();



let activeLocale = EMERGENCY_LOCALE;
let localeSaveGeneration = 0;
let localeSaveQueue = Promise.resolve();

function normalizeLocaleCode(locale) {
  return String(locale || "").trim().toLowerCase();
}

function normalizeLocale(locale) {
  const normalized = normalizeLocaleCode(locale);
  return supportedLocales.has(normalized) ? normalized : publicDefaultLocale;
}

function validateLocaleRegistry(payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("Locale registry must be a JSON object.");
  }

  const defaultLocale = normalizeLocaleCode(payload.default);
  const fallback = normalizeLocaleCode(payload.fallback);
  const definitions = Array.isArray(payload.locales) ? payload.locales : [];
  const locales = [];
  const seen = new Set();

  for (const definition of definitions) {
    const code = normalizeLocaleCode(definition?.code);
    const name = String(definition?.name || "").trim();
    if (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(code)) {
      throw new Error(`Invalid locale code in registry: ${code || "<empty>"}`);
    }
    if (!name) {
      throw new Error(`Locale ${code} is missing a display name.`);
    }
    if (seen.has(code)) {
      throw new Error(`Duplicate locale code in registry: ${code}`);
    }
    seen.add(code);
    locales.push({ code, name });
  }

  if (!locales.length) {
    throw new Error("Locale registry does not define any locales.");
  }
  if (!seen.has(defaultLocale)) {
    throw new Error(`Default locale is not registered: ${defaultLocale || "<empty>"}`);
  }
  if (!seen.has(fallback)) {
    throw new Error(`Fallback locale is not registered: ${fallback || "<empty>"}`);
  }
  if (defaultLocale !== "en" || fallback !== "en") {
    throw new Error("English must be both the default and fallback locale.");
  }

  return { default: defaultLocale, fallback, locales };
}

async function fetchStaticJson(url) {
  const operation = diagnosticOperationStart("frontend.fetch.static", { url });
  let response;
  try {
    response = await fetch(url, { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`Could not load ${url} (HTTP ${response.status}).`);
    }
    const payload = await response.json();
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      throw new Error(`Invalid JSON object in ${url}.`);
    }
    diagnosticOperationEnd("frontend.fetch.static", operation, {
      url,
      status: response.status,
      payload_bytes: JSON.stringify(payload).length,
    });
    return payload;
  } catch (error) {
    diagnosticOperationEnd("frontend.fetch.static", operation, {
      url,
      status: response?.status || 0,
      error: String(error?.message || error),
    });
    throw error;
  }
}

async function ensureLocaleLoaded(locale) {
  const normalized = normalizeLocale(locale);
  if (localeMessages.has(normalized)) return normalized;

  const messages = await fetchStaticJson(`${LOCALE_FILE_BASE_URL}/${encodeURIComponent(normalized)}.json`);
  localeMessages.set(normalized, messages);
  return normalized;
}

function populateLocaleSelect() {
  if (!els.localeSelect) return;
  els.localeSelect.replaceChildren();
  for (const definition of localeRegistry.locales) {
    const option = document.createElement("option");
    option.value = definition.code;
    option.textContent = definition.name;
    els.localeSelect.appendChild(option);
  }
}

async function initializeLocalization() {
  try {
    localeRegistry = validateLocaleRegistry(await fetchStaticJson(LOCALE_REGISTRY_URL));
    supportedLocales = new Set(localeRegistry.locales.map(definition => definition.code));
    publicDefaultLocale = localeRegistry.default;
    fallbackLocale = localeRegistry.fallback;
    activeLocale = publicDefaultLocale;
    await ensureLocaleLoaded(fallbackLocale);
  } catch (error) {
    console.error("Localization initialization failed:", error);
    localeRegistry = {
      default: EMERGENCY_LOCALE,
      fallback: EMERGENCY_LOCALE,
      locales: [{ code: EMERGENCY_LOCALE, name: "English" }],
    };
    supportedLocales = new Set([EMERGENCY_LOCALE]);
    publicDefaultLocale = EMERGENCY_LOCALE;
    fallbackLocale = EMERGENCY_LOCALE;
    activeLocale = EMERGENCY_LOCALE;
    localeMessages.set(EMERGENCY_LOCALE, {});
  }
  populateLocaleSelect();
}

function setLocaleForSession(locale, options = {}) {
  activeLocale = normalizeLocale(locale);
  if (options.apply !== false) {
    applyStaticTexts();
  }
}

function getTranslationTemplate(key) {
  const activeTranslations = localeMessages.get(activeLocale) || {};
  const fallbackTranslations = localeMessages.get(fallbackLocale) || {};
  return activeTranslations[key] ?? fallbackTranslations[key] ?? null;
}

function hasTranslation(key) {
  return getTranslationTemplate(key) !== null;
}

const state = {
  view: "folder",
  folder: "",
  currentFolder: null,
  mediaType: "all",
  mediaPage: 1,
  mediaPages: 0,
  childPage: 1,
  childPages: 0,
  childPageSize: 0,
  collapsedChildFoldersByFolder: {},
  rootPage: 1,
  rootPages: 0,
  rootTotal: 0,
  treeLoaded: false,
  treeLoadPromise: null,
  treeLoadGeneration: 0,
  treeKnownChildrenByParent: new Map(),
  currentFolderChildrenRequest: null,
  viewLoadRequestId: 0,
  searchQuery: "",
  searchFolder: "",
  jobPollTimer: null,
  latestScan: null,
  jobStatusPayload: null,
  jobRunning: false,
  jobStartPending: false,
  folderRepairCloseHandler: null,
  catalogUpdateWorkflow: { phase: "idle", preview: null, scanId: null },
  sourceRootVerification: null,
  mediaRename: null,
  folderRename: null,
  cacheSettingsPayload: null,
  protectedCacheAudit: null,
  protectedCacheAuditLoading: false,
  protectedCacheAuditError: "",
  protectedCacheCleanupPlan: null,
  protectedCacheCleanupPlanLoading: false,
  protectedCacheCleanupPlanError: "",
  protectedCacheCleanupExecute: null,
  protectedCacheCleanupExecuteLoading: false,
  protectedCacheCleanupExecuteError: "",
  protectedCacheMaintenanceLoading: false,
  protectedCacheMaintenanceError: "",
  catalogStatus: null,
};

const THEMES = {
  original: { labelKey: "theme.original" },
  "serious-light": { labelKey: "theme.seriousLight" },
  "serious-dark": { labelKey: "theme.seriousDark" },
  "vivid-content": { labelKey: "theme.vividContent" },
  "dark-cinema": { labelKey: "theme.darkCinema" },
};

function normalizeThemeId(value) {
  const themeId = String(value || "").trim();
  return Object.prototype.hasOwnProperty.call(THEMES, themeId) ? themeId : "original";
}

function themeLabel(themeId) {
  const normalized = normalizeThemeId(themeId);
  return text(THEMES[normalized].labelKey);
}

const GALLERY_DENSITIES = new Set(["compact", "comfortable", "large", "extra-large"]);

function normalizeGalleryDensity(value) {
  const density = String(value || "").trim().toLowerCase();
  return GALLERY_DENSITIES.has(density) ? density : "comfortable";
}

function applyGalleryDensity(value) {
  const density = normalizeGalleryDensity(value);
  document.documentElement.dataset.galleryDensity = density;
  document.body.dataset.galleryDensity = density;
  if (els.galleryDensitySelect && els.galleryDensitySelect.value !== density) {
    els.galleryDensitySelect.value = density;
  }
}

function setThemeStatus(message = "", isError = false) {
  if (!els.themeStatus) return;
  els.themeStatus.textContent = isError ? message : "";
  els.themeStatus.classList.toggle("error", Boolean(isError));
}

function updateCachedRuntimeSetting(key, value) {
  if (!state.cacheSettingsPayload) return;
  state.cacheSettingsPayload = {
    ...state.cacheSettingsPayload,
    settings: {
      ...(state.cacheSettingsPayload.settings || {}),
      [key]: value,
    },
  };
}

const DEFAULT_CATALOG_TITLE = "Catalog 2.0";

function normalizeCatalogTitle(value) {
  return String(value || "").trim();
}

function applyCatalogTitle(value) {
  const title = normalizeCatalogTitle(value) || DEFAULT_CATALOG_TITLE;
  if (els.catalogHome) {
    els.catalogHome.textContent = title;
    els.catalogHome.setAttribute("title", title);
  }
  document.title = title;
  if (els.catalogTitleInput && els.catalogTitleInput.value !== title) {
    els.catalogTitleInput.value = title;
  }
}

function setCatalogTitleStatus(message, isError = false) {
  if (!els.catalogTitleStatus) return;
  els.catalogTitleStatus.textContent = message;
  els.catalogTitleStatus.classList.toggle("error", Boolean(isError));
}

function validateCatalogTitleInput(value) {
  const title = normalizeCatalogTitle(value);
  return title.length >= 1 && title.length <= 80;
}

function applyTheme(themeId, options = {}) {
  const normalized = normalizeThemeId(themeId);

  document.documentElement.dataset.theme = normalized;
  document.body.dataset.theme = normalized;

  if (els.themeSelect && els.themeSelect.value !== normalized) {
    els.themeSelect.value = normalized;
  }

  setThemeStatus(normalized);
}

function initializeTheme() {
  applyTheme(document.documentElement.dataset.theme || "original");
}

const els = {
  statusText: document.getElementById("statusText"),
  catalogHome: document.getElementById("catalogHome"),
  searchForm: document.getElementById("searchForm"),
  searchInput: document.getElementById("searchInput"),
  searchInCurrentFolder: document.getElementById("searchInCurrentFolder"),
  favoritesView: document.getElementById("favoritesView"),
  catalogManagementOpen: document.getElementById("catalogManagementOpen"),
  catalogManagementModal: document.getElementById("catalogManagementModal"),
  catalogManagementClose: document.getElementById("catalogManagementClose"),
  catalogTitleInput: document.getElementById("catalogTitleInput"),
  catalogTitleSave: document.getElementById("catalogTitleSave"),
  catalogTitleStatus: document.getElementById("catalogTitleStatus"),
  jobResultClear: document.getElementById("jobResultClear"),
  folderList: document.getElementById("folderList"),
  rootPageInfo: document.getElementById("rootPageInfo"),
  prevRootPage: document.getElementById("prevRootPage"),
  nextRootPage: document.getElementById("nextRootPage"),
  breadcrumb: document.getElementById("breadcrumb"),
  folderTitle: document.getElementById("folderTitle"),
  folderCounts: document.getElementById("folderCounts"),
  currentFolderOpen: document.getElementById("currentFolderOpen"),
  currentFolderRename: document.getElementById("currentFolderRename"),
  currentFolderUpdate: document.getElementById("currentFolderUpdate"),
  currentFolderMore: document.getElementById("currentFolderMore"),
  currentFolderGeneratePreviews: document.getElementById("currentFolderGeneratePreviews"),
  childFolders: document.getElementById("childFolders"),
  childFoldersToggle: document.getElementById("childFoldersToggle"),
  childPageInfo: document.getElementById("childPageInfo"),
  childPager: document.getElementById("childPager"),
  firstChildPage: document.getElementById("firstChildPage"),
  prevChildPage: document.getElementById("prevChildPage"),
  childPageJumpForm: document.getElementById("childPageJumpForm"),
  childPageJumpInput: document.getElementById("childPageJumpInput"),
  nextChildPage: document.getElementById("nextChildPage"),
  lastChildPage: document.getElementById("lastChildPage"),
  mediaTitle: document.getElementById("mediaTitle"),
  mediaList: document.getElementById("mediaList"),
  pageInfo: document.getElementById("pageInfo"),
  firstPage: document.getElementById("firstPage"),
  prevPage: document.getElementById("prevPage"),
  pageJumpForm: document.getElementById("pageJumpForm"),
  pageJumpInput: document.getElementById("pageJumpInput"),
  nextPage: document.getElementById("nextPage"),
  lastPage: document.getElementById("lastPage"),
  message: document.getElementById("message"),
  jobStatusText: document.getElementById("jobStatusText"),
  jobResult: document.getElementById("jobResult"),
  jobResultDetails: document.getElementById("jobResultDetails"),
  catalogUpdateDecision: document.getElementById("catalogUpdateDecision"),
  sourceRootSummary: document.getElementById("sourceRootSummary"),
  sourceRootInput: document.getElementById("sourceRootInput"),
  sourceRootVerify: document.getElementById("sourceRootVerify"),
  sourceRootSave: document.getElementById("sourceRootSave"),
  sourceRootMessage: document.getElementById("sourceRootMessage"),
  cacheStatusSummary: document.getElementById("cacheStatusSummary"),
  cacheStatusRefresh: document.getElementById("cacheStatusRefresh"),
  cacheLimitInput: document.getElementById("cacheLimitInput"),
  cacheLimitSave: document.getElementById("cacheLimitSave"),
  cacheLimitMessage: document.getElementById("cacheLimitMessage"),
  photoPageSizeInput: document.getElementById("photoPageSizeInput"),
  videoPageSizeInput: document.getElementById("videoPageSizeInput"),
  gifPageSizeInput: document.getElementById("gifPageSizeInput"),
  otherPageSizeInput: document.getElementById("otherPageSizeInput"),
  folderPageSizeInput: document.getElementById("folderPageSizeInput"),
  galleryDensitySelect: document.getElementById("galleryDensitySelect"),
  pageSizeSave: document.getElementById("pageSizeSave"),
  pageSizeMessage: document.getElementById("pageSizeMessage"),
  imageThumbWidthInput: document.getElementById("imageThumbWidthInput"),
  imageThumbHeightInput: document.getElementById("imageThumbHeightInput"),
  gifThumbWidthInput: document.getElementById("gifThumbWidthInput"),
  gifThumbHeightInput: document.getElementById("gifThumbHeightInput"),
  videoPreviewWidthInput: document.getElementById("videoPreviewWidthInput"),
  ffmpegTimeoutInput: document.getElementById("ffmpegTimeoutInput"),
  ffmpegThreadsInput: document.getElementById("ffmpegThreadsInput"),
  thumbnailParamsSave: document.getElementById("thumbnailParamsSave"),
  thumbnailParamsReset: document.getElementById("thumbnailParamsReset"),
  thumbnailParamsMessage: document.getElementById("thumbnailParamsMessage"),
  catalogUpdateAll: document.getElementById("catalogUpdateAll"),
  catalogPrepareAllPreviews: document.getElementById("catalogPrepareAllPreviews"),
  scanPreviewAll: document.getElementById("scanPreviewAll"),
  scanPreviewCurrent: document.getElementById("scanPreviewCurrent"),
  scanStageAll: document.getElementById("scanStageAll"),
  scanStageCurrent: document.getElementById("scanStageCurrent"),
  scanActivate: document.getElementById("scanActivate"),
  gifPreviewAll: document.getElementById("gifPreviewAll"),
  gifPreviewCurrent: document.getElementById("gifPreviewCurrent"),
  videoPosterAll: document.getElementById("videoPosterAll"),
  videoPosterCurrent: document.getElementById("videoPosterCurrent"),
  videoFramesAll: document.getElementById("videoFramesAll"),
  videoFramesCurrent: document.getElementById("videoFramesCurrent"),
  folderPreviewPlanCurrent: document.getElementById("folderPreviewPlanCurrent"),
  themeSelect: document.getElementById("themeSelect"),
  themeStatus: document.getElementById("themeStatus"),
  localeSelect: document.getElementById("localeSelect"),
  localeStatus: document.getElementById("localeStatus"),
};

function text(key, values = {}) {
  const template = getTranslationTemplate(key) ?? key;
  return template.replace(/\{([a-zA-Z0-9_]+)\}/g, (_match, name) => String(values[name] ?? ""));
}

function localizedMessageText(value) {
  if (value && typeof value === "object") {
    const code = String(value.code || "").trim();
    if (!code) return "";
    const translationKey = `backendMessage.${code}`;
    return hasTranslation(translationKey)
      ? text(translationKey, value.params || {})
      : code;
  }
  return String(value || "");
}

function firstResultMessage(payload) {
  const messages = Array.isArray(payload?.result_messages) ? payload.result_messages : [];
  return messages.length ? messages[0] : null;
}

function setSettingsServiceCardOpen(card, isOpen) {
  const trigger = card.querySelector(":scope > .settings-service-summary");
  const body = card.querySelector(":scope > .settings-service-body");
  if (!trigger || !body) return;

  card.classList.toggle("open", Boolean(isOpen));
  body.hidden = !isOpen;
  trigger.setAttribute("aria-expanded", isOpen ? "true" : "false");
  const label = isOpen ? text("actions.collapse") : text("actions.expand");
  trigger.setAttribute("aria-label", label);
  trigger.setAttribute("title", label);
}

function bindSettingsServiceCards() {
  for (const card of document.querySelectorAll("[data-settings-service-card]")) {
    if (card.dataset.serviceToggleBound === "1") continue;
    const trigger = card.querySelector(":scope > .settings-service-summary");
    const body = card.querySelector(":scope > .settings-service-body");
    if (!trigger || !body) continue;

    card.dataset.serviceToggleBound = "1";
    setSettingsServiceCardOpen(card, card.classList.contains("open"));
    trigger.addEventListener("click", () => {
      setSettingsServiceCardOpen(card, !card.classList.contains("open"));
    });
  }
}

function applyDisclosureLabels() {
  for (const element of document.querySelectorAll(".management-disclosure, .management-result-disclosure")) {
    const summary = element.querySelector(":scope > summary");
    if (!summary) continue;
    const label = element.open ? text("actions.collapse") : text("actions.expand");
    summary.setAttribute("aria-label", label);
    summary.setAttribute("title", label);
  }

  for (const card of document.querySelectorAll("[data-settings-service-card]")) {
    setSettingsServiceCardOpen(card, card.classList.contains("open"));
  }

  if (els.jobResult) {
    els.jobResult.dataset.emptyText = text("settings.resultEmpty");
  }
}

function refreshVisibleRenameModalsForLocale() {
  const folderModal = document.getElementById("folderRenameModal");
  if (folderModal) {
    if (state.folderRename?.plan) {
      renderFolderRenamePlan(state.folderRename.plan);
    } else if (folderModal.classList.contains("show")) {
      renderFolderRenamePlan(null);
    }
    setFolderRenameBusy(Boolean(state.folderRename?.busy));
  }

  const mediaModal = document.getElementById("mediaRenameModal");
  if (mediaModal) {
    if (state.mediaRename?.plan) {
      renderMediaRenamePlan(state.mediaRename.plan);
    } else if (mediaModal.classList.contains("show")) {
      renderMediaRenamePlan(null);
    }
    setMediaRenameBusy(Boolean(state.mediaRename?.busy));
  }
}

function bindDisclosureLocaleLabels() {
  for (const element of document.querySelectorAll(".management-disclosure, .management-result-disclosure")) {
    if (element.dataset.localeLabelBound === "1") continue;
    element.dataset.localeLabelBound = "1";
    element.addEventListener("toggle", applyDisclosureLabels);
  }
  bindSettingsServiceCards();
}

function applyStaticTexts() {
  const operation = diagnosticOperationStart("frontend.locale.apply_static", { locale: activeLocale });
  document.documentElement.lang = activeLocale;

  for (const element of document.querySelectorAll("[data-text]")) {
    element.textContent = text(element.dataset.text);
  }

  for (const element of document.querySelectorAll("[data-aria-label]")) {
    element.setAttribute("aria-label", text(element.dataset.ariaLabel));
  }

  for (const element of document.querySelectorAll("[data-placeholder]")) {
    element.setAttribute("placeholder", text(element.dataset.placeholder));
  }

  for (const element of document.querySelectorAll("[data-title]")) {
    element.setAttribute("title", text(element.dataset.title));
  }

  bindDisclosureLocaleLabels();
  applyDisclosureLabels();
  diagnosticOperationEnd("frontend.locale.apply_static", operation, {
    locale: activeLocale,
    translated_nodes: document.querySelectorAll("[data-text], [data-aria-label], [data-placeholder], [data-title]").length,
  });
}


function localeLabel(locale) {
  const normalized = normalizeLocale(locale);
  return localeRegistry.locales.find(definition => definition.code === normalized)?.name || normalized;
}

function syncLocaleSelect() {
  if (els.localeSelect && els.localeSelect.value !== activeLocale) {
    els.localeSelect.value = activeLocale;
  }
}

function setLocaleStatus(message, isError = false) {
  if (!els.localeStatus) return;
  els.localeStatus.textContent = isError ? message : "";
  els.localeStatus.classList.toggle("error", Boolean(isError));
}

function refreshTreeLocaleLabels() {
  const rootNode = treeNodeForPath("");
  if (rootNode) {
    const rootName = rootNode.querySelector(".tree-node-name");
    const rootMeta = rootNode.querySelector(".tree-node-meta");
    if (rootName) rootName.textContent = text("tree.root");
    if (rootMeta) rootMeta.textContent = text("tree.kind.root");
  }

  for (const node of els.folderList.querySelectorAll(".tree-node[data-folder-path]")) {
    const folder = node._treeFolder;
    if (!folder) continue;

    const kind = folderTreeKind(folder);
    const meta = node.querySelector(":scope > .tree-row > .tree-node-meta");
    if (meta) {
      meta.textContent = folderFilesystemIsUsable(folder)
        ? folderTreeMeta(kind)
        : text("tree.kind.missing");
    }

    const toggle = node.querySelector(":scope > .tree-row > .tree-toggle");
    if (toggle) {
      toggle.setAttribute(
        "aria-label",
        text(treeNodeIsExpanded(node) ? "tree.closeBranch" : "tree.openBranch"),
      );
    }
  }

  for (const note of els.folderList.querySelectorAll(".tree-more")) {
    note.textContent = text("tree.moreFolders");
  }

  if (els.rootPageInfo && state.treeLoaded) {
    els.rootPageInfo.textContent = text("pagination.folders", {
      total: state.rootTotal,
      page: state.rootPages ? state.rootPage : 0,
      pages: state.rootPages,
    });
  }
}

function applyLocaleToCurrentUi() {
  applyStaticTexts();
  refreshTreeLocaleLabels();
  refreshVisibleRenameModalsForLocale();
  syncLocaleSelect();
  setThemeStatus(document.documentElement.dataset.theme || "original");
}

function startupRuntimeSettingsAreUsable(payload) {
  return Boolean(
    payload
    && typeof payload === "object"
    && !Array.isArray(payload)
    && String(payload.ui_locale || "").trim()
    && String(payload.ui_theme || "").trim()
    && String(payload.gallery_density || "").trim()
    && String(payload.catalog_title || "").trim()
  );
}

function waitForStartupRetry(delayMs) {
  if (!delayMs) return Promise.resolve();
  return new Promise(resolve => window.setTimeout(resolve, delayMs));
}

async function loadStartupRuntimeSettings() {
  if (startupRuntimeSettingsAreUsable(STARTUP_RUNTIME_SETTINGS)) {
    return {
      payload: STARTUP_RUNTIME_SETTINGS,
      source: "initial_html",
      attempts: 0,
    };
  }

  let lastError = null;
  for (let index = 0; index < STARTUP_RUNTIME_API_RETRY_DELAYS_MS.length; index += 1) {
    await waitForStartupRetry(STARTUP_RUNTIME_API_RETRY_DELAYS_MS[index]);
    try {
      const payload = await fetchJson("/api/settings/runtime/ui-locale");
      if (!startupRuntimeSettingsAreUsable(payload)) {
        throw new Error("Runtime settings response is incomplete.");
      }
      return {
        payload,
        source: "runtime_api_retry",
        attempts: index + 1,
      };
    } catch (error) {
      lastError = error;
      console.error(`Runtime settings startup attempt ${index + 1} failed:`, error);
    }
  }

  throw new Error(
    `Could not load saved interface settings. Reload the page to try again.${
      lastError?.message ? ` ${lastError.message}` : ""
    }`,
  );
}

async function loadInitialLocale() {
  const runtimeSettings = await loadStartupRuntimeSettings();
  const payload = runtimeSettings.payload;

  // Theme, density and title are already embedded in the initial HTML. Apply
  // them again to the live controls, without depending on locale file loading.
  applyTheme(payload.ui_theme);
  applyGalleryDensity(payload.gallery_density);
  applyCatalogTitle(payload.catalog_title);

  const requestedLocale = normalizeLocale(payload.ui_locale);
  let localeSource = "requested_locale";
  try {
    await ensureLocaleLoaded(requestedLocale);
    setLocaleForSession(requestedLocale, { apply: false });
  } catch (error) {
    console.error(`Could not load locale ${requestedLocale}; using ${fallbackLocale}.`, error);
    await ensureLocaleLoaded(fallbackLocale);
    setLocaleForSession(fallbackLocale, { apply: false });
    localeSource = "fallback_locale_file";
  }

  diagnosticEvent("frontend.startup.runtime_settings", {
    source: runtimeSettings.source,
    attempts: runtimeSettings.attempts,
    ui_locale: activeLocale,
    ui_locale_source: payload.ui_locale_source || localeSource,
    ui_theme: normalizeThemeId(payload.ui_theme),
    ui_theme_source: payload.ui_theme_source || runtimeSettings.source,
    gallery_density: normalizeGalleryDensity(payload.gallery_density),
    gallery_density_source: payload.gallery_density_source || runtimeSettings.source,
    catalog_title_source: payload.catalog_title_source || runtimeSettings.source,
    locale_load_source: localeSource,
  });
}

async function processLocaleSave(requestId, nextLocale, diagnosticOperation) {
  try {
    await ensureLocaleLoaded(nextLocale);
    if (requestId !== localeSaveGeneration) {
      diagnosticOperationEnd("frontend.locale.save", diagnosticOperation, {
        result: "stale_before_save",
        final_locale: activeLocale,
      });
      return;
    }

    const payload = await postJson("/api/settings/runtime/ui-locale", { ui_locale: nextLocale });
    if (requestId !== localeSaveGeneration) {
      diagnosticOperationEnd("frontend.locale.save", diagnosticOperation, {
        result: "stale_after_save",
        final_locale: activeLocale,
      });
      return;
    }

    const savedLocale = normalizeLocale(payload.ui_locale || nextLocale);
    await ensureLocaleLoaded(savedLocale);
    if (requestId !== localeSaveGeneration) {
      diagnosticOperationEnd("frontend.locale.save", diagnosticOperation, {
        result: "stale_before_commit",
        final_locale: activeLocale,
      });
      return;
    }

    // Commit the locale once, after both its messages and the persisted setting
    // are ready. Re-localize already available state synchronously; do not
    // trigger unrelated backend reloads that replace Settings DOM twice.
    setLocaleForSession(savedLocale, { apply: false });
    updateCachedRuntimeSetting("ui_locale", savedLocale);
    applyLocaleToCurrentUi();

    if (state.catalogStatus) {
      renderCatalogStatus(state.catalogStatus);
    }
    if (state.jobStatusPayload) {
      renderJobStatus(state.jobStatusPayload);
    }
    if (catalogManagementIsOpen() && state.cacheSettingsPayload) {
      renderCacheSettingsStatus(state.cacheSettingsPayload, { preserveControlStatuses: true });
    }

    updateViewButtons();
    syncLocaleSelect();
    setLocaleStatus(text("settings.languageSaved"));

    diagnosticOperationEnd("frontend.locale.save", diagnosticOperation, {
      result: "ok",
      final_locale: activeLocale,
    });
  } catch (error) {
    if (requestId === localeSaveGeneration) {
      syncLocaleSelect();
      setLocaleStatus(`${text("settings.languageSaveError")} ${error.message || ""}`.trim(), true);
    }
    diagnosticOperationEnd("frontend.locale.save", diagnosticOperation, {
      result: "error",
      final_locale: activeLocale,
      error: String(error?.message || error),
    });
  }
}

function saveLocaleSetting() {
  if (!els.localeSelect) return;

  const nextLocale = normalizeLocale(els.localeSelect.value);
  const requestId = ++localeSaveGeneration;
  const diagnosticOperation = diagnosticOperationStart("frontend.locale.save", {
    from_locale: activeLocale,
    requested_locale: nextLocale,
    request_id: requestId,
  });

  // Do not render a temporary status in the previous locale. The visible UI
  // stays unchanged until the target locale can be committed atomically.
  setLocaleStatus("");
  localeSaveQueue = localeSaveQueue
    .catch(() => {})
    .then(() => processLocaleSave(requestId, nextLocale, diagnosticOperation));
}

async function saveThemeSetting() {
  if (!els.themeSelect) return;
  const nextTheme = normalizeThemeId(els.themeSelect.value);
  const previousTheme = normalizeThemeId(document.documentElement.dataset.theme || "original");

  applyTheme(nextTheme);
  updateCachedRuntimeSetting("ui_theme", nextTheme);

  try {
    const payload = await postJson("/api/settings/runtime/ui-theme", { ui_theme: nextTheme });
    const savedTheme = normalizeThemeId(payload.ui_theme || nextTheme);
    applyTheme(savedTheme);
    updateCachedRuntimeSetting("ui_theme", savedTheme);
  } catch (error) {
    applyTheme(previousTheme);
    updateCachedRuntimeSetting("ui_theme", previousTheme);
    setThemeStatus(`${text("settings.themeSaveError")} ${error.message || ""}`.trim(), true);
  }
}

async function saveCatalogTitleSetting(options = {}) {
  if (!els.catalogTitleInput) return true;
  const saveButton = options.saveButton === undefined ? els.catalogTitleSave : options.saveButton;
  const shouldRefreshStatus = options.refreshStatus !== false;
  const quietStatus = options.quietStatus === true;
  const nextTitle = normalizeCatalogTitle(els.catalogTitleInput.value);
  const previousTitle = els.catalogHome ? els.catalogHome.textContent : DEFAULT_CATALOG_TITLE;

  if (!validateCatalogTitleInput(nextTitle)) {
    setCatalogTitleStatus(text("settings.catalogTitleInvalid"), true);
    return false;
  }

  applyCatalogTitle(nextTitle);
  setCatalogTitleStatus(quietStatus ? "" : text("settings.catalogTitleSaving"));
  if (saveButton) saveButton.disabled = true;

  try {
    const payload = await postJson("/api/settings/runtime/catalog-title", { catalog_title: nextTitle });
    applyCatalogTitle(payload.catalog_title || nextTitle);
    setCatalogTitleStatus(quietStatus ? "" : text("settings.catalogTitleSaved"));
    if (shouldRefreshStatus && catalogManagementIsOpen()) {
      await loadCacheSettingsStatus();
    }
    return true;
  } catch (error) {
    applyCatalogTitle(previousTitle);
    setCatalogTitleStatus(`${text("settings.catalogTitleSaveError")} ${error.message || ""}`.trim(), true);
    return false;
  } finally {
    if (saveButton) saveButton.disabled = false;
  }
}

function apiUrl(path, params = {}) {
  const url = new URL(path, window.location.origin);
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null) {
      url.searchParams.set(key, value);
    }
  }
  return url.toString();
}


const MEDIA_THUMBNAIL_LAZY_ROOT_MARGIN = "900px 0px 1200px 0px";
const MEDIA_THUMBNAIL_EAGER_VIEWPORT_MARGIN_PX = 700;
const MEDIA_THUMBNAIL_EAGER_INITIAL_LIMIT = 24;
const MEDIA_THUMBNAIL_MAX_AUTO_RETRIES = 1;
const MEDIA_THUMBNAIL_RETRY_DELAY_MS = 700;
let mediaThumbnailLazyObserver = null;
let mediaThumbnailLazyGeneration = 0;

function mediaThumbnailLazyAttrs(url) {
  return `data-lazy-thumbnail-src="${escapeHtml(url)}" loading="lazy" decoding="async" fetchpriority="low"`;
}

function mediaThumbnailUrl(image) {
  if (!image) return "";
  return String(image.getAttribute("src") || image.dataset.lazyThumbnailSrc || image.dataset.thumbnailOriginalSrc || "");
}

function mediaThumbnailRetryUrl(url, attempt) {
  const source = String(url || "").trim();
  if (!source) return "";

  try {
    const retryUrl = new URL(source, window.location.origin);
    retryUrl.searchParams.set("thumbnail_retry", String(attempt));
    return retryUrl.toString();
  } catch (_) {
    const separator = source.includes("?") ? "&" : "?";
    return `${source}${separator}thumbnail_retry=${encodeURIComponent(String(attempt))}`;
  }
}

function setMediaThumbnailFailed(preview, image) {
  if (!preview || !image) return;
  preview.classList.remove("thumbnail-retrying");
  preview.classList.add("thumbnail-failed");
  image.removeAttribute("src");
  delete image.dataset.lazyThumbnailSrc;
}

function retryMediaThumbnail(preview, image, sourceUrl, attempt) {
  if (!preview || !image || !sourceUrl) return false;
  if (!image.isConnected || !preview.isConnected) return false;

  preview.classList.remove("thumbnail-failed");
  preview.classList.add("thumbnail-retrying");
  image.removeAttribute("src");
  delete image.dataset.lazyThumbnailSrc;

  window.setTimeout(() => {
    if (!image.isConnected || !preview.isConnected) return;
    preview.classList.remove("thumbnail-retrying");
    image.src = mediaThumbnailRetryUrl(sourceUrl, attempt);
  }, MEDIA_THUMBNAIL_RETRY_DELAY_MS);

  return true;
}

function handleMediaThumbnailLoad(image) {
  if (!image) return;
  const preview = image.closest("[data-thumbnail-preview]");
  if (preview) preview.classList.remove("thumbnail-failed", "thumbnail-retrying");
  delete image.dataset.thumbnailRetryAttempt;
}

function handleMediaThumbnailError(image) {
  if (!image) return;
  const preview = image.closest("[data-thumbnail-preview]");
  if (!preview) return;

  const sourceUrl = String(
    image.dataset.thumbnailOriginalSrc
    || image.getAttribute("src")
    || image.dataset.lazyThumbnailSrc
    || ""
  ).trim();

  if (sourceUrl && !image.dataset.thumbnailOriginalSrc) {
    image.dataset.thumbnailOriginalSrc = sourceUrl;
  }

  const attempt = Number.parseInt(String(image.dataset.thumbnailRetryAttempt || "0"), 10) || 0;
  if (sourceUrl && attempt < MEDIA_THUMBNAIL_MAX_AUTO_RETRIES) {
    image.dataset.thumbnailRetryAttempt = String(attempt + 1);
    if (retryMediaThumbnail(preview, image, sourceUrl, attempt + 1)) return;
  }

  setMediaThumbnailFailed(preview, image);
}

function bindMediaThumbnailResilience(image) {
  if (!image || image.dataset.thumbnailResilienceBound === "1") return;
  image.dataset.thumbnailResilienceBound = "1";
  const sourceUrl = String(image.dataset.lazyThumbnailSrc || image.getAttribute("src") || "").trim();
  if (sourceUrl) image.dataset.thumbnailOriginalSrc = sourceUrl;
  image.addEventListener("load", () => handleMediaThumbnailLoad(image));
  image.addEventListener("error", () => handleMediaThumbnailError(image));
}

function activateLazyMediaThumbnail(image, generation = mediaThumbnailLazyGeneration) {
  if (!image || generation !== mediaThumbnailLazyGeneration || !image.isConnected) return;

  const pendingSrc = String(image.dataset.lazyThumbnailSrc || "");
  if (!pendingSrc) return;

  const currentSrc = image.getAttribute("src") || "";
  if (currentSrc) {
    if (currentSrc === pendingSrc) {
      delete image.dataset.lazyThumbnailSrc;
    }
    return;
  }

  if (!image.dataset.thumbnailOriginalSrc) image.dataset.thumbnailOriginalSrc = pendingSrc;
  image.src = pendingSrc;
  delete image.dataset.lazyThumbnailSrc;
}

function resetMediaThumbnailLazyLoading() {
  mediaThumbnailLazyGeneration += 1;
  if (mediaThumbnailLazyObserver) {
    mediaThumbnailLazyObserver.disconnect();
    mediaThumbnailLazyObserver = null;
  }
}

function shouldEagerLoadMediaThumbnail(image, index) {
  if (!image || !image.isConnected) return false;

  const viewportHeight = window.innerHeight || document.documentElement.clientHeight || 0;
  if (!viewportHeight) return index < MEDIA_THUMBNAIL_EAGER_INITIAL_LIMIT;

  const rect = image.getBoundingClientRect();
  const eagerTopLimit = viewportHeight + MEDIA_THUMBNAIL_EAGER_VIEWPORT_MARGIN_PX;
  const eagerBottomLimit = -MEDIA_THUMBNAIL_EAGER_VIEWPORT_MARGIN_PX;

  if (rect.top <= eagerTopLimit && rect.bottom >= eagerBottomLimit) return true;

  return index < MEDIA_THUMBNAIL_EAGER_INITIAL_LIMIT && rect.top <= eagerTopLimit * 1.5;
}

function bindMediaThumbnailLazyLoading(rootElement) {
  const root = rootElement || els.mediaList;
  if (!root) return;

  const images = Array.from(root.querySelectorAll("img.media-thumbnail[data-lazy-thumbnail-src]"));
  if (!images.length) return;

  const generation = mediaThumbnailLazyGeneration;
  const imagesToObserve = [];

  for (const [index, image] of images.entries()) {
    if (shouldEagerLoadMediaThumbnail(image, index)) {
      activateLazyMediaThumbnail(image, generation);
    } else {
      imagesToObserve.push(image);
    }
  }

  if (!imagesToObserve.length) return;

  if (!("IntersectionObserver" in window)) {
    for (const image of imagesToObserve) {
      activateLazyMediaThumbnail(image, generation);
    }
    return;
  }

  mediaThumbnailLazyObserver = new IntersectionObserver((entries, observer) => {
    for (const entry of entries) {
      if (!entry.isIntersecting && entry.intersectionRatio <= 0) continue;

      const image = entry.target;
      observer.unobserve(image);
      window.requestAnimationFrame(() => activateLazyMediaThumbnail(image, generation));
    }
  }, {
    root: null,
    rootMargin: MEDIA_THUMBNAIL_LAZY_ROOT_MARGIN,
    threshold: 0.01,
  });

  for (const image of imagesToObserve) {
    mediaThumbnailLazyObserver.observe(image);
  }
}

async function fetchJson(path, params = {}) {
  const url = apiUrl(path, params);
  const operation = diagnosticOperationStart("frontend.fetch.api", { method: "GET", path, url });
  const response = await fetch(url, { cache: "no-store" });
  const payload = await response.json();
  diagnosticOperationEnd("frontend.fetch.api", operation, {
    method: "GET",
    path,
    url,
    status: response.status,
    payload_bytes: JSON.stringify(payload).length,
  });
  if (!response.ok || payload.ok === false) {
    const backendError = payload.message_object;
    const error = new Error(localizedMessageText(backendError) || text("error.http", { status: response.status }));
    error.status = response.status;
    error.code = payload.code || backendError?.code || "";
    error.payload = payload;
    throw error;
  }
  return payload;
}

async function postJson(path, params = {}) {
  const url = apiUrl(path, params);
  const operation = diagnosticOperationStart("frontend.fetch.api", { method: "POST", path, url });
  const response = await fetch(url, { method: "POST", cache: "no-store" });
  const payload = await response.json();
  diagnosticOperationEnd("frontend.fetch.api", operation, {
    method: "POST",
    path,
    url,
    status: response.status,
    payload_bytes: JSON.stringify(payload).length,
  });
  if (!response.ok || payload.ok === false) {
    const backendError = payload.message_object;
    const error = new Error(localizedMessageText(backendError) || text("error.http", { status: response.status }));
    error.status = response.status;
    error.code = payload.code || backendError?.code || "";
    error.payload = payload;
    throw error;
  }
  return payload;
}


function numericStatusValue(value) {
  const numberValue = Number(value || 0);
  return Number.isFinite(numberValue) ? numberValue : 0;
}

function localizedInteger(value) {
  return String(Math.round(numericStatusValue(value))).replace(/\B(?=(\d{3})+(?!\d))/g, " ");
}

function cacheStatusEntriesText(count) {
  return text("settings.cacheStatusEntries", { count: localizedInteger(count) });
}

function positiveIntegerInputValue(input) {
  const raw = String(input?.value || "").trim();
  if (!/^\d+$/.test(raw)) return null;

  const value = Number(raw);
  if (!Number.isInteger(value) || value <= 0) return null;

  return value;
}

function pageSizeSourceSummary(sources = {}) {
  const values = Object.values(sources || {}).filter(value => String(value || "").trim() !== "");
  if (!values.length) return "config.json";

  return [...new Set(values)].join(", ");
}

function settingSourceSummary(sources = {}) {
  return pageSizeSourceSummary(sources);
}

function cacheStatusCard(titleText, mainText, metaLines = [], statusText = "") {
  const card = document.createElement("article");
  card.className = "cache-status-card";

  const title = document.createElement("h4");
  title.textContent = titleText;
  card.appendChild(title);

  const main = document.createElement("p");
  main.className = "cache-status-main";
  main.textContent = mainText;
  card.appendChild(main);

  if (statusText) {
    const badge = document.createElement("p");
    badge.className = "cache-status-badge";
    badge.textContent = statusText;
    card.appendChild(badge);
  }

  const lines = metaLines.filter(value => value !== undefined && value !== null && String(value).trim() !== "");
  if (lines.length) {
    const list = document.createElement("ul");
    list.className = "cache-status-lines";
    for (const line of lines) {
      const item = document.createElement("li");
      item.textContent = String(line);
      list.appendChild(item);
    }
    card.appendChild(list);
  }

  return card;
}


function cacheOverviewItem(labelText, valueText, detailText = "") {
  const item = document.createElement("div");
  item.className = "cache-overview-item";

  const label = document.createElement("span");
  label.className = "cache-overview-label";
  label.textContent = labelText;
  item.appendChild(label);

  const value = document.createElement("strong");
  value.className = "cache-overview-value";
  value.textContent = valueText;
  item.appendChild(value);

  if (String(detailText || "").trim()) {
    const detail = document.createElement("span");
    detail.className = "cache-overview-detail";
    detail.textContent = detailText;
    item.appendChild(detail);
  }

  return item;
}


function settingsSummaryRow(labelText, valueText, detailLines = [], options = {}) {
  const row = document.createElement("div");
  row.className = options.warning ? "settings-summary-row warning" : "settings-summary-row";

  const label = document.createElement("span");
  label.className = "settings-summary-label";
  label.textContent = labelText;
  row.appendChild(label);

  const valueBlock = document.createElement("div");
  valueBlock.className = "settings-summary-value-block";

  const value = document.createElement("strong");
  value.className = "settings-summary-value";
  value.textContent = valueText;
  valueBlock.appendChild(value);

  const lines = Array.isArray(detailLines) ? detailLines : [detailLines];
  for (const line of lines) {
    const trimmed = String(line || "").trim();
    if (!trimmed) continue;
    const detail = document.createElement("span");
    detail.className = "settings-summary-detail";
    detail.textContent = trimmed;
    valueBlock.appendChild(detail);
  }

  row.appendChild(valueBlock);
  return row;
}

function settingsSummaryList(rows = [], className = "") {
  const list = document.createElement("div");
  list.className = className ? `settings-summary-list ${className}` : "settings-summary-list";
  for (const row of rows) {
    if (!row) continue;
    const [labelText, valueText, detailLines, options] = row;
    list.appendChild(settingsSummaryRow(labelText, valueText, detailLines, options || {}));
  }
  return list;
}

function renderCacheStatusOverview({ dynamicBytes, limitBytes, protectedBytes, totalBytes, dynamicEntries, protectedEntries, totalEntries }) {
  const section = document.createElement("section");
  section.className = "cache-status-overview settings-stat-overview";

  section.appendChild(settingsSummaryList([
    [
      text("settings.cacheOverviewDynamic"),
      `${formatBytes(dynamicBytes)} / ${formatBytes(limitBytes)}`,
      text("settings.cacheOverviewItems", { count: localizedInteger(numericStatusValue(dynamicEntries)) }),
      { warning: limitBytes > 0 && dynamicBytes > limitBytes },
    ],
    [
      text("settings.cacheOverviewProtected"),
      formatBytes(protectedBytes),
      text("settings.cacheOverviewItems", { count: localizedInteger(numericStatusValue(protectedEntries)) }),
    ],
    [
      text("settings.cacheOverviewTotal"),
      formatBytes(totalBytes),
      text("settings.cacheOverviewItems", { count: localizedInteger(numericStatusValue(totalEntries)) }),
    ],
  ], "cache-overview-list"));

  return section;
}


function renderSourceRootStatus(sourceRoot) {
  if (!els.sourceRootSummary) return;

  const root = sourceRoot || {};
  const available = root.available === true;
  const container = document.createElement("div");
  container.className = "source-root-status-card settings-flat-status";

  const path = document.createElement("div");
  path.className = "source-root-path";
  path.textContent = root.data_root || "—";
  container.appendChild(path);

  const meta = document.createElement("div");
  meta.className = available ? "source-root-meta" : "source-root-meta warning";
  meta.textContent = available ? text("settings.sourceRootAvailable") : text("settings.sourceRootUnavailable");
  container.appendChild(meta);

  const localizedStatus = localizedMessageText(root.status_message);
  if (localizedStatus && localizedStatus !== meta.textContent) {
    const message = document.createElement("div");
    message.className = available ? "source-root-message" : "source-root-message warning";
    message.textContent = localizedStatus;
    container.appendChild(message);
  }

  els.sourceRootSummary.replaceChildren(container);

  if (els.sourceRootInput && root.data_root && !String(els.sourceRootInput.value || "").trim()) {
    els.sourceRootInput.value = root.data_root;
  }
}

function renderSourceRootVerification(payload, options = {}) {
  if (!els.sourceRootMessage) return;

  const filesystem = payload?.filesystem || {};
  const match = payload?.match_check || {};
  const primaryMessage = Array.isArray(payload?.result_messages) ? payload.result_messages[0] : null;
  const filesystemMessage = Array.isArray(filesystem?.result_messages) ? filesystem.result_messages[0] : null;
  const matchMessage = Array.isArray(match?.result_messages) ? match.result_messages[0] : null;
  const parts = [localizedMessageText(primaryMessage)];

  if (filesystem?.passed === false) {
    parts.push(localizedMessageText(filesystemMessage));
  } else if (Number(match.sample_count || 0) > 0) {
    parts.push(localizedMessageText(matchMessage) || text("settings.sourceRootMatch", {
      matched: localizedInteger(match.matched_count || 0),
      sample: localizedInteger(match.sample_count || 0),
    }));
  } else if (filesystem?.passed) {
    parts.push(localizedMessageText(matchMessage) || text("settings.sourceRootMatchNone"));
  }

  els.sourceRootMessage.textContent = [...new Set(parts.filter(Boolean))].join(" ");
  els.sourceRootMessage.classList.toggle("error", !payload?.can_save && options.error !== false);
}

function sourceRootInputValue() {
  return String(els.sourceRootInput?.value || "").trim();
}

async function verifySourceRootSetting() {
  if (!els.sourceRootVerify) return;

  const value = sourceRootInputValue();
  if (!value) {
    if (els.sourceRootMessage) {
      els.sourceRootMessage.textContent = text("settings.sourceRootVerifyError");
      els.sourceRootMessage.classList.add("error");
    }
    state.sourceRootVerification = null;
    return;
  }

  if (els.sourceRootMessage) {
    els.sourceRootMessage.textContent = text("settings.sourceRootVerifying");
    els.sourceRootMessage.classList.remove("error");
  }

  els.sourceRootVerify.disabled = true;
  if (els.sourceRootSave) els.sourceRootSave.disabled = true;

  try {
    const payload = await postJson("/api/settings/runtime/data-root/verify", { data_root: value });
    state.sourceRootVerification = payload;
    renderSourceRootVerification(payload);
  } catch (error) {
    state.sourceRootVerification = null;
    if (els.sourceRootMessage) {
      els.sourceRootMessage.textContent = error.message || text("settings.sourceRootVerifyError");
      els.sourceRootMessage.classList.add("error");
    }
  } finally {
    els.sourceRootVerify.disabled = false;
    if (els.sourceRootSave) els.sourceRootSave.disabled = false;
  }
}

async function saveSourceRootSetting() {
  if (!els.sourceRootSave) return;

  const value = sourceRootInputValue();
  const verified = state.sourceRootVerification;

  if (!verified || verified.request_path !== value || !verified.can_save) {
    if (els.sourceRootMessage) {
      els.sourceRootMessage.textContent = text("settings.sourceRootSaveBlocked");
      els.sourceRootMessage.classList.add("error");
    }
    return;
  }

  if (els.sourceRootMessage) {
    els.sourceRootMessage.textContent = text("settings.sourceRootSaving");
    els.sourceRootMessage.classList.remove("error");
  }

  els.sourceRootSave.disabled = true;
  if (els.sourceRootVerify) els.sourceRootVerify.disabled = true;

  try {
    const payload = await postJson("/api/settings/runtime/data-root", { data_root: value });
    renderCacheSettingsStatus(payload);
    if (els.sourceRootMessage) {
      const resultMessage = Array.isArray(payload?.result_messages) ? payload.result_messages[0] : null;
      els.sourceRootMessage.textContent = localizedMessageText(resultMessage) || text("settings.sourceRootSaved");
      els.sourceRootMessage.classList.remove("error");
    }
    state.sourceRootVerification = null;
    await loadStatus();
    await reloadSafely(() => openFolder(state.folder || ""));
  } catch (error) {
    if (els.sourceRootMessage) {
      els.sourceRootMessage.textContent = error.message || text("settings.sourceRootSaveError");
      els.sourceRootMessage.classList.add("error");
    }
  } finally {
    els.sourceRootSave.disabled = false;
    if (els.sourceRootVerify) els.sourceRootVerify.disabled = false;
  }
}


function appendCacheBackendMessageList(section, messages, className = "") {
  const list = Array.isArray(messages) ? messages : [];
  for (const message of list) {
    const localized = localizedMessageText(message);
    if (!localized) continue;
    const paragraph = document.createElement("p");
    paragraph.className = className ? `cache-status-note ${className}` : "cache-status-note";
    paragraph.textContent = localized;
    section.appendChild(paragraph);
  }
}

function renderCacheCleanupPlan(plan) {
  const section = document.createElement("section");
  section.className = "cache-cleanup-plan settings-action-panel regular-preview-cleanup";

  const heading = document.createElement("div");
  heading.className = "settings-section-head-row";
  const title = document.createElement("h4");
  title.textContent = text("settings.cacheCleanupPlanTitle");
  heading.appendChild(title);
  section.appendChild(heading);

  const needed = plan && plan.needed === true;
  const candidates = numericStatusValue(plan?.candidate_entries);
  const candidateBytes = numericStatusValue(plan?.candidate_bytes);
  const afterDynamic = numericStatusValue(plan?.dynamic_size_after_plan_bytes);

  const status = document.createElement("p");
  status.className = needed ? "cache-status-note warning settings-brief-note" : "cache-status-note settings-brief-note";
  status.textContent = needed ? text("settings.cacheCleanupPlanNeeded") : text("settings.cacheCleanupPlanNotNeeded");
  section.appendChild(status);

  section.appendChild(settingsSummaryList([
    [text("settings.cacheCleanupPlanCandidates"), cacheStatusEntriesText(candidates)],
    [text("settings.cacheCleanupPlanReclaimable"), formatBytes(candidateBytes)],
    [text("settings.cacheCleanupPlanAfter"), formatBytes(afterDynamic)],
  ], "cache-cleanup-summary"));

  appendCacheBackendMessageList(section, plan?.blocker_messages || [], "warning");
  appendCacheBackendMessageList(section, plan?.warning_messages || [], "warning");

  if (needed && candidates > 0) {
    const actions = document.createElement("div");
    actions.className = "cache-cleanup-actions settings-actions-right";

    const cleanupButton = document.createElement("button");
    cleanupButton.type = "button";
    cleanupButton.className = "write-action";
    cleanupButton.dataset.longJobAction = "true";
    cleanupButton.textContent = text("settings.cacheCleanupExecute");
    cleanupButton.addEventListener("click", () => startDynamicCacheCleanup(plan));
    actions.appendChild(cleanupButton);

    section.appendChild(actions);
  }

  return section;
}

function protectedAuditSampleLine(item) {
  if (!item || typeof item !== "object") return "";
  const size = item.size_bytes ?? item.recorded_size_bytes;
  const path = item.output_rel_path || item.media_rel_path || "";
  const type = item.thumbnail_type ? `${item.thumbnail_type} · ` : "";
  const status = item.status ? `${item.status} · ` : "";
  const reasonValue = item.reason_code ? text(item.reason_code) : (item.reason || "");
  const reason = reasonValue ? ` · ${reasonValue}` : "";
  const sizeText = size !== undefined && size !== null ? `${formatBytes(numericStatusValue(size))} · ` : "";
  return `${sizeText}${type}${status}${path}${reason}`.trim();
}

function appendProtectedAuditSample(section, titleText, items) {
  if (!Array.isArray(items) || !items.length) return;

  const title = document.createElement("p");
  title.className = "cache-cleanup-sample-title";
  title.textContent = titleText;
  section.appendChild(title);

  const list = document.createElement("ul");
  list.className = "cache-status-lines cache-cleanup-sample";
  for (const item of items) {
    const line = document.createElement("li");
    line.textContent = protectedAuditSampleLine(item);
    list.appendChild(line);
  }
  section.appendChild(list);
}

function renderProtectedCacheAuditPanel() {
  const section = document.createElement("section");
  section.className = "cache-cleanup-plan protected-cache-audit";

  const heading = document.createElement("h4");
  heading.textContent = text("settings.protectedAuditTitle");
  section.appendChild(heading);

  const note = document.createElement("p");
  note.className = "cache-status-note";
  note.textContent = text("settings.protectedAuditNote");
  section.appendChild(note);

  const actions = document.createElement("div");
  actions.className = "cache-cleanup-actions";

  const button = document.createElement("button");
  button.type = "button";
  button.textContent = text("settings.protectedAuditRun");
  button.disabled = Boolean(state.protectedCacheAuditLoading || state.protectedCacheCleanupExecuteLoading);
  button.addEventListener("click", loadProtectedCacheAudit);
  actions.appendChild(button);
  section.appendChild(actions);

  if (state.protectedCacheAuditLoading) {
    const loading = document.createElement("p");
    loading.className = "cache-status-note";
    loading.textContent = text("settings.protectedAuditRunning");
    section.appendChild(loading);
    return section;
  }

  if (state.protectedCacheAuditError) {
    const error = document.createElement("p");
    error.className = "cache-status-note warning";
    error.textContent = `${text("settings.protectedAuditError")} ${state.protectedCacheAuditError}`.trim();
    section.appendChild(error);
    return section;
  }

  const audit = state.protectedCacheAudit;
  if (!audit) {
    const empty = document.createElement("p");
    empty.className = "cache-status-note";
    empty.textContent = text("settings.protectedAuditNotRun");
    section.appendChild(empty);
    return section;
  }

  const db = audit.db || {};
  const filesystem = audit.filesystem || {};
  const orphans = audit.potential_orphans || {};
  const samples = audit.samples || {};

  const grid = document.createElement("div");
  grid.className = "cache-status-grid";

  grid.appendChild(cacheStatusCard(
    text("settings.protectedAuditValid"),
    cacheStatusEntriesText(numericStatusValue(db.valid_rows)),
    [
      text("settings.protectedAuditFilesystemSize", { size: formatBytes(numericStatusValue(db.valid_rows_bytes)) }),
      text("settings.protectedAuditDbRecordedSize", { size: formatBytes(numericStatusValue(db.recorded_size_bytes)) }),
    ]
  ));

  grid.appendChild(cacheStatusCard(
    text("settings.protectedAuditPotentialOrphans"),
    cacheStatusEntriesText(numericStatusValue(orphans.entries)),
    [
      text("settings.protectedAuditEstimatedReclaimable", { size: formatBytes(numericStatusValue(orphans.estimated_reclaimable_bytes)) }),
      text("settings.protectedAuditDuration", { seconds: Number(audit.duration_seconds || 0).toFixed(3) }),
    ]
  ));

  grid.appendChild(cacheStatusCard(
    text("settings.protectedAuditFilesWithoutDb"),
    cacheStatusEntriesText(numericStatusValue(filesystem.files_without_db)),
    [text("settings.protectedAuditEstimatedReclaimable", { size: formatBytes(numericStatusValue(filesystem.files_without_db_bytes)) })]
  ));

  grid.appendChild(cacheStatusCard(
    text("settings.protectedAuditDbMissingFiles"),
    cacheStatusEntriesText(numericStatusValue(db.rows_missing_file)),
    [text("settings.protectedAuditDbRecordedSize", { size: formatBytes(numericStatusValue(db.rows_missing_file_recorded_bytes)) })]
  ));

  grid.appendChild(cacheStatusCard(
    text("settings.protectedAuditMediaUnavailable"),
    cacheStatusEntriesText(numericStatusValue(db.rows_media_unavailable) + numericStatusValue(db.rows_media_missing)),
    [cacheStatusEntriesText(numericStatusValue(db.rows_status_not_ready))]
  ));

  grid.appendChild(cacheStatusCard(
    text("settings.protectedAuditTemporaryFiles"),
    cacheStatusEntriesText(numericStatusValue(filesystem.temporary_files)),
    [text("settings.protectedAuditEstimatedReclaimable", { size: formatBytes(numericStatusValue(filesystem.temporary_files_bytes)) })]
  ));

  section.appendChild(grid);

  appendProtectedAuditSample(section, text("settings.protectedAuditSampleFilesWithoutDb"), samples.files_without_db || []);
  appendProtectedAuditSample(section, text("settings.protectedAuditSampleDbMissingFiles"), samples.db_rows_missing_file || []);
  appendProtectedAuditSample(section, text("settings.protectedAuditSampleMediaUnavailable"), samples.media_unavailable || []);

  const readOnly = document.createElement("p");
  readOnly.className = "cache-status-note";
  readOnly.textContent = text("settings.protectedAuditReadOnly");
  section.appendChild(readOnly);

  return section;
}

async function loadProtectedCacheAudit() {
  state.protectedCacheAuditLoading = true;
  state.protectedCacheAuditError = "";
  state.protectedCacheAudit = null;
  if (state.cacheSettingsPayload) {
    renderCacheSettingsStatus(state.cacheSettingsPayload);
  }

  try {
    const payload = await fetchJson("/api/thumbnail-cache/protected-orphan-audit");
    state.protectedCacheAudit = payload?.protected_orphan_audit || payload;
  } catch (error) {
    state.protectedCacheAuditError = error.message || String(error);
  } finally {
    state.protectedCacheAuditLoading = false;
    if (state.cacheSettingsPayload) {
      renderCacheSettingsStatus(state.cacheSettingsPayload);
    }
  }
}

function protectedPlanSamples(...groups) {
  const result = [];
  for (const group of groups) {
    if (Array.isArray(group)) result.push(...group);
  }
  return result;
}


function renderProtectedCacheCleanupExecuteResult(result) {
  const section = document.createElement("section");
  section.className = "cache-cleanup-plan protected-cache-cleanup-execute settings-action-panel";

  const status = document.createElement("p");
  status.className = numericStatusValue(result.error_count) > 0 ? "cache-status-note warning settings-brief-note" : "cache-status-note settings-brief-note";
  status.textContent = numericStatusValue(result.error_count) > 0
    ? text("settings.protectedExecuteCompletedWithErrors")
    : text("settings.protectedExecuteCompleted");
  section.appendChild(status);

  section.appendChild(settingsSummaryList([
    [
      text("settings.protectedExecuteDeletedFiles"),
      cacheStatusEntriesText(numericStatusValue(result.deleted_files)),
      text("settings.protectedExecuteMissingFiles", { count: numericStatusValue(result.missing_files) }),
    ],
    [
      text("settings.protectedExecuteDeletedDbRows"),
      cacheStatusEntriesText(numericStatusValue(result.deleted_db_rows)),
      text("settings.protectedExecuteSkipped", { count: numericStatusValue(result.skipped_changed) }),
    ],
    [
      text("settings.protectedExecuteRemovedBytes"),
      formatBytes(numericStatusValue(result.removed_bytes)),
      text("settings.protectedExecuteDuration", { seconds: formatSeconds(result.duration_seconds) }),
    ],
    [
      text("settings.protectedExecuteErrors"),
      cacheStatusEntriesText(numericStatusValue(result.error_count)),
      "",
      { warning: numericStatusValue(result.error_count) > 0 },
    ],
  ], "cache-execute-summary"));

  appendProtectedAuditSample(section, text("settings.protectedExecuteSampleDeletedFiles"), result.sample_deleted_files || []);
  appendProtectedAuditSample(section, text("settings.protectedExecuteSampleDeletedDbRows"), result.sample_deleted_db_rows || []);
  appendProtectedAuditSample(section, text("settings.protectedExecuteSampleErrors"), result.sample_errors || []);
  return section;
}


function protectedCacheMaintenanceStatusCard(titleText, count, detailLines = [], statusText = "") {
  return cacheStatusCard(
    titleText,
    cacheStatusEntriesText(numericStatusValue(count)),
    detailLines,
    statusText
  );
}

function renderProtectedCacheMaintenanceTechnicalDetails(audit, plan) {
  const details = document.createElement("details");
  details.className = "cache-technical-details settings-subdetails";

  const summary = document.createElement("summary");
  summary.textContent = text("settings.stableMaintenanceTechnicalDetails");
  details.appendChild(summary);

  const body = document.createElement("div");
  body.className = "cache-technical-details-body";

  if (audit) {
    const auditBlock = document.createElement("div");
    auditBlock.className = "cache-technical-block";
    const title = document.createElement("h5");
    title.textContent = text("settings.protectedAuditTitle");
    auditBlock.appendChild(title);
    const filesystem = audit.filesystem || {};
    const db = audit.db || {};
    const orphans = audit.potential_orphans || {};
    const lines = document.createElement("ul");
    lines.className = "cache-status-lines";
    for (const lineText of [
      `${text("settings.protectedAuditValid")}: ${localizedInteger(numericStatusValue(db.valid_rows))}`,
      `${text("settings.protectedAuditPotentialOrphans")}: ${localizedInteger(numericStatusValue(orphans.entries))}`,
      `${text("settings.protectedAuditFilesWithoutDb")}: ${localizedInteger(numericStatusValue(filesystem.files_without_db))}`,
      `${text("settings.protectedAuditDbMissingFiles")}: ${localizedInteger(numericStatusValue(db.rows_missing_file))}`,
      `${text("settings.protectedAuditTemporaryFiles")}: ${localizedInteger(numericStatusValue(filesystem.temporary_files))}`,
    ]) {
      const item = document.createElement("li");
      item.textContent = lineText;
      lines.appendChild(item);
    }
    auditBlock.appendChild(lines);
    body.appendChild(auditBlock);
  }

  if (plan) {
    const planBlock = document.createElement("div");
    planBlock.className = "cache-technical-block";
    const title = document.createElement("h5");
    title.textContent = text("settings.protectedPlanTitle");
    planBlock.appendChild(title);
    const summaryData = plan.summary || {};
    const lines = document.createElement("ul");
    lines.className = "cache-status-lines";
    for (const lineText of [
      `${text("settings.protectedPlanSafeFiles")}: ${localizedInteger(numericStatusValue(plan.safe_to_delete_files?.entries))}`,
      `${text("settings.protectedPlanSafeDbRows")}: ${localizedInteger(numericStatusValue(plan.safe_to_delete_db_rows?.entries))}`,
      `${text("settings.protectedPlanReviewRequired")}: ${localizedInteger(numericStatusValue(plan.review_required?.entries))}`,
      `${text("settings.protectedPlanBlocked")}: ${localizedInteger(numericStatusValue(plan.blocked?.entries))}`,
      `${text("settings.protectedPlanEstimatedReclaimable", { size: formatBytes(numericStatusValue(summaryData.estimated_reclaimable_bytes)) })}`,
    ]) {
      const item = document.createElement("li");
      item.textContent = lineText;
      lines.appendChild(item);
    }
    planBlock.appendChild(lines);
    body.appendChild(planBlock);
  }

  details.appendChild(body);
  return details;
}

function protectedCacheMaintenanceJobKind(kind) {
  return kind === "protected-cache-orphan-check" || kind === "protected-cache-orphan-clean";
}

function resetProtectedCacheMaintenanceState() {
  state.protectedCacheAudit = null;
  state.protectedCacheAuditLoading = false;
  state.protectedCacheAuditError = "";
  state.protectedCacheCleanupPlan = null;
  state.protectedCacheCleanupPlanLoading = false;
  state.protectedCacheCleanupPlanError = "";
  state.protectedCacheCleanupExecute = null;
  state.protectedCacheCleanupExecuteLoading = false;
  state.protectedCacheCleanupExecuteError = "";
  state.protectedCacheMaintenanceLoading = false;
  state.protectedCacheMaintenanceError = "";
}

function syncProtectedCacheMaintenanceJob(job) {
  if (!job || !protectedCacheMaintenanceJobKind(job.kind)) return false;

  state.protectedCacheMaintenanceError = "";
  state.protectedCacheAuditError = "";
  state.protectedCacheCleanupPlanError = "";
  state.protectedCacheCleanupExecuteError = "";

  if (job.kind === "protected-cache-orphan-check") {
    state.protectedCacheCleanupExecute = null;
    state.protectedCacheCleanupExecuteLoading = false;
    if (jobIsRunning(job)) {
      state.protectedCacheMaintenanceLoading = true;
      return true;
    }
    state.protectedCacheMaintenanceLoading = false;
    if (job.state === "completed") {
      const result = job.result || {};
      state.protectedCacheAudit = result.protected_orphan_audit || null;
      state.protectedCacheCleanupPlan = result.protected_orphan_cleanup_plan || null;
      return true;
    }
    if (job.state === "failed") {
      state.protectedCacheMaintenanceError = job.error || text("settings.stableMaintenanceError");
      return true;
    }
  }

  if (job.kind === "protected-cache-orphan-clean") {
    state.protectedCacheMaintenanceLoading = false;
    if (jobIsRunning(job)) {
      state.protectedCacheCleanupExecuteLoading = true;
      return true;
    }
    state.protectedCacheCleanupExecuteLoading = false;
    state.protectedCacheCleanupPlan = null;
    if (job.state === "completed") {
      const result = job.result || {};
      state.protectedCacheCleanupExecute = result.protected_orphan_cleanup_execute || result;
      return true;
    }
    if (job.state === "failed") {
      state.protectedCacheCleanupExecuteError = job.error || text("settings.protectedExecuteError");
      return true;
    }
  }

  return false;
}

function renderProtectedCacheMaintenancePanel() {
  const section = document.createElement("section");
  section.className = "cache-maintenance-card stable-preview-maintenance settings-action-panel";

  const heading = document.createElement("div");
  heading.className = "settings-section-head-row stable-maintenance-heading";
  const title = document.createElement("h4");
  title.textContent = text("settings.stableMaintenanceTitle");
  heading.appendChild(title);
  section.appendChild(heading);

  const makeCheckButton = () => {
    const checkButton = document.createElement("button");
    checkButton.type = "button";
    checkButton.textContent = text("settings.stableMaintenanceRun");
    checkButton.disabled = Boolean(state.protectedCacheMaintenanceLoading || state.protectedCacheCleanupExecuteLoading || isLongJobLocked() || catalogUpdateWorkflowIsActive());
    checkButton.addEventListener("click", loadProtectedCacheMaintenance);
    return checkButton;
  };

  const appendMaintenanceActions = (plan, safeEntries) => {
    const actions = document.createElement("div");
    actions.className = "cache-cleanup-actions stable-maintenance-actions";
    actions.appendChild(makeCheckButton());

    if (safeEntries > 0 && plan) {
      const executeButton = document.createElement("button");
      executeButton.type = "button";
      executeButton.className = "write-action";
      executeButton.textContent = text("settings.stableMaintenanceCleanRun");
      executeButton.disabled = Boolean(state.protectedCacheCleanupExecuteLoading || isLongJobLocked() || catalogUpdateWorkflowIsActive());
      executeButton.addEventListener("click", () => executeProtectedCacheCleanup(plan));
      actions.appendChild(executeButton);
    }

    section.appendChild(actions);
  };

  if (state.protectedCacheMaintenanceLoading) {
    const loading = document.createElement("p");
    loading.className = "cache-status-note settings-brief-note";
    loading.textContent = text("settings.stableMaintenanceRunning");
    section.appendChild(loading);
    return section;
  }

  const errorMessage = state.protectedCacheMaintenanceError || state.protectedCacheAuditError || state.protectedCacheCleanupPlanError;
  if (errorMessage) {
    const error = document.createElement("p");
    error.className = "cache-status-note warning settings-brief-note";
    error.textContent = `${text("settings.stableMaintenanceError")} ${errorMessage}`.trim();
    section.appendChild(error);
    appendMaintenanceActions(null, 0);
    return section;
  }

  const audit = state.protectedCacheAudit;
  const plan = state.protectedCacheCleanupPlan;

  if (state.protectedCacheCleanupExecuteLoading) {
    const loading = document.createElement("p");
    loading.className = "cache-status-note settings-brief-note";
    loading.textContent = text("settings.protectedExecuteRunning");
    section.appendChild(loading);
    return section;
  }

  if (!audit && !plan && !state.protectedCacheCleanupExecute) {
    const empty = document.createElement("p");
    empty.className = "cache-status-note settings-brief-note";
    empty.textContent = text("settings.stableMaintenanceNotRun");
    section.appendChild(empty);
    appendMaintenanceActions(null, 0);
    return section;
  }

  if (state.protectedCacheCleanupExecute && !state.protectedCacheCleanupExecuteLoading) {
    section.appendChild(renderProtectedCacheCleanupExecuteResult(state.protectedCacheCleanupExecute));
    appendMaintenanceActions(null, 0);
    section.appendChild(renderProtectedCacheMaintenanceTechnicalDetails(audit, null));
    return section;
  }

  const auditDb = audit?.db || {};
  const planSummary = plan?.summary || {};
  const safeEntries = numericStatusValue(planSummary.safe_to_delete_entries);
  const safeBytes = numericStatusValue(planSummary.estimated_reclaimable_bytes);
  const reviewEntries = numericStatusValue(plan?.review_required?.entries);

  section.appendChild(settingsSummaryList([
    [text("settings.stableMaintenanceValid"), cacheStatusEntriesText(numericStatusValue(auditDb.valid_rows))],
    [
      text("settings.stableMaintenanceCleanable"),
      cacheStatusEntriesText(safeEntries),
      safeEntries > 0 ? text("settings.stableMaintenanceReclaimable", { size: formatBytes(safeBytes) }) : "",
      { warning: safeEntries > 0 },
    ],
    [
      text("settings.stableMaintenanceReview"),
      cacheStatusEntriesText(reviewEntries),
      "",
      { warning: reviewEntries > 0 },
    ],
  ], "stable-maintenance-summary"));

  if (state.protectedCacheCleanupExecuteLoading) {
    const loading = document.createElement("p");
    loading.className = "cache-status-note settings-brief-note";
    loading.textContent = text("settings.protectedExecuteRunning");
    section.appendChild(loading);
  } else if (state.protectedCacheCleanupExecuteError) {
    const error = document.createElement("p");
    error.className = "cache-status-note warning settings-brief-note";
    error.textContent = `${text("settings.protectedExecuteError")} ${state.protectedCacheCleanupExecuteError}`.trim();
    section.appendChild(error);
  } else if (plan && safeEntries <= 0) {
    const clean = document.createElement("p");
    clean.className = "cache-status-note settings-brief-note";
    clean.textContent = text("settings.stableMaintenanceNothingToClean");
    section.appendChild(clean);
  }

  appendMaintenanceActions(plan, safeEntries);
  section.appendChild(renderProtectedCacheMaintenanceTechnicalDetails(audit, plan));
  return section;
}

async function loadProtectedCacheMaintenance() {
  state.protectedCacheMaintenanceLoading = true;
  state.protectedCacheMaintenanceError = "";
  state.protectedCacheAuditLoading = false;
  state.protectedCacheAuditError = "";
  state.protectedCacheCleanupPlanLoading = false;
  state.protectedCacheCleanupPlanError = "";
  state.protectedCacheCleanupExecute = null;
  state.protectedCacheCleanupExecuteLoading = false;
  state.protectedCacheCleanupExecuteError = "";
  if (state.cacheSettingsPayload) {
    renderCacheSettingsStatus(state.cacheSettingsPayload);
  }

  const started = await startJob("/api/jobs/thumbnail-cache/protected-orphan-check", "");
  if (!started) {
    state.protectedCacheMaintenanceLoading = false;
    if (state.cacheSettingsPayload) {
      renderCacheSettingsStatus(state.cacheSettingsPayload);
    }
  }
}

function renderProtectedCacheCleanupPlanPanel() {
  const section = document.createElement("section");
  section.className = "cache-cleanup-plan protected-cache-cleanup-plan";

  const heading = document.createElement("h4");
  heading.textContent = text("settings.protectedPlanTitle");
  section.appendChild(heading);

  const note = document.createElement("p");
  note.className = "cache-status-note";
  note.textContent = text("settings.protectedPlanNote");
  section.appendChild(note);

  const actions = document.createElement("div");
  actions.className = "cache-cleanup-actions";

  const button = document.createElement("button");
  button.type = "button";
  button.textContent = text("settings.protectedPlanRun");
  button.disabled = Boolean(state.protectedCacheCleanupPlanLoading || state.protectedCacheCleanupExecuteLoading);
  button.addEventListener("click", loadProtectedCacheCleanupPlan);
  actions.appendChild(button);
  section.appendChild(actions);

  if (state.protectedCacheCleanupPlanLoading) {
    const loading = document.createElement("p");
    loading.className = "cache-status-note";
    loading.textContent = text("settings.protectedPlanRunning");
    section.appendChild(loading);
    return section;
  }

  if (state.protectedCacheCleanupPlanError) {
    const error = document.createElement("p");
    error.className = "cache-status-note warning";
    error.textContent = `${text("settings.protectedPlanError")} ${state.protectedCacheCleanupPlanError}`.trim();
    section.appendChild(error);
    return section;
  }

  const plan = state.protectedCacheCleanupPlan;
  if (!plan) {
    const empty = document.createElement("p");
    empty.className = "cache-status-note";
    empty.textContent = text("settings.protectedPlanNotRun");
    section.appendChild(empty);
    return section;
  }

  const summary = plan.summary || {};
  const safeFiles = plan.safe_to_delete_files || {};
  const safeDbRows = plan.safe_to_delete_db_rows || {};
  const review = plan.review_required || {};
  const blocked = plan.blocked || {};
  const samples = plan.samples || {};

  const status = document.createElement("p");
  status.className = plan.plan_needed ? "cache-status-note warning" : "cache-status-note";
  status.textContent = plan.plan_needed ? text("settings.protectedPlanNeeded") : text("settings.protectedPlanNotNeeded");
  section.appendChild(status);

  const grid = document.createElement("div");
  grid.className = "cache-status-grid";

  grid.appendChild(cacheStatusCard(
    text("settings.protectedPlanSafeFiles"),
    cacheStatusEntriesText(numericStatusValue(safeFiles.entries)),
    [
      text("settings.protectedPlanFilesWithoutDb", { count: numericStatusValue(safeFiles.files_without_db) }),
      text("settings.protectedPlanTemporaryFiles", { count: numericStatusValue(safeFiles.temporary_files) }),
      text("settings.protectedPlanEstimatedReclaimable", { size: formatBytes(numericStatusValue(safeFiles.estimated_reclaimable_bytes)) }),
    ]
  ));

  grid.appendChild(cacheStatusCard(
    text("settings.protectedPlanSafeDbRows"),
    cacheStatusEntriesText(numericStatusValue(safeDbRows.entries)),
    [
      text("settings.protectedPlanDbMissingFiles", { count: numericStatusValue(safeDbRows.rows_missing_file) }),
      text("settings.protectedPlanDbInvalidPaths", { count: numericStatusValue(safeDbRows.rows_invalid_path) }),
    ]
  ));

  grid.appendChild(cacheStatusCard(
    text("settings.protectedPlanReviewRequired"),
    cacheStatusEntriesText(numericStatusValue(review.entries)),
    [
      text("settings.protectedPlanReviewNote"),
      text("settings.protectedPlanEstimatedReclaimable", { size: formatBytes(numericStatusValue(review.unexpected_files_bytes)) }),
    ]
  ));

  grid.appendChild(cacheStatusCard(
    text("settings.protectedPlanBlocked"),
    cacheStatusEntriesText(numericStatusValue(blocked.entries)),
    []
  ));

  section.appendChild(grid);

  appendProtectedAuditSample(
    section,
    text("settings.protectedPlanSampleDeleteFiles"),
    protectedPlanSamples(samples.delete_files_without_db, samples.delete_temporary_files)
  );
  appendProtectedAuditSample(
    section,
    text("settings.protectedPlanSampleDeleteDbRows"),
    protectedPlanSamples(samples.delete_db_rows_missing_file, samples.delete_db_rows_invalid_path)
  );
  appendProtectedAuditSample(
    section,
    text("settings.protectedPlanSampleReview"),
    protectedPlanSamples(samples.review_media_unavailable, samples.review_status_not_ready, samples.review_unexpected_files)
  );

  if (state.protectedCacheCleanupExecuteLoading) {
    const loading = document.createElement("p");
    loading.className = "cache-status-note";
    loading.textContent = text("settings.protectedExecuteRunning");
    section.appendChild(loading);
  } else if (state.protectedCacheCleanupExecuteError) {
    const error = document.createElement("p");
    error.className = "cache-status-note warning";
    error.textContent = `${text("settings.protectedExecuteError")} ${state.protectedCacheCleanupExecuteError}`.trim();
    section.appendChild(error);
  } else if (state.protectedCacheCleanupExecute) {
    section.appendChild(renderProtectedCacheCleanupExecuteResult(state.protectedCacheCleanupExecute));
  }

  const safeEntries = numericStatusValue(summary.safe_to_delete_entries);
  if (safeEntries > 0) {
    const executeActions = document.createElement("div");
    executeActions.className = "cache-cleanup-actions";
    const executeButton = document.createElement("button");
    executeButton.type = "button";
    executeButton.textContent = text("settings.protectedExecuteRun");
    executeButton.disabled = Boolean(state.protectedCacheCleanupExecuteLoading || isLongJobLocked() || catalogUpdateWorkflowIsActive());
    executeButton.addEventListener("click", () => executeProtectedCacheCleanup(plan));
    executeActions.appendChild(executeButton);
    section.appendChild(executeActions);
  }

  const readOnly = document.createElement("p");
  readOnly.className = "cache-status-note";
  readOnly.textContent = text("settings.protectedPlanReadOnly");
  section.appendChild(readOnly);

  return section;
}

async function loadProtectedCacheCleanupPlan() {
  state.protectedCacheCleanupPlanLoading = true;
  state.protectedCacheCleanupPlanError = "";
  state.protectedCacheCleanupExecute = null;
  state.protectedCacheCleanupExecuteLoading = false;
  state.protectedCacheCleanupExecuteError = "";
  if (state.cacheSettingsPayload) {
    renderCacheSettingsStatus(state.cacheSettingsPayload);
  }

  try {
    const payload = await fetchJson("/api/thumbnail-cache/protected-orphan-cleanup-plan");
    state.protectedCacheCleanupPlan = payload?.protected_orphan_cleanup_plan || payload;
  } catch (error) {
    state.protectedCacheCleanupPlanError = error.message || String(error);
  } finally {
    state.protectedCacheCleanupPlanLoading = false;
    if (state.cacheSettingsPayload) {
      renderCacheSettingsStatus(state.cacheSettingsPayload);
    }
  }
}


async function executeProtectedCacheCleanup(plan) {
  const safeEntries = numericStatusValue(plan?.summary?.safe_to_delete_entries);
  if (safeEntries <= 0) return;

  if (!window.confirm(text("settings.stableMaintenanceCleanConfirm"))) {
    return;
  }

  state.protectedCacheCleanupExecuteLoading = true;
  state.protectedCacheMaintenanceError = "";
  state.protectedCacheCleanupExecuteError = "";
  state.protectedCacheCleanupExecute = null;
  state.protectedCacheAuditLoading = false;
  state.protectedCacheAuditError = "";
  if (state.cacheSettingsPayload) {
    renderCacheSettingsStatus(state.cacheSettingsPayload);
  }

  const started = await startJob(
    "/api/jobs/thumbnail-cache/protected-orphan-clean",
    "",
    { confirm: "protected-orphan-cleanup" }
  );
  if (!started) {
    state.protectedCacheCleanupExecuteLoading = false;
    if (state.cacheSettingsPayload) {
      renderCacheSettingsStatus(state.cacheSettingsPayload);
    }
  }
}

function renderCacheSettingsStatus(payload, options = {}) {
  if (!els.cacheStatusSummary) return;
  state.cacheSettingsPayload = payload;
  const preserveControlStatuses = options.preserveControlStatuses === true;

  // The element starts with data-text only for the initial loading label. Once
  // it contains dynamic Settings content, static locale application must not
  // replace that content with the loading label before the cached re-render.
  els.cacheStatusSummary.removeAttribute("data-text");

  const cache = payload?.thumbnail_cache || {};
  const database = payload?.database || {};
  const settings = payload?.settings || {};
  const cleanupPlan = payload?.cleanup_plan || null;
  const byClass = database.by_cache_class || {};
  const dynamic = byClass.dynamic || {};
  const protectedCache = byClass.protected || {};

  const limitBytes = numericStatusValue(cache.limit_bytes || database.limit_bytes);
  const dynamicBytes = numericStatusValue(dynamic.size_bytes);
  const protectedBytes = numericStatusValue(protectedCache.size_bytes);
  const totalBytes = numericStatusValue(database.size_bytes);
  const dynamicOverLimit = limitBytes > 0 && dynamicBytes > limitBytes;

  const pageSizes = settings.page_sizes || {};
  const pageSizeSources = settings.page_size_sources || {};
  const galleryDensity = normalizeGalleryDensity(settings.gallery_density || "comfortable");
  const thumbnailSizes = settings.thumbnail_sizes || {};
  const video = settings.video || {};
  const thumbnailVideoSources = settings.thumbnail_video_param_sources || {};
  renderSourceRootStatus(settings.source_root || {});
  const settingsLocale = normalizeLocale(settings.ui_locale || activeLocale);
  const settingsTheme = normalizeThemeId(settings.ui_theme || document.documentElement.dataset.theme || "original");
  const settingsCatalogTitle = normalizeCatalogTitle(settings.catalog_title || DEFAULT_CATALOG_TITLE) || DEFAULT_CATALOG_TITLE;
  if (els.catalogTitleInput && els.catalogTitleInput.value !== settingsCatalogTitle) {
    els.catalogTitleInput.value = settingsCatalogTitle;
  }
  if (els.localeSelect && els.localeSelect.value !== settingsLocale) {
    els.localeSelect.value = settingsLocale;
  }
  if (els.themeSelect && els.themeSelect.value !== settingsTheme) {
    els.themeSelect.value = settingsTheme;
  }
  applyGalleryDensity(galleryDensity);
  if (!preserveControlStatuses) {
    if (els.localeStatus && !els.localeStatus.classList.contains("error")) {
      els.localeStatus.textContent = "";
    }
    if (els.themeStatus && !els.themeStatus.classList.contains("error")) {
      els.themeStatus.textContent = "";
    }
    if (els.catalogTitleStatus && !els.catalogTitleStatus.classList.contains("error")) {
      els.catalogTitleStatus.textContent = "";
    }
  }
  const imageSize = Array.isArray(thumbnailSizes.image_thumb_size) ? thumbnailSizes.image_thumb_size.join("×") : "";
  const gifSize = Array.isArray(thumbnailSizes.gif_thumb_size) ? thumbnailSizes.gif_thumb_size.join("×") : "";

  const container = document.createElement("div");
  container.className = "cache-status-panel";

  container.appendChild(renderCacheStatusOverview({
    dynamicBytes,
    limitBytes,
    protectedBytes,
    totalBytes,
    dynamicEntries: dynamic.entries,
    protectedEntries: protectedCache.entries,
    totalEntries: database.entries,
  }));

  if (els.cacheLimitInput) {
    const currentLimit = Number(settings.thumbnail_cache_limit_gb || cache.limit_gb || 0);
    if (Number.isFinite(currentLimit) && currentLimit > 0) {
      els.cacheLimitInput.value = String(currentLimit);
    }
  }

  const pageSizeInputs = [
    [els.photoPageSizeInput, pageSizes.photo_page_size],
    [els.videoPageSizeInput, pageSizes.video_page_size],
    [els.gifPageSizeInput, pageSizes.gif_page_size],
    [els.otherPageSizeInput, pageSizes.other_page_size],
    [els.folderPageSizeInput, pageSizes.folder_page_size],
  ];
  for (const [input, value] of pageSizeInputs) {
    const numberValue = Number(value || 0);
    if (input && Number.isInteger(numberValue) && numberValue > 0) {
      input.value = String(numberValue);
    }
  }

  const imageThumbSize = Array.isArray(thumbnailSizes.image_thumb_size) ? thumbnailSizes.image_thumb_size : [];
  const gifThumbSize = Array.isArray(thumbnailSizes.gif_thumb_size) ? thumbnailSizes.gif_thumb_size : [];
  const thumbnailParamInputs = [
    [els.imageThumbWidthInput, imageThumbSize[0]],
    [els.imageThumbHeightInput, imageThumbSize[1]],
    [els.gifThumbWidthInput, gifThumbSize[0]],
    [els.gifThumbHeightInput, gifThumbSize[1]],
    [els.videoPreviewWidthInput, thumbnailSizes.video_preview_width],
    [els.ffmpegTimeoutInput, video.ffmpeg_timeout_seconds],
    [els.ffmpegThreadsInput, video.ffmpeg_threads_per_job],
  ];
  for (const [input, value] of thumbnailParamInputs) {
    const numberValue = Number(value || 0);
    if (input && Number.isInteger(numberValue) && numberValue > 0) {
      input.value = String(numberValue);
    }
  }

  if (dynamicOverLimit) {
    const overLimitNotice = document.createElement("p");
    overLimitNotice.className = "cache-status-note warning";
    overLimitNotice.textContent = text("settings.cacheStatusDynamicAboveLimit");
    container.appendChild(overLimitNotice);
  }

  if (cleanupPlan) {
    container.appendChild(renderCacheCleanupPlan(cleanupPlan));
  }

  container.appendChild(renderProtectedCacheMaintenancePanel());


  els.cacheStatusSummary.classList.remove("error");
  els.cacheStatusSummary.replaceChildren(container);
}

async function loadCacheSettingsStatus(options = {}) {
  if (!els.cacheStatusSummary) return;

  const showLoading = options.showLoading === true || !state.cacheSettingsPayload;
  resetProtectedCacheMaintenanceState();
  els.cacheStatusSummary.classList.remove("error");
  if (showLoading) {
    els.cacheStatusSummary.textContent = text("settings.cacheStatusLoading");
  } else {
    renderCacheSettingsStatus(state.cacheSettingsPayload);
  }

  try {
    const [payload, jobPayload] = await Promise.all([
      fetchJson("/api/thumbnail-cache/status"),
      fetchJson("/api/jobs/status").catch(() => null),
    ]);
    syncProtectedCacheMaintenanceJob(jobPayload?.current_job || null);
    if (jobPayload) {
      ensureJobPollingForStatus(jobPayload);
    }
    renderCacheSettingsStatus(payload);
  } catch (error) {
    if (state.cacheSettingsPayload) {
      renderCacheSettingsStatus(state.cacheSettingsPayload);
    }
    els.cacheStatusSummary.classList.add("error");
    els.cacheStatusSummary.textContent = `${text("settings.cacheStatusLoadError")} ${error.message || ""}`.trim();
  }
}

async function saveCacheLimitSetting() {
  if (!els.cacheLimitInput || !els.cacheLimitSave) return;

  const valueText = String(els.cacheLimitInput.value || "").trim().replace(",", ".");
  const valueNumber = Number(valueText);

  if (!Number.isFinite(valueNumber) || valueNumber <= 0) {
    if (els.cacheLimitMessage) {
      els.cacheLimitMessage.textContent = text("settings.cacheLimitInvalid");
      els.cacheLimitMessage.classList.add("error");
    }
    return;
  }

  if (els.cacheLimitMessage) {
    els.cacheLimitMessage.textContent = text("settings.cacheLimitSaving");
    els.cacheLimitMessage.classList.remove("error");
  }

  els.cacheLimitSave.disabled = true;

  try {
    const payload = await postJson("/api/settings/runtime/cache-limit", {
      thumbnail_cache_limit_gb: valueText
    });
    renderCacheSettingsStatus(payload);
    if (els.cacheLimitMessage) {
      els.cacheLimitMessage.textContent = text("settings.cacheLimitSaved");
      els.cacheLimitMessage.classList.remove("error");
    }
  } catch (error) {
    if (els.cacheLimitMessage) {
      els.cacheLimitMessage.textContent = `${text("settings.cacheLimitSaveError")} ${error.message || ""}`.trim();
      els.cacheLimitMessage.classList.add("error");
    }
  } finally {
    els.cacheLimitSave.disabled = false;
  }
}

async function savePageSizeSettings(options = {}) {
  const saveButton = options.saveButton === undefined ? els.pageSizeSave : options.saveButton;
  const quietStatus = options.quietStatus === true;

  const values = {
    photo_page_size: positiveIntegerInputValue(els.photoPageSizeInput),
    video_page_size: positiveIntegerInputValue(els.videoPageSizeInput),
    gif_page_size: positiveIntegerInputValue(els.gifPageSizeInput),
    other_page_size: positiveIntegerInputValue(els.otherPageSizeInput),
    folder_page_size: positiveIntegerInputValue(els.folderPageSizeInput),
    gallery_density: normalizeGalleryDensity(els.galleryDensitySelect ? els.galleryDensitySelect.value : "comfortable"),
  };

  if ([values.photo_page_size, values.video_page_size, values.gif_page_size, values.other_page_size, values.folder_page_size].some(value => value === null)) {
    if (els.pageSizeMessage) {
      els.pageSizeMessage.textContent = text("settings.pageSizeInvalid");
      els.pageSizeMessage.classList.add("error");
    }
    return false;
  }

  if (els.pageSizeMessage) {
    els.pageSizeMessage.textContent = quietStatus ? "" : text("settings.pageSizeSaving");
    els.pageSizeMessage.classList.remove("error");
  }

  if (saveButton) saveButton.disabled = true;

  try {
    const payload = await postJson("/api/settings/runtime/page-sizes", values);
    renderCacheSettingsStatus(payload);
    if (els.pageSizeMessage) {
      els.pageSizeMessage.textContent = quietStatus ? "" : text("settings.pageSizeSaved");
      els.pageSizeMessage.classList.remove("error");
    }
    return true;
  } catch (error) {
    if (els.pageSizeMessage) {
      els.pageSizeMessage.textContent = `${text("settings.pageSizeSaveError")} ${error.message || ""}`.trim();
      els.pageSizeMessage.classList.add("error");
    }
    return false;
  } finally {
    if (saveButton) saveButton.disabled = false;
  }
}

async function saveGeneralSettings() {
  if (!els.pageSizeSave) return;

  setCatalogTitleStatus("");
  if (els.pageSizeMessage) {
    els.pageSizeMessage.textContent = "";
    els.pageSizeMessage.classList.remove("error");
  }

  els.pageSizeSave.disabled = true;
  try {
    const titleSaved = await saveCatalogTitleSetting({ saveButton: null, refreshStatus: false, quietStatus: true });
    if (!titleSaved) return;
    await savePageSizeSettings({ saveButton: null, quietStatus: true });
  } finally {
    els.pageSizeSave.disabled = false;
  }
}

async function saveThumbnailVideoParams() {
  if (!els.thumbnailParamsSave) return;

  const values = {
    image_thumb_width: positiveIntegerInputValue(els.imageThumbWidthInput),
    image_thumb_height: positiveIntegerInputValue(els.imageThumbHeightInput),
    gif_thumb_width: positiveIntegerInputValue(els.gifThumbWidthInput),
    gif_thumb_height: positiveIntegerInputValue(els.gifThumbHeightInput),
    video_preview_width: positiveIntegerInputValue(els.videoPreviewWidthInput),
    ffmpeg_timeout_seconds: positiveIntegerInputValue(els.ffmpegTimeoutInput),
    ffmpeg_threads_per_job: positiveIntegerInputValue(els.ffmpegThreadsInput),
  };

  if ([values.photo_page_size, values.video_page_size, values.gif_page_size, values.other_page_size, values.folder_page_size].some(value => value === null)) {
    if (els.thumbnailParamsMessage) {
      els.thumbnailParamsMessage.textContent = text("settings.thumbnailParamsInvalid");
      els.thumbnailParamsMessage.classList.add("error");
    }
    return;
  }

  if (els.thumbnailParamsMessage) {
    els.thumbnailParamsMessage.textContent = text("settings.thumbnailParamsSaving");
    els.thumbnailParamsMessage.classList.remove("error");
  }

  els.thumbnailParamsSave.disabled = true;

  try {
    const payload = await postJson("/api/settings/runtime/thumbnail-video-params", values);
    renderCacheSettingsStatus(payload);
    if (els.thumbnailParamsMessage) {
      els.thumbnailParamsMessage.textContent = text("settings.thumbnailParamsSaved");
      els.thumbnailParamsMessage.classList.remove("error");
    }
  } catch (error) {
    if (els.thumbnailParamsMessage) {
      els.thumbnailParamsMessage.textContent = `${text("settings.thumbnailParamsSaveError")} ${error.message || ""}`.trim();
      els.thumbnailParamsMessage.classList.add("error");
    }
  } finally {
    els.thumbnailParamsSave.disabled = false;
  }
}


async function resetThumbnailVideoParams() {
  if (!els.thumbnailParamsReset) return;

  if (!window.confirm(text("settings.thumbnailParamsResetConfirm"))) {
    return;
  }

  if (els.thumbnailParamsMessage) {
    els.thumbnailParamsMessage.textContent = text("settings.thumbnailParamsResetting");
    els.thumbnailParamsMessage.classList.remove("error");
  }

  els.thumbnailParamsReset.disabled = true;
  if (els.thumbnailParamsSave) els.thumbnailParamsSave.disabled = true;

  try {
    const payload = await postJson("/api/settings/runtime/thumbnail-video-params/reset", {});
    renderCacheSettingsStatus(payload);
    if (els.thumbnailParamsMessage) {
      els.thumbnailParamsMessage.textContent = text("settings.thumbnailParamsResetDone");
      els.thumbnailParamsMessage.classList.remove("error");
    }
  } catch (error) {
    if (els.thumbnailParamsMessage) {
      els.thumbnailParamsMessage.textContent = `${text("settings.thumbnailParamsResetError")} ${error.message || ""}`.trim();
      els.thumbnailParamsMessage.classList.add("error");
    }
  } finally {
    els.thumbnailParamsReset.disabled = false;
    if (els.thumbnailParamsSave) els.thumbnailParamsSave.disabled = false;
  }
}

function setMessage(messageText, isError = false) {
  els.message.textContent = messageText || "";
  els.message.classList.toggle("error", isError);
}

function clientPathKey(path) {
  return String(path || "").normalize("NFKC").toLowerCase();
}

function sameCatalogPath(left, right) {
  return clientPathKey(left) === clientPathKey(right);
}

function catalogPathIsInBranch(path, branch) {
  const pathKey = clientPathKey(path);
  const branchKey = clientPathKey(branch);
  if (!branchKey) return true;
  return pathKey === branchKey || pathKey.startsWith(`${branchKey}/`);
}

function parentCatalogPath(path) {
  const normalized = String(path || "").replaceAll("\\", "/").replace(/^\/+|\/+$/g, "");
  if (!normalized || !normalized.includes("/")) return "";
  return normalized.slice(0, normalized.lastIndexOf("/"));
}

function folderFilesystemStatus(folder) {
  return folder && folder.filesystem ? folder.filesystem : null;
}

function folderFilesystemIsUsable(folder) {
  const status = folderFilesystemStatus(folder);
  return !status || status.is_usable !== false;
}

function folderSourceRootIsUnavailable(folder) {
  const status = folderFilesystemStatus(folder);
  return Boolean(status && status.is_usable === false && status.reason === "source_root_unavailable");
}

function folderFilesystemProblemText(folder) {
  const status = folderFilesystemStatus(folder);
  if (!status || status.is_usable !== false) return "";

  if (status.reason === "source_root_unavailable") return text("folderStatus.sourceRootUnavailable");
  if (status.reason === "missing") return text("folderStatus.missing");
  if (status.reason === "not_directory") return text("folderStatus.notDirectory");
  if (status.reason === "link_or_junction") return text("folderStatus.linkOrJunction");
  return status.message || text("folderStatus.error");
}

function folderFilesystemProblemMarkup(folder) {
  const message = folderFilesystemProblemText(folder);
  if (!message) return "";
  return `<div class="folder-runtime-status">${escapeHtml(message)}</div>`;
}

function showFolderFilesystemProblem(folder, fallbackKey = "jobs.folderMissingUpdateBlocked") {
  const message = folderFilesystemProblemText(folder) || text(fallbackKey);
  setMessage(message, true);
}

function currentFolderFilesystemIsUsable() {
  return folderFilesystemIsUsable(state.currentFolder);
}

const mediaModalState = {
  imageMode: "fit",
  imageScale: 1,
  imageItems: [],
  imageIndex: -1,
  imagePaging: null,
  videoItem: null,
};

function modalStageAvailableSize(stage) {
  const style = window.getComputedStyle(stage);
  const horizontalPadding = parseFloat(style.paddingLeft || "0") + parseFloat(style.paddingRight || "0");
  const verticalPadding = parseFloat(style.paddingTop || "0") + parseFloat(style.paddingBottom || "0");
  return {
    width: Math.max(1, stage.clientWidth - horizontalPadding),
    height: Math.max(1, stage.clientHeight - verticalPadding),
  };
}

function modalFitScale(naturalWidth, naturalHeight, width, height) {
  if (!naturalWidth || !naturalHeight) return 1;
  return Math.min(width / naturalWidth, height / naturalHeight, 1);
}

function clampModalScale(scale) {
  return Math.max(0.1, Math.min(5, scale));
}

function currentModalImageScale() {
  const modal = document.getElementById("mediaModal");
  const stage = modal ? modal.querySelector("#mediaModalStage") : null;
  const image = stage ? stage.querySelector(".media-modal-image") : null;
  if (!stage || !image) return 1;

  const { width, height } = modalStageAvailableSize(stage);
  const fitScale = modalFitScale(image.naturalWidth || 0, image.naturalHeight || 0, width, height);
  return mediaModalState.imageMode === "fit" ? fitScale : mediaModalState.imageScale;
}

function applyModalImageMode() {
  const modal = document.getElementById("mediaModal");
  if (!modal || !modal.classList.contains("show")) return;

  const stage = modal.querySelector("#mediaModalStage");
  const image = stage ? stage.querySelector(".media-modal-image") : null;
  if (!stage || !image) return;

  const { width, height } = modalStageAvailableSize(stage);
  const naturalWidth = image.naturalWidth || 0;
  const naturalHeight = image.naturalHeight || 0;

  image.style.maxWidth = "none";
  image.style.maxHeight = "none";
  image.style.objectFit = "contain";

  if (!naturalWidth || !naturalHeight) {
    image.style.width = "auto";
    image.style.height = "auto";
    image.style.maxWidth = `${width}px`;
    image.style.maxHeight = `${height}px`;
    return;
  }

  const fitScale = modalFitScale(naturalWidth, naturalHeight, width, height);
  const scale = mediaModalState.imageMode === "fit"
    ? fitScale
    : clampModalScale(mediaModalState.imageScale);

  const displayWidth = Math.max(1, Math.round(naturalWidth * scale));
  const displayHeight = Math.max(1, Math.round(naturalHeight * scale));

  image.style.width = `${displayWidth}px`;
  image.style.height = `${displayHeight}px`;
}

function fitModalVideo(video, width, height) {
  video.style.width = `${width}px`;
  video.style.height = `${height}px`;
  video.style.maxWidth = `${width}px`;
  video.style.maxHeight = `${height}px`;
  video.style.objectFit = "contain";
}

function fitMediaModalContent() {
  const modal = document.getElementById("mediaModal");
  if (!modal || !modal.classList.contains("show")) return;

  const stage = modal.querySelector("#mediaModalStage");
  if (!stage) return;

  const image = stage.querySelector(".media-modal-image");
  if (image) {
    applyModalImageMode();
    return;
  }

  const video = stage.querySelector(".media-modal-video");
  if (video) {
    const { width, height } = modalStageAvailableSize(stage);
    fitModalVideo(video, width, height);
  }
}

function setModalImageFit() {
  mediaModalState.imageMode = "fit";
  mediaModalState.imageScale = 1;
  fitMediaModalContent();
}

function setModalImageOriginalSize() {
  mediaModalState.imageMode = "manual";
  mediaModalState.imageScale = 1;
  fitMediaModalContent();
}

function zoomModalImage(multiplier) {
  const currentScale = currentModalImageScale();
  mediaModalState.imageMode = "manual";
  mediaModalState.imageScale = clampModalScale(currentScale * multiplier);
  fitMediaModalContent();
}

function modalFavoriteEligible(item) {
  return Boolean(item
    && item.relPath
    && (item.mediaType === "image" || item.mediaType === "gif" || item.mediaType === "video"));
}

function modalOriginalEligible(item) {
  return Boolean(item
    && item.relPath
    && (item.mediaType === "image" || item.mediaType === "gif" || item.mediaType === "video" || item.mediaType === "video_frame"));
}

function modalOriginalButtonText(item) {
  return item && item.mediaType === "video_frame"
    ? text("actions.openVideo")
    : text("actions.openOriginal");
}

function updateModalControls(modal, mode) {
  const controls = modal.querySelector("#mediaModalControls");
  if (!controls) return;

  const isImageMode = mode === "image";
  const isVideoMode = mode === "video";
  const currentItem = isVideoMode
    ? mediaModalState.videoItem
    : (mediaModalState.imageItems[mediaModalState.imageIndex] || null);

  const paging = mediaModalState.imagePaging;
  const canNavigate = isImageMode && (mediaModalState.imageItems.length > 1 || Boolean(paging && paging.total > 1));

  const prev = modal.querySelector("#mediaModalPrev");
  const next = modal.querySelector("#mediaModalNext");
  for (const button of [prev, next]) {
    if (!button) continue;
    button.hidden = !isImageMode;
    button.disabled = !canNavigate;
  }

  const favorite = modal.querySelector("#mediaModalFavorite");
  const canToggleFavorite = (isImageMode || isVideoMode) && modalFavoriteEligible(currentItem);
  if (favorite) {
    favorite.hidden = !canToggleFavorite;
    favorite.disabled = !canToggleFavorite;
    favorite.textContent = currentItem && currentItem.isFavorite
      ? text("actions.removeFavorite")
      : text("actions.addFavorite");
    favorite.classList.toggle("is-favorite", Boolean(currentItem && currentItem.isFavorite));
  }

  const openOriginal = modal.querySelector("#mediaModalOpenOriginal");
  const canOpenOriginal = (isImageMode || isVideoMode) && modalOriginalEligible(currentItem);
  if (openOriginal) {
    openOriginal.hidden = !canOpenOriginal;
    openOriginal.disabled = !canOpenOriginal;
    openOriginal.textContent = modalOriginalButtonText(currentItem);
  }

  for (const selector of ["#mediaModalZoomOut", "#mediaModalZoomIn", "#mediaModalFit", "#mediaModalOriginalSize"]) {
    const button = modal.querySelector(selector);
    if (!button) continue;
    button.hidden = !isImageMode;
    button.disabled = !isImageMode;
  }

  controls.hidden = !(isImageMode || (isVideoMode && (canOpenOriginal || canToggleFavorite)));
}
function showMediaModal(modal) {
  modal.classList.add("show");
  modal.setAttribute("aria-hidden", "false");
  document.body.classList.add("modal-open");
  window.requestAnimationFrame(fitMediaModalContent);
}

function modalStageClickNavigationAvailable(modal, stage, event) {
  if (!modal.classList.contains("image-mode")) return false;
  if (mediaModalState.imageMode !== "fit") return false;
  if (!window.matchMedia("(hover: hover) and (pointer: fine)").matches) return false;
  if (mediaModalState.imageItems.length <= 1 && !mediaModalState.imagePaging) return false;
  if (event.button !== 0) return false;
  if (event.target.closest("button, a, input, select, textarea, video, [role='button']")) return false;

  const rect = stage.getBoundingClientRect();
  const x = event.clientX - rect.left;
  const y = event.clientY - rect.top;

  // Ignore clicks in the scrollbar area when the stage is scrollable.
  if (x < 0 || y < 0 || x >= stage.clientWidth || y >= stage.clientHeight) return false;

  return true;
}

function handleMediaModalStageClick(event) {
  const modal = document.getElementById("mediaModal");
  const stage = event.currentTarget;
  if (!modal || !stage || !modalStageClickNavigationAvailable(modal, stage, event)) return;

  const rect = stage.getBoundingClientRect();
  const relativeX = (event.clientX - rect.left) / Math.max(1, rect.width);

  if (relativeX <= 0.35) {
    showPreviousModalImage();
  } else if (relativeX >= 0.65) {
    showNextModalImage();
  }
}

function ensureMediaModal() {
  let modal = document.getElementById("mediaModal");
  if (modal) return modal;

  modal = document.createElement("div");
  modal.id = "mediaModal";
  modal.className = "media-modal";
  modal.setAttribute("aria-hidden", "true");
  modal.innerHTML = `
    <div class="media-modal-backdrop" data-modal-close="1"></div>
    <section class="media-modal-panel" role="dialog" aria-modal="true" aria-labelledby="mediaModalTitle">
      <header class="media-modal-bar">
        <div id="mediaModalTitle" class="media-modal-title"></div>
        <div id="mediaModalControls" class="media-modal-controls" hidden>
          <button id="mediaModalPrev" type="button">${escapeHtml(text("actions.previous"))}</button>
          <button id="mediaModalNext" type="button">${escapeHtml(text("actions.next"))}</button>
          <button id="mediaModalFavorite" type="button" class="favorite-action" hidden>${escapeHtml(text("actions.addFavorite"))}</button>
          <button id="mediaModalOpenOriginal" type="button" hidden>${escapeHtml(text("actions.openOriginal"))}</button>
          <button id="mediaModalZoomOut" type="button">${escapeHtml(text("modal.zoomOut"))}</button>
          <button id="mediaModalZoomIn" type="button">${escapeHtml(text("modal.zoomIn"))}</button>
          <button id="mediaModalFit" type="button">${escapeHtml(text("modal.fit"))}</button>
          <button id="mediaModalOriginalSize" type="button">${escapeHtml(text("modal.originalSize"))}</button>
        </div>
        <button id="mediaModalClose" type="button">${escapeHtml(text("actions.close"))}</button>
      </header>
      <div id="mediaModalStage" class="media-modal-stage"></div>
      <footer id="mediaModalFooter" class="media-modal-footer"></footer>
    </section>
  `;
  document.body.appendChild(modal);

  modal.querySelector("#mediaModalClose").addEventListener("click", closeMediaModal);
  modal.querySelector("[data-modal-close]").addEventListener("click", closeMediaModal);
  modal.querySelector("#mediaModalPrev").addEventListener("click", showPreviousModalImage);
  modal.querySelector("#mediaModalNext").addEventListener("click", showNextModalImage);
  modal.querySelector("#mediaModalStage").addEventListener("click", handleMediaModalStageClick);
  modal.querySelector("#mediaModalFavorite").addEventListener("click", toggleCurrentModalFavorite);
  modal.querySelector("#mediaModalOpenOriginal").addEventListener("click", openCurrentModalOriginal);
  modal.querySelector("#mediaModalZoomOut").addEventListener("click", () => zoomModalImage(0.8));
  modal.querySelector("#mediaModalZoomIn").addEventListener("click", () => zoomModalImage(1.25));
  modal.querySelector("#mediaModalFit").addEventListener("click", setModalImageFit);
  modal.querySelector("#mediaModalOriginalSize").addEventListener("click", setModalImageOriginalSize);
  return modal;
}

function imageModalItemFromElement(element) {
  if (!element) return null;

  const src = String(element.dataset.imageModalSrc || "").trim();
  if (!src) return null;

  const mediaType = String(element.dataset.imageModalType || "").trim();
  const relPath = String(element.dataset.imageModalPath || "").trim();

  return {
    title: String(element.dataset.imageModalTitle || text("modal.imageTitle")),
    src,
    footer: String(element.dataset.imageModalFooter || ""),
    relPath,
    mediaType,
    isFavorite: element.dataset.imageModalFavorite === "1",
  };
}

function imageModalGroupOfElement(element) {
  return String(element?.dataset?.imageModalGroup || "").trim();
}

function parsePositiveDatasetInt(value, fallback = 1) {
  const parsed = Number.parseInt(String(value || ""), 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function modalPagingFromElement(element, items) {
  if (!element) return null;

  const pagingKind = String(element.dataset.imageModalPaging || "").trim();
  if (!pagingKind) return null;

  const mediaType = String(element.dataset.imageModalType || "").trim();
  if (mediaType !== "image" && mediaType !== "gif") return null;

  const page = parsePositiveDatasetInt(element.dataset.imageModalPage, 1);
  const pageSize = parsePositiveDatasetInt(element.dataset.imageModalPageSize, Math.max(1, items.length));
  const pages = parsePositiveDatasetInt(element.dataset.imageModalPages, page);
  const total = parsePositiveDatasetInt(element.dataset.imageModalTotal, items.length);
  const anchorPath = String(element.dataset.imageModalAnchorPath || "").trim();
  const anchorRequired = element.dataset.imageModalAnchorRequired === "1";

  if (pagingKind === "folder-media") {
    const folder = String(element.dataset.imageModalFolder || "");
    if (!folder && element.dataset.imageModalFolder === undefined) return null;
    if (pages <= 1 && !anchorRequired) return null;

    return {
      kind: "folder-media",
      folder,
      mediaType,
      currentPage: page,
      pageSize,
      pages,
      total,
      anchorPath,
      anchorRequired,
      cache: pages > 1 && !anchorRequired ? {
        [String(page)]: items,
      } : {},
    };
  }

  if (pagingKind === "search-media") {
    const query = String(element.dataset.imageModalQuery || "").trim();
    const folder = String(element.dataset.imageModalFolder || "");
    if (!query) return null;

    return {
      kind: "search-media",
      query,
      folder,
      mediaType,
      currentPage: page,
      pageSize,
      pages,
      total,
      anchorPath,
      anchorRequired: true,
      cache: {},
    };
  }

  if (pagingKind === "favorites-media") {
    return {
      kind: "favorites-media",
      mediaType,
      currentPage: page,
      pageSize,
      pages,
      total,
      anchorPath,
      anchorRequired: true,
      cache: {},
    };
  }

  return null;
}

function collectImageModalItems(triggerElement) {
  const container = els.mediaList || document;
  const triggerGroup = imageModalGroupOfElement(triggerElement);

  const elements = Array.from(container.querySelectorAll("[data-image-modal-src]"))
    .filter((element) => String(element.dataset.imageModalSrc || "").trim())
    .filter((element) => {
      const elementGroup = imageModalGroupOfElement(element);
      return triggerGroup ? elementGroup === triggerGroup : !elementGroup;
    });

  const items = elements
    .map(imageModalItemFromElement)
    .filter(Boolean);

  const index = Math.max(0, elements.indexOf(triggerElement));
  const paging = modalPagingFromElement(triggerElement, items);

  return { items, index, paging };
}

function imageModalItemFromMedia(media) {
  if (!media || media.is_available === false) return null;
  return {
    title: String(media.file_name || text("modal.imageTitle")),
    src: apiUrl("/media/original", { path: media.rel_path }),
    footer: String(media.rel_path || ""),
    relPath: String(media.rel_path || ""),
    mediaType: String(media.media_type || ""),
    isFavorite: media.is_favorite === true,
  };
}

async function loadModalImagePage(page) {
  const paging = mediaModalState.imagePaging;
  if (!paging || !["folder-media", "search-media", "favorites-media"].includes(paging.kind)) return false;

  const safePage = Math.max(1, Math.min(page, paging.pages));
  const cached = paging.cache[String(safePage)];
  if (cached) {
    mediaModalState.imageItems = cached;
    paging.currentPage = safePage;
    return cached.length > 0;
  }

  let endpoint = "/api/media";
  let params = {
    folder: paging.folder,
    type: paging.mediaType,
    page: safePage,
    page_size: paging.pageSize,
  };

  if (paging.kind === "search-media") {
    endpoint = "/api/search/media";
    params = {
      q: paging.query,
      folder: paging.folder,
      type: paging.mediaType,
      page: safePage,
      page_size: paging.pageSize,
    };
  }

  if (paging.kind === "favorites-media") {
    endpoint = "/api/favorites/media";
    params = {
      type: paging.mediaType,
      page: safePage,
      page_size: paging.pageSize,
    };
  }

  const data = await fetchJson(endpoint, params);

  const items = (data.media || [])
    .map(imageModalItemFromMedia)
    .filter(Boolean);

  paging.cache[String(safePage)] = items;
  paging.currentPage = safePage;
  paging.pages = data.pages || paging.pages;
  paging.total = data.total || paging.total;
  mediaModalState.imageItems = items;
  return items.length > 0;
}

async function loadModalAnchorPage(paging) {
  if (!paging || !["folder-media", "search-media", "favorites-media"].includes(paging.kind) || !paging.anchorPath) return null;

  let endpoint = "/api/media/anchor";
  let params = {
    folder: paging.folder,
    type: paging.mediaType,
    path: paging.anchorPath,
  };

  if (paging.kind === "search-media") {
    endpoint = "/api/search/anchor";
    params = {
      q: paging.query,
      folder: paging.folder,
      type: paging.mediaType,
      path: paging.anchorPath,
    };
  }

  if (paging.kind === "favorites-media") {
    endpoint = "/api/favorites/anchor";
    params = {
      type: paging.mediaType,
      path: paging.anchorPath,
    };
  }

  if (paging.kind === "search-media" || paging.kind === "favorites-media" || (!paging.anchorRequired && paging.pageSize)) {
    params.page_size = paging.pageSize;
  }

  const data = await fetchJson(endpoint, params);
  const items = (data.media || [])
    .map(imageModalItemFromMedia)
    .filter(Boolean);

  const page = parsePositiveDatasetInt(data.page, 1);
  const pageSize = parsePositiveDatasetInt(data.page_size, paging.pageSize || Math.max(1, items.length));
  const pages = parsePositiveDatasetInt(data.pages, page);
  const total = parsePositiveDatasetInt(data.total, items.length);
  const anchorIndex = Math.max(0, Math.min(
    Number.parseInt(String(data.anchor_index ?? "0"), 10) || 0,
    Math.max(0, items.length - 1),
  ));

  paging.currentPage = page;
  paging.pageSize = pageSize;
  paging.pages = pages;
  paging.total = total;
  paging.anchorRequired = false;
  paging.cache[String(page)] = items;

  return { items, index: anchorIndex };
}

function compactModalTitle(title, suffix = "") {
  const baseTitle = String(title || "").trim();
  const safeSuffix = String(suffix || "");
  const maxBaseLength = 120;

  if (baseTitle.length <= maxBaseLength) {
    return baseTitle + safeSuffix;
  }

  return baseTitle.slice(0, maxBaseLength - 1).trimEnd() + "…" + safeSuffix;
}

function setMediaModalTitle(modal, title, suffix = "") {
  const titleElement = modal.querySelector("#mediaModalTitle");
  if (!titleElement) return;

  const baseTitle = String(title || "").trim();
  const fullTitle = baseTitle + String(suffix || "");
  titleElement.textContent = compactModalTitle(baseTitle, suffix);
  titleElement.title = fullTitle;
}

function modalTitleSuffix(items) {
  const paging = mediaModalState.imagePaging;
  if (paging && ["folder-media", "search-media", "favorites-media"].includes(paging.kind) && paging.total > 1) {
    const absoluteIndex = ((paging.currentPage - 1) * paging.pageSize) + mediaModalState.imageIndex + 1;
    return ` (${Math.min(absoluteIndex, paging.total)} / ${paging.total})`;
  }

  return items.length > 1
    ? ` (${mediaModalState.imageIndex + 1} / ${items.length})`
    : "";
}

function renderCurrentModalImage() {
  const modal = ensureMediaModal();
  const stage = modal.querySelector("#mediaModalStage");
  const items = mediaModalState.imageItems;

  if (!items.length || mediaModalState.imageIndex < 0 || mediaModalState.imageIndex >= items.length) {
    return;
  }

  const item = items[mediaModalState.imageIndex];
  const titleSuffix = modalTitleSuffix(items);

  setMediaModalTitle(modal, item.title || text("modal.imageTitle"), titleSuffix);
  modal.querySelector("#mediaModalFooter").textContent = item.footer || "";

  cleanupMediaModalStage(stage);
  stage.scrollLeft = 0;
  stage.scrollTop = 0;
  mediaModalState.imageMode = "fit";
  mediaModalState.imageScale = 1;

  const image = document.createElement("img");
  image.className = "media-modal-image";
  image.src = item.src;
  image.alt = text("modal.imageAlt");
  stage.appendChild(image);

  image.addEventListener("load", fitMediaModalContent, { once: true });
  updateModalControls(modal, "image");
  fitMediaModalContent();
}

function updateModalItemsFavoriteState(path, isFavorite) {
  const key = clientPathKey(path);
  if (!key) return;

  const updateItems = (items) => {
    for (const item of items || []) {
      if (sameCatalogPath(item.relPath, path)) {
        item.isFavorite = isFavorite;
      }
    }
  };

  updateItems(mediaModalState.imageItems);

  if (mediaModalState.videoItem && sameCatalogPath(mediaModalState.videoItem.relPath, path)) {
    mediaModalState.videoItem.isFavorite = isFavorite;
  }

  const paging = mediaModalState.imagePaging;
  if (paging && paging.cache) {
    for (const cachedItems of Object.values(paging.cache)) {
      updateItems(cachedItems);
    }
  }
}

function updateVisibleFavoriteState(path, isFavorite) {
  const key = clientPathKey(path);
  if (!key) return;

  for (const card of document.querySelectorAll("[data-media-path]")) {
    if (!sameCatalogPath(card.dataset.mediaPath, path)) continue;

    card.dataset.mediaFavorite = isFavorite ? "1" : "0";

    const favoriteButton = card.querySelector('[data-action="favorite"]');
    if (favoriteButton) {
      favoriteButton.dataset.isFavorite = isFavorite ? "1" : "0";
      favoriteButton.textContent = isFavorite ? text("actions.removeFavorite") : text("actions.addFavorite");
      favoriteButton.classList.toggle("is-favorite", isFavorite);
    }

    for (const trigger of card.querySelectorAll("[data-image-modal-path]")) {
      trigger.dataset.imageModalFavorite = isFavorite ? "1" : "0";
    }
  }
}

async function openCurrentModalOriginal() {
  const modal = ensureMediaModal();
  const mode = modal.classList.contains("video-mode") ? "video" : "image";
  const item = mode === "video"
    ? mediaModalState.videoItem
    : (mediaModalState.imageItems[mediaModalState.imageIndex] || null);
  if (!modalOriginalEligible(item)) return;

  const button = modal.querySelector("#mediaModalOpenOriginal");
  if (button) button.disabled = true;

  try {
    await openOriginal(item.relPath);
  } finally {
    updateModalControls(modal, mode);
  }
}

async function toggleCurrentModalFavorite() {
  const modal = ensureMediaModal();
  const mode = modal.classList.contains("video-mode") ? "video" : "image";
  const item = mode === "video"
    ? mediaModalState.videoItem
    : (mediaModalState.imageItems[mediaModalState.imageIndex] || null);
  if (!modalFavoriteEligible(item)) return;

  const button = modal.querySelector("#mediaModalFavorite");
  if (button) button.disabled = true;

  try {
    const shouldBeFavorite = !item.isFavorite;
    const result = await setFavoritePath(item.relPath, shouldBeFavorite);
    const resultPath = result.path || item.relPath;

    item.isFavorite = shouldBeFavorite;
    updateModalItemsFavoriteState(resultPath, shouldBeFavorite);
    updateVisibleFavoriteState(resultPath, shouldBeFavorite);
    updateModalControls(modal, mode);

    setMessage(shouldBeFavorite
      ? text("message.favoriteAdded", { path: resultPath })
      : text("message.favoriteRemoved", { path: resultPath }));

    if (state.view === "favorites") {
      mediaModalState.imagePaging && (mediaModalState.imagePaging.cache = {});
      await loadCurrentFolder();
    }
  } catch (error) {
    setMessage(error.message, true);
    updateModalControls(modal, mode);
  }
}

function openImageModal(title, src, footer = "", items = null, index = 0, paging = null) {
  if (!src) return;

  const modal = ensureMediaModal();
  modal.classList.remove("video-mode");
  modal.classList.add("image-mode");
  mediaModalState.videoItem = null;

  const normalizedItems = Array.isArray(items) && items.length
    ? items
    : [{ title: title || text("modal.imageTitle"), src, footer: footer || "" }];

  mediaModalState.imageItems = normalizedItems;
  mediaModalState.imageIndex = Math.max(0, Math.min(index || 0, normalizedItems.length - 1));
  mediaModalState.imagePaging = paging;

  updateModalControls(modal, "image");
  renderCurrentModalImage();
  showMediaModal(modal);
}

async function openImageModalFromElement(element) {
  const item = imageModalItemFromElement(element);
  if (!item) return;

  const { items, index, paging } = collectImageModalItems(element);

  if (paging && paging.anchorRequired) {
    try {
      const anchor = await loadModalAnchorPage(paging);
      if (anchor && anchor.items.length) {
        openImageModal(item.title, item.src, item.footer, anchor.items, anchor.index, paging);
        return;
      }
    } catch (error) {
      setMessage(error.message, true);
    }
  }

  openImageModal(item.title, item.src, item.footer, items.length ? items : [item], index, paging);
}

async function showPreviousModalImage() {
  const paging = mediaModalState.imagePaging;

  if (mediaModalState.imageItems.length <= 1 && !paging) return;

  if (mediaModalState.imageIndex > 0) {
    mediaModalState.imageIndex -= 1;
    renderCurrentModalImage();
    return;
  }

  if (!paging) {
    mediaModalState.imageIndex = mediaModalState.imageItems.length - 1;
    renderCurrentModalImage();
    return;
  }

  try {
    const targetPage = paging.currentPage > 1 ? paging.currentPage - 1 : paging.pages;
    const ok = await loadModalImagePage(targetPage);
    if (!ok) return;
    mediaModalState.imageIndex = mediaModalState.imageItems.length - 1;
    renderCurrentModalImage();
  } catch (error) {
    setMessage(error.message, true);
  }
}

async function showNextModalImage() {
  const paging = mediaModalState.imagePaging;

  if (mediaModalState.imageItems.length <= 1 && !paging) return;

  if (mediaModalState.imageIndex < mediaModalState.imageItems.length - 1) {
    mediaModalState.imageIndex += 1;
    renderCurrentModalImage();
    return;
  }

  if (!paging) {
    mediaModalState.imageIndex = 0;
    renderCurrentModalImage();
    return;
  }

  try {
    const targetPage = paging.currentPage < paging.pages ? paging.currentPage + 1 : 1;
    const ok = await loadModalImagePage(targetPage);
    if (!ok) return;
    mediaModalState.imageIndex = 0;
    renderCurrentModalImage();
  } catch (error) {
    setMessage(error.message, true);
  }
}


const activeVideoHoverPreviewElements = new Set();

function stopAllVideoHoverPreviews() {
  const activePreviews = Array.from(activeVideoHoverPreviewElements);
  activeVideoHoverPreviewElements.clear();

  for (const preview of activePreviews) {
    if (!preview || !preview.isConnected) continue;
    const stop = preview.__catalog2StopVideoHoverPreview;
    if (typeof stop === "function") {
      stop();
    } else {
      preview.classList.remove("video-hover-active");
      const hoverImage = preview.querySelector("img.video-hover-frame-image");
      if (hoverImage) hoverImage.removeAttribute("src");
    }
  }

  document.querySelectorAll(".video-hover-active").forEach((preview) => {
    preview.classList.remove("video-hover-active");
    const hoverImage = preview.querySelector("img.video-hover-frame-image");
    if (hoverImage) hoverImage.removeAttribute("src");
  });
}

function cleanupModalVideoElement(video) {
  if (!video) return;
  try { video.pause(); } catch (_) {}
  try { video.removeAttribute("src"); } catch (_) {}
  try { video.load(); } catch (_) {}
}

function cleanupMediaModalStage(stage) {
  stopAllVideoHoverPreviews();
  if (!stage) return;
  for (const video of stage.querySelectorAll("video")) {
    cleanupModalVideoElement(video);
  }
  stage.replaceChildren();
}

function renderVideoModalError(modal, stage) {
  if (!modal || !stage) return;

  cleanupMediaModalStage(stage);

  const box = document.createElement("div");
  box.className = "media-modal-error";

  const message = document.createElement("p");
  message.className = "media-modal-error-message";
  message.textContent = text("modal.videoPlaybackError");
  box.appendChild(message);

  const actions = document.createElement("div");
  actions.className = "media-modal-error-actions";

  const currentItem = mediaModalState.videoItem;
  if (modalOriginalEligible(currentItem)) {
    const openButton = document.createElement("button");
    openButton.type = "button";
    openButton.textContent = text("modal.openOriginalFallback");
    openButton.addEventListener("click", async () => {
      openButton.disabled = true;
      try {
        await openOriginal(currentItem.relPath);
      } catch (error) {
        setMessage(error.message, true);
      } finally {
        openButton.disabled = false;
      }
    });
    actions.appendChild(openButton);
  }

  const closeButton = document.createElement("button");
  closeButton.type = "button";
  closeButton.textContent = text("actions.close");
  closeButton.addEventListener("click", closeMediaModal);
  actions.appendChild(closeButton);

  box.appendChild(actions);
  stage.appendChild(box);
  updateModalControls(modal, "video");
  fitMediaModalContent();
}

function openVideoModal(title, src, footer = "", relPath = "", isFavorite = false) {
  if (!src) return;

  const modal = ensureMediaModal();
  const stage = modal.querySelector("#mediaModalStage");
  modal.classList.remove("image-mode");
  modal.classList.add("video-mode");
  setMediaModalTitle(modal, title || text("modal.videoTitle"));
  modal.querySelector("#mediaModalFooter").textContent = footer || "";
  cleanupMediaModalStage(stage);
  stage.scrollLeft = 0;
  stage.scrollTop = 0;
  mediaModalState.imageItems = [];
  mediaModalState.imageIndex = -1;
  mediaModalState.imagePaging = null;
  mediaModalState.videoItem = relPath ? {
    title: title || text("modal.videoTitle"),
    relPath,
    mediaType: "video",
    isFavorite: Boolean(isFavorite),
  } : null;
  updateModalControls(modal, "video");

  const video = document.createElement("video");
  video.className = "media-modal-video";
  video.controls = true;
  video.autoplay = true;
  video.muted = true;
  video.playsInline = true;
  video.preload = "metadata";
  video.src = src;
  stage.appendChild(video);

  video.addEventListener("loadedmetadata", fitMediaModalContent, { once: true });
  video.addEventListener("error", () => {
    if (!stage.contains(video)) return;
    renderVideoModalError(modal, stage);
  }, { once: true });
  showMediaModal(modal);
}

function closeMediaModal() {
  const modal = document.getElementById("mediaModal");
  if (!modal) return;

  const stage = modal.querySelector("#mediaModalStage");
  cleanupMediaModalStage(stage);
  modal.classList.remove("show", "image-mode", "video-mode");
  modal.setAttribute("aria-hidden", "true");
  document.body.classList.remove("modal-open");
  updateModalControls(modal, "none");
  mediaModalState.imageMode = "fit";
  mediaModalState.imageScale = 1;
  mediaModalState.imageItems = [];
  mediaModalState.imageIndex = -1;
  mediaModalState.imagePaging = null;
  mediaModalState.videoItem = null;
  window.requestAnimationFrame(() => bindMediaThumbnailLazyLoading(els.mediaList));
}

function updateViewButtons() {
  els.favoritesView.classList.toggle("active", state.view === "favorites");
  updateJobActionButtons();
}

function catalogManagementIsOpen() {
  return Boolean(els.catalogManagementModal && els.catalogManagementModal.classList.contains("show"));
}

function folderRepairModalIsOpen() {
  const modal = document.getElementById("folderRepairModal");
  return Boolean(modal && modal.classList.contains("show"));
}

function openCatalogManagementModal() {
  if (!els.catalogManagementModal) return;
  els.catalogManagementModal.classList.add("show");
  els.catalogManagementModal.setAttribute("aria-hidden", "false");
  document.body.classList.add("catalog-management-open");
  loadCacheSettingsStatus();
}

function closeCatalogManagementModal() {
  if (!els.catalogManagementModal) return false;
  if (catalogUpdateWorkflowCloseIsBlocked()) {
    setMessage(text("catalogUpdate.closeBlocked"), true);
    return false;
  }
  els.catalogManagementModal.classList.remove("show");
  els.catalogManagementModal.setAttribute("aria-hidden", "true");
  document.body.classList.remove("catalog-management-open");
  return true;
}

function ensureFolderRepairModal() {
  let modal = document.getElementById("folderRepairModal");
  if (modal) return modal;

  modal = document.createElement("div");
  modal.id = "folderRepairModal";
  modal.className = "folder-repair-modal";
  modal.setAttribute("aria-hidden", "true");
  modal.innerHTML = `
    <button class="folder-repair-backdrop" type="button" data-folder-repair-close aria-label="${escapeHtml(text("folderRepair.close"))}"></button>
    <section class="folder-repair-panel" role="dialog" aria-modal="true" aria-labelledby="folderRepairModalTitle">
      <header class="folder-repair-bar">
        <div>
          <h2 id="folderRepairModalTitle">${escapeHtml(text("folderRepair.modalTitle"))}</h2>
          <p class="muted">${escapeHtml(text("folderRepair.modalSubtitle"))}</p>
        </div>
        <button id="folderRepairModalClose" type="button" data-folder-repair-close>${escapeHtml(text("folderRepair.close"))}</button>
      </header>
      <div id="folderRepairModalBody" class="folder-repair-body"></div>
    </section>
  `;
  document.body.appendChild(modal);

  for (const closeButton of modal.querySelectorAll("[data-folder-repair-close]")) {
    closeButton.addEventListener("click", closeFolderRepairModal);
  }

  return modal;
}

function setFolderRepairModalHeader(titleText, subtitleText) {
  const modal = ensureFolderRepairModal();
  const title = modal.querySelector("#folderRepairModalTitle");
  const subtitle = modal.querySelector(".folder-repair-bar .muted");
  if (title) title.textContent = titleText || text("folderRepair.modalTitle");
  if (subtitle) subtitle.textContent = subtitleText || text("folderRepair.modalSubtitle");
}

function openFolderRepairModal() {
  const modal = ensureFolderRepairModal();
  modal.classList.add("show");
  modal.setAttribute("aria-hidden", "false");
  document.body.classList.add("folder-repair-modal-open");
}

function setFolderRepairCloseHandler(handler) {
  state.folderRepairCloseHandler = typeof handler === "function" ? handler : null;
}

async function closeFolderRepairModal(options = {}) {
  const modal = document.getElementById("folderRepairModal");
  if (!modal) return;

  const handler = state.folderRepairCloseHandler;
  state.folderRepairCloseHandler = null;

  modal.classList.remove("show");
  modal.setAttribute("aria-hidden", "true");
  document.body.classList.remove("folder-repair-modal-open");

  if (handler && options.runHandler !== false) {
    await handler();
  }
}

function clearCatalogUpdateDecision() {
  if (!els.catalogUpdateDecision) return;
  els.catalogUpdateDecision.replaceChildren();
  els.catalogUpdateDecision.textContent = "";
  els.catalogUpdateDecision.hidden = true;
}

function renderCatalogUpdateDecisionMessage(message, className = "") {
  const target = els.catalogUpdateDecision || els.jobResult;
  if (!target) return;
  target.hidden = false;
  target.replaceChildren();
  const paragraph = document.createElement("p");
  paragraph.className = className ? `job-result-message ${className}` : "job-result-message";
  paragraph.textContent = localizedMessageText(message || "");
  target.appendChild(paragraph);
}

function clearJobResult(options = {}) {
  if (catalogUpdateWorkflowIsActive() && options.force !== true) {
    setMessage(text("catalogUpdate.clearBlocked"), true);
    return false;
  }
  if (els.jobResult) {
    els.jobResult.replaceChildren();
    els.jobResult.textContent = "";
  }
  clearCatalogUpdateDecision();
  return true;
}

function catalogUpdatePhase() {
  return state.catalogUpdateWorkflow?.phase || "idle";
}

function catalogUpdateWorkflowIsActive() {
  return [
    "preview-running",
    "preview-ready",
    "execute-running",
    "cancel-running",
    "no-changes-discard-running",
  ].includes(catalogUpdatePhase());
}

function catalogUpdateWorkflowCloseIsBlocked() {
  return catalogUpdateWorkflowIsActive();
}

function updateCatalogManagementCloseAvailability() {
  const disabled = catalogUpdateWorkflowCloseIsBlocked();
  for (const button of document.querySelectorAll("[data-management-close]")) {
    button.disabled = disabled;
  }
}

function catalogUpdateWorkflowHasPendingDecision() {
  return catalogUpdatePhase() === "preview-ready";
}

function jobScopeText(job) {
  const branch = job && job.branch ? job.branch : "";
  return branch ? text("jobs.scopeBranch", { branch }) : text("jobs.scopeFull");
}

function jobLifecycleState(job, options = {}) {
  if (options.startAccepted) return "started";
  if (!job) return "";
  if (job.is_running || job.state === "running") return "running";
  return String(job.state || "");
}

function jobStatusText(job) {
  const stateName = jobLifecycleState(job);
  const kind = job?.kind || "job";
  const scope = jobScopeText(job);

  if (kind === "update-branch") {
    if (stateName === "running") return text("jobs.statusRunningUpdateBranch");
    if (stateName === "completed") return text("jobs.statusCompletedUpdateBranch");
    if (stateName === "failed") return text("jobs.statusFailedUpdateBranch");
    if (stateName === "cancelled") return text("jobs.statusCancelledUpdateBranch");
    if (stateName === "requires_decision") return text("jobs.statusRequiresDecisionUpdateBranch");
  }

  if (kind === "folder-preview-build-tree" || kind === "prepare-previews") {
    if (stateName === "running") return text("jobs.statusRunningFolderPreview");
    if (stateName === "completed") return text("jobs.statusCompletedFolderPreview");
    if (stateName === "failed") return text("jobs.statusFailedFolderPreview");
    if (stateName === "cancelled") return text("jobs.statusCancelledFolderPreview");
  }

  if (kind === "dynamic-cache-cleanup") {
    if (stateName === "running") return text("jobs.statusRunningDynamicCacheCleanup");
    if (stateName === "completed") return text("jobs.statusCompletedDynamicCacheCleanup");
    if (stateName === "failed") return text("jobs.statusFailedDynamicCacheCleanup");
    if (stateName === "cancelled") return text("jobs.statusCancelledDynamicCacheCleanup");
  }

  if (stateName === "running") return text("jobs.statusRunning", { kind, scope });
  if (stateName === "completed") return text("jobs.statusCompleted", { kind, scope });
  if (stateName === "failed") return text("jobs.statusFailed", { kind, scope });
  if (stateName === "cancelled") return text("jobs.statusCancelled", { kind, scope });
  if (stateName === "requires_decision") return text("jobs.statusRequiresDecision", { scope });
  return text("jobs.statusRaw", { state: stateName || "?" });
}

function jobMessageText(job, options = {}) {
  const stateName = jobLifecycleState(job, options);
  const kind = job?.kind || "job";
  const scope = jobScopeText(job);

  if (kind === "update-branch") {
    if (stateName === "started") return text("jobs.lifecycleStartedUpdateBranch");
    if (stateName === "running") return text("jobs.lifecycleRunningUpdateBranch");
    if (stateName === "completed") return text("jobs.lifecycleCompletedUpdateBranch");
    if (stateName === "failed") return text("jobs.lifecycleFailedUpdateBranch");
    if (stateName === "cancelled") return text("jobs.lifecycleCancelledUpdateBranch");
    if (stateName === "requires_decision") return text("jobs.lifecycleRequiresDecisionUpdateBranch");
  }

  if (kind === "folder-preview-build-tree" || kind === "prepare-previews") {
    if (stateName === "started") return text("jobs.lifecycleStartedFolderPreview");
    if (stateName === "running") return text("jobs.lifecycleRunningFolderPreview");
    if (stateName === "completed") return text("jobs.lifecycleCompletedFolderPreview");
    if (stateName === "failed") return text("jobs.lifecycleFailedFolderPreview");
    if (stateName === "cancelled") return text("jobs.lifecycleCancelledFolderPreview");
  }

  if (kind === "dynamic-cache-cleanup") {
    if (stateName === "started") return text("jobs.lifecycleStartedDynamicCacheCleanup");
    if (stateName === "running") return text("jobs.lifecycleRunningDynamicCacheCleanup");
    if (stateName === "completed") return text("jobs.lifecycleCompletedDynamicCacheCleanup");
    if (stateName === "failed") return text("jobs.lifecycleFailedDynamicCacheCleanup");
    if (stateName === "cancelled") return text("jobs.lifecycleCancelledDynamicCacheCleanup");
  }

  if (kind === "protected-cache-orphan-check") {
    if (stateName === "started") return text("jobs.lifecycleStartedProtectedCacheCheck");
    if (stateName === "running") return text("jobs.lifecycleRunningProtectedCacheCheck");
    if (stateName === "completed") return text("jobs.lifecycleCompletedProtectedCacheCheck");
    if (stateName === "failed") return text("jobs.lifecycleFailedProtectedCacheCheck");
  }

  if (kind === "protected-cache-orphan-clean") {
    if (stateName === "started") return text("jobs.lifecycleStartedProtectedCacheClean");
    if (stateName === "running") return text("jobs.lifecycleRunningProtectedCacheClean");
    if (stateName === "completed") return text("jobs.lifecycleCompletedProtectedCacheClean");
    if (stateName === "failed") return text("jobs.lifecycleFailedProtectedCacheClean");
  }

  if (stateName === "started" || stateName === "running") return text("jobs.started", { kind, scope });
  if (stateName === "completed") return text("jobs.completed", { kind, scope });
  if (stateName === "failed") return text("jobs.failed", { kind, scope });
  if (stateName === "cancelled") return text("jobs.cancelled", { kind, scope });
  return "";
}

function syncJobLifecycleMessage(job, options = {}) {
  const message = jobMessageText(job, options);
  if (!message) return;
  const stateName = jobLifecycleState(job, options);
  setMessage(message, stateName === "failed");
}

function isLongJobLocked() {
  return Boolean(state.jobRunning || state.jobStartPending);
}

function catalogUpdateDecisionIsActive() {
  return catalogUpdateWorkflowHasPendingDecision();
}

function catalogUpdateWorkflowBlocksLongJobs(options = {}) {
  return !options.allowCatalogUpdateDecision && catalogUpdateWorkflowIsActive();
}

function isLongJobBlocked(options = {}) {
  return Boolean(
    state.jobRunning ||
    state.jobStartPending ||
    catalogUpdateWorkflowBlocksLongJobs(options)
  );
}

function showLongJobBusyMessage(options = {}) {
  if (catalogUpdateWorkflowBlocksLongJobs(options)) {
    setMessage(text("catalogUpdate.finishFirst"), true);
    return;
  }
  setMessage(text("jobs.busy"), true);
}

function canStartLongJob(options = {}) {
  if (isLongJobBlocked(options)) {
    showLongJobBusyMessage(options);
    return false;
  }
  return true;
}

function setJobStartPending(isPending) {
  state.jobStartPending = Boolean(isPending);
  updateJobActionButtons(state.jobRunning);
}

function jobIsRunning(job) {
  return Boolean(job && (job.is_running || job.state === "running"));
}

function clearJobPollingTimer() {
  if (state.jobPollTimer) {
    clearTimeout(state.jobPollTimer);
    state.jobPollTimer = null;
  }
}

function setManagementActionAvailability(button, available, isRunning) {
  if (!button) return;
  const card = button.closest(".job-action-card");
  if (card) {
    card.hidden = !available;
  } else {
    button.hidden = !available;
  }
  button.disabled = !available || isRunning;
}

function updateCurrentFolderAction(isRunning = state.jobRunning) {
  const openButton = els.currentFolderOpen;
  const updateButton = els.currentFolderUpdate;
  const more = els.currentFolderMore;
  const generateButton = els.currentFolderGeneratePreviews;
  if (!openButton && !updateButton && !more) return;

  const visible = state.view === "folder" && Boolean(state.folder) && Boolean(state.currentFolder);
  const updateVisible = visible;
  const previewsVisible = visible && folderCanGeneratePreviews(state.currentFolder);
  const menuVisible = updateVisible || previewsVisible;
  const longJobDisabled = Boolean(isRunning) || state.jobStartPending || !visible;

  if (openButton) {
    openButton.hidden = !visible;
    openButton.disabled = !visible || !currentFolderFilesystemIsUsable();
  }

  if (updateButton) {
    updateButton.hidden = !updateVisible;
    updateButton.disabled = longJobDisabled || !updateVisible;
  }

  if (generateButton) {
    generateButton.hidden = !previewsVisible;
    generateButton.disabled = longJobDisabled || !previewsVisible;
  }

  if (more) {
    more.hidden = !menuVisible;
    if (!menuVisible) {
      more.open = false;
    }
  }
}

function catalogHasAvailableMedia() {
  return numericStatusValue(state.catalogStatus?.available_media) > 0;
}

function canPrepareAllPreviews() {
  return catalogHasAvailableMedia();
}

function updateJobActionButtons(isRunning = false) {
  state.jobRunning = Boolean(isRunning);
  const locked = isLongJobLocked() || catalogUpdateWorkflowIsActive();
  updateCatalogManagementCloseAvailability();
  if (els.jobResultClear) {
    els.jobResultClear.disabled = catalogUpdateWorkflowIsActive();
  }
  const canScanCurrent = state.view === "folder" && Boolean(state.folder) && currentFolderFilesystemIsUsable();
  const currentButtons = [
    els.scanPreviewCurrent,
    els.scanStageCurrent,
    els.gifPreviewCurrent,
    els.videoPosterCurrent,
    els.videoFramesCurrent,
    els.folderPreviewPlanCurrent,
  ];
  for (const button of currentButtons) {
    setManagementActionAvailability(button, canScanCurrent, locked);
  }
  if (els.catalogUpdateAll) els.catalogUpdateAll.disabled = locked;
  if (els.catalogPrepareAllPreviews) {
    const canPrepareAll = canPrepareAllPreviews();
    els.catalogPrepareAllPreviews.disabled = locked || !canPrepareAll;
    els.catalogPrepareAllPreviews.classList.toggle("secondary-action", true);
  }
  els.scanPreviewAll.disabled = locked;
  els.scanStageAll.disabled = locked;
  els.gifPreviewAll.disabled = locked;
  els.videoPosterAll.disabled = locked;
  els.videoFramesAll.disabled = locked;

  const canActivate = Boolean(
    state.latestScan &&
    state.latestScan.status === "completed" &&
    state.latestScan.staged &&
    state.latestScan.staged.folders > 0
  );
  els.scanActivate.disabled = locked || !canActivate;
  els.scanActivate.title = canActivate ? "" : text("jobs.noCompletedStage");

  for (const button of document.querySelectorAll("[data-action=\"update-folder\"]")) {
    if (button === els.currentFolderUpdate) continue;
    button.disabled = locked;
  }

  for (const button of document.querySelectorAll("[data-long-job-action]")) {
    const baseDisabled = button.dataset.baseDisabled === "true";
    button.disabled = locked || baseDisabled;
  }

  updateCurrentFolderAction(locked);
}

function renderJobStatus(payload) {
  state.jobStatusPayload = payload;
  state.latestScan = payload.latest_scan || null;
  const job = payload.current_job;
  updateJobActionButtons(jobIsRunning(job));

  if (!job || job.state === "idle") {
    els.jobStatusText.textContent = text("jobs.statusIdle");
    updateJobActionButtons(false);
    if (!catalogUpdateWorkflowIsActive()) {
      clearJobResult({ force: true });
    }
    return;
  }

  const scope = jobScopeText(job);
  const kind = job.kind || "job";
  const catalogWorkflowPhase = state.catalogUpdateWorkflow?.phase || "idle";
  const isCatalogUpdateStageJob = kind === "scan-stage" && catalogWorkflowPhase === "preview-running";
  const isCatalogUpdateActivateJob = kind === "scan-activate" && catalogWorkflowPhase === "execute-running";
  const isCatalogUpdateCancelJob = kind === "scan-activate" && catalogWorkflowPhase === "cancel-running";
  const isCatalogUpdateNoChangesDiscardJob = kind === "scan-activate" && catalogWorkflowPhase === "no-changes-discard-running";
  if (jobIsRunning(job)) {
    if (isCatalogUpdateStageJob) {
      els.jobStatusText.textContent = text("catalogUpdate.statusPreparing");
    } else if (isCatalogUpdateActivateJob) {
      els.jobStatusText.textContent = text("catalogUpdate.statusRunning");
    } else if (isCatalogUpdateCancelJob || isCatalogUpdateNoChangesDiscardJob) {
      els.jobStatusText.textContent = text("catalogUpdate.statusDiscarding");
    } else {
      els.jobStatusText.textContent = jobStatusText(job);
      syncJobLifecycleMessage(job);
    }
    updateJobActionButtons(true);
    if (isCatalogUpdateNoChangesDiscardJob && state.catalogUpdateWorkflow?.preview) {
      renderCatalogUpdatePreview(state.catalogUpdateWorkflow.preview, { cleanupRunning: true });
    } else if (isCatalogUpdateCancelJob && state.catalogUpdateWorkflow?.preview) {
      renderCatalogUpdatePreview(state.catalogUpdateWorkflow.preview);
    } else {
      renderResultMessage(text("jobs.resultMissing"));
    }
    return;
  }

  if (job.state === "completed") {
    if (isCatalogUpdateStageJob) {
      els.jobStatusText.textContent = text("catalogUpdate.statusReady");
    } else if (isCatalogUpdateActivateJob) {
      els.jobStatusText.textContent = text("catalogUpdate.statusCompleted");
    } else {
      els.jobStatusText.textContent = jobStatusText(job);
      syncJobLifecycleMessage(job);
    }
    updateJobActionButtons(false);
    renderJobResult(job.result);
    return;
  }

  if (job.state === "cancelled") {
    if (isCatalogUpdateNoChangesDiscardJob) {
      els.jobStatusText.textContent = text("catalogUpdate.statusNoChanges");
      updateJobActionButtons(false);
      if (state.catalogUpdateWorkflow?.preview) {
        renderCatalogUpdatePreview(state.catalogUpdateWorkflow.preview, { noChangesFinal: true });
      } else {
        renderResultMessage(text("catalogUpdate.noChanges"));
      }
      return;
    }
    if (isCatalogUpdateCancelJob) {
      els.jobStatusText.textContent = text("catalogUpdate.statusCancelled");
      updateJobActionButtons(false);
      renderResultMessage(text("catalogUpdate.cancelled"));
      return;
    }
    els.jobStatusText.textContent = jobStatusText(job);
    syncJobLifecycleMessage(job);
    updateJobActionButtons(false);
    renderJobResult(job.result);
    return;
  }

  if (job.state === "failed") {
    els.jobStatusText.textContent = jobStatusText(job);
    syncJobLifecycleMessage(job);
    updateJobActionButtons(false);
    renderResultMessage(job.error || text("jobs.resultMissing"), "error");
    return;
  }

  if (job.state === "requires_decision") {
    els.jobStatusText.textContent = jobStatusText(job);
    syncJobLifecycleMessage(job);
    updateJobActionButtons(false);
    renderJobResult(job.result);
    return;
  }

  els.jobStatusText.textContent = text("jobs.statusRaw", { state: job.state });
}

function secondsText(value) {
  return `${Number(value || 0).toFixed(3)} s`;
}

function resultValue(value, fallback = "0") {
  if (value === undefined || value === null || value === "") return fallback;
  return localizedMessageText(value);
}

function resultScopeText(result) {
  const branch = result && result.branch ? result.branch : "";
  return branch ? text("jobs.scopeBranch", { branch }) : text("jobs.scopeFull");
}

function renderResultMessage(message, className = "") {
  els.jobResult.replaceChildren();
  const paragraph = document.createElement("p");
  paragraph.className = className ? `job-result-message ${className}` : "job-result-message";
  paragraph.textContent = localizedMessageText(message || "");
  els.jobResult.appendChild(paragraph);
}

function createResultPanel(titleText, targetElement = els.jobResult) {
  targetElement.replaceChildren();
  const container = document.createElement("div");
  container.className = "job-result-panel";
  const title = document.createElement("h4");
  title.textContent = titleText;
  container.appendChild(title);
  targetElement.appendChild(container);
  return container;
}

function revealJobResultPanel(container) {
  if (els.jobResultDetails) {
    setSettingsServiceCardOpen(els.jobResultDetails, true);
  }

  const heading = container?.querySelector("h4");
  if (!heading) return;

  heading.tabIndex = -1;
  window.requestAnimationFrame(() => {
    heading.scrollIntoView({ block: "start" });
    heading.focus({ preventScroll: true });
  });
}

function appendResultSection(container, titleText, rows = []) {
  const section = document.createElement("section");
  section.className = "job-result-section";

  const title = document.createElement("h5");
  title.textContent = titleText;
  section.appendChild(title);

  if (rows.length) {
    const list = document.createElement("dl");
    list.className = "job-result-grid";
    for (const row of rows) {
      const term = document.createElement("dt");
      term.textContent = row.label;
      const value = document.createElement("dd");
      value.textContent = resultValue(row.value);
      list.appendChild(term);
      list.appendChild(value);
    }
    section.appendChild(list);
  }

  container.appendChild(section);
  return section;
}


function scanModeText(value) {
  const mode = String(value || "").trim();
  const key = `backendMessage.scan.update.mode.${mode}`;
  if (mode && hasTranslation(key)) return text(key);
  return text("backendMessage.scan.update.mode.unknown", { mode: mode || "?" });
}

function scanTimingStepText(step) {
  if (!step || typeof step !== "object") return text("result.label.phase");
  const localized = localizedMessageText(step.phase);
  return localized || text("result.label.phase");
}

function affectedScopeLabel(group) {
  const key = String(group?.key || "").trim();
  const translationKey = `result.affectedScope.${key}`;
  if (key && hasTranslation(translationKey)) return text(translationKey);
  return key || text("result.label.group");
}

function appendTimingSection(container, titleText, timing) {
  const steps = Array.isArray(timing?.steps) ? timing.steps : [];
  if (!steps.length) return;

  const rows = steps.map(step => ({
    label: scanTimingStepText(step),
    value: secondsText(step.seconds),
  }));

  rows.push({
    label: text("result.label.totalMeasuredScanActivate"),
    value: secondsText(timing.total_seconds),
  });

  appendResultSection(container, titleText, rows);
}

function affectedScopeValue(group) {
  if (!group) return "0";

  const count = Number(group.count || 0);
  const samples = Array.isArray(group.samples) ? group.samples : [];
  const sampleText = samples
    .map(value => String(value || "root"))
    .join(", ");
  const suffix = group.truncated ? " …" : "";

  if (!samples.length) {
    return String(count);
  }

  return `${count} (${sampleText}${suffix})`;
}

function appendAffectedScopesSection(container, affectedScopes) {
  if (!affectedScopes || affectedScopes.affected_scopes !== true) return;

  const summary = affectedScopes.summary || {};
  appendResultSection(container, text("result.section.affectedScopes"), [
    { label: text("result.label.directFolders"), value: summary.direct_folder_count || 0 },
    { label: text("result.label.ancestorFolders"), value: summary.ancestor_folder_count || 0 },
    { label: text("result.label.previewCandidateFolders"), value: summary.preview_candidate_folder_count || 0 },
    { label: text("result.label.removedFolders"), value: summary.removed_folder_count || 0 },
  ]);

  const groups = Array.isArray(affectedScopes.groups)
    ? affectedScopes.groups.filter(group => Number(group.count || 0) > 0)
    : [];

  if (groups.length) {
    appendResultSection(container, text("result.section.affectedGroups"), groups.map(group => ({
      label: affectedScopeLabel(group),
      value: affectedScopeValue(group),
    })));
  }
}

function appendResultNotice(container, message, className = "") {
  if (!message) return;
  const paragraph = document.createElement("p");
  paragraph.className = className ? `job-result-notice ${className}` : "job-result-notice";
  paragraph.textContent = localizedMessageText(message);
  container.appendChild(paragraph);
}

function appendErrorSamples(container, samples) {
  if (!samples || !samples.length) return;
  const details = document.createElement("details");
  details.className = "job-result-errors";
  const summary = document.createElement("summary");
  summary.textContent = text("result.section.errors");
  details.appendChild(summary);

  const list = document.createElement("ul");
  for (const sample of samples) {
    const row = document.createElement("li");
    const detail = sample.technical_detail || "";
    row.textContent = sample.path ? `${sample.path}: ${detail}` : String(detail || sample);
    list.appendChild(row);
  }
  details.appendChild(list);
  container.appendChild(details);
}

function renderChangeSummaryRows(changes) {
  if (!changes) return [];
  if (!changes.has_changes) {
    return [{ label: text("result.section.changes"), value: text("result.changesNone") }];
  }
  return [
    { label: text("result.label.newFolders"), value: changes.folders_new || 0 },
    { label: text("result.label.restoredFolders"), value: changes.folders_restored || 0 },
    { label: text("result.label.missingFolders"), value: changes.folders_missing || 0 },
    { label: text("result.label.newMedia"), value: changes.media_new || 0 },
    { label: text("result.label.restoredMedia"), value: changes.media_restored || 0 },
    { label: text("result.label.changedMedia"), value: changes.media_changed || 0 },
    { label: text("result.label.missingMedia"), value: changes.media_missing || 0 },
  ];
}

function appendChangeSummary(container, changes) {
  const rows = renderChangeSummaryRows(changes);
  if (rows.length) {
    appendResultSection(container, text("result.section.changes"), rows);
  }
}

function renderThumbnailResult(result, titleKey, noticeKey, extraRows = []) {
  const container = createResultPanel(text(titleKey));
  appendResultSection(container, text("result.section.summary"), [
    { label: text("result.label.scope"), value: resultScopeText(result) },
    { label: text("result.label.processed"), value: result.processed || 0 },
    ...extraRows,
    { label: text("result.label.created"), value: result.created || 0 },
    { label: text("result.label.reused"), value: result.reused || 0 },
    { label: text("result.label.errors"), value: result.errors || 0 },
    { label: text("result.label.seconds"), value: secondsText(result.duration_seconds) },
  ]);
  appendResultNotice(container, text(noticeKey));
  appendErrorSamples(container, result.error_samples || []);
}

function renderJobResult(result) {
  if (!result) {
    renderResultMessage(text("jobs.resultMissing"));
    return;
  }

  if (result.requires_decision) {
    renderMissingDecision(result);
    return;
  }

  if (result.update_branch) {
    if (result.activation_decision_required) {
      const decision = {
        ...result.activation_decision_required,
        update_branch: true,
        branch: result.branch || result.activation_decision_required.branch || "",
      };
      renderMissingDecision(decision, {
        title: text("result.title.updateBranchDecision"),
      });
      return;
    }

    const summary = result.summary || {};
    const container = createResultPanel(text("result.title.updateBranch"));
    appendResultSection(container, text("result.section.summary"), [
      { label: text("result.label.branch"), value: result.branch || text("jobs.scopeFull") },
      { label: text("result.label.scanId"), value: summary.scan_id || result.scan_id || "?" },
      { label: text("result.label.media"), value: summary.scanned_media || 0 },
      { label: text("result.label.seconds"), value: secondsText(result.duration_seconds) },
    ]);
    appendResultNotice(container, text("result.notice.updateFolderPreviewsNotPrepared"));
    appendAffectedScopesSection(container, result.affected_scopes || result.update_plan?.affected_scopes);
    appendResultSection(container, text("result.section.writes"), [
      { label: text("result.label.sourceMedia"), value: text("result.value.notChanged") },
    ]);
    return;
  }

  if (result.prepare_previews) {
    const summary = result.summary || {};
    const container = createResultPanel(text("result.title.folderPreviewBuild"));
    appendResultSection(container, text("result.section.summary"), [
      { label: text("result.label.branch"), value: result.branch || text("jobs.scopeFull") },
      { label: text("result.label.seconds"), value: secondsText(result.duration_seconds) },
    ]);
    appendResultSection(container, text("result.section.mediaPreviews"), [
      { label: text("result.label.media"), value: summary.processed_media || 0 },
      { label: text("result.label.framesProcessed"), value: summary.processed_frames || 0 },
      { label: text("result.label.mediaCreatedReusedErrors"), value: `${summary.media_preview_created || 0}/${summary.media_preview_reused || 0}/${summary.media_preview_errors || 0}` },
    ]);
    appendResultSection(container, text("result.section.folderPreviews"), [
      { label: text("result.label.folderAutoRows"), value: summary.folder_preview_auto_inserted || 0 },
      { label: text("result.label.folderParentRows"), value: summary.folder_preview_auto_parent_inserted || 0 },
      { label: text("result.label.folderThumbsCreatedReusedErrors"), value: `${summary.folder_preview_thumbnail_created || 0}/${summary.folder_preview_thumbnail_reused || 0}/${summary.folder_preview_thumbnail_errors || 0}` },
    ]);
    appendResultSection(container, text("result.section.writes"), [
      { label: text("result.label.sourceMedia"), value: text("result.value.notChanged") },
    ]);
    return;
  }

  if (result.folder_preview_build_tree) {
    const container = createResultPanel(text("result.title.folderPreviewBuild"));
    appendResultSection(container, text("result.section.summary"), [
      { label: text("result.label.branch"), value: result.branch || text("jobs.scopeFull") },
      { label: text("result.label.autoFolders"), value: result.auto?.folders_applied || 0 },
      { label: text("result.label.parentFolders"), value: result.parent?.folders_applied || 0 },
      { label: text("result.label.seconds"), value: secondsText(result.duration_seconds) },
    ]);
    appendResultSection(container, text("result.section.folderPreviews"), [
      { label: text("result.label.autoRows"), value: result.auto?.rows_inserted || 0 },
      { label: text("result.label.parentRows"), value: result.parent?.rows_inserted || 0 },
    ]);
    appendResultSection(container, text("result.section.thumbnails"), [
      { label: text("result.label.reused"), value: result.thumbnails?.reused || 0 },
      { label: text("result.label.created"), value: result.thumbnails?.created || 0 },
      { label: text("result.label.errors"), value: result.thumbnails?.errors || 0 },
    ]);
    appendResultSection(container, text("result.section.writes"), [
      { label: text("result.label.sourceMedia"), value: text("result.value.notChanged") },
    ]);
    return;
  }

  if (result.thumbnail_job && result.thumbnail_type === "gif_preview") {
    renderThumbnailResult(result, "result.title.gifPreview", "jobs.gifPreviewNotice");
    return;
  }

  if (result.thumbnail_job && result.thumbnail_type === "video_poster") {
    renderThumbnailResult(result, "result.title.videoPoster", "jobs.videoPosterNotice");
    return;
  }

  if (result.thumbnail_job && result.thumbnail_type === "video_frame") {
    renderThumbnailResult(result, "result.title.videoFrames", "jobs.videoFramesNotice", [
      { label: text("result.label.framesProcessed"), value: result.frames_processed || 0 },
    ]);
    return;
  }

  if (result.dynamic_cache_cleanup_execute) {
    const container = createResultPanel(text("result.title.dynamicCacheCleanup"));
    const primaryMessage = result.result_messages?.[0] || null;
    appendResultNotice(container, primaryMessage);
    for (const warning of (result.warning_messages || [])) {
      appendResultNotice(container, warning, "warning");
    }
    appendResultSection(container, text("result.section.summary"), [
      { label: text("result.label.planned"), value: result.planned_entries || 0 },
      { label: text("result.label.deletedDbRows"), value: result.deleted_entries || 0 },
      { label: text("result.label.deletedFiles"), value: result.deleted_files || 0 },
      { label: text("result.label.missingFiles"), value: result.missing_files || 0 },
      { label: text("result.label.reclaimed"), value: formatBytes(numericStatusValue(result.removed_bytes)) },
      { label: text("result.label.errors"), value: result.error_count || 0 },
      { label: text("result.label.seconds"), value: secondsText(result.duration_seconds) },
    ]);
    appendResultSection(container, text("result.section.cacheStatus"), [
      { label: text("result.label.dynamicBefore"), value: formatBytes(numericStatusValue(result.dynamic_size_before_bytes)) },
      { label: text("result.label.dynamicAfter"), value: formatBytes(numericStatusValue(result.dynamic_size_after_bytes)) },
      { label: text("result.label.protectedOutsidePlan"), value: formatBytes(numericStatusValue(result.protected_size_before_bytes)) },
      { label: text("result.label.keptForFolderPreviews"), value: `${result.skipped_referenced_entries || 0} (${formatBytes(numericStatusValue(result.skipped_referenced_bytes))})` },
    ]);
    appendResultNotice(container, text("jobs.dynamicCacheCleanupNotice"));
    appendErrorSamples(container, result.sample_errors || []);
    return;
  }

  if (result.protected_cache_maintenance) {
    const container = createResultPanel(text("settings.stableMaintenanceTitle"));
    if (result.operation === "check") {
      const plan = result.protected_orphan_cleanup_plan || {};
      const audit = result.protected_orphan_audit || {};
      const summary = plan.summary || {};
      const db = audit.db || {};
      appendResultSection(container, text("result.section.summary"), [
        { label: text("settings.stableMaintenanceValid"), value: cacheStatusEntriesText(numericStatusValue(db.valid_rows)) },
        { label: text("settings.stableMaintenanceCleanable"), value: cacheStatusEntriesText(numericStatusValue(summary.safe_to_delete_entries)) },
        { label: text("settings.protectedExecuteRemovedBytes"), value: formatBytes(numericStatusValue(summary.estimated_reclaimable_bytes)) },
        { label: text("result.label.seconds"), value: secondsText(result.duration_seconds) },
      ]);
      appendResultNotice(container, text("settings.protectedPlanReadOnly"));
      return;
    }

    if (result.operation === "clean") {
      const clean = result.protected_orphan_cleanup_execute || {};
      appendResultSection(container, text("result.section.summary"), [
        { label: text("settings.protectedExecuteDeletedFiles"), value: clean.deleted_files || 0 },
        { label: text("settings.protectedExecuteDeletedDbRows"), value: clean.deleted_db_rows || 0 },
        { label: text("settings.protectedExecuteRemovedBytes"), value: formatBytes(numericStatusValue(clean.removed_bytes)) },
        { label: text("settings.protectedExecuteErrors"), value: clean.error_count || 0 },
        { label: text("result.label.seconds"), value: secondsText(clean.duration_seconds || result.duration_seconds) },
      ]);
      appendResultNotice(container, clean.result_messages?.[0] || text("settings.protectedExecuteCompleted"));
      return;
    }
  }

  if (result.catalog_update) {
    state.catalogStatus = {
      ...(state.catalogStatus || {}),
      available_folders: result.folders || 0,
      available_media: result.media?.total || 0,
    };
    updateJobActionButtons(state.jobRunning);
    const container = createResultPanel(text("result.title.catalogUpdate"));
    appendResultSection(container, text("result.section.summary"), [
      { label: text("result.label.scanId"), value: result.scan_id || "?" },
      { label: text("result.label.folders"), value: result.folders || 0 },
      { label: text("result.label.media"), value: result.media?.total || 0 },
      { label: text("result.label.seconds"), value: secondsText(result.duration_seconds) },
    ]);
    const changes = result.changes || {};
    appendResultSection(container, text("result.section.changes"), [
      { label: text("result.label.newFolders"), value: changes.folders_new || 0 },
      { label: text("result.label.restoredFolders"), value: changes.folders_restored || 0 },
      { label: text("result.label.missingFolders"), value: changes.folders_missing_new || 0 },
      { label: text("result.label.newMedia"), value: changes.media_new || 0 },
      { label: text("result.label.restoredMedia"), value: changes.media_restored || 0 },
      { label: text("result.label.changedMedia"), value: changes.media_changed || 0 },
      { label: text("result.label.missingMedia"), value: changes.media_missing_new || 0 },
      { label: text("result.label.foldersPurged"), value: changes.folders_purged || 0 },
      { label: text("result.label.mediaPurged"), value: changes.media_purged || 0 },
      { label: text("result.label.favoritesPurged"), value: changes.favorites_purged || 0 },
    ]);
    appendTimingSection(container, text("result.section.timingScanActivate"), result.phases?.scan_activate?.timing);

    appendAffectedScopesSection(container, result.affected_scopes || result.update_plan?.affected_scopes || result.phases?.scan_activate?.affected_scopes);
    appendResultNotice(container, text("result.notice.catalogUpdateFollowupPreviews"));

    const actions = document.createElement("div");
    actions.className = "decision-actions";
    const prepareButton = document.createElement("button");
    prepareButton.type = "button";
    prepareButton.dataset.longJobAction = "true";
    prepareButton.textContent = text("jobs.prepareAllPreviews");
    prepareButton.disabled = numericStatusValue(result.media?.total) <= 0;
    prepareButton.classList.add("secondary-action");
    prepareButton.addEventListener("click", () => startPrepareAllPreviews());
    actions.appendChild(prepareButton);
    container.appendChild(actions);
    return;
  }

  if (result.activation_cancelled) {
    const container = createResultPanel(text("result.title.activationCancelled"));
    appendResultNotice(container, result.result_messages?.[0] || text("jobs.activationCancelled"));
    return;
  }

  if (result.activation) {
    const container = createResultPanel(text("result.title.activation"));
    appendResultSection(container, text("result.section.summary"), [
      { label: text("result.label.scanId"), value: result.scan_id },
      { label: text("result.label.mode"), value: scanModeText(result.mode) },
      { label: text("result.label.folders"), value: result.folders },
      { label: text("result.label.media"), value: result.media?.total || 0 },
      { label: text("result.label.seconds"), value: secondsText(result.duration_seconds) },
    ]);
    const changes = result.changes || {};
    appendResultSection(container, text("result.section.changes"), [
      { label: text("result.label.newFolders"), value: changes.folders_new || 0 },
      { label: text("result.label.restoredFolders"), value: changes.folders_restored || 0 },
      { label: text("result.label.missingFolders"), value: changes.folders_missing_new || 0 },
      { label: text("result.label.newMedia"), value: changes.media_new || 0 },
      { label: text("result.label.restoredMedia"), value: changes.media_restored || 0 },
      { label: text("result.label.changedMedia"), value: changes.media_changed || 0 },
      { label: text("result.label.missingMedia"), value: changes.media_missing_new || 0 },
      { label: text("result.label.foldersPurged"), value: changes.folders_purged || 0 },
      { label: text("result.label.mediaPurged"), value: changes.media_purged || 0 },
      { label: text("result.label.favoritesPurged"), value: changes.favorites_purged || 0 },
    ]);
    appendTimingSection(container, text("result.section.timingScanActivate"), result.timing);
    appendAffectedScopesSection(container, result.affected_scopes || result.update_plan?.affected_scopes);
    appendResultNotice(container, text("jobs.activateNotice"));
    return;
  }

  const skipped = (result.skipped?.links || 0) + (result.skipped?.ignored_directories || 0) + (result.skipped?.other || 0);
  const container = createResultPanel(text("result.title.scan"));
  appendResultSection(container, text("result.section.summary"), [
    { label: text("result.label.scope"), value: resultScopeText(result) },
    { label: text("result.label.folders"), value: result.folders || 0 },
    { label: text("result.label.media"), value: result.media?.total || 0 },
    { label: text("result.label.errors"), value: result.errors || 0 },
    { label: text("result.label.skipped"), value: skipped },
    { label: text("result.label.seconds"), value: secondsText(result.duration_seconds) },
  ]);
  appendResultSection(container, text("result.section.media"), [
    { label: text("result.label.images"), value: result.media?.images || 0 },
    { label: text("result.label.gifs"), value: result.media?.gifs || 0 },
    { label: text("result.label.videos"), value: result.media?.videos || 0 },
    { label: text("result.label.other"), value: result.media?.other || 0 },
  ]);
  appendChangeSummary(container, result.changes);
  if (result.read_only === false) {
    appendResultNotice(container, text("jobs.stageNotice"), "warning");
  }
}

function localizedCount(count, oneKey, fewKey, manyKey) {
  const value = Number(count || 0);
  let key = manyKey;
  if (activeLocale === "cs") {
    if (value === 1) key = oneKey;
    else if (value >= 2 && value <= 4) key = fewKey;
  } else if (value === 1) {
    key = oneKey;
  }
  return `${localizedInteger(value)} ${text(key)}`;
}

function missingDecisionItemsText(folders, media) {
  const parts = [];
  const folderCount = Number(folders || 0);
  const mediaCount = Number(media || 0);

  if (folderCount) {
    parts.push(localizedCount(folderCount, "count.folder.one", "count.folder.few", "count.folder.many"));
  }

  if (mediaCount) {
    parts.push(localizedCount(mediaCount, "count.media.one", "count.media.few", "count.media.many"));
  }

  return parts.length ? parts.join(text("count.item.separator")) : text("count.item.zero");
}

function missingDecisionScopeText(branch) {
  const value = String(branch || "").trim();
  return value ? text("jobs.scopeInFolder", { branch: value }) : text("jobs.scopeCatalog");
}

function renderMissingDecision(result, options = {}) {
  const modal = ensureFolderRepairModal();
  setFolderRepairModalHeader(
    options.title || text("jobs.decisionModalTitle"),
    options.subtitle || text("jobs.decisionModalSubtitle"),
  );

  const body = modal.querySelector("#folderRepairModalBody");
  body.replaceChildren();

  const container = document.createElement("div");
  container.className = "decision-panel";

  const missingFolders = result.missing?.folders_total || 0;
  const missingMedia = result.missing?.media_total || 0;

  const summary = document.createElement("p");
  summary.textContent = text("jobs.decisionRequired", {
    scope: missingDecisionScopeText(result.branch),
    items: missingDecisionItemsText(missingFolders, missingMedia),
  });
  container.appendChild(summary);

  const warning = document.createElement("p");
  warning.className = "decision-warning";
  warning.textContent = text("jobs.decisionWarning");
  container.appendChild(warning);

  appendDecisionSample(container, text("jobs.decisionSampleFolders"), result.sample?.folders || []);
  appendDecisionSample(container, text("jobs.decisionSampleMedia"), result.sample?.media || []);

  const actions = document.createElement("div");
  actions.className = "decision-actions";

  const scanId = Number(result.scan_id || 0);
  const branch = result.branch || "";
  setFolderRepairCloseHandler(async () => {
    await startScanActivate("cancel", null, scanId, branch, { skipConfirm: true });
  });
  actions.appendChild(decisionButton(text("jobs.decisionPurge"), "delete_from_db", "jobs.decisionConfirmPurge", true, scanId, branch));
  actions.appendChild(decisionButton(text("jobs.decisionCancel"), "cancel", "jobs.decisionConfirmCancel", false, scanId, branch));

  container.appendChild(actions);
  body.appendChild(container);
  openFolderRepairModal();
  renderResultMessage(text("jobs.decisionShownInModal"), "warning");
}

function appendDecisionSample(container, title, items) {
  if (!items.length) return;

  const details = document.createElement("details");
  details.className = "decision-sample";
  const summary = document.createElement("summary");
  summary.textContent = title;
  details.appendChild(summary);

  const list = document.createElement("ul");
  for (const item of items) {
    const row = document.createElement("li");
    row.textContent = item;
    list.appendChild(row);
  }
  details.appendChild(list);
  container.appendChild(details);
}

function appendCatalogUpdateSample(container, title, items) {
  const paths = Array.isArray(items) ? items.filter(Boolean) : [];
  if (!paths.length) return;

  const details = document.createElement("details");
  details.className = "decision-sample";
  details.open = true;
  const summary = document.createElement("summary");
  summary.textContent = title;
  details.appendChild(summary);

  const list = document.createElement("ul");
  for (const item of paths) {
    const row = document.createElement("li");
    row.textContent = item;
    list.appendChild(row);
  }
  details.appendChild(list);
  container.appendChild(details);
}

function renderCatalogUpdatePreview(result, options = {}) {
  const changes = result?.changes || {};
  const samples = result?.change_samples || {};
  const hasChanges = Boolean(changes.has_changes);
  const target = els.catalogUpdateDecision || els.jobResult;
  if (target) target.hidden = false;
  const container = createResultPanel(text("catalogUpdate.previewTitle"), target);

  appendResultSection(container, text("result.section.summary"), [
    { label: text("result.label.scope"), value: text("jobs.scopeFull") },
    { label: text("result.label.folders"), value: result?.folders || 0 },
    { label: text("result.label.media"), value: result?.media?.total || 0 },
    { label: text("result.label.seconds"), value: secondsText(result?.duration_seconds) },
  ]);

  appendChangeSummary(container, changes);
  appendCatalogUpdateSample(container, text("catalogUpdate.sampleNewFolders"), samples.folders_new || []);
  appendCatalogUpdateSample(container, text("catalogUpdate.sampleMissingFolders"), samples.folders_missing || []);

  if (!hasChanges) {
    appendResultNotice(
      container,
      options.cleanupRunning ? text("catalogUpdate.noChangesCleanup") : text("catalogUpdate.noChanges")
    );
    return;
  }

  appendResultNotice(container, text("catalogUpdate.previewNotice"));
  appendResultNotice(container, text("catalogUpdate.performWarning"), "warning");

  const actions = document.createElement("div");
  actions.className = "decision-actions";

  const performButton = document.createElement("button");
  performButton.type = "button";
  performButton.className = "write-action";
  performButton.textContent = text("catalogUpdate.perform");
  performButton.disabled = isLongJobLocked();
  performButton.addEventListener("click", startCatalogUpdateStage);
  actions.appendChild(performButton);

  const cancelButton = document.createElement("button");
  cancelButton.type = "button";
  cancelButton.textContent = text("catalogUpdate.cancel");
  cancelButton.disabled = isLongJobLocked();
  cancelButton.addEventListener("click", cancelCatalogUpdateWorkflow);
  actions.appendChild(cancelButton);

  container.appendChild(actions);
}


function isSourceRootUnavailableError(error) {
  const message = String(error?.message || "");
  return error?.code === "source_root_unavailable" || message.includes("Zdrojová složka není dostupná") || message.includes("Zdrojovou složku nelze") || message.includes("Zdrojová cesta není složka");
}

function renderCatalogUpdateSourceRootUnavailable(error) {
  const target = els.catalogUpdateDecision || els.jobResult;
  if (!target) return;
  target.hidden = false;
  const container = createResultPanel(text("catalogUpdate.sourceRootUnavailableTitle"), target);
  appendResultNotice(container, text("catalogUpdate.sourceRootUnavailableReason"), "warning");
  appendResultNotice(container, text("catalogUpdate.sourceRootUnavailableHint"));
  const details = String(error?.message || "").trim();
  if (details && details !== text("catalogUpdate.sourceRootUnavailableReason")) {
    appendResultNotice(container, details);
  }
}

async function startCatalogUpdateWorkflow() {
  if (!canStartLongJob()) {
    return;
  }
  state.catalogUpdateWorkflow = { phase: "preview-running", preview: null, scanId: null };
  clearJobResult({ force: true });
  renderCatalogUpdateDecisionMessage(text("catalogUpdate.statusPreparing"));
  updateJobActionButtons(false);
  const started = await startJob("/api/jobs/scan-stage", "", {}, {
    allowCatalogUpdateDecision: true,
    onStartError: (error) => {
      if (isSourceRootUnavailableError(error)) {
        renderCatalogUpdateSourceRootUnavailable(error);
      }
    },
  });
  if (!started) {
    state.catalogUpdateWorkflow = { phase: "idle", preview: null, scanId: null };
    updateJobActionButtons(false);
  }
}

async function startCatalogUpdateStage() {
  const workflow = state.catalogUpdateWorkflow || { phase: "idle", preview: null, scanId: null };
  const previousPreview = workflow.preview || null;
  const scanId = Number(workflow.scanId || previousPreview?.scan_id || 0);

  if (!scanId) {
    setMessage(text("jobs.noCompletedStage"), true);
    return;
  }

  if (!window.confirm(text("catalogUpdate.confirmPerform"))) {
    return;
  }

  state.catalogUpdateWorkflow = { phase: "execute-running", preview: previousPreview, scanId };
  updateJobActionButtons(false);
  renderCatalogUpdateDecisionMessage(text("catalogUpdate.stageStarted"));
  const started = await startScanActivate("delete_from_db", null, scanId, "", {
    skipConfirm: true,
    allowCatalogUpdateDecision: true,
  });
  if (started) {
    setMessage(text("catalogUpdate.activating"));
  } else {
    state.catalogUpdateWorkflow = { phase: "preview-ready", preview: previousPreview, scanId };
    if (previousPreview) renderCatalogUpdatePreview(previousPreview);
  }
}

async function cancelCatalogUpdateWorkflow() {
  const workflow = state.catalogUpdateWorkflow || { phase: "idle", preview: null, scanId: null };
  const previousPreview = workflow.preview || null;
  const scanId = Number(workflow.scanId || previousPreview?.scan_id || 0);

  if (!scanId) {
    state.catalogUpdateWorkflow = { phase: "idle", preview: null, scanId: null };
    renderResultMessage(text("catalogUpdate.cancelled"));
    updateJobActionButtons(false);
    return;
  }

  state.catalogUpdateWorkflow = { phase: "cancel-running", preview: previousPreview, scanId };
  updateJobActionButtons(false);
  renderCatalogUpdateDecisionMessage(text("catalogUpdate.statusDiscarding"));
  const started = await startScanActivate("cancel", null, scanId, "", {
    skipConfirm: true,
    allowCatalogUpdateDecision: true,
  });
  if (!started) {
    state.catalogUpdateWorkflow = { phase: "preview-ready", preview: previousPreview, scanId };
    if (previousPreview) renderCatalogUpdatePreview(previousPreview);
  }
}

async function maybeAdvanceCatalogUpdateWorkflow(data) {
  const workflow = state.catalogUpdateWorkflow || { phase: "idle" };
  const job = data.current_job || null;
  if (!job || workflow.phase === "idle") return false;

  if (job.state === "completed" && job.kind === "scan-stage" && workflow.phase === "preview-running") {
    const preview = job.result || {};
    const scanId = Number(preview.scan_id || 0);
    const hasChanges = Boolean(preview.changes?.has_changes);

    if (!hasChanges) {
      state.catalogUpdateWorkflow = { phase: "no-changes-discard-running", preview, scanId };
      renderCatalogUpdatePreview(preview, { cleanupRunning: true });
      updateJobActionButtons(false);
      if (scanId) {
        const started = await startScanActivate("cancel", null, scanId, "", {
          skipConfirm: true,
          allowCatalogUpdateDecision: true,
          quietStart: true,
        });
        if (!started) {
          state.catalogUpdateWorkflow = { phase: "done", preview: null, scanId: null };
          renderCatalogUpdatePreview(preview, { noChangesFinal: true });
          updateJobActionButtons(false);
        }
      } else {
        state.catalogUpdateWorkflow = { phase: "done", preview: null, scanId: null };
        renderCatalogUpdatePreview(preview, { noChangesFinal: true });
        updateJobActionButtons(false);
      }
      return true;
    }

    state.catalogUpdateWorkflow = { phase: "preview-ready", preview, scanId };
    renderCatalogUpdatePreview(preview);
    updateJobActionButtons(false);
    return true;
  }

  if (job.state === "completed" && job.kind === "scan-activate" && workflow.phase === "execute-running") {
    state.catalogUpdateWorkflow = { phase: "done", preview: null, scanId: null };
    clearCatalogUpdateDecision();
    updateJobActionButtons(false);
    return false;
  }

  if (job.state === "cancelled" && job.kind === "scan-activate" && workflow.phase === "cancel-running") {
    state.catalogUpdateWorkflow = { phase: "done", preview: null, scanId: null };
    clearCatalogUpdateDecision();
    renderResultMessage(text("catalogUpdate.cancelled"));
    updateJobActionButtons(false);
    return true;
  }

  if (job.state === "cancelled" && job.kind === "scan-activate" && workflow.phase === "no-changes-discard-running") {
    const preview = workflow.preview || null;
    state.catalogUpdateWorkflow = { phase: "done", preview: null, scanId: null };
    if (preview) {
      renderCatalogUpdatePreview(preview, { noChangesFinal: true });
    } else {
      renderResultMessage(text("catalogUpdate.noChanges"));
    }
    updateJobActionButtons(false);
    return true;
  }

  if (["failed", "cancelled", "requires_decision"].includes(job.state) && workflow.phase !== "preview-ready") {
    state.catalogUpdateWorkflow = { phase: "idle", preview: null, scanId: null };
    clearCatalogUpdateDecision();
    updateJobActionButtons(false);
  }

  return false;
}

function decisionButton(label, missingDecision, confirmKey, danger, scanId, branch) {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = label;
  if (danger) button.className = "write-action";
  button.addEventListener("click", async () => {
    const started = await startScanActivate(missingDecision, confirmKey, scanId, branch);
    if (started) {
      setFolderRepairCloseHandler(null);
      closeFolderRepairModal({ runHandler: false });
    }
  });
  return button;
}

async function loadJobStatus() {
  const data = await fetchJson("/api/jobs/status");
  renderJobStatus(data);
  return data;
}

function scheduleJobPolling() {
  clearJobPollingTimer();
  state.jobPollTimer = setTimeout(async () => {
    state.jobPollTimer = null;
    try {
      const data = await loadJobStatus();
      if (await maybeAdvanceCatalogUpdateWorkflow(data)) {
        if (jobIsRunning(data.current_job)) {
          ensureJobPolling();
        }
        return;
      }
      if (jobIsRunning(data.current_job)) {
        ensureJobPolling();
      } else if (data.current_job && data.current_job.state === "completed") {
        if (["scan-activate", "update-branch", "catalog-update"].includes(data.current_job.kind)) {
          await loadStatus();
          try {
            await refreshCurrentViewAndTree({ forceTree: true });
          } catch (error) {
            const branch = data.current_job.branch || "";
            const fallback = branch ? parentCatalogPath(branch) : "";
            if (state.view === "folder") {
              await openFolder(fallback);
            }
            setMessage(error.message || text("jobs.resultMissing"), true);
          }
        } else if (["gif-preview", "video-poster", "video-frames", "folder-preview-build-tree", "prepare-previews"].includes(data.current_job.kind)) {
          await loadCurrentFolder();
        } else if (["dynamic-cache-cleanup", "protected-cache-orphan-check", "protected-cache-orphan-clean"].includes(data.current_job.kind)) {
          await loadCacheSettingsStatus();
        }
      }
    } catch (error) {
      setMessage(error.message, true);
    }
  }, 1000);
}

function ensureJobPolling() {
  if (!state.jobPollTimer) {
    scheduleJobPolling();
  }
}

function ensureJobPollingForStatus(data) {
  if (jobIsRunning(data?.current_job)) {
    ensureJobPolling();
  }
}

async function startJob(endpoint, branch, extraParams = {}, options = {}) {
  if (!canStartLongJob(options)) {
    return false;
  }

  setJobStartPending(true);
  try {
    const params = { ...extraParams };
    if (branch) params.branch = branch;
    const response = await postJson(endpoint, params);
    setJobStartPending(false);
    if (options.quietStart !== true) {
      syncJobLifecycleMessage(response.job, { startAccepted: true });
    }
    renderJobStatus({ current_job: response.job });
    scheduleJobPolling();
    return true;
  } catch (error) {
    setJobStartPending(false);
    if (typeof options.onStartError === "function") {
      options.onStartError(error);
    }
    setMessage(error.status === 409 && error.code !== "source_root_unavailable" ? text("jobs.busy") : error.message, true);
    const status = await loadJobStatus();
    ensureJobPollingForStatus(status);
    return false;
  }
}

async function startScanPreview(branch) {
  await startJob("/api/jobs/scan-preview", branch);
}

async function startScanStage(branch) {
  await startJob("/api/jobs/scan-stage", branch);
}

async function startGifPreview(branch) {
  await startJob("/api/jobs/thumbnails/gif-preview", branch);
}

async function startVideoPoster(branch) {
  await startJob("/api/jobs/thumbnails/video-poster", branch);
}

async function startVideoFrames(branch) {
  await startJob("/api/jobs/thumbnails/video-frames", branch);
}

async function startDynamicCacheCleanup(plan) {
  const candidates = numericStatusValue(plan?.candidate_entries);
  if (candidates <= 0) return;

  if (!window.confirm(text("settings.cacheCleanupConfirm"))) {
    return;
  }

  const started = await startJob("/api/jobs/thumbnail-cache/dynamic-cleanup", "");
  if (started) {
    setMessage(text("settings.cacheCleanupStarted"));
  }
}

async function loadMissingFolderDeletePlan(branch) {
  setFolderRepairCloseHandler(null);
  const modal = ensureFolderRepairModal();
  setFolderRepairModalHeader(text("folderRepair.modalTitle"), text("folderRepair.modalSubtitle"));
  const body = modal.querySelector("#folderRepairModalBody");
  renderFolderRepairLoading(body);
  openFolderRepairModal();

  try {
    const payload = await fetchJson("/api/folder/missing-delete-plan", { path: branch });
    renderMissingFolderDeletePlan(payload, body);
    setMessage(text("folderRepair.planReady"));
  } catch (error) {
    renderFolderRepairError(body, error.message);
    setMessage(error.message, true);
  }
}

function renderFolderRepairLoading(targetElement) {
  targetElement.replaceChildren();
  const paragraph = document.createElement("p");
  paragraph.className = "folder-repair-loading muted";
  paragraph.textContent = text("folderRepair.loading");
  targetElement.appendChild(paragraph);
}

function renderFolderRepairError(targetElement, message) {
  setFolderRepairCloseHandler(null);
  targetElement.replaceChildren();
  const container = document.createElement("div");
  container.className = "job-result-panel";
  const title = document.createElement("h4");
  title.textContent = text("folderRepair.loadError");
  container.appendChild(title);
  appendResultNotice(container, message || text("folderRepair.loadError"), "warning");
  targetElement.appendChild(container);
}

function renderMissingFolderDeletePlan(payload, targetElement = els.jobResult) {
  setFolderRepairCloseHandler(null);
  const plan = payload.plan || {};
  const oldFilesystem = plan.old_filesystem || {};
  const impact = plan.impact || {};
  const blockers = Array.isArray(plan.blocker_messages) ? plan.blocker_messages : [];

  const container = createResultPanel(text("result.title.folderRepairPlan"), targetElement);
  appendResultSection(container, text("result.section.summary"), [
    { label: text("result.label.oldPath"), value: payload.old_path || plan.old_path || "" },
    { label: text("result.label.oldStatus"), value: localizedMessageText(oldFilesystem.message_object) },
  ]);

  appendResultNotice(container, text("folderRepair.deleteNotice"), "warning");

  appendResultSection(container, text("result.section.changes"), [
    { label: text("result.label.folders"), value: impact.folders || 0 },
    { label: text("result.label.media"), value: impact.media || 0 },
    { label: text("result.label.favorites"), value: impact.favorites || 0 },
  ]);

  if (blockers.length) {
    appendResultSection(container, text("result.label.blockers"), blockers.map((item, index) => ({
      label: String(index + 1),
      value: localizedMessageText(item),
    })));
  }

  appendResultNotice(container, text("folderRepair.deleteFutureNotice"));

  const actions = document.createElement("div");
  actions.className = "decision-actions";

  if (!blockers.length) {
    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.className = "write-action";
    deleteButton.textContent = text("folderRepair.deleteAction");
    deleteButton.addEventListener("click", () => executeMissingFolderDelete(payload.old_path || plan.old_path || ""));
    actions.appendChild(deleteButton);
  } else {
    appendResultNotice(container, text("folderRepair.readOnlyNotice"), "warning");
  }

  const cancelButton = document.createElement("button");
  cancelButton.type = "button";
  cancelButton.textContent = text("jobs.decisionCancel");
  cancelButton.addEventListener("click", () => closeFolderRepairModal({ runHandler: false }));
  actions.appendChild(cancelButton);
  container.appendChild(actions);
}

async function executeMissingFolderDelete(branch) {
  if (isLongJobBlocked()) {
    showLongJobBusyMessage();
    return;
  }

  if (!branch) {
    setMessage(text("jobs.unavailableCurrent"), true);
    return;
  }

  if (!window.confirm(text("folderRepair.deleteConfirm"))) {
    return;
  }

  try {
    const payload = await postJson("/api/folder/missing-delete-execute", { path: branch });
    closeFolderRepairModal();
    await loadStatus();

    const oldPath = payload.old_path || branch;
    const parentPath = payload.parent_path !== undefined ? payload.parent_path : parentCatalogPath(oldPath);
    invalidateFolderTree();
    if (state.view === "folder" && catalogPathIsInBranch(state.folder, oldPath)) {
      await openFolder(parentPath);
    } else {
      await refreshCurrentViewAndTree({ forceTree: true });
    }

    setMessage(text("folderRepair.deleteDone", { path: oldPath }));
  } catch (error) {
    setMessage(error.message || text("folderRepair.deleteError"), true);
    const modal = ensureFolderRepairModal();
    const body = modal.querySelector("#folderRepairModalBody");
    renderFolderRepairError(body, error.message || text("folderRepair.deleteError"));
    openFolderRepairModal();
  }
}

async function startUpdateBranch(branch, folder = null) {
  if (!branch) {
    setMessage(text("jobs.unavailableCurrent"), true);
    return;
  }

  if (isLongJobBlocked()) {
    showLongJobBusyMessage();
    return;
  }

  const targetFolder = folder || (sameCatalogPath(branch, state.folder) ? state.currentFolder : null);
  if (targetFolder && !folderFilesystemIsUsable(targetFolder)) {
    if (folderSourceRootIsUnavailable(targetFolder)) {
      showFolderFilesystemProblem(targetFolder);
      return;
    }
    await loadMissingFolderDeletePlan(branch);
    return;
  }

  if (!window.confirm(text("jobs.updateBranchConfirm"))) {
    return;
  }
  await startJob("/api/jobs/update-branch", branch);
}

async function loadFolderPreviewPlan(branch) {
  try {
    const payload = await fetchJson("/api/folder-preview/build-tree/plan", { folder: branch });
    renderFolderPreviewPlan(payload.plan);
  } catch (error) {
    setMessage(error.message, true);
  }
}

async function startFolderPreviewBuildTree(branch) {
  if (isLongJobBlocked()) {
    showLongJobBusyMessage();
    return;
  }

  if (!window.confirm(text("jobs.folderPreviewConfirm"))) {
    return;
  }
  await startJob("/api/jobs/folder-preview/build-tree", branch);
}

async function startPrepareAllPreviews() {
  if (!canPrepareAllPreviews()) {
    return;
  }

  if (isLongJobBlocked()) {
    showLongJobBusyMessage();
    return;
  }

  if (!window.confirm(text("jobs.prepareAllPreviewsConfirm"))) {
    return;
  }

  await startJob("/api/jobs/prepare-previews", "");
}

async function startGenerateFolderPreviews(branch) {
  if (branch === null || branch === undefined) {
    setMessage(text("jobs.unavailableCurrent"), true);
    return;
  }

  if (isLongJobBlocked()) {
    showLongJobBusyMessage();
    return;
  }

  await startJob("/api/jobs/prepare-previews", branch);
}

function renderFolderPreviewPlan(plan) {
  const container = createResultPanel(text("jobs.folderPreviewPlanTitle"));

  const missing = (plan.auto?.missing_or_not_ready || 0) + (plan.parent?.missing_or_not_ready || 0);
  appendResultSection(container, text("result.section.summary"), [
    { label: text("result.label.branch"), value: plan.branch || text("jobs.scopeFull") },
    { label: text("result.label.folders"), value: plan.candidate_folder_count || 0 },
    { label: text("result.label.autoFolders"), value: plan.auto?.folders_to_build || 0 },
    { label: text("result.label.parentFolders"), value: plan.parent?.folders_to_apply || 0 },
  ]);
  appendResultSection(container, text("result.section.folderPreviews"), [
    { label: text("result.label.selectedAutoItems"), value: plan.auto?.selected_items || 0 },
    { label: text("result.label.selectedParentItems"), value: plan.parent?.selected_items || 0 },
    { label: text("result.label.missingThumbnails"), value: missing },
    { label: text("result.label.replacedAutoRows"), value: plan.auto?.rows_to_delete_in_scope || 0 },
    { label: text("result.label.replacedParentRows"), value: plan.parent?.rows_to_delete_in_scope || 0 },
  ]);
  appendResultNotice(container, text("jobs.folderPreviewPlanNotice"), "warning");

  const actions = document.createElement("div");
  actions.className = "decision-actions";
  const runButton = document.createElement("button");
  runButton.type = "button";
  runButton.className = "write-action";
  runButton.dataset.longJobAction = "true";
  runButton.dataset.baseDisabled = "false";
  runButton.textContent = text("jobs.folderPreviewRun");
  runButton.addEventListener("click", () => startFolderPreviewBuildTree(plan.branch || ""));
  actions.appendChild(runButton);
  container.appendChild(actions);
  revealJobResultPanel(container);
}

async function startScanActivate(missingDecision = "require_decision", confirmKey = "jobs.activateConfirm", scanId = null, branch = null, options = {}) {
  const selectedScan = state.latestScan || null;
  const effectiveScanId = scanId || selectedScan?.scan_id || null;
  const effectiveBranch = branch !== null ? branch : (selectedScan?.scope_rel_path || "");

  if (!effectiveScanId || (els.scanActivate.disabled && missingDecision === "require_decision")) {
    setMessage(text("jobs.noCompletedStage"), true);
    return false;
  }
  if (isLongJobBlocked({ allowCatalogUpdateDecision: options.allowCatalogUpdateDecision === true })) {
    showLongJobBusyMessage();
    return false;
  }

  if (!options.skipConfirm && confirmKey && !window.confirm(text(confirmKey))) {
    return false;
  }

  const started = await startJob("/api/jobs/scan-activate", effectiveBranch, {
    scan_id: effectiveScanId,
    missing_decision: missingDecision,
  }, {
    allowCatalogUpdateDecision: options.allowCatalogUpdateDecision === true,
    quietStart: options.quietStart === true,
  });
  return started;
}

function localizedFolderCount(count) {
  return localizedCount(count, "count.folder.one", "count.folder.few", "count.folder.many");
}

function folderCountSummary(counts, suffixKey) {
  const values = counts || {};
  return [
    localizedMediaCount(values.images, "image"),
    localizedMediaCount(values.gifs, "gif"),
    localizedMediaCount(values.videos, "video"),
    localizedMediaCount(values.other, "other"),
    `${localizedFolderCount(values.folders)} ${text(suffixKey)}`,
  ].join(" · ");
}

function recursiveCountText(folder) {
  return folderCountSummary(folder.recursive, "count.recursiveSuffix");
}

function directCountText(folder) {
  return folderCountSummary(folder.direct, "count.directSuffix");
}

function safeCount(value) {
  const number = Number(value || 0);
  return Number.isFinite(number) && number > 0 ? number : 0;
}

function formatCount(value) {
  return localizedInteger(safeCount(value));
}

function folderRecursiveCounts(folder) {
  const r = folder.recursive || {};
  return {
    images: safeCount(r.images),
    gifs: safeCount(r.gifs),
    videos: safeCount(r.videos),
    other: safeCount(r.other),
    folders: safeCount(r.folders),
  };
}

function localizedMediaCount(count, type) {
  return localizedCount(count, `count.${type}.one`, `count.${type}.few`, `count.${type}.many`);
}

function folderPrimarySummary(folder) {
  const r = folderRecursiveCounts(folder);
  const parts = [];

  if (r.images) parts.push(localizedMediaCount(r.images, "image"));
  if (r.gifs) parts.push(localizedMediaCount(r.gifs, "gif"));
  if (r.videos) parts.push(localizedMediaCount(r.videos, "video"));
  if (r.other) parts.push(localizedMediaCount(r.other, "other"));

  return parts.length ? parts.join(" · ") : text("folderCard.primaryEmpty");
}

function folderInsightBar(label, value, maxValue) {
  const safeValue = safeCount(value);
  const safeMax = Math.max(1, safeCount(maxValue));
  const percent = safeValue > 0 ? Math.max(6, Math.round((safeValue / safeMax) * 100)) : 0;

  return `
    <div class="folder-insight-item">
      <div class="folder-insight-labelrow">
        <span>${escapeHtml(label)}</span>
        <strong>${escapeHtml(formatCount(safeValue))}</strong>
      </div>
      <div class="folder-insight-track" aria-hidden="true">
        <div class="folder-insight-fill" style="width:${percent}%"></div>
      </div>
    </div>
  `;
}

function folderInsightMarkup(folder) {
  const r = folderRecursiveCounts(folder);
  const mediaTotal = r.images + r.gifs + r.videos + r.other;
  const maxValue = Math.max(r.images, r.gifs, r.videos, r.other, 1);

  return `
    <aside class="folder-card-insights" aria-label="${escapeHtml(text("aria.folderContentSummary"))}">
      <div class="folder-insight-total">
        <strong>${escapeHtml(formatCount(mediaTotal))}</strong>
        <span>${escapeHtml(text("folderCard.totalMedia"))}</span>
      </div>
      <div class="folder-insight-bars">
        ${folderInsightBar(text("folderCard.photos"), r.images, maxValue)}
        ${folderInsightBar(text("folderCard.gifs"), r.gifs, maxValue)}
        ${folderInsightBar(text("folderCard.videos"), r.videos, maxValue)}
        ${folderInsightBar(text("folderCard.other"), r.other, maxValue)}
      </div>
      <div class="folder-insight-foot">${escapeHtml(text("folderCard.recursiveFolders", { count: formatCount(r.folders) }))}</div>
    </aside>
  `;
}

function mediaTypeLabel(type) {
  return text(`mediaType.${type}`) || type;
}

function mediaSectionTitle(type) {
  const key = `mediaSection.${type}`;
  return hasTranslation(key) ? text(key) : text("mediaSection.all");
}

function mediaCardHint(media) {
  if (media.is_available === false) return text("hint.unavailableFavorite");
  if (media.media_type === "image") return text("hint.image");
  if (media.media_type === "gif") return text("hint.gif");
  if (media.media_type === "video") return text("hint.video");
  return text("hint.other");
}

function mediaPreviewMarkup(media, isAvailable) {
  const label = mediaTypeLabel(media.media_type);
  if (isAvailable && media.media_type === "image") {
    const thumbnailUrl = apiUrl("/media/thumbnail", {
      path: media.rel_path,
      variant: "photo_tile",
    });
    return `
      <div class="media-preview media-preview--photo-tile" data-thumbnail-preview>
        <img
          class="media-thumbnail media-thumbnail--photo-tile"
          ${mediaThumbnailLazyAttrs(thumbnailUrl)}
          alt=""
        >
        <div class="media-placeholder media-thumbnail-fallback" aria-hidden="true">
          <span>${escapeHtml(label)}</span>
        </div>
      </div>
    `;
  }

  if (isAvailable && media.media_type === "gif") {
    const thumbnailUrl = apiUrl("/media/thumbnail", {
      path: media.rel_path,
      variant: "gif_preview",
    });
    return `
      <div class="media-preview media-preview--gif-preview" data-thumbnail-preview>
        <img
          class="media-thumbnail media-thumbnail--gif-preview"
          ${mediaThumbnailLazyAttrs(thumbnailUrl)}
          alt=""
        >
        <div class="media-placeholder media-thumbnail-fallback" aria-hidden="true">
          <span>${escapeHtml(text("hint.gifMissingPreview"))}</span>
        </div>
      </div>
    `;
  }

  if (isAvailable && media.media_type === "video") {
    const thumbnailUrl = apiUrl("/media/thumbnail", {
      path: media.rel_path,
      variant: "video_poster",
      existing_only: "1",
    });
    const frameUrls = videoFramePreviewUrls(media);
    const frameAttrs = frameUrls
      .map((url, index) => `data-video-hover-frame${index + 1}="${escapeHtml(url)}"`)
      .join(" ");
    const playableClass = media.is_html_playable ? " is-playable" : "";
    const posterLabel = text("hint.video");
    return `
      <div class="media-preview media-preview--video-poster${playableClass}" data-thumbnail-preview ${frameAttrs}>
        <img
          class="media-thumbnail media-thumbnail--video-poster"
          ${mediaThumbnailLazyAttrs(thumbnailUrl)}
          alt=""
        >
        <img
          class="video-hover-frame-image"
          alt=""
          aria-hidden="true"
          loading="lazy"
          decoding="async"
          fetchpriority="low"
        >
        <div class="media-placeholder media-thumbnail-fallback" aria-hidden="true">
          <span>${escapeHtml(text("hint.videoMissingPoster"))}</span>
        </div>
        <span class="video-play-indicator" aria-hidden="true">▶</span>
        <span class="sr-only">${escapeHtml(posterLabel)}</span>
      </div>
    `;
  }

  return `
    <div class="media-placeholder" aria-hidden="true">
      <span>${escapeHtml(label)}</span>
    </div>
  `;
}


function videoFramePreviewUrls(media) {
  return [1, 2, 3, 4].map((frameIndex) => apiUrl("/media/thumbnail", {
    path: media.rel_path,
    variant: "video_frame",
    frame: String(frameIndex),
    existing_only: "1",
  }));
}


function mediaMetaText(media) {
  const parts = [
    mediaTypeLabel(media.media_type),
    media.extension ? media.extension.toUpperCase() : text("meta.noExtension"),
  ];

  if (media.is_available !== false) {
    parts.push(formatBytes(media.size_bytes));
  } else {
    parts.push(text("meta.unavailable"));
  }

  return parts.join(" · ");
}

function parentFolderPath(mediaPath) {
  const normalized = String(mediaPath || "").replaceAll("\\", "/");
  const index = normalized.lastIndexOf("/");
  if (index < 0) return "";
  return normalized.slice(0, index);
}

function folderButton(folder, active = false) {
  const isDiskCandidate = folder.is_disk_candidate === true;
  const row = document.createElement("div");
  row.className = `folder-row${active ? " active" : ""}${isDiskCandidate ? " disk-candidate" : ""}`;

  const openButton = document.createElement("button");
  openButton.type = "button";
  openButton.className = "folder-row-main";
  const metaText = isDiskCandidate ? text("hint.diskRootCandidate") : recursiveCountText(folder);
  openButton.innerHTML = `
    <div class="row-title">${escapeHtml(folder.name)}</div>
    <div class="row-meta">${escapeHtml(metaText)}</div>
  `;
  openButton.addEventListener("click", () => {
    if (isDiskCandidate) {
      setMessage(text("jobs.diskRootCandidateOpen"), false);
      return;
    }
    openFolder(folder.rel_path);
  });
  row.appendChild(openButton);

  const actions = document.createElement("div");
  actions.className = "folder-row-actions";
  actions.appendChild(folderSystemOpenButton(folder.rel_path, folder));
  actions.appendChild(folderUpdateButton(folder.rel_path, folder));
  row.appendChild(actions);
  return row;
}

function folderSystemOpenButton(folderPath, folder = null) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "folder-open-action";
  button.textContent = text("actions.openFolder");
  button.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    if (folder && !folderFilesystemIsUsable(folder)) {
      showFolderFilesystemProblem(folder, "jobs.folderMissingOpenBlocked");
      return;
    }
    openSystemFolder(folderPath);
  });
  return button;
}

function folderUpdateButton(branch, folder = null) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "folder-update-action write-action";
  button.dataset.action = "update-folder";
  button.textContent = text("actions.updateFolder");
  button.disabled = state.jobRunning;

  if (folder && !folderFilesystemIsUsable(folder)) {
    button.classList.add("folder-unavailable-action");
    button.dataset.folderMissing = "1";
  }

  button.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    startUpdateBranch(branch, folder);
  });
  return button;
}

function closeFolderMoreMenus(except = null) {
  document.querySelectorAll(".folder-more-menu[open]").forEach((menu) => {
    if (menu !== except) {
      menu.open = false;
    }
  });
}

function prepareFolderMoreMenu(details) {
  if (!details) return;

  details.addEventListener("click", (event) => {
    event.stopPropagation();
  });

  const summary = details.querySelector("summary");
  if (summary) {
    summary.addEventListener("click", () => {
      closeFolderMoreMenus(details);
    });
  }
}

function folderMoreMenu(branch, folder = null, options = {}) {
  const includeUpdate = options.includeUpdate === true;
  const includeGenerate = folderCanGeneratePreviews(folder);

  if (!includeUpdate && !includeGenerate) return null;

  const details = document.createElement("details");
  details.className = "folder-more-menu";

  const summary = document.createElement("summary");
  summary.textContent = text("actions.more");
  details.appendChild(summary);

  const menu = document.createElement("div");
  menu.className = "folder-more-menu-panel";

  if (includeUpdate) {
    const updateButton = folderUpdateButton(branch, folder);
    updateButton.addEventListener("click", () => {
      details.open = false;
    });
    menu.appendChild(updateButton);
  }

  if (includeGenerate) {
    const generateButton = document.createElement("button");
    generateButton.type = "button";
    generateButton.dataset.longJobAction = "1";
    generateButton.textContent = text("actions.generatePreviews");
    generateButton.disabled = state.jobRunning || state.jobStartPending;
    generateButton.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      details.open = false;
      startGenerateFolderPreviews(branch);
    });
    menu.appendChild(generateButton);
  }
  details.appendChild(menu);
  prepareFolderMoreMenu(details);
  return details;
}

function renderCatalogStatus(status) {
  state.catalogStatus = status;
  els.statusText.textContent = text("status.summary", {
    folders: status.available_folders,
    media: status.available_media,
  });

  if (status.source_root && status.source_root.available === false) {
    els.statusText.textContent += " · " + text("status.sourceRootUnavailable");
  }
  updateJobActionButtons(state.jobRunning);
}

async function loadStatus() {
  const status = await fetchJson("/api/status");
  renderCatalogStatus(status);
}

function folderPathPrefixes(path) {
  const normalized = String(path || "").replaceAll("\\", "/").replace(/^\/+|\/+$/g, "");
  if (!normalized) return [];

  const parts = normalized.split("/").filter(Boolean);
  const result = [];

  for (let index = 1; index <= parts.length; index += 1) {
    result.push(parts.slice(0, index).join("/"));
  }

  return result;
}

function folderHasKnownChildren(folder) {
  return folder.is_disk_candidate !== true && Number(folder.direct?.folders || 0) > 0;
}

function folderTreeKind(folder) {
  if (folder.is_disk_candidate === true) return "disk";

  const direct = folder.direct || {};
  const mediaKinds = [];

  if (Number(direct.images || 0) > 0) mediaKinds.push("photos");
  if (Number(direct.videos || 0) > 0) mediaKinds.push("videos");
  if (Number(direct.gifs || 0) > 0) mediaKinds.push("gifs");
  if (Number(direct.other || 0) > 0) mediaKinds.push("other");

  if (mediaKinds.length > 1) return "mixed";
  if (mediaKinds.length === 1) return mediaKinds[0];
  if (folderHasKnownChildren(folder)) return "folder";
  return "empty";
}

function folderTreeIcon(kind) {
  return {
    folder: "📁",
    photos: "🖼️",
    videos: "🎬",
    gifs: "🌀",
    mixed: "🧩",
    other: "📄",
    empty: "□",
    disk: "⊕",
    root: "🏠",
  }[kind] || "📁";
}

function folderTreeMeta(kind) {
  return text(`tree.kind.${kind}`);
}

function treeNodePath(node) {
  return String(node?.dataset?.folderPath || "");
}

function treeNodeForPath(path) {
  const normalized = String(path || "");
  for (const node of els.folderList.querySelectorAll(".tree-node[data-folder-path]")) {
    if (treeNodePath(node) === normalized) return node;
  }
  return null;
}

function rememberTreeChildren(parentPath, data, options = {}) {
  const normalizedParent = String(parentPath || "");
  const replace = options.replace === true;
  let entry = state.treeKnownChildrenByParent.get(normalizedParent);
  if (!entry || replace) {
    entry = { folders: new Map() };
    state.treeKnownChildrenByParent.set(normalizedParent, entry);
  }

  for (const folder of data?.folders || []) {
    const { folder_previews: _folderPreviews, ...treeFolder } = folder;
    entry.folders.set(String(treeFolder.rel_path || ""), treeFolder);
  }
  entry.page = Number(data?.page || 1);
  entry.pageSize = Number(data?.page_size || 0);
  entry.pages = Number(data?.pages || 0);
  entry.total = Number(data?.total || 0);
  return entry;
}

function treeContainerFolderNodes(container) {
  return Array.from(container?.children || []).filter((child) => (
    child.classList?.contains("tree-node")
    && String(child.dataset?.folderPath || "") !== ""
  ));
}

function setTreeContainerMoreNote(container, visible) {
  if (!container) return;
  const notes = Array.from(container.children).filter((child) => child.classList?.contains("tree-more"));

  if (!visible) {
    for (const note of notes) note.remove();
    return;
  }

  if (notes.length === 0) {
    container.appendChild(treeMoreNote());
    return;
  }

  for (const note of notes.slice(1)) note.remove();
  container.appendChild(notes[0]);
}

function ensureTreeChildrenContainer(node) {
  if (node._treeChildrenElement) return node._treeChildrenElement;

  const children = document.createElement("div");
  children.className = "tree-children";
  const toggle = node.firstElementChild?.querySelector(".tree-toggle") || null;
  children.hidden = toggle?.getAttribute("aria-expanded") !== "true";
  node._treeChildrenElement = children;
  node.appendChild(children);
  return children;
}

function mergeTreeFolderNodes(container, folders, depth, options = {}) {
  if (!container) return;

  const folderItems = Array.from(folders || []);
  if (options.replace === true) {
    const desiredPaths = new Set(folderItems.map((folder) => String(folder.rel_path || "")));
    for (const node of treeContainerFolderNodes(container)) {
      if (!desiredPaths.has(treeNodePath(node))) node.remove();
    }
  }

  const existingByPath = new Map(
    treeContainerFolderNodes(container).map((node) => [treeNodePath(node), node]),
  );
  const moreNote = Array.from(container.children).find((child) => child.classList?.contains("tree-more")) || null;

  for (const folder of folderItems) {
    const path = String(folder.rel_path || "");
    const existing = existingByPath.get(path);
    if (existing) {
      existing._treeFolder = folder;
      continue;
    }

    const node = folderTreeNode(folder, depth);
    container.insertBefore(node, moreNote);
    existingByPath.set(path, node);
  }
}

function applyKnownTreeChildrenToNode(node) {
  if (!node) return null;
  const entry = state.treeKnownChildrenByParent.get(treeNodePath(node));
  if (!entry || entry.folders.size === 0) return node._treeChildrenElement || null;

  const children = ensureTreeChildrenContainer(node);
  mergeTreeFolderNodes(children, entry.folders.values(), Number(node._treeDepth || 0) + 1);
  return children;
}

function hydrateTreeBranchFromChildrenData(parentPath, data) {
  const normalizedParent = String(parentPath || "");
  const isCurrentParent = state.view === "folder" && state.folder === normalizedParent;
  const entry = rememberTreeChildren(normalizedParent, data, { replace: isCurrentParent });

  if (!state.treeLoaded && normalizedParent === "" && isCurrentParent) {
    renderRootTreePage(data);
    return;
  }
  if (!state.treeLoaded) return;

  if (normalizedParent === "") {
    mergeTreeFolderNodes(els.folderList, entry.folders.values(), 0, {
      replace: isCurrentParent,
    });
    setTreeContainerMoreNote(els.folderList, Number(data?.pages || 0) > 1);
    setTreeActiveFolder();
    return;
  }

  const parentNode = treeNodeForPath(normalizedParent);
  if (!parentNode) return;

  const children = ensureTreeChildrenContainer(parentNode);
  if (children) {
    mergeTreeFolderNodes(
      children,
      entry.folders.values(),
      Number(parentNode._treeDepth || 0) + 1,
      { replace: isCurrentParent },
    );
    setTreeContainerMoreNote(children, Number(data?.pages || 0) > 1);
  }
  if (isCurrentParent) parentNode._treeChildrenLoaded = true;
  if (state.view === "folder" && state.folder === normalizedParent) {
    setTreeNodeExpanded(parentNode, true);
  }
  setTreeActiveFolder();
}

function setTreeActiveFolder(path = state.folder) {
  const activePath = state.view === "folder" ? String(path || "") : null;
  for (const row of els.folderList.querySelectorAll(".tree-row[data-folder-path]")) {
    const isActive = activePath !== null && String(row.dataset.folderPath || "") === activePath;
    row.classList.toggle("active", isActive);
    if (isActive) {
      row.setAttribute("aria-current", "page");
    } else {
      row.removeAttribute("aria-current");
    }
  }
}

function treeNodeIsExpanded(node) {
  const toggle = node?.firstElementChild?.querySelector(".tree-toggle") || null;
  return toggle?.getAttribute("aria-expanded") === "true";
}

function setTreeNodeExpanded(node, expanded) {
  if (!node || node._treeHasChildren !== true) return;

  const children = node._treeChildrenElement || null;
  if (children) children.hidden = !expanded;

  const row = node.firstElementChild;
  const toggle = row?.querySelector(".tree-toggle") || null;
  if (toggle) {
    toggle.textContent = expanded ? "▾" : "▸";
    toggle.setAttribute("aria-label", text(expanded ? "tree.closeBranch" : "tree.openBranch"));
    toggle.setAttribute("aria-expanded", expanded ? "true" : "false");
  }
}

async function toggleTreeNode(node) {
  if (!node || node._treeHasChildren !== true) return;

  const navigationRequestId = state.viewLoadRequestId;
  if (treeNodeIsExpanded(node)) {
    node._treeManualCollapsedRequestId = navigationRequestId;
    setTreeNodeExpanded(node, false);
    return;
  }

  node._treeManualCollapsedRequestId = null;
  setTreeNodeExpanded(node, true);

  try {
    await ensureTreeNodeChildren(node);
  } catch (error) {
    if (node._treeManualCollapsedRequestId !== navigationRequestId) {
      setTreeNodeExpanded(node, false);
    }
    throw error;
  }

  // A second click may have collapsed the branch while its children were loading.
  // Do not reopen it after that delayed request completes.
  if (node._treeManualCollapsedRequestId !== navigationRequestId) {
    setTreeNodeExpanded(node, true);
  }
}

function collapseTreeOutsidePath(path) {
  const expandedPaths = new Set(folderPathPrefixes(path));
  for (const node of els.folderList.querySelectorAll(".tree-node[data-folder-path]")) {
    const nodePath = treeNodePath(node);
    if (!nodePath || expandedPaths.has(nodePath)) continue;
    setTreeNodeExpanded(node, false);
  }
}

async function ensureTreeNodeChildren(node) {
  if (!node || node._treeHasChildren !== true) return null;
  if (node._treeChildrenLoaded === true) return node._treeChildrenElement || null;
  if (node._treeChildrenPromise) return node._treeChildrenPromise;

  const folder = node._treeFolder;
  const depth = Number(node._treeDepth || 0);

  if (state.view === "folder" && sameCatalogPath(folder.rel_path, state.folder)) {
    const pending = state.currentFolderChildrenRequest;
    if (pending && sameCatalogPath(pending.parent, folder.rel_path)) {
      const data = await pending.promise;
      if (state.view === "folder" && sameCatalogPath(folder.rel_path, state.folder)) {
        hydrateTreeBranchFromChildrenData(folder.rel_path, data);
      }
    } else {
      const entry = state.treeKnownChildrenByParent.get(String(folder.rel_path || ""));
      if (entry) {
        const children = ensureTreeChildrenContainer(node);
        mergeTreeFolderNodes(children, entry.folders.values(), depth + 1, { replace: true });
        setTreeContainerMoreNote(children, Number(entry.pages || 0) > 1);
        node._treeChildrenLoaded = true;
      }
    }
    return node._treeChildrenElement || null;
  }

  node._treeChildrenPromise = (async () => {
    const childrenData = await fetchJson("/api/folders", {
      parent: folder.rel_path,
      page: 1,
      page_size: 200,
      include_previews: 0,
    });

    const entry = rememberTreeChildren(folder.rel_path, childrenData);
    const children = ensureTreeChildrenContainer(node);
    mergeTreeFolderNodes(children, entry.folders.values(), depth + 1);
    setTreeContainerMoreNote(children, childrenData.pages > 1);

    node._treeChildrenLoaded = true;
    return children;
  })();

  try {
    return await node._treeChildrenPromise;
  } finally {
    node._treeChildrenPromise = null;
  }
}

async function syncTreePath(path, options = {}) {
  if (!state.treeLoaded) return;

  const requestId = options.requestId ?? null;
  const targetPath = String(path || "");
  collapseTreeOutsidePath(targetPath);
  setTreeActiveFolder(targetPath);

  for (const prefix of folderPathPrefixes(targetPath)) {
    if (requestId !== null && requestId !== state.viewLoadRequestId) return;

    const node = treeNodeForPath(prefix);
    if (!node) return;

    const navigationRequestId = requestId ?? state.viewLoadRequestId;
    const manuallyCollapsed = node._treeManualCollapsedRequestId === navigationRequestId;

    // Reflect a new navigation target immediately. A manual collapse performed
    // during this same navigation request must remain respected.
    if (!manuallyCollapsed) setTreeNodeExpanded(node, true);
    await ensureTreeNodeChildren(node);
    if (requestId !== null && requestId !== state.viewLoadRequestId) return;

    if (node._treeManualCollapsedRequestId !== navigationRequestId) {
      setTreeNodeExpanded(node, true);
    }
    setTreeActiveFolder(targetPath);
  }
}

function invalidateFolderTree() {
  state.treeLoaded = false;
  state.treeLoadGeneration += 1;
  state.treeKnownChildrenByParent.clear();
}

async function loadRootFolders(options = {}) {
  const force = options.force === true;
  const requestId = options.requestId ?? null;

  if (state.treeLoaded && !force) {
    await syncTreePath(state.folder, { requestId });
    return;
  }

  if (state.treeLoadPromise && !force) {
    await state.treeLoadPromise;
    await syncTreePath(state.folder, { requestId });
    return;
  }

  const operation = diagnosticOperationStart("frontend.tree.load", {
    active_folder: state.folder,
    force,
  });
  const generation = ++state.treeLoadGeneration;

  const loadPromise = (async () => {
    const data = await fetchJson("/api/folders", {
      parent: "",
      page: 1,
      page_size: 200,
      include_previews: 0,
    });

    if (generation !== state.treeLoadGeneration) {
      diagnosticOperationEnd("frontend.tree.load", operation, { result: "stale" });
      return;
    }

    rememberTreeChildren("", data);
    renderRootTreePage(data);
    await syncTreePath(state.folder, { requestId });

    diagnosticOperationEnd("frontend.tree.load", operation, {
      result: "ok",
      root_folders: data.folders.length,
      pages: data.pages,
      tree_nodes: els.folderList.querySelectorAll(".tree-node").length,
      includes_previews: data.includes_previews === true,
    });
  })();

  state.treeLoadPromise = loadPromise;
  try {
    await loadPromise;
  } finally {
    if (state.treeLoadPromise === loadPromise) {
      state.treeLoadPromise = null;
    }
  }
}

function renderRootTreePage(data) {
  state.rootPage = Number(data?.page || 1);
  state.rootPages = Number(data?.pages || 0);
  state.rootTotal = Number(data?.total || 0);
  els.folderList.classList.add("folder-tree");
  els.rootPageInfo.textContent = text("pagination.folders", {
    total: state.rootTotal,
    page: state.rootPages ? state.rootPage : 0,
    pages: state.rootPages,
  });
  els.prevRootPage.disabled = true;
  els.nextRootPage.disabled = true;

  const fragment = document.createDocumentFragment();
  fragment.appendChild(folderTreeRootNode());
  const folders = Array.from(data?.folders || []).map((folder) => {
    const { folder_previews: _folderPreviews, ...treeFolder } = folder;
    return treeFolder;
  });

  if (folders.length === 0) {
    fragment.appendChild(emptyText(text("empty.rootFolders")));
  } else {
    for (const folder of folders) fragment.appendChild(folderTreeNode(folder, 0));
    if (state.rootPages > 1) fragment.appendChild(treeMoreNote());
  }

  els.folderList.replaceChildren(fragment);
  state.treeLoaded = true;
  setTreeActiveFolder();
}

function folderTreeRootNode() {
  const node = document.createElement("div");
  node.className = "tree-node";
  node.dataset.folderPath = "";

  const row = document.createElement("div");
  row.className = "tree-row tree-root-row";
  row.dataset.folderPath = "";
  row.style.setProperty("--tree-depth", "0");

  const spacer = document.createElement("span");
  spacer.className = "tree-toggle-spacer";
  row.appendChild(spacer);

  const icon = document.createElement("span");
  icon.className = "tree-node-icon tree-kind-root";
  icon.textContent = folderTreeIcon("root");
  icon.setAttribute("aria-hidden", "true");
  row.appendChild(icon);

  const main = document.createElement("button");
  main.type = "button";
  main.className = "tree-node-main";
  main.innerHTML = `<span class="tree-node-name">${escapeHtml(text("tree.root"))}</span>`;
  main.addEventListener("click", () => openFolder(""));
  row.appendChild(main);

  const meta = document.createElement("span");
  meta.className = "tree-node-meta";
  meta.textContent = text("tree.kind.root");
  row.appendChild(meta);

  node.appendChild(row);
  return node;
}

function folderTreeNode(folder, depth) {
  const isDiskCandidate = folder.is_disk_candidate === true;
  const hasChildren = folderHasKnownChildren(folder);
  const kind = folderTreeKind(folder);

  const node = document.createElement("div");
  node.className = "tree-node";
  node.dataset.folderPath = folder.rel_path;
  node._treeFolder = folder;
  node._treeDepth = depth;
  node._treeHasChildren = hasChildren;
  node._treeChildrenLoaded = false;
  node._treeChildrenElement = null;
  node._treeChildrenPromise = null;
  node._treeManualCollapsedRequestId = null;

  const row = document.createElement("div");
  row.className = `tree-row${hasChildren ? " branch" : " leaf"}${isDiskCandidate ? " disk-candidate" : ""}${folderFilesystemIsUsable(folder) ? "" : " folder-missing"}`;
  row.dataset.folderPath = folder.rel_path;
  row.style.setProperty("--tree-depth", String(depth));

  if (hasChildren) {
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "tree-toggle";
    toggle.textContent = "▸";
    toggle.setAttribute("aria-label", text("tree.openBranch"));
    toggle.setAttribute("aria-expanded", "false");
    toggle.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      reloadSafely(() => toggleTreeNode(node));
    });
    row.appendChild(toggle);
  } else {
    const spacer = document.createElement("span");
    spacer.className = "tree-toggle-spacer";
    row.appendChild(spacer);
  }

  const icon = document.createElement("span");
  icon.className = `tree-node-icon tree-kind-${kind}`;
  icon.textContent = folderTreeIcon(kind);
  icon.setAttribute("aria-hidden", "true");
  row.appendChild(icon);

  const main = document.createElement("button");
  main.type = "button";
  main.className = "tree-node-main";
  main.innerHTML = `<span class="tree-node-name">${escapeHtml(folder.name)}</span>`;
  main.addEventListener("click", () => {
    if (isDiskCandidate) {
      setMessage(text("jobs.diskRootCandidateOpen"), false);
      return;
    }

    openFolder(folder.rel_path);
  });
  row.appendChild(main);

  const meta = document.createElement("span");
  meta.className = "tree-node-meta";
  meta.textContent = folderFilesystemIsUsable(folder) ? folderTreeMeta(kind) : text("tree.kind.missing");
  row.appendChild(meta);

  node.appendChild(row);
  applyKnownTreeChildrenToNode(node);
  return node;
}

function treeMoreNote() {
  const note = document.createElement("div");
  note.className = "tree-more muted";
  note.textContent = text("tree.moreFolders");
  return note;
}

function folderCardWideRequiredWidth(card) {
  const layout = card.querySelector(".folder-card-layout");
  const main = card.querySelector(".folder-card-main");
  const preview = card.querySelector(".folder-card-preview");
  const previewStrip = card.querySelector(".folder-preview-strip");
  const insights = card.querySelector(".folder-card-insights");

  if (!layout || !main || !preview || !previewStrip || !insights) {
    return 0;
  }

  const style = window.getComputedStyle(layout);
  const columnGap = parseFloat(style.columnGap || style.gap || "0") || 0;
  const mainWidth = Math.max(main.scrollWidth, main.getBoundingClientRect().width, 300);
  const previewWidth = Math.max(previewStrip.scrollWidth, preview.getBoundingClientRect().width);
  const insightsWidth = Math.max(insights.scrollWidth, insights.getBoundingClientRect().width, 230);

  // Wide card structure: text | flexible spacer | previews | statistics.
  // The spacer may collapse, but the three content blocks and gaps must fit.
  return mainWidth + previewWidth + insightsWidth + columnGap * 3;
}

function folderCardWideLayoutFits() {
  const content = document.querySelector(".content");
  const cards = Array.from(document.querySelectorAll(".folder-card.has-folder-preview"));

  if (!content || !cards.length) {
    return true;
  }

  const availableWidth = content.clientWidth;
  const tolerance = 8;

  for (const card of cards) {
    const requiredWidth = folderCardWideRequiredWidth(card);

    if (requiredWidth && requiredWidth > availableWidth + tolerance) {
      return false;
    }
  }

  return true;
}

function contentAreaNeedsCompact() {
  const content = document.querySelector(".content");

  if (!content) {
    return false;
  }

  // The sidebar should disappear when it leaves too little usable width for
  // either folder cards or media galleries. This measures the real content
  // column after the sidebar is present, not the monitor or full viewport.
  const minimumUsableContentWidth = 1080;
  return content.clientWidth > 0 && content.clientWidth < minimumUsableContentWidth;
}

function updateResponsiveLayoutMode() {
  const body = document.body;
  if (!body) return;

  // Always test the full layout first. This makes compact mode depend on the
  // actual content column width instead of a fixed monitor-width breakpoint.
  body.classList.remove("layout-compact");

  if (contentAreaNeedsCompact() || !folderCardWideLayoutFits()) {
    body.classList.add("layout-compact");
  }
}

function scheduleResponsiveLayoutUpdate() {
  window.requestAnimationFrame(updateResponsiveLayoutMode);
}

function syncAppTopbarHeight() {
  const topbar = document.querySelector(".topbar");
  if (!topbar) return;

  const height = Math.ceil(topbar.getBoundingClientRect().height);
  if (height > 0) {
    document.documentElement.style.setProperty("--app-topbar-height", `${height}px`);
  }
}

function initializeAppShellLayout() {
  syncAppTopbarHeight();

  const topbar = document.querySelector(".topbar");
  if (topbar && "ResizeObserver" in window) {
    const observer = new ResizeObserver(() => {
      syncAppTopbarHeight();
      scheduleResponsiveLayoutUpdate();
    });
    observer.observe(topbar);
  }
}

function usesIndependentContentScroll() {
  return window.matchMedia
    && window.matchMedia("(min-width: 851px)").matches
    && !document.body.classList.contains("layout-compact");
}

function syncMediaTypeTabs() {
  document.querySelectorAll(".tab").forEach((item) => {
    item.classList.toggle("active", item.dataset.type === state.mediaType);
  });
}

function setMediaType(type) {
  state.mediaType = type || "all";
  syncMediaTypeTabs();
}

function beginViewLoadRequest() {
  state.viewLoadRequestId += 1;
  return state.viewLoadRequestId;
}

function currentViewLoadSnapshot() {
  return {
    view: state.view,
    folder: state.folder,
    mediaType: state.mediaType,
    mediaPage: state.mediaPage,
    childPage: state.childPage,
    searchQuery: state.searchQuery,
    searchFolder: state.searchFolder,
  };
}

function viewLoadIsCurrent(requestId, snapshot) {
  return requestId === state.viewLoadRequestId
    && snapshot.view === state.view
    && snapshot.folder === state.folder
    && snapshot.mediaType === state.mediaType
    && snapshot.mediaPage === state.mediaPage
    && snapshot.childPage === state.childPage
    && snapshot.searchQuery === state.searchQuery
    && snapshot.searchFolder === state.searchFolder;
}

async function refreshCurrentViewAndTree(options = {}) {
  const forceTree = options.forceTree === true;
  if (forceTree) invalidateFolderTree();

  const requestId = beginViewLoadRequest();
  setTreeActiveFolder();
  await loadCurrentFolder({ requestId });
  await loadRootFolders({ requestId });
}

async function openFolder(path) {
  const nextFolder = path || "";
  const operation = diagnosticOperationStart("frontend.navigation.folder", {
    from_folder: state.folder,
    to_folder: nextFolder,
    from_view: state.view,
  });
  const folderChanged = state.view !== "folder" || state.folder !== nextFolder;
  state.view = "folder";
  state.folder = nextFolder;
  if (folderChanged && state.mediaType !== "all") {
    setMediaType("all");
  }
  state.mediaPage = 1;
  state.childPage = 1;
  const requestId = beginViewLoadRequest();

  scrollToCatalogTop();
  setMessage("");
  updateViewButtons();
  setTreeActiveFolder(nextFolder);

  const contentRendered = await loadCurrentFolder({ requestId });
  await loadRootFolders({ requestId });
  diagnosticOperationEnd("frontend.navigation.folder", operation, {
    to_folder: state.folder,
    final_view: state.view,
    result: contentRendered === false ? "stale" : "ok",
    active_tree_nodes: els.folderList.querySelectorAll(".tree-row.active").length,
  });
}

async function openFavorites() {
  state.view = "favorites";
  state.mediaPage = 1;
  state.childPage = 1;
  const requestId = beginViewLoadRequest();
  setMessage("");
  updateViewButtons();
  setTreeActiveFolder();
  await loadCurrentFolder({ requestId });
}

async function loadCurrentFolder(options = {}) {
  const requestId = options.requestId ?? beginViewLoadRequest();
  const snapshot = currentViewLoadSnapshot();
  const operation = diagnosticOperationStart("frontend.folder.load_current", {
    request_id: requestId,
    requested_folder: snapshot.folder,
    requested_view: snapshot.view,
  });

  if (snapshot.view === "favorites") {
    const rendered = await loadFavoritesView({ requestId, snapshot });
    diagnosticOperationEnd("frontend.folder.load_current", operation, {
      resolved_view: "favorites",
      result: rendered ? "ok" : "stale",
    });
    return rendered;
  }

  if (snapshot.view === "search") {
    const rendered = await loadSearchView({ requestId, snapshot });
    diagnosticOperationEnd("frontend.folder.load_current", operation, {
      resolved_view: "search",
      result: rendered ? "ok" : "stale",
    });
    return rendered;
  }

  const childrenPromise = fetchJson("/api/folders", {
    parent: snapshot.folder,
    page: snapshot.childPage,
  });
  const childrenRequest = {
    parent: snapshot.folder,
    page: snapshot.childPage,
    promise: childrenPromise,
  };
  state.currentFolderChildrenRequest = childrenRequest;

  let folderData;
  let childrenData;
  let mediaData;
  try {
    [folderData, childrenData, mediaData] = await Promise.all([
      fetchJson("/api/folder", { path: snapshot.folder }),
      childrenPromise,
      fetchJson("/api/media", {
        folder: snapshot.folder,
        type: snapshot.mediaType,
        page: snapshot.mediaPage,
      }),
    ]);
  } finally {
    if (state.currentFolderChildrenRequest === childrenRequest) {
      state.currentFolderChildrenRequest = null;
    }
  }

  if (!viewLoadIsCurrent(requestId, snapshot)) {
    diagnosticOperationEnd("frontend.folder.load_current", operation, {
      resolved_view: "folder",
      result: "stale",
    });
    return false;
  }

  hydrateTreeBranchFromChildrenData(snapshot.folder, childrenData);
  renderFolder(folderData.folder, folderData.breadcrumb);
  renderChildFolders(childrenData);
  renderMedia(mediaData);
  diagnosticOperationEnd("frontend.folder.load_current", operation, {
    resolved_view: "folder",
    result: "ok",
    child_count: childrenData.folders.length,
    media_count: mediaData.media.length,
    breadcrumb_count: folderData.breadcrumb.length,
  });
  return true;
}

// Folder media pagination should refresh only the media grid, not folder header/children.
async function loadCurrentFolderMediaPage() {
  const requestId = beginViewLoadRequest();
  const snapshot = currentViewLoadSnapshot();

  if (snapshot.view !== "folder") {
    await loadCurrentFolder({ requestId });
    return;
  }

  const mediaData = await fetchJson("/api/media", {
    folder: snapshot.folder,
    type: snapshot.mediaType,
    page: snapshot.mediaPage,
  });
  if (!viewLoadIsCurrent(requestId, snapshot)) return;
  renderMedia(mediaData);
}

async function loadFavoritesView({ requestId, snapshot }) {
  const mediaData = await fetchJson("/api/favorites", {
    type: snapshot.mediaType,
    page: snapshot.mediaPage,
  });

  if (!viewLoadIsCurrent(requestId, snapshot)) return false;
  renderFavoritesHeader(mediaData);
  renderMedia(mediaData);
  return true;
}

async function startSearch() {
  const query = els.searchInput.value.trim();
  if (!query) {
    setMessage(text("search.emptyQuery"), true);
    return;
  }

  state.view = "search";
  state.searchQuery = query;
  state.searchFolder = els.searchInCurrentFolder.checked ? state.folder : "";
  state.mediaPage = 1;
  state.childPage = 1;
  const requestId = beginViewLoadRequest();
  setMessage("");
  updateViewButtons();
  setTreeActiveFolder();
  await loadCurrentFolder({ requestId });
}

async function loadSearchView({ requestId, snapshot }) {
  const data = await fetchJson("/api/search", {
    q: snapshot.searchQuery,
    folder: snapshot.searchFolder,
    type: snapshot.mediaType,
    page: snapshot.mediaPage,
    page_size: 50,
  });

  if (!viewLoadIsCurrent(requestId, snapshot)) return false;
  renderSearchHeader(data);
  renderSearchResults(data);
  return true;
}

function folderDisplayName(folder) {
  const relPath = String(folder?.rel_path || "");
  const depth = Number(folder?.depth || 0);
  if (!relPath && depth === 0) return text("app.root");
  return String(folder?.name || "");
}

function breadcrumbDisplayName(item) {
  const relPath = String(item?.rel_path || "");
  if (!relPath) return text("app.root");
  return String(item?.name || "");
}

function renderFolder(folder, breadcrumb) {
  const operation = diagnosticOperationStart("frontend.render.folder", {
    folder: folder?.rel_path || "",
    breadcrumb_count: breadcrumb.length,
  });
  state.currentFolder = folder;
  els.folderTitle.textContent = folderDisplayName(folder);
  if (els.currentFolderRename) {
    const canRename = folderCanBeRenamed(folder);
    els.currentFolderRename.hidden = !canRename;
    els.currentFolderRename.title = text("folderRename.editAction");
    els.currentFolderRename.setAttribute("aria-label", text("folderRename.editAction"));
  }
  els.folderCounts.replaceChildren();
  els.folderCounts.appendChild(countLine(text("count.directLabel"), directCountText(folder)));
  els.folderCounts.appendChild(countLine(text("count.recursiveLabel"), recursiveCountText(folder)));

  const folderProblem = folderFilesystemProblemText(folder);
  if (folderProblem) {
    const statusLine = document.createElement("div");
    statusLine.className = "folder-runtime-status";
    statusLine.textContent = folderProblem;
    els.folderCounts.appendChild(statusLine);
  }

  els.breadcrumb.replaceChildren();

  for (const item of breadcrumb) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = breadcrumbDisplayName(item);
    button.addEventListener("click", () => openFolder(item.rel_path));
    els.breadcrumb.appendChild(button);
  }

  updateJobActionButtons(state.jobRunning);
  diagnosticOperationEnd("frontend.render.folder", operation, {
    folder: folder?.rel_path || "",
  });
}

function renderFavoritesHeader(mediaData) {
  state.currentFolder = null;
  if (els.currentFolderRename) els.currentFolderRename.hidden = true;
  els.folderTitle.textContent = text("app.favorites");
  els.folderCounts.replaceChildren();
  els.folderCounts.appendChild(countLine(text("count.recursiveLabel"), text("count.favoritesTotal", { total: mediaData.total })));

  els.breadcrumb.replaceChildren();
  const rootButton = document.createElement("button");
  rootButton.type = "button";
  rootButton.textContent = text("app.root");
  rootButton.addEventListener("click", () => openFolder(""));
  els.breadcrumb.appendChild(rootButton);

  const favoritesButton = document.createElement("button");
  favoritesButton.type = "button";
  favoritesButton.textContent = text("app.favorites");
  favoritesButton.disabled = true;
  els.breadcrumb.appendChild(favoritesButton);

  els.childFolders.replaceChildren();
  resetChildPager();
  resetChildFoldersCollapseUi();
  setChildFoldersSectionVisible(false);
  updateJobActionButtons(state.jobRunning);
}


function clampPageNumber(page, pages) {
  const safePages = Math.max(0, Number(pages) || 0);
  if (safePages <= 0) return 1;

  const safePage = Math.trunc(Number(page));
  if (!Number.isFinite(safePage)) return 1;

  return Math.max(1, Math.min(safePage, safePages));
}

function updateMediaPager(data) {
  const page = clampPageNumber(data.page || 1, data.pages || 0);
  const pages = Math.max(0, Number(data.pages) || 0);
  const hasPages = pages > 0;

  state.mediaPages = pages;
  els.pageInfo.textContent = text("pagination.items", {
    total: data.total,
    page: hasPages ? page : 0,
    pages,
  });

  els.firstPage.disabled = !hasPages || page <= 1;
  els.prevPage.disabled = !hasPages || page <= 1;
  els.nextPage.disabled = !hasPages || page >= pages;
  els.lastPage.disabled = !hasPages || page >= pages;

  els.pageJumpInput.disabled = !hasPages;
  els.pageJumpInput.min = "1";
  els.pageJumpInput.max = hasPages ? String(pages) : "1";
  els.pageJumpInput.value = hasPages ? String(page) : "";
}

async function goToMediaPage(page) {
  if (state.mediaPages <= 0) return;

  const targetPage = clampPageNumber(page, state.mediaPages);
  if (targetPage === state.mediaPage) {
    els.pageJumpInput.value = String(targetPage);
    return;
  }

  state.mediaPage = targetPage;
  scrollToCatalogTop();
  await reloadSafely(state.view === "folder" ? loadCurrentFolderMediaPage : loadCurrentFolder);
}

function setChildPagerVisible(visible) {
  if (els.childPager) {
    els.childPager.hidden = !visible;
  }
}

function resetChildPager() {
  state.childPages = 0;
  els.childPageInfo.textContent = "";
  setChildPagerVisible(false);
  els.firstChildPage.disabled = true;
  els.prevChildPage.disabled = true;
  els.nextChildPage.disabled = true;
  els.lastChildPage.disabled = true;
  els.childPageJumpInput.disabled = true;
  els.childPageJumpInput.value = "";
}

function updateChildPager(data) {
  const page = clampPageNumber(data.page || 1, data.pages || 0);
  const pages = Math.max(0, Number(data.pages) || 0);
  const hasPages = pages > 0;

  state.childPages = pages;
  state.childPageSize = Math.max(0, Number(data.page_size) || 0);
  els.childPageInfo.textContent = text("pagination.folders", {
    total: data.total,
    page: hasPages ? page : 0,
    pages,
  });

  setChildPagerVisible(hasPages);
  els.firstChildPage.disabled = !hasPages || page <= 1;
  els.prevChildPage.disabled = !hasPages || page <= 1;
  els.nextChildPage.disabled = !hasPages || page >= pages;
  els.lastChildPage.disabled = !hasPages || page >= pages;

  els.childPageJumpInput.disabled = !hasPages;
  els.childPageJumpInput.min = "1";
  els.childPageJumpInput.max = hasPages ? String(pages) : "1";
  els.childPageJumpInput.value = hasPages ? String(page) : "";
}

async function goToChildPage(page) {
  if (state.childPages <= 0) return;

  const targetPage = clampPageNumber(page, state.childPages);
  if (targetPage === state.childPage) {
    els.childPageJumpInput.value = String(targetPage);
    return;
  }

  state.childPage = targetPage;
  scrollToCatalogTop();
  await reloadSafely(loadCurrentFolder);
}

function renderSearchHeader(data) {
  state.currentFolder = null;
  if (els.currentFolderRename) els.currentFolderRename.hidden = true;
  els.folderTitle.textContent = data.folder
    ? text("search.titleScoped", { query: data.query, folder: data.folder })
    : text("search.title", { query: data.query });
  els.folderCounts.replaceChildren();
  els.folderCounts.appendChild(countLine(text("count.recursiveLabel"), text("search.summary", {
    total: data.total,
    folders: data.counts.folders,
    media: data.counts.media,
  })));

  els.breadcrumb.replaceChildren();
  const rootButton = document.createElement("button");
  rootButton.type = "button";
  rootButton.textContent = text("app.root");
  rootButton.addEventListener("click", () => openFolder(""));
  els.breadcrumb.appendChild(rootButton);

  const searchButton = document.createElement("button");
  searchButton.type = "button";
  searchButton.textContent = text("app.search");
  searchButton.disabled = true;
  els.breadcrumb.appendChild(searchButton);

  resetChildPager();
  updateJobActionButtons(state.jobRunning);
}

function renderSearchResults(data) {
  const folderResults = data.results.filter((item) => item.kind === "folder");
  const mediaResults = data.results.filter((item) => item.kind === "media");

  els.childFolders.replaceChildren();
  resetChildFoldersCollapseUi();

  if (folderResults.length === 0) {
    els.childPageInfo.textContent = "";
    setChildPagerVisible(false);
    setChildFoldersSectionVisible(false);
  } else {
    els.childPageInfo.textContent = text("search.childFolders");
    setChildPagerVisible(false);
    setChildFoldersSectionVisible(true);
    for (const folder of folderResults) {
      els.childFolders.appendChild(folderResultCard(folder));
    }
  }

  setMediaSectionVisible(true);
  resetMediaThumbnailLazyLoading();
  els.mediaList.replaceChildren();
  const sectionTitle = mediaSectionTitle(data.type);
  els.mediaTitle.textContent = text("search.title", { query: data.query }) + " – " + sectionTitle;
  updateMediaPager(data);

  if (mediaResults.length === 0) {
    els.mediaList.appendChild(emptyText(data.total === 0 ? text("empty.search") : text("empty.searchMedia")));
    return;
  }

  for (const media of mediaResults) {
    els.mediaList.appendChild(mediaCard(
      media,
      { view: "search", query: data.query, searchFolder: data.folder, type: data.type, page: data.page, pageSize: data.page_size, pages: data.pages, total: data.total },
    ));
  }
  bindMediaThumbnailLazyLoading(els.mediaList);
  diagnosticOperationEnd("frontend.render.media", operation, {
    rendered: data.media.length,
    media_cards: els.mediaList.children.length,
  });
}

function folderResultCard(folder) {
  const card = document.createElement("article");
  card.className = "card";
  card.innerHTML = `
    <div class="row-title">${escapeHtml(folder.name)}</div>
    <div class="row-path">${escapeHtml(folder.rel_path)}</div>
    <div class="row-meta"><strong>${escapeHtml(text("count.directLabel"))}:</strong> ${escapeHtml(directCountText(folder))}</div>
    <div class="row-meta"><strong>${escapeHtml(text("count.recursiveLabel"))}:</strong> ${escapeHtml(recursiveCountText(folder))}</div>
  `;
  card.addEventListener("click", () => openFolder(folder.rel_path));
  return card;
}

function countLine(label, lineText) {
  const line = document.createElement("span");
  line.textContent = `${label}: ${lineText}`;
  return line;
}

function folderPreviewUrl(preview) {
  return apiUrl("/media/thumbnail", {
    path: preview.rel_path,
    variant: preview.thumbnail_type,
    existing_only: "1",
  });
}

function folderPreviewMarkup(folder) {
  const previews = Array.isArray(folder.folder_previews) ? folder.folder_previews : [];
  if (!previews.length) return "";

  return `
    <div class="folder-preview-strip" aria-label="${escapeHtml(text("folderPreview.stripLabel"))}">
      ${previews.map((preview) => `
        <div class="folder-preview-thumb" data-thumbnail-preview>
          <img
            class="folder-preview-image"
            src="${escapeHtml(folderPreviewUrl(preview))}"
            alt=""
            loading="lazy"
            decoding="async"
          >
        </div>
      `).join("")}
    </div>
  `;
}

function bindFolderPreviewImageErrors(card) {
  for (const image of card.querySelectorAll("img.folder-preview-image")) {
    const box = image.closest("[data-thumbnail-preview]");
    if (!box) continue;

    image.addEventListener("error", () => {
      box.classList.add("thumbnail-failed");
      image.removeAttribute("src");
    }, { once: true });
  }
}

function setChildFoldersSectionVisible(visible) {
  const section = els.childFolders ? els.childFolders.closest("section") : null;
  if (section) {
    section.hidden = !visible;
  }
}

function childFoldersCollapseKey() {
  return String(state.folder || "");
}

function areChildFoldersCollapsed() {
  return state.collapsedChildFoldersByFolder[childFoldersCollapseKey()] === true;
}

function setChildFoldersToggleVisible(visible) {
  if (els.childFoldersToggle) {
    els.childFoldersToggle.hidden = !visible;
  }
}

function setChildFoldersExpandedInDom(expanded) {
  if (els.childFolders) {
    els.childFolders.hidden = !expanded;
  }
}

function updateChildFoldersCollapseState() {
  const collapsed = areChildFoldersCollapsed();

  setChildFoldersExpandedInDom(!collapsed);

  if (!els.childFoldersToggle) return;

  els.childFoldersToggle.textContent = collapsed ? "▸" : "▾";
  els.childFoldersToggle.classList.toggle("is-collapsed", collapsed);
  els.childFoldersToggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
  els.childFoldersToggle.setAttribute("aria-label", collapsed
    ? text("actions.expandSubfolders")
    : text("actions.collapseSubfolders"));
  els.childFoldersToggle.title = collapsed
    ? text("actions.expandSubfolders")
    : text("actions.collapseSubfolders");
}

function setChildFoldersCollapsed(collapsed) {
  state.collapsedChildFoldersByFolder[childFoldersCollapseKey()] = collapsed === true;
  updateChildFoldersCollapseState();
  scheduleResponsiveLayoutUpdate();
}

function resetChildFoldersCollapseUi() {
  setChildFoldersToggleVisible(false);
  setChildFoldersExpandedInDom(true);
}

function setMediaSectionVisible(visible) {
  const section = els.mediaList ? els.mediaList.closest("section") : null;
  if (section) {
    section.hidden = !visible;
  }
}

function folderDirectMediaCount(folder) {
  const direct = folder && folder.direct ? folder.direct : {};
  return (Number(direct.images) || 0)
    + (Number(direct.gifs) || 0)
    + (Number(direct.videos) || 0)
    + (Number(direct.other) || 0);
}

function folderVisualMediaCount(folder) {
  const direct = folder && folder.direct ? folder.direct : {};
  const recursive = folder && folder.recursive ? folder.recursive : {};
  return (Number(direct.images) || 0)
    + (Number(direct.gifs) || 0)
    + (Number(direct.videos) || 0)
    + (Number(recursive.images) || 0)
    + (Number(recursive.gifs) || 0)
    + (Number(recursive.videos) || 0);
}

function folderCanGeneratePreviews(folder) {
  if (!folder) return false;
  if (folder.is_disk_candidate === true) return false;
  if (!folderFilesystemIsUsable(folder)) return false;
  return folderVisualMediaCount(folder) > 0;
}

function shouldHideMediaSection(data) {
  if (state.view !== "folder") return false;

  const folder = state.currentFolder || (data ? data.folder : null);
  return folderDirectMediaCount(folder) === 0;
}

function renderChildFolders(data) {
  const operation = diagnosticOperationStart("frontend.render.child_folders", {
    count: data.folders.length,
    page: data.page,
  });
  els.childFolders.replaceChildren();
  updateChildPager(data);

  if (data.folders.length === 0) {
    els.childPageInfo.textContent = "";
    setChildPagerVisible(false);
    resetChildFoldersCollapseUi();
    setChildFoldersSectionVisible(false);
    scheduleResponsiveLayoutUpdate();
    diagnosticOperationEnd("frontend.render.child_folders", operation, { rendered: 0 });
    return;
  }

  setChildFoldersSectionVisible(true);
  setChildFoldersToggleVisible(true);

  for (const folder of data.folders) {
    const card = document.createElement("article");
    const previewHtml = folderPreviewMarkup(folder);
    card.className = `card folder-card${previewHtml ? " has-folder-preview" : ""}${folderFilesystemIsUsable(folder) ? "" : " folder-missing"}`;
    card.innerHTML = `
      <div class="folder-card-layout">
        <div class="folder-card-main">
          <div class="folder-card-title-row">
            <div class="row-title folder-card-title">${escapeHtml(folder.name)}</div>
            ${folderCanBeRenamed(folder) ? folderRenameIconMarkup() : ""}
          </div>
          <div class="folder-card-primary-meta">${escapeHtml(folderPrimarySummary(folder))}</div>
          ${folderFilesystemProblemMarkup(folder)}
          <div class="folder-card-detail-meta">
            <div><strong>${escapeHtml(text("count.directLabel"))}:</strong> ${escapeHtml(directCountText(folder))}</div>
            <div><strong>${escapeHtml(text("count.recursiveLabel"))}:</strong> ${escapeHtml(recursiveCountText(folder))}</div>
          </div>
          <div class="folder-card-actions"></div>
        </div>
        ${previewHtml ? `<div class="folder-card-preview">${previewHtml}</div>` : ""}
        ${folderInsightMarkup(folder)}
      </div>
    `;
    const actions = card.querySelector(".folder-card-actions");
    actions.appendChild(folderSystemOpenButton(folder.rel_path, folder));
    const moreMenu = folderMoreMenu(folder.rel_path, folder, { includeUpdate: true });
    if (moreMenu) {
      actions.appendChild(moreMenu);
    }
    const folderRenameButton = card.querySelector('[data-action="rename-folder"]');
    if (folderRenameButton) {
      folderRenameButton.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        openFolderRenameModal(folder);
      });
    }
    bindFolderPreviewImageErrors(card);
    card.addEventListener("click", () => openFolder(folder.rel_path));
    els.childFolders.appendChild(card);
  }

  updateChildFoldersCollapseState();
  scheduleResponsiveLayoutUpdate();
  diagnosticOperationEnd("frontend.render.child_folders", operation, {
    rendered: data.folders.length,
  });
}

function renderMedia(data) {
  const operation = diagnosticOperationStart("frontend.render.media", {
    count: data.media.length,
    page: data.page,
    type: data.type,
  });
  resetMediaThumbnailLazyLoading();
  els.mediaList.replaceChildren();
  const sectionTitle = mediaSectionTitle(data.type);
  els.mediaTitle.textContent = state.view === "favorites"
    ? text("mediaSection.favorites", { section: sectionTitle })
    : sectionTitle;
  updateMediaPager(data);

  if (shouldHideMediaSection(data)) {
    setMediaSectionVisible(false);
    scheduleResponsiveLayoutUpdate();
    diagnosticOperationEnd("frontend.render.media", operation, { rendered: 0, hidden: true });
    return;
  }

  setMediaSectionVisible(true);

  if (data.media.length === 0) {
    els.mediaList.appendChild(emptyText(state.view === "favorites" ? text("empty.favorites") : text("empty.media")));
    diagnosticOperationEnd("frontend.render.media", operation, { rendered: 0, hidden: false });
    return;
  }

  const renderContext = {
    view: state.view,
    folder: data.folder ? data.folder.rel_path : state.folder,
    type: data.type,
    page: data.page,
    pageSize: data.page_size,
    pages: data.pages,
    total: data.total,
  };

  for (const media of data.media) {
    els.mediaList.appendChild(mediaCard(media, renderContext));
  }
  bindMediaThumbnailLazyLoading(els.mediaList);
}

function supportsGifHoverPreview() {
  return window.matchMedia
    && window.matchMedia("(hover: hover) and (pointer: fine)").matches;
}

function bindGifHoverPreview(preview, originalUrl) {
  if (!preview || !originalUrl || !supportsGifHoverPreview()) return;

  const image = preview.querySelector("img.media-thumbnail--gif-preview");
  if (!image) return;

  const staticSrc = mediaThumbnailUrl(image);
  if (!staticSrc) return;

  const hoverDelayMs = 350;
  let hoverTimer = 0;

  const clearHoverTimer = () => {
    if (hoverTimer) {
      window.clearTimeout(hoverTimer);
      hoverTimer = 0;
    }
  };

  preview.addEventListener("pointerenter", (event) => {
    if (event.pointerType && event.pointerType !== "mouse") return;
    if (preview.classList.contains("thumbnail-failed")) return;

    clearHoverTimer();
    hoverTimer = window.setTimeout(() => {
      hoverTimer = 0;
      image.src = originalUrl;
      preview.classList.add("gif-hover-active");
    }, hoverDelayMs);
  });

  preview.addEventListener("pointerleave", () => {
    clearHoverTimer();
    if (image.getAttribute("src") !== staticSrc) {
      image.src = staticSrc;
    }
    if (image.getAttribute("src") === staticSrc) {
      delete image.dataset.lazyThumbnailSrc;
    }
    preview.classList.remove("gif-hover-active");
  });
}

function bindVideoHoverPreview(preview) {
  if (!preview || !supportsGifHoverPreview()) return;
  if (preview.dataset.videoHoverPreviewBound === "1") return;
  preview.dataset.videoHoverPreviewBound = "1";
  if (preview.classList.contains("thumbnail-failed")) return;

  const hoverImage = preview.querySelector("img.video-hover-frame-image");
  if (!hoverImage) return;

  const frameUrls = [1, 2, 3, 4]
    .map((frameIndex) => String(preview.dataset[`videoHoverFrame${frameIndex}`] || ""))
    .filter(Boolean);
  if (!frameUrls.length) return;

  const hoverDelayMs = 250;
  const frameIntervalMs = 650;
  const failedFrameUrls = new Set();
  let hoverTimer = 0;
  let frameTimer = 0;
  let frameIndex = 0;
  let active = false;

  const clearTimers = () => {
    if (hoverTimer) {
      window.clearTimeout(hoverTimer);
      hoverTimer = 0;
    }
    if (frameTimer) {
      window.clearInterval(frameTimer);
      frameTimer = 0;
    }
  };

  const availableFrameUrls = () => frameUrls.filter((url) => !failedFrameUrls.has(url));

  const stopHover = () => {
    active = false;
    clearTimers();
    hoverImage.removeAttribute("src");
    preview.classList.remove("video-hover-active");
    activeVideoHoverPreviewElements.delete(preview);
  };

  preview.__catalog2StopVideoHoverPreview = stopHover;

  const showNextFrame = () => {
    if (!active) return;
    const urls = availableFrameUrls();
    if (!urls.length) {
      stopHover();
      return;
    }
    const url = urls[frameIndex % urls.length];
    frameIndex += 1;
    hoverImage.src = url;
  };

  hoverImage.addEventListener("load", () => {
    if (!active || !hoverImage.getAttribute("src")) return;
    preview.classList.add("video-hover-active");
  });

  hoverImage.addEventListener("error", () => {
    const failedUrl = hoverImage.getAttribute("src") || "";
    if (failedUrl) failedFrameUrls.add(failedUrl);
    preview.classList.remove("video-hover-active");
    if (active) window.requestAnimationFrame(showNextFrame);
  });

  preview.addEventListener("pointerenter", (event) => {
    if (event.pointerType && event.pointerType !== "mouse") return;
    if (preview.classList.contains("thumbnail-failed")) return;

    clearTimers();
    hoverTimer = window.setTimeout(() => {
      hoverTimer = 0;
      active = true;
      activeVideoHoverPreviewElements.add(preview);
      frameIndex = 0;
      showNextFrame();
      frameTimer = window.setInterval(showNextFrame, frameIntervalMs);
    }, hoverDelayMs);
  });

  preview.addEventListener("pointerleave", stopHover);
}

function configureFolderImageModalPaging(element, media, renderContext) {
  if (!element || !media || !renderContext) return;

  const isFolderImage = renderContext.view === "folder"
    && (media.media_type === "image" || media.media_type === "gif");

  if (!isFolderImage) return;

  const isTypedView = renderContext.type === media.media_type;
  const isAllView = renderContext.type === "all";

  if (!isTypedView && !isAllView) return;

  element.dataset.imageModalPaging = "folder-media";
  element.dataset.imageModalFolder = String(renderContext.folder || "");
  element.dataset.imageModalType = String(media.media_type || "");
  element.dataset.imageModalAnchorPath = String(media.rel_path || "");

  if (isTypedView) {
    element.dataset.imageModalPage = String(renderContext.page || 1);
    element.dataset.imageModalPageSize = String(renderContext.pageSize || 1);
    element.dataset.imageModalPages = String(renderContext.pages || 1);
    element.dataset.imageModalTotal = String(renderContext.total || 1);
    return;
  }

  // Mixed "Vše" view: the visible UI page is not the same as the logical
  // image/GIF gallery page. Resolve the correct typed page from the clicked
  // rel_path before opening the modal.
  element.dataset.imageModalAnchorRequired = "1";
}


function configureSearchImageModalPaging(element, media, renderContext) {
  if (!element || !media || !renderContext) return;

  const isSearchImage = renderContext.view === "search"
    && (media.media_type === "image" || media.media_type === "gif");

  if (!isSearchImage) return;

  const isTypedView = renderContext.type === media.media_type;
  const isAllView = renderContext.type === "all";

  if (!isTypedView && !isAllView) return;

  // Search result pages can contain folders and multiple media types. The modal
  // therefore resolves the logical image/GIF-only search page from the clicked
  // rel_path before opening.
  element.dataset.imageModalPaging = "search-media";
  element.dataset.imageModalQuery = String(renderContext.query || "");
  element.dataset.imageModalFolder = String(renderContext.searchFolder || "");
  element.dataset.imageModalType = String(media.media_type || "");
  element.dataset.imageModalAnchorPath = String(media.rel_path || "");
  element.dataset.imageModalPage = String(renderContext.page || 1);
  element.dataset.imageModalPageSize = String(renderContext.pageSize || 1);
  element.dataset.imageModalAnchorRequired = "1";
}

function configureFavoritesImageModalPaging(element, media, renderContext) {
  if (!element || !media || !renderContext) return;

  const isFavoriteImage = renderContext.view === "favorites"
    && (media.media_type === "image" || media.media_type === "gif");

  if (!isFavoriteImage) return;

  const isTypedView = renderContext.type === media.media_type;
  const isAllView = renderContext.type === "all";

  if (!isTypedView && !isAllView) return;

  // Favorites can contain multiple media types and unavailable historical items.
  // The modal resolves the available image/GIF-only favorite page from the
  // clicked rel_path before opening. This keeps the modal in one media type.
  element.dataset.imageModalPaging = "favorites-media";
  element.dataset.imageModalType = String(media.media_type || "");
  element.dataset.imageModalAnchorPath = String(media.rel_path || "");
  element.dataset.imageModalPage = String(renderContext.page || 1);
  element.dataset.imageModalPageSize = String(renderContext.pageSize || 1);
  element.dataset.imageModalAnchorRequired = "1";
}

function configureImageModalFavorite(element, media) {
  if (!element || !media) return;
  if (media.media_type !== "image" && media.media_type !== "gif") return;

  element.dataset.imageModalPath = String(media.rel_path || "");
  element.dataset.imageModalType = String(media.media_type || "");
  element.dataset.imageModalFavorite = media.is_favorite ? "1" : "0";
}

function configureImageModalPaging(element, media, renderContext) {
  configureFolderImageModalPaging(element, media, renderContext);
  configureSearchImageModalPaging(element, media, renderContext);
  configureFavoritesImageModalPaging(element, media, renderContext);
}

function editIconSvgMarkup() {
  return `
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M5 19h14v-6h-2v4H7V7h4V5H5v14z"></path>
      <path d="M14.2 5.8l4 4-7.9 7.9H6.3v-4l7.9-7.9zm1.4-1.4l1.1-1.1a1.4 1.4 0 0 1 2 0l2 2a1.4 1.4 0 0 1 0 2l-1.1 1.1-4-4z"></path>
    </svg>
  `;
}

function folderCanBeRenamed(folder) {
  if (!folder) return false;
  if (folder.is_disk_candidate === true) return false;
  if (String(folder.rel_path || "") === "") return false;
  if (Number(folder.id) <= 0) return false;
  if (Number(folder.depth) <= 0) return false;
  return folderFilesystemIsUsable(folder);
}

function folderRenameIconMarkup() {
  return `<button type="button" class="media-rename-icon folder-rename-icon" data-action="rename-folder" title="${escapeHtml(text("folderRename.editAction"))}" aria-label="${escapeHtml(text("folderRename.editAction"))}">${editIconSvgMarkup()}</button>`;
}

function ensureFolderRenameModal() {
  let modal = document.getElementById("folderRenameModal");
  if (modal) return modal;

  modal = document.createElement("div");
  modal.id = "folderRenameModal";
  modal.className = "media-rename-modal folder-rename-modal";
  modal.setAttribute("aria-hidden", "true");
  modal.innerHTML = `
    <button class="media-rename-backdrop" type="button" data-folder-rename-close data-aria-label="folderRename.close" aria-label="${escapeHtml(text("folderRename.close"))}"></button>
    <section class="media-rename-panel" role="dialog" aria-modal="true" aria-labelledby="folderRenameModalTitle">
      <header class="media-rename-bar">
        <div>
          <h2 id="folderRenameModalTitle" data-text="folderRename.modalTitle">${escapeHtml(text("folderRename.modalTitle"))}</h2>
          <p class="muted" data-text="folderRename.modalSubtitle">${escapeHtml(text("folderRename.modalSubtitle"))}</p>
        </div>
        <button id="folderRenameClose" type="button" data-folder-rename-close data-text="folderRename.close">${escapeHtml(text("folderRename.close"))}</button>
      </header>
      <div class="media-rename-body">
        <section class="media-rename-explain">
          <h3 data-text="folderRename.whatTitle">${escapeHtml(text("folderRename.whatTitle"))}</h3>
          <p data-text="folderRename.whatText">${escapeHtml(text("folderRename.whatText"))}</p>
        </section>
        <dl class="media-rename-current">
          <dt data-text="folderRename.currentName">${escapeHtml(text("folderRename.currentName"))}</dt>
          <dd id="folderRenameCurrentName"></dd>
          <dt data-text="folderRename.currentPath">${escapeHtml(text("folderRename.currentPath"))}</dt>
          <dd id="folderRenameCurrentPath"></dd>
        </dl>
        <form id="folderRenameForm" class="media-rename-form" novalidate>
          <label for="folderRenameInput" data-text="folderRename.newName">${escapeHtml(text("folderRename.newName"))}</label>
          <div class="media-rename-input-row">
            <input id="folderRenameInput" type="text" autocomplete="off" spellcheck="false">
            <button id="folderRenameVerify" type="submit" data-text="folderRename.verify">${escapeHtml(text("folderRename.verify"))}</button>
          </div>
        </form>
        <div id="folderRenamePlanResult" class="media-rename-plan-result"></div>
      </div>
      <footer class="media-rename-footer">
        <button type="button" data-folder-rename-close data-text="folderRename.close">${escapeHtml(text("folderRename.close"))}</button>
        <button id="folderRenameExecute" class="write-action" type="button" data-text="folderRename.execute" disabled>${escapeHtml(text("folderRename.execute"))}</button>
      </footer>
    </section>
  `;
  document.body.appendChild(modal);

  for (const closeButton of modal.querySelectorAll("[data-folder-rename-close]")) {
    closeButton.addEventListener("click", closeFolderRenameModal);
  }

  const form = modal.querySelector("#folderRenameForm");
  if (form) {
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      verifyFolderRenamePlan();
    });
  }

  const input = modal.querySelector("#folderRenameInput");
  if (input) {
    input.addEventListener("input", () => {
      if (state.folderRename) state.folderRename.plan = null;
      renderFolderRenamePlan(null);
    });
  }

  const executeButton = modal.querySelector("#folderRenameExecute");
  if (executeButton) {
    executeButton.addEventListener("click", executeFolderRename);
  }

  return modal;
}

function openFolderRenameModal(folder) {
  if (!folderCanBeRenamed(folder)) return;
  const modal = ensureFolderRenameModal();
  state.folderRename = { folder, plan: null, busy: false };

  const currentName = modal.querySelector("#folderRenameCurrentName");
  const currentPath = modal.querySelector("#folderRenameCurrentPath");
  const input = modal.querySelector("#folderRenameInput");
  if (currentName) currentName.textContent = folder.name || "";
  if (currentPath) currentPath.textContent = folder.rel_path || text("app.root");
  if (input) input.value = folder.name || "";

  renderFolderRenamePlan(null);
  modal.classList.add("show");
  modal.setAttribute("aria-hidden", "false");
  document.body.classList.add("media-rename-modal-open");
  if (input) {
    input.focus();
    input.setSelectionRange(0, input.value.length);
  }
}

function closeFolderRenameModal() {
  const modal = document.getElementById("folderRenameModal");
  if (!modal) return;
  modal.classList.remove("show");
  modal.setAttribute("aria-hidden", "true");
  document.body.classList.remove("media-rename-modal-open");
  state.folderRename = null;
}

function folderRenameInputValue() {
  const modal = ensureFolderRenameModal();
  const input = modal.querySelector("#folderRenameInput");
  return input ? input.value.trim() : "";
}

function setFolderRenameBusy(isBusy, mode = "") {
  const modal = ensureFolderRenameModal();
  const verifyButton = modal.querySelector("#folderRenameVerify");
  const executeButton = modal.querySelector("#folderRenameExecute");
  const input = modal.querySelector("#folderRenameInput");
  if (state.folderRename) state.folderRename.busy = isBusy;
  if (input) input.disabled = isBusy;
  if (verifyButton) {
    verifyButton.disabled = isBusy;
    verifyButton.textContent = isBusy && mode === "verify" ? text("folderRename.verifying") : text("folderRename.verify");
  }
  if (executeButton) {
    executeButton.disabled = isBusy || !(state.folderRename?.plan?.can_execute === true);
    executeButton.textContent = isBusy && mode === "execute" ? text("folderRename.executing") : text("folderRename.execute");
  }
}

async function verifyFolderRenamePlan() {
  if (!state.folderRename?.folder) return;
  const folder = state.folderRename.folder;
  const newName = folderRenameInputValue();
  setFolderRenameBusy(true, "verify");
  try {
    const payload = await postJson("/api/folders/rename/plan", {
      folder_id: folder.id,
      new_name: newName,
    });
    state.folderRename.plan = payload.plan || null;
    renderFolderRenamePlan(state.folderRename.plan);
  } catch (error) {
    state.folderRename.plan = null;
    renderFolderRenameError(error.message || text("folderRename.planError"));
  } finally {
    setFolderRenameBusy(false);
  }
}

async function executeFolderRename() {
  if (!state.folderRename?.folder || !state.folderRename?.plan?.can_execute) return;
  const folder = state.folderRename.folder;
  const oldPath = folder.rel_path || "";
  const newName = folderRenameInputValue();
  setFolderRenameBusy(true, "execute");
  try {
    const payload = await postJson("/api/folders/rename/execute", {
      folder_id: folder.id,
      new_name: newName,
    });
    if (payload.executed !== true) {
      state.folderRename.plan = payload.plan || null;
      renderFolderRenamePlan(state.folderRename.plan, {
        notice: firstResultMessage(payload) || text("folderRename.executeBlocked"),
        noticeClass: "warning",
      });
      return;
    }
    setMessage(text("folderRename.executeDone"));
    closeFolderRenameModal();
    if (state.view === "folder" && state.folder === oldPath) {
      state.folder = payload.new_path || state.folder;
    }
    await refreshCurrentViewAndTree({ forceTree: true });
  } catch (error) {
    renderFolderRenameError(error.message || text("folderRename.executeError"));
  } finally {
    setFolderRenameBusy(false);
  }
}

function renderFolderRenameError(message) {
  const modal = ensureFolderRenameModal();
  const target = modal.querySelector("#folderRenamePlanResult");
  if (!target) return;
  target.replaceChildren();
  const paragraph = document.createElement("p");
  paragraph.className = "job-result-notice warning";
  paragraph.textContent = message || text("folderRename.planError");
  target.appendChild(paragraph);
  const executeButton = modal.querySelector("#folderRenameExecute");
  if (executeButton) executeButton.disabled = true;
}

function renderFolderRenamePlan(plan, options = {}) {
  const modal = ensureFolderRenameModal();
  const target = modal.querySelector("#folderRenamePlanResult");
  const executeButton = modal.querySelector("#folderRenameExecute");
  if (!target) return;

  if (!plan) {
    target.replaceChildren();
    const paragraph = document.createElement("p");
    paragraph.className = "muted";
    paragraph.textContent = text("folderRename.planPlaceholder");
    target.appendChild(paragraph);
    if (executeButton) executeButton.disabled = true;
    return;
  }

  const container = createResultPanel(
    plan.can_execute ? text("folderRename.planReady") : text("folderRename.planBlocked"),
    target,
  );

  if (options.notice) {
    appendResultNotice(container, options.notice, options.noticeClass || "");
  }

  const blockers = Array.isArray(plan.blocker_messages) ? plan.blocker_messages : [];
  const warnings = Array.isArray(plan.warning_messages) ? plan.warning_messages : [];
  if (blockers.length) {
    appendResultList(container, text("folderRename.blockers"), blockers, "warning");
  }
  if (warnings.length) {
    appendResultList(container, text("folderRename.warnings"), warnings, "warning");
  } else if (plan.can_execute) {
    appendResultNotice(container, text("folderRename.noWarnings"));
  }

  appendResultSection(container, text("folderRename.sectionPaths"), [
    { label: text("folderRename.oldRelPath"), value: plan.old?.rel_path || "" },
    { label: text("folderRename.newRelPath"), value: plan.new?.rel_path || "" },
  ]);

  const folders = plan.impact?.folders || {};
  const mediaFiles = plan.impact?.media_files || {};
  const favorites = plan.impact?.favorites || {};
  const thumbnails = plan.impact?.thumbnails || {};
  const folderPreviewItems = plan.impact?.folder_preview_items || {};
  const previewCount = (Number(folderPreviewItems.for_renamed_folders_count) || 0)
    + (Number(folderPreviewItems.using_renamed_media_count) || 0);

  appendResultSection(container, text("folderRename.sectionImpact"), [
    {
      label: text("folderRename.foldersImpact"),
      value: `${folders.count || 0} · ${text("folderRename.foldersWillUpdate")}`,
    },
    {
      label: text("folderRename.mediaImpact"),
      value: `${mediaFiles.count || 0} · ${text("folderRename.mediaWillUpdate")}`,
    },
    {
      label: text("folderRename.favoritesImpact"),
      value: favorites.will_update_if_executed
        ? `${text("folderRename.favoritesWillUpdate")} (${favorites.matched || 0})`
        : text("folderRename.favoritesNoUpdate"),
    },
    {
      label: text("folderRename.thumbnailsImpact"),
      value: `${thumbnails.count || 0} · ${text("folderRename.thumbnailsKept")}`,
    },
    {
      label: text("folderRename.folderPreviewImpact"),
      value: `${previewCount} · ${text("folderRename.folderPreviewKept")}`,
    },
    { label: text("folderRename.cacheImpact"), value: text("folderRename.cacheKept") },
  ]);

  const checks = Array.isArray(plan.checks) ? plan.checks : [];
  if (checks.length) {
    const details = document.createElement("details");
    details.className = "media-rename-check-details";
    const summary = document.createElement("summary");
    summary.textContent = text("folderRename.sectionChecks");
    details.appendChild(summary);
    const list = document.createElement("ul");
    for (const check of checks) {
      const row = document.createElement("li");
      row.className = check.passed ? "passed" : "failed";
      row.textContent = localizedMessageText(check.message_object);
      list.appendChild(row);
    }
    details.appendChild(list);
    container.appendChild(details);
  }

  if (executeButton) executeButton.disabled = plan.can_execute !== true;
}


function ensureMediaRenameModal() {
  let modal = document.getElementById("mediaRenameModal");
  if (modal) return modal;

  modal = document.createElement("div");
  modal.id = "mediaRenameModal";
  modal.className = "media-rename-modal";
  modal.setAttribute("aria-hidden", "true");
  modal.innerHTML = `
    <button class="media-rename-backdrop" type="button" data-media-rename-close data-aria-label="mediaRename.close" aria-label="${escapeHtml(text("mediaRename.close"))}"></button>
    <section class="media-rename-panel" role="dialog" aria-modal="true" aria-labelledby="mediaRenameModalTitle">
      <header class="media-rename-bar">
        <div>
          <h2 id="mediaRenameModalTitle" data-text="mediaRename.modalTitle">${escapeHtml(text("mediaRename.modalTitle"))}</h2>
          <p class="muted" data-text="mediaRename.modalSubtitle">${escapeHtml(text("mediaRename.modalSubtitle"))}</p>
        </div>
        <button id="mediaRenameClose" type="button" data-media-rename-close data-text="mediaRename.close">${escapeHtml(text("mediaRename.close"))}</button>
      </header>
      <div class="media-rename-body">
        <section class="media-rename-explain">
          <h3 data-text="mediaRename.whatTitle">${escapeHtml(text("mediaRename.whatTitle"))}</h3>
          <p data-text="mediaRename.whatText">${escapeHtml(text("mediaRename.whatText"))}</p>
        </section>
        <dl class="media-rename-current">
          <dt data-text="mediaRename.currentName">${escapeHtml(text("mediaRename.currentName"))}</dt>
          <dd id="mediaRenameCurrentName"></dd>
          <dt data-text="mediaRename.currentPath">${escapeHtml(text("mediaRename.currentPath"))}</dt>
          <dd id="mediaRenameCurrentPath"></dd>
        </dl>
        <form id="mediaRenameForm" class="media-rename-form" novalidate>
          <label for="mediaRenameInput" data-text="mediaRename.newName">${escapeHtml(text("mediaRename.newName"))}</label>
          <div class="media-rename-input-row">
            <input id="mediaRenameInput" type="text" autocomplete="off" spellcheck="false">
            <button id="mediaRenameVerify" type="submit" data-text="mediaRename.verify">${escapeHtml(text("mediaRename.verify"))}</button>
          </div>
        </form>
        <div id="mediaRenamePlanResult" class="media-rename-plan-result"></div>
      </div>
      <footer class="media-rename-footer">
        <button type="button" data-media-rename-close data-text="mediaRename.close">${escapeHtml(text("mediaRename.close"))}</button>
        <button id="mediaRenameExecute" class="write-action" type="button" data-text="mediaRename.execute" disabled>${escapeHtml(text("mediaRename.execute"))}</button>
      </footer>
    </section>
  `;
  document.body.appendChild(modal);

  for (const closeButton of modal.querySelectorAll("[data-media-rename-close]")) {
    closeButton.addEventListener("click", closeMediaRenameModal);
  }

  const form = modal.querySelector("#mediaRenameForm");
  if (form) {
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      verifyMediaRenamePlan();
    });
  }

  const input = modal.querySelector("#mediaRenameInput");
  if (input) {
    input.addEventListener("input", () => {
      if (state.mediaRename) state.mediaRename.plan = null;
      renderMediaRenamePlan(null);
    });
  }

  const executeButton = modal.querySelector("#mediaRenameExecute");
  if (executeButton) {
    executeButton.addEventListener("click", executeMediaRename);
  }

  return modal;
}

function openMediaRenameModal(media) {
  if (!media || media.is_available === false) return;
  const modal = ensureMediaRenameModal();
  state.mediaRename = { media, plan: null, busy: false };

  const currentName = modal.querySelector("#mediaRenameCurrentName");
  const currentPath = modal.querySelector("#mediaRenameCurrentPath");
  const input = modal.querySelector("#mediaRenameInput");
  if (currentName) currentName.textContent = media.file_name || "";
  if (currentPath) currentPath.textContent = media.rel_path || "";
  if (input) input.value = media.file_name || "";

  renderMediaRenamePlan(null);
  modal.classList.add("show");
  modal.setAttribute("aria-hidden", "false");
  document.body.classList.add("media-rename-modal-open");
  if (input) {
    input.focus();
    const dotIndex = String(input.value || "").lastIndexOf(".");
    const selectionEnd = dotIndex > 0 ? dotIndex : input.value.length;
    input.setSelectionRange(0, selectionEnd);
  }
}

function closeMediaRenameModal() {
  const modal = document.getElementById("mediaRenameModal");
  if (!modal) return;
  modal.classList.remove("show");
  modal.setAttribute("aria-hidden", "true");
  document.body.classList.remove("media-rename-modal-open");
  state.mediaRename = null;
}

function mediaRenameInputValue() {
  const modal = ensureMediaRenameModal();
  const input = modal.querySelector("#mediaRenameInput");
  return input ? input.value.trim() : "";
}

function setMediaRenameBusy(isBusy, mode = "") {
  const modal = ensureMediaRenameModal();
  const verifyButton = modal.querySelector("#mediaRenameVerify");
  const executeButton = modal.querySelector("#mediaRenameExecute");
  const input = modal.querySelector("#mediaRenameInput");
  if (state.mediaRename) state.mediaRename.busy = isBusy;
  if (input) input.disabled = isBusy;
  if (verifyButton) {
    verifyButton.disabled = isBusy;
    verifyButton.textContent = isBusy && mode === "verify" ? text("mediaRename.verifying") : text("mediaRename.verify");
  }
  if (executeButton) {
    executeButton.disabled = isBusy || !(state.mediaRename?.plan?.can_execute === true);
    executeButton.textContent = isBusy && mode === "execute" ? text("mediaRename.executing") : text("mediaRename.execute");
  }
}

async function verifyMediaRenamePlan() {
  if (!state.mediaRename?.media) return;
  const media = state.mediaRename.media;
  const newName = mediaRenameInputValue();
  setMediaRenameBusy(true, "verify");
  try {
    const payload = await postJson("/api/media/rename/plan", {
      media_id: media.id,
      new_name: newName,
    });
    state.mediaRename.plan = payload.plan || null;
    renderMediaRenamePlan(state.mediaRename.plan);
  } catch (error) {
    state.mediaRename.plan = null;
    renderMediaRenameError(error.message || text("mediaRename.planError"));
  } finally {
    setMediaRenameBusy(false);
  }
}

async function executeMediaRename() {
  if (!state.mediaRename?.media || !state.mediaRename?.plan?.can_execute) return;
  const media = state.mediaRename.media;
  const newName = mediaRenameInputValue();
  setMediaRenameBusy(true, "execute");
  try {
    const payload = await postJson("/api/media/rename/execute", {
      media_id: media.id,
      new_name: newName,
    });
    if (payload.executed !== true) {
      state.mediaRename.plan = payload.plan || null;
      renderMediaRenamePlan(state.mediaRename.plan, {
        notice: firstResultMessage(payload) || text("mediaRename.executeBlocked"),
        noticeClass: "warning",
      });
      return;
    }
    setMessage(text("mediaRename.executeDone"));
    closeMediaRenameModal();
    await loadCurrentFolder();
  } catch (error) {
    renderMediaRenameError(error.message || text("mediaRename.executeError"));
  } finally {
    setMediaRenameBusy(false);
  }
}

function renderMediaRenameError(message) {
  const modal = ensureMediaRenameModal();
  const target = modal.querySelector("#mediaRenamePlanResult");
  if (!target) return;
  target.replaceChildren();
  const paragraph = document.createElement("p");
  paragraph.className = "job-result-notice warning";
  paragraph.textContent = message || text("mediaRename.planError");
  target.appendChild(paragraph);
  const executeButton = modal.querySelector("#mediaRenameExecute");
  if (executeButton) executeButton.disabled = true;
}

function renderMediaRenamePlan(plan, options = {}) {
  const modal = ensureMediaRenameModal();
  const target = modal.querySelector("#mediaRenamePlanResult");
  const executeButton = modal.querySelector("#mediaRenameExecute");
  if (!target) return;

  if (!plan) {
    target.replaceChildren();
    const paragraph = document.createElement("p");
    paragraph.className = "muted";
    paragraph.textContent = text("mediaRename.planPlaceholder");
    target.appendChild(paragraph);
    if (executeButton) executeButton.disabled = true;
    return;
  }

  const container = createResultPanel(
    plan.can_execute ? text("mediaRename.planReady") : text("mediaRename.planBlocked"),
    target,
  );

  if (options.notice) {
    appendResultNotice(container, options.notice, options.noticeClass || "");
  }

  const blockers = Array.isArray(plan.blocker_messages) ? plan.blocker_messages : [];
  const warnings = Array.isArray(plan.warning_messages) ? plan.warning_messages : [];
  if (blockers.length) {
    appendResultList(container, text("mediaRename.blockers"), blockers, "warning");
  }
  if (warnings.length) {
    appendResultList(container, text("mediaRename.warnings"), warnings, "warning");
  } else if (plan.can_execute) {
    appendResultNotice(container, text("mediaRename.noWarnings"));
  }

  appendResultSection(container, text("mediaRename.sectionPaths"), [
    { label: text("mediaRename.oldRelPath"), value: plan.old?.rel_path || "" },
    { label: text("mediaRename.newRelPath"), value: plan.new?.rel_path || "" },
  ]);

  const favorites = plan.impact?.favorites || {};
  const thumbnails = plan.impact?.thumbnails || {};
  const folderPreviewItems = plan.impact?.folder_preview_items || {};
  appendResultSection(container, text("mediaRename.sectionImpact"), [
    {
      label: text("mediaRename.favoritesImpact"),
      value: favorites.will_update_if_executed
        ? `${text("mediaRename.favoritesWillUpdate")} (${favorites.matched || 0})`
        : text("mediaRename.favoritesNoUpdate"),
    },
    {
      label: text("mediaRename.thumbnailsImpact"),
      value: `${thumbnails.count || 0} · ${text("mediaRename.thumbnailsKept")}`,
    },
    {
      label: text("mediaRename.folderPreviewImpact"),
      value: `${folderPreviewItems.count || 0} · ${text("mediaRename.folderPreviewKept")}`,
    },
    { label: text("mediaRename.cacheImpact"), value: text("mediaRename.cacheKept") },
  ]);

  const checks = Array.isArray(plan.checks) ? plan.checks : [];
  if (checks.length) {
    const details = document.createElement("details");
    details.className = "media-rename-check-details";
    const summary = document.createElement("summary");
    summary.textContent = text("mediaRename.sectionChecks");
    details.appendChild(summary);
    const list = document.createElement("ul");
    for (const check of checks) {
      const row = document.createElement("li");
      row.className = check.passed ? "passed" : "failed";
      row.textContent = localizedMessageText(check.message_object);
      list.appendChild(row);
    }
    details.appendChild(list);
    container.appendChild(details);
  }

  if (executeButton) executeButton.disabled = plan.can_execute !== true;
}

function appendResultList(container, titleText, items, className = "") {
  const section = document.createElement("section");
  section.className = className ? `job-result-section ${className}` : "job-result-section";
  const title = document.createElement("h5");
  title.textContent = titleText;
  section.appendChild(title);
  const list = document.createElement("ul");
  for (const item of items) {
    const row = document.createElement("li");
    row.textContent = localizedMessageText(item);
    list.appendChild(row);
  }
  section.appendChild(list);
  container.appendChild(section);
}

function mediaCard(media, renderContext = {}) {
  const card = document.createElement("article");
  const isAvailable = media.is_available !== false;
  card.className = `media-card media-${media.media_type}${isAvailable ? "" : " unavailable"}`;
  card.dataset.mediaPath = String(media.rel_path || "");
  card.dataset.mediaFavorite = media.is_favorite ? "1" : "0";
  const originalUrl = apiUrl("/media/original", { path: media.rel_path });
  const favoriteText = media.is_favorite ? text("actions.removeFavorite") : text("actions.addFavorite");
  const favoriteClass = media.is_favorite ? "favorite-action is-favorite" : "favorite-action";
  const viewAction = isAvailable
    ? `<a href="${originalUrl}" target="_blank" rel="noopener">${escapeHtml(text("actions.show"))}</a>`
    : `<span class="disabled-action">${escapeHtml(text("actions.unavailable"))}</span>`;
  const disabledAttr = isAvailable ? "" : " disabled";
  const showFolderAction = (state.view === "favorites" || state.view === "search") && isAvailable
    ? `<button type="button" data-action="show-folder">${escapeHtml(text("actions.showFolder"))}</button>`
    : "";

  card.innerHTML = `
    ${mediaPreviewMarkup(media, isAvailable)}
    <div class="media-card-body">
      <div class="media-type-line">${escapeHtml(mediaCardHint(media))}</div>
      <div class="media-card-title-row">
        <div class="row-title media-card-title">${escapeHtml(media.file_name)}</div>
        ${isAvailable ? `<button type="button" class="media-rename-icon" data-action="rename-media" title="${escapeHtml(text("mediaRename.editAction"))}" aria-label="${escapeHtml(text("mediaRename.editAction"))}">
          <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
            <path d="M5 19h14v-6h-2v4H7V7h4V5H5v14z"></path>
            <path d="M14.2 5.8l4 4-7.9 7.9H6.3v-4l7.9-7.9zm1.4-1.4l1.1-1.1a1.4 1.4 0 0 1 2 0l2 2a1.4 1.4 0 0 1 0 2l-1.1 1.1-4-4z"></path>
          </svg>
        </button>` : ""}
      </div>
      <div class="row-meta media-card-meta">${escapeHtml(mediaMetaText(media))}</div>
    </div>
    <div class="media-actions">
      ${viewAction}
      ${showFolderAction}
      <button type="button" data-action="open-original"${disabledAttr}>${escapeHtml(text("actions.openOriginal"))}</button>
      <button type="button" data-action="open-folder"${disabledAttr}>${escapeHtml(text("actions.openFolder"))}</button>
      <button type="button" data-action="favorite" class="${favoriteClass}">${escapeHtml(favoriteText)}</button>
    </div>
  `;

  const thumbnailImages = card.querySelectorAll("img.media-thumbnail");
  const showFolderButton = card.querySelector('[data-action="show-folder"]');
  const openOriginalButton = card.querySelector('[data-action="open-original"]');
  const openFolderButton = card.querySelector('[data-action="open-folder"]');
  const favoriteButton = card.querySelector('[data-action="favorite"]');
  const renameButton = card.querySelector('[data-action="rename-media"]');
  if (renameButton) {
    renameButton.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      openMediaRenameModal(media);
    });
  }
  if (favoriteButton) {
    favoriteButton.dataset.favoritePath = String(media.rel_path || "");
    favoriteButton.dataset.isFavorite = media.is_favorite ? "1" : "0";
  }

  for (const thumbnailImage of thumbnailImages) {
    bindMediaThumbnailResilience(thumbnailImage);
  }

  if (isAvailable) {
    const photoPreview = card.querySelector(".media-preview--photo-tile");
    if (photoPreview) {
      photoPreview.classList.add("modal-trigger");
      photoPreview.dataset.imageModalSrc = originalUrl;
      photoPreview.dataset.imageModalTitle = media.file_name;
      photoPreview.dataset.imageModalFooter = media.rel_path;
      configureImageModalFavorite(photoPreview, media);
      configureImageModalPaging(photoPreview, media, renderContext);
      photoPreview.addEventListener("click", () => openImageModalFromElement(photoPreview));
    }

    const gifPreview = card.querySelector(".media-preview--gif-preview");
    if (gifPreview) {
      gifPreview.classList.add("modal-trigger");
      gifPreview.dataset.imageModalSrc = originalUrl;
      gifPreview.dataset.imageModalTitle = media.file_name;
      gifPreview.dataset.imageModalFooter = media.rel_path;
      configureImageModalFavorite(gifPreview, media);
      configureImageModalPaging(gifPreview, media, renderContext);
      bindGifHoverPreview(gifPreview, originalUrl);
      gifPreview.addEventListener("click", () => openImageModalFromElement(gifPreview));
    }

    const videoPosterPreview = card.querySelector(".media-preview--video-poster");
    if (videoPosterPreview) {
      videoPosterPreview.classList.add("modal-trigger");
      const posterImage = videoPosterPreview.querySelector("img.media-thumbnail");
      const posterUrl = mediaThumbnailUrl(posterImage);

      if (!media.is_html_playable && posterUrl) {
        videoPosterPreview.dataset.imageModalSrc = posterUrl;
        videoPosterPreview.dataset.imageModalTitle = `${media.file_name} · poster`;
        videoPosterPreview.dataset.imageModalFooter = media.rel_path;
      }

      videoPosterPreview.addEventListener("click", () => {
        if (media.is_html_playable) {
          openVideoModal(media.file_name, originalUrl, media.rel_path, media.rel_path, media.is_favorite === true);
          return;
        }

        void openOriginal(media.rel_path);
      });
    }

    bindVideoHoverPreview(videoPosterPreview);

    if (showFolderButton) {
      showFolderButton.addEventListener("click", () => openFolder(parentFolderPath(media.rel_path)));
    }
    openOriginalButton.addEventListener("click", () => openOriginal(media.rel_path));
    openFolderButton.addEventListener("click", () => openSystemFolder(media.rel_path));
  }

  favoriteButton.addEventListener("click", () => {
    const currentFavorite = favoriteButton.dataset.isFavorite === "1";
    setFavorite(media, !currentFavorite);
  });
  return card;
}

async function setFavoritePath(path, shouldBeFavorite) {
  const result = await postJson(
    shouldBeFavorite ? "/api/favorites/add" : "/api/favorites/remove",
    { path },
  );
  const resultPath = result.path || path;

  if (!shouldBeFavorite && result.removed === false) {
    throw new Error(text("message.favoriteRemoveNoChange", { path: resultPath }));
  }

  return { ...result, path: resultPath };
}

async function setFavorite(media, shouldBeFavorite) {
  try {
    const result = await setFavoritePath(media.rel_path, shouldBeFavorite);
    const resultPath = result.path || media.rel_path;

    media.is_favorite = shouldBeFavorite;
    updateVisibleFavoriteState(resultPath, shouldBeFavorite);
    updateModalItemsFavoriteState(resultPath, shouldBeFavorite);

    setMessage(shouldBeFavorite
      ? text("message.favoriteAdded", { path: resultPath })
      : text("message.favoriteRemoved", { path: resultPath }));
    if (state.view === "favorites") {
      state.mediaPage = 1;
    }
    await loadCurrentFolder();
  } catch (error) {
    setMessage(error.message, true);
  }
}

async function openOriginal(path) {
  try {
    await postJson("/api/open-original", { path });
    setMessage(text("message.originalOpened", { path }));
  } catch (error) {
    setMessage(error.message, true);
  }
}

async function openSystemFolder(path) {
  try {
    await postJson("/api/open-folder", { path });
    setMessage(text("message.folderOpened", { path: path || text("app.root") }));
  } catch (error) {
    setMessage(error.message, true);
  }
}

function emptyText(messageText) {
  const p = document.createElement("p");
  p.className = "muted";
  p.textContent = messageText;
  return p;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatBytes(bytes) {
  if (bytes < 1024) return text("unit.bytes", { bytes });
  const units = [text("unit.kb"), text("unit.mb"), text("unit.gb"), text("unit.tb")];
  let size = bytes;
  let unitIndex = -1;
  do {
    size /= 1024;
    unitIndex += 1;
  } while (size >= 1024 && unitIndex < units.length - 1);
  return `${size.toFixed(size >= 10 ? 1 : 2)} ${units[unitIndex]}`;
}

async function reloadSafely(loader) {
  try {
    await loader();
    return true;
  } catch (error) {
    setMessage(error.message, true);
    return false;
  }
}

function scrollToCatalogTop() {
  if (usesIndependentContentScroll()) {
    const content = document.querySelector(".content");
    if (content) {
      content.scrollTo({
        top: 0,
        behavior: "auto",
      });
      return;
    }
  }

  window.scrollTo({
    top: 0,
    behavior: "auto",
  });
}

els.searchForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  await reloadSafely(startSearch);
});

for (const button of document.querySelectorAll(".tab")) {
  button.addEventListener("click", async () => {
    setMediaType(button.dataset.type);
    state.mediaPage = 1;
    await reloadSafely(loadCurrentFolder);
  });
}

els.firstPage.addEventListener("click", () => goToMediaPage(1));

els.prevPage.addEventListener("click", () => goToMediaPage(state.mediaPage - 1));

els.pageJumpForm.addEventListener("submit", (event) => {
  event.preventDefault();
  goToMediaPage(els.pageJumpInput.value);
});

els.nextPage.addEventListener("click", () => goToMediaPage(state.mediaPage + 1));

els.lastPage.addEventListener("click", () => goToMediaPage(state.mediaPages));

if (els.childFoldersToggle) {
  els.childFoldersToggle.addEventListener("click", () => {
    setChildFoldersCollapsed(!areChildFoldersCollapsed());
  });
}

els.firstChildPage.addEventListener("click", () => goToChildPage(1));

els.prevChildPage.addEventListener("click", () => goToChildPage(state.childPage - 1));

els.childPageJumpForm.addEventListener("submit", (event) => {
  event.preventDefault();
  goToChildPage(els.childPageJumpInput.value);
});

els.nextChildPage.addEventListener("click", () => goToChildPage(state.childPage + 1));

els.lastChildPage.addEventListener("click", () => goToChildPage(state.childPages));

els.prevRootPage.addEventListener("click", async () => {
  if (state.rootPage > 1) {
    state.rootPage -= 1;
    await reloadSafely(loadRootFolders);
  }
});

els.nextRootPage.addEventListener("click", async () => {
  if (state.rootPage < state.rootPages) {
    state.rootPage += 1;
    await reloadSafely(loadRootFolders);
  }
});

els.catalogHome.addEventListener("click", () => reloadSafely(() => openFolder("")));

els.catalogManagementOpen.addEventListener("click", openCatalogManagementModal);

els.catalogManagementClose.addEventListener("click", closeCatalogManagementModal);

for (const trigger of document.querySelectorAll("[data-management-close]")) {
  trigger.addEventListener("click", closeCatalogManagementModal);
}

if (els.catalogTitleInput) {
  els.catalogTitleInput.addEventListener("keydown", event => {
    if (event.key === "Enter") {
      event.preventDefault();
      saveGeneralSettings();
    }
  });
  els.catalogTitleInput.addEventListener("input", () => {
    setCatalogTitleStatus("");
  });
}

if (els.themeSelect) {
  els.themeSelect.addEventListener("change", () => {
    saveThemeSetting();
  });
}

if (els.localeSelect) {
  els.localeSelect.addEventListener("change", () => {
    saveLocaleSetting();
  });
}


if (els.sourceRootVerify) {
  els.sourceRootVerify.addEventListener("click", verifySourceRootSetting);
}

if (els.sourceRootSave) {
  els.sourceRootSave.addEventListener("click", saveSourceRootSetting);
}

if (els.sourceRootInput) {
  els.sourceRootInput.addEventListener("input", () => {
    state.sourceRootVerification = null;
  });
  els.sourceRootInput.addEventListener("keydown", event => {
    if (event.key === "Enter") {
      event.preventDefault();
      verifySourceRootSetting();
    }
  });
}

if (els.cacheStatusRefresh) {
  els.cacheStatusRefresh.addEventListener("click", () => loadCacheSettingsStatus({ showLoading: true }));
}

if (els.cacheLimitSave) {
  els.cacheLimitSave.addEventListener("click", saveCacheLimitSetting);
}

if (els.cacheLimitInput) {
  els.cacheLimitInput.addEventListener("keydown", event => {
    if (event.key === "Enter") {
      event.preventDefault();
      saveCacheLimitSetting();
    }
  });
}

if (els.pageSizeSave) {
  els.pageSizeSave.addEventListener("click", saveGeneralSettings);
}

if (els.thumbnailParamsSave) {
  els.thumbnailParamsSave.addEventListener("click", saveThumbnailVideoParams);
}

if (els.thumbnailParamsReset) {
  els.thumbnailParamsReset.addEventListener("click", resetThumbnailVideoParams);
}

for (const input of [
  els.photoPageSizeInput,
  els.videoPageSizeInput,
  els.gifPageSizeInput,
  els.otherPageSizeInput,
  els.folderPageSizeInput,
]) {
  if (input) {
    input.addEventListener("keydown", event => {
      if (event.key === "Enter") {
        event.preventDefault();
        savePageSizeSettings();
      }
    });
  }
}

for (const input of [
  els.imageThumbWidthInput,
  els.imageThumbHeightInput,
  els.gifThumbWidthInput,
  els.gifThumbHeightInput,
  els.videoPreviewWidthInput,
  els.ffmpegTimeoutInput,
  els.ffmpegThreadsInput,
]) {
  if (input) {
    input.addEventListener("keydown", event => {
      if (event.key === "Enter") {
        event.preventDefault();
        saveThumbnailVideoParams();
      }
    });
  }
}

els.jobResultClear.addEventListener("click", () => clearJobResult());
if (els.catalogUpdateAll) {
  els.catalogUpdateAll.addEventListener("click", startCatalogUpdateWorkflow);
}
if (els.catalogPrepareAllPreviews) {
  els.catalogPrepareAllPreviews.addEventListener("click", startPrepareAllPreviews);
}

els.favoritesView.addEventListener("click", () => reloadSafely(openFavorites));

if (els.currentFolderRename) {
  els.currentFolderRename.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    if (state.view !== "folder" || !state.currentFolder) {
      setMessage(text("jobs.unavailableCurrent"), true);
      return;
    }
    openFolderRenameModal(state.currentFolder);
  });
}

if (els.currentFolderOpen) {
  els.currentFolderOpen.addEventListener("click", () => {
    if (state.view !== "folder" || !state.folder || !state.currentFolder) {
      setMessage(text("jobs.unavailableCurrent"), true);
      return;
    }
    if (!currentFolderFilesystemIsUsable()) {
      showFolderFilesystemProblem(state.currentFolder, "jobs.unavailableCurrent");
      return;
    }
    openSystemFolder(state.folder);
  });
}

if (els.currentFolderUpdate) {
  els.currentFolderUpdate.addEventListener("click", () => {
    if (state.view !== "folder" || !state.folder || !state.currentFolder) {
      setMessage(text("jobs.unavailableCurrent"), true);
      return;
    }
    if (els.currentFolderMore) {
      els.currentFolderMore.open = false;
    }
    startUpdateBranch(state.folder, state.currentFolder);
  });
}

if (els.currentFolderMore) {
  prepareFolderMoreMenu(els.currentFolderMore);
}

if (els.currentFolderGeneratePreviews) {
  els.currentFolderGeneratePreviews.addEventListener("click", () => {
    if (state.view !== "folder" || !state.folder || !state.currentFolder) {
      setMessage(text("jobs.unavailableCurrent"), true);
      return;
    }
    if (!folderCanGeneratePreviews(state.currentFolder)) {
      setMessage(text("jobs.unavailableCurrent"), true);
      return;
    }
    if (els.currentFolderMore) {
      els.currentFolderMore.open = false;
    }
    startGenerateFolderPreviews(state.folder);
  });
}

document.addEventListener("click", () => {
  closeFolderMoreMenus();
});

els.scanPreviewAll.addEventListener("click", () => startScanPreview(""));
els.scanPreviewCurrent.addEventListener("click", () => {
  if (state.view !== "folder" || !state.folder) {
    setMessage(text("jobs.unavailableCurrent"), true);
    return;
  }
  startScanPreview(state.folder);
});
els.scanStageAll.addEventListener("click", () => startScanStage(""));
els.scanStageCurrent.addEventListener("click", () => {
  if (state.view !== "folder" || !state.folder) {
    setMessage(text("jobs.unavailableCurrent"), true);
    return;
  }
  startScanStage(state.folder);
});
els.gifPreviewAll.addEventListener("click", () => startGifPreview(""));
els.gifPreviewCurrent.addEventListener("click", () => {
  if (state.view !== "folder" || !state.folder) {
    setMessage(text("jobs.unavailableCurrent"), true);
    return;
  }
  startGifPreview(state.folder);
});
els.videoPosterAll.addEventListener("click", () => startVideoPoster(""));
els.videoPosterCurrent.addEventListener("click", () => {
  if (state.view !== "folder" || !state.folder) {
    setMessage(text("jobs.unavailableCurrent"), true);
    return;
  }
  startVideoPoster(state.folder);
});
els.videoFramesAll.addEventListener("click", () => startVideoFrames(""));
els.videoFramesCurrent.addEventListener("click", () => {
  if (state.view !== "folder" || !state.folder) {
    setMessage(text("jobs.unavailableCurrent"), true);
    return;
  }
  startVideoFrames(state.folder);
});
els.folderPreviewPlanCurrent.addEventListener("click", () => {
  if (state.view !== "folder" || !state.folder) {
    setMessage(text("jobs.unavailableCurrent"), true);
    return;
  }
  loadFolderPreviewPlan(state.folder);
});
els.scanActivate.addEventListener("click", () => startScanActivate());

document.addEventListener("keydown", (event) => {
  const modal = document.getElementById("mediaModal");
  const modalOpen = Boolean(modal && modal.classList.contains("show"));

  if (event.key === "Escape") {
    if (document.getElementById("folderRenameModal")?.classList.contains("show")) {
      closeFolderRenameModal();
      return;
    }
    if (document.getElementById("mediaRenameModal")?.classList.contains("show")) {
      closeMediaRenameModal();
      return;
    }
    if (folderRepairModalIsOpen()) {
      closeFolderRepairModal();
      return;
    }
    if (catalogManagementIsOpen()) {
      closeCatalogManagementModal();
      return;
    }
    closeMediaModal();
    return;
  }

  if (!modalOpen || !modal.classList.contains("image-mode")) return;

  if (event.key === "ArrowLeft") {
    event.preventDefault();
    showPreviousModalImage();
  } else if (event.key === "ArrowRight") {
    event.preventDefault();
    showNextModalImage();
  } else if (event.key === "+" || event.key === "=") {
    event.preventDefault();
    zoomModalImage(1.25);
  } else if (event.key === "-" || event.key === "_") {
    event.preventDefault();
    zoomModalImage(0.8);
  }
});

window.addEventListener("resize", () => {
  syncAppTopbarHeight();
  fitMediaModalContent();
  scheduleResponsiveLayoutUpdate();
});

(async function main() {
  const operation = diagnosticOperationStart("frontend.app.main", {
    diagnostics_enabled: DIAGNOSTICS_ENABLED,
  });
  try {
    initializeAppShellLayout();
    await initializeLocalization();
    await loadInitialLocale();
    applyStaticTexts();
    syncLocaleSelect();
    initializeTheme();
    updateViewButtons();
    await loadStatus();
    const jobStatus = await loadJobStatus();
    ensureJobPollingForStatus(jobStatus);
    await openFolder("");
    diagnosticOperationEnd("frontend.app.main", operation, {
      result: "ok",
      active_locale: activeLocale,
      active_folder: state.folder,
    });
  } catch (error) {
    diagnosticOperationEnd("frontend.app.main", operation, {
      result: "error",
      error: String(error?.message || error),
    });
    setMessage(error.message, true);
  }
})();
