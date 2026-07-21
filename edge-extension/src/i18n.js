(() => {
  const STORAGE_KEY = "raybetMonitorUiLanguage";
  const DEFAULT_LANGUAGE = "en";
  const SUPPORTED_LANGUAGES = Object.freeze(["zh-CN", "en"]);

  const messages = Object.freeze({
    en: Object.freeze({
      "language.label": "Language",
      "language.zh-CN": "Simplified Chinese",
      "language.en": "English",
      "common.loading": "Loading",
      "common.checking": "Checking",
      "common.none": "None",
      "common.notSeen": "Not seen",
      "common.connected": "Connected",
      "common.unavailable": "Unavailable",
      "common.configFailed": "Config failed",
      "popup.documentTitle": "Dota 2 Monitor",
      "popup.heading": "Dota 2 Monitor",
      "popup.settings": "Settings",
      "popup.capture": "Capture",
      "popup.companion": "Companion",
      "popup.dotaMatches": "Dota matches",
      "popup.queued": "Queued",
      "popup.dropped": "Dropped",
      "popup.lastEvent": "Last event",
      "popup.pageHook": "Page hook",
      "popup.bridge": "Bridge",
      "popup.observed": "Observed",
      "popup.classified": "Classified",
      "popup.lastRequest": "Last request",
      "popup.lastDecision": "Last decision",
      "popup.openReport": "Open shadow report",
      "popup.initialization": "top {top} | child {child}",
      "popup.observedCounts": "F {fetch} | X {xhr} | W {websocket}",
      "popup.classifiedCounts": "{accepted} accepted | {ignored} ignored",
      "popup.lastEventValue": "{eventType} · {time}",
      "popup.lastObservedValue": "{host}{path} | {time}",
      "popup.lastDecisionValue": "{outcome}: {reason}",
      "options.documentTitle": "Dota 2 Monitor Settings",
      "options.heading": "Monitor Settings",
      "options.capture": "Capture",
      "options.raybetSite": "RayBet site",
      "options.raybetApis": "RayBet market APIs",
      "options.save": "Save",
      "options.saved": "Saved",
      "options.saveFailed": "Save failed",
      "code.captureStatus.capturing": "Capturing",
      "code.captureStatus.backpressure": "Backpressure",
      "code.captureStatus.buffering": "Buffering",
      "code.captureStatus.companion_offline": "Companion offline",
      "code.captureStatus.paused": "Paused",
      "code.captureStatus.reconnecting": "Reconnecting",
      "code.captureStatus.waiting_for_traffic": "Waiting for traffic",
      "code.captureStatus.degraded": "Degraded",
      "code.captureStatus.page_hook_missing": "Page hook missing",
      "code.captureStatus.unsupported_page": "Unsupported page",
      "code.captureStatus.error": "Error",
      "code.statusReason.capture_paused": "Capture paused",
      "code.statusReason.unsupported_page": "Unsupported page",
      "code.statusReason.active_tab_unavailable": "Active tab unavailable",
      "code.statusReason.events_dropped": "Events dropped",
      "code.statusReason.events_rejected": "Events rejected",
      "code.statusReason.body_too_large": "Request body too large",
      "code.statusReason.forbidden_field": "Request contains a forbidden field",
      "code.statusReason.invalid_batch": "Invalid event batch",
      "code.statusReason.invalid_json": "Invalid JSON request",
      "code.statusReason.unsupported_media_type": "Unsupported media type",
      "code.statusReason.queue_state_invalid": "Queue state invalid",
      "code.statusReason.bridge_config_failed": "Bridge configuration failed",
      "code.statusReason.database_unavailable": "Database unavailable",
      "code.statusReason.forbidden_origin": "Origin forbidden",
      "code.statusReason.invalid_origin": "Invalid origin",
      "code.statusReason.invalid_protocol_response": "Invalid protocol response",
      "code.statusReason.invalid_status_body": "Invalid status response",
      "code.statusReason.origin_required": "Origin required",
      "code.statusReason.origin_not_allowed": "Origin not allowed",
      "code.statusReason.unsupported_extension_version": "Unsupported extension version",
      "code.statusReason.unsupported_protocol_version": "Unsupported protocol version",
      "code.statusReason.companion_http_error": "Companion HTTP error",
      "code.statusReason.queue_event_high_water": "Event queue near capacity",
      "code.statusReason.queue_byte_high_water": "Queue storage near capacity",
      "code.statusReason.bridge_unreachable": "Page bridge unreachable",
      "code.statusReason.main_hook_not_seen": "Main page hook not seen",
      "code.statusReason.status_probe_timeout": "Companion status timed out",
      "code.statusReason.status_probe_network_error": "Companion network unavailable",
      "code.statusReason.draining_queue": "Draining queued events",
      "code.statusReason.companion_rate_limited": "Companion rate limited",
      "code.statusReason.retry_scheduled": "Retry scheduled",
      "code.statusReason.companion_retry_pending": "Companion retry pending",
      "code.statusReason.page_loading": "Page loading",
      "code.statusReason.hook_initializing": "Page hook initializing",
      "code.statusReason.no_transport_observed": "Waiting for page traffic",
      "code.statusReason.no_dota_event_accepted": "No Dota event accepted yet",
      "code.statusReason.acknowledgements_current": "Acknowledgements current",
      "code.statusReason.extension_status_unavailable": "Extension status unavailable",
      "code.outcome.accepted": "Accepted",
      "code.outcome.ignored": "Ignored",
      "code.reason.non_dota": "Non-Dota traffic",
      "code.reason.match_list": "Match list",
      "code.reason.video": "Video",
      "code.reason.odds": "Odds",
      "code.reason.market_update": "Market update",
      "code.reason.manual_control": "Manual control",
      "code.reason.unknown": "Unknown event",
      "code.reason.service_worker_rejected": "Service worker rejected event",
      "code.reason.payload_too_large": "Payload too large",
      "code.reason.raw_payload_too_large": "Raw payload too large",
      "code.reason.binary_payload": "Binary payload",
      "code.reason.invalid_envelope": "Invalid event envelope",
      "code.reason.unknown_structure": "Unknown payload structure",
      "code.reason.invalid_raw": "Invalid raw event",
      "code.reason.disabled_source": "Source disabled",
      "code.reason.invalid_json": "Invalid JSON",
      "code.reason.invalid_candidate": "Invalid candidate",
      "code.reason.invalid_page_origin": "Invalid page origin",
      "code.reason.invalid_bridge_json": "Invalid bridge JSON",
      "code.reason.match_id_mismatch": "Match ID mismatch",
      "code.reason.untrusted_match": "Untrusted match",
      "code.reason.missing_match_id": "Missing match ID",
      "code.reason.invalid_odds": "Invalid odds",
      "code.reason.invalid_manual_control": "Invalid manual control",
      "code.reason.diagnostic_untrusted": "Untrusted diagnostic",
      "code.reason.max_nodes": "Payload node limit exceeded",
      "code.reason.max_depth": "Payload depth limit exceeded",
      "code.reason.non_json_value": "Non-JSON value",
      "code.reason.max_string_bytes": "String size limit exceeded",
      "code.reason.cycle": "Cyclic payload",
      "code.reason.max_array_items": "Array item limit exceeded",
      "code.reason.max_object_keys": "Object key limit exceeded",
      "code.reason.metadata_untrusted_match": "Metadata match is not trusted",
      "code.reason.capture_inactive": "Capture inactive",
      "code.reason.no_events": "No events classified",
      "code.reason.rate_limited": "Page traffic rate limited",
      "code.reason.processing_error": "Event processing failed",
      "code.reason.invalid_sender": "Invalid sender",
      "code.reason.invalid_diagnostic": "Invalid diagnostic",
      "code.eventType.match_list": "Match list",
      "code.eventType.video": "Video",
      "code.eventType.odds": "Odds",
      "code.eventType.market_update": "Market update",
      "code.eventType.manual_control": "Manual control",
      "code.eventType.unknown": "Unknown event",
    }),
    "zh-CN": Object.freeze({
      "language.label": "语言",
      "language.zh-CN": "简体中文",
      "language.en": "英语",
      "common.loading": "加载中",
      "common.checking": "检查中",
      "common.none": "无",
      "common.notSeen": "未检测到",
      "common.connected": "已连接",
      "common.unavailable": "不可用",
      "common.configFailed": "配置失败",
      "popup.documentTitle": "Dota 2 监控",
      "popup.heading": "Dota 2 监控",
      "popup.settings": "设置",
      "popup.capture": "采集",
      "popup.companion": "Companion",
      "popup.dotaMatches": "Dota 2 比赛",
      "popup.queued": "队列中",
      "popup.dropped": "已丢弃",
      "popup.lastEvent": "最近事件",
      "popup.pageHook": "页面 Hook",
      "popup.bridge": "桥接",
      "popup.observed": "已观察",
      "popup.classified": "已分类",
      "popup.lastRequest": "最近请求",
      "popup.lastDecision": "最近判定",
      "popup.openReport": "打开影子报告",
      "popup.initialization": "顶层 {top} | 子框架 {child}",
      "popup.observedCounts": "F {fetch} | X {xhr} | W {websocket}",
      "popup.classifiedCounts": "已接受 {accepted} | 已忽略 {ignored}",
      "popup.lastEventValue": "{eventType} · {time}",
      "popup.lastObservedValue": "{host}{path} | {time}",
      "popup.lastDecisionValue": "{outcome}：{reason}",
      "options.documentTitle": "Dota 2 监控设置",
      "options.heading": "监控设置",
      "options.capture": "采集",
      "options.raybetSite": "RayBet 站点",
      "options.raybetApis": "RayBet 行情 API",
      "options.save": "保存",
      "options.saved": "已保存",
      "options.saveFailed": "保存失败",
      "code.captureStatus.capturing": "采集中",
      "code.captureStatus.backpressure": "队列压力",
      "code.captureStatus.buffering": "缓冲中",
      "code.captureStatus.companion_offline": "Companion 离线",
      "code.captureStatus.paused": "已暂停",
      "code.captureStatus.reconnecting": "正在重连",
      "code.captureStatus.waiting_for_traffic": "等待页面流量",
      "code.captureStatus.degraded": "运行降级",
      "code.captureStatus.page_hook_missing": "页面 Hook 缺失",
      "code.captureStatus.unsupported_page": "不支持的页面",
      "code.captureStatus.error": "错误",
      "code.statusReason.capture_paused": "采集已暂停",
      "code.statusReason.unsupported_page": "当前页面不受支持",
      "code.statusReason.active_tab_unavailable": "无法访问当前标签页",
      "code.statusReason.events_dropped": "已丢弃事件",
      "code.statusReason.events_rejected": "事件被拒绝",
      "code.statusReason.body_too_large": "请求正文过大",
      "code.statusReason.forbidden_field": "请求包含禁止字段",
      "code.statusReason.invalid_batch": "事件批次无效",
      "code.statusReason.invalid_json": "请求 JSON 无效",
      "code.statusReason.unsupported_media_type": "不支持的媒体类型",
      "code.statusReason.queue_state_invalid": "队列状态无效",
      "code.statusReason.bridge_config_failed": "桥接配置失败",
      "code.statusReason.database_unavailable": "数据库不可用",
      "code.statusReason.forbidden_origin": "来源被禁止",
      "code.statusReason.invalid_origin": "来源无效",
      "code.statusReason.invalid_protocol_response": "协议响应无效",
      "code.statusReason.invalid_status_body": "状态响应无效",
      "code.statusReason.origin_required": "缺少来源",
      "code.statusReason.origin_not_allowed": "来源不在允许范围内",
      "code.statusReason.unsupported_extension_version": "扩展版本不受支持",
      "code.statusReason.unsupported_protocol_version": "协议版本不受支持",
      "code.statusReason.companion_http_error": "Companion HTTP 错误",
      "code.statusReason.queue_event_high_water": "事件队列接近上限",
      "code.statusReason.queue_byte_high_water": "队列存储接近上限",
      "code.statusReason.bridge_unreachable": "无法连接页面桥接",
      "code.statusReason.main_hook_not_seen": "未检测到主页面 Hook",
      "code.statusReason.status_probe_timeout": "Companion 状态请求超时",
      "code.statusReason.status_probe_network_error": "Companion 网络不可用",
      "code.statusReason.draining_queue": "正在发送队列事件",
      "code.statusReason.companion_rate_limited": "Companion 已限流",
      "code.statusReason.retry_scheduled": "已安排重试",
      "code.statusReason.companion_retry_pending": "Companion 等待重试",
      "code.statusReason.page_loading": "页面加载中",
      "code.statusReason.hook_initializing": "页面 Hook 初始化中",
      "code.statusReason.no_transport_observed": "等待页面流量",
      "code.statusReason.no_dota_event_accepted": "尚未接受 Dota 事件",
      "code.statusReason.acknowledgements_current": "事件确认已同步",
      "code.statusReason.extension_status_unavailable": "扩展状态不可用",
      "code.outcome.accepted": "已接受",
      "code.outcome.ignored": "已忽略",
      "code.reason.non_dota": "非 Dota 流量",
      "code.reason.match_list": "比赛列表",
      "code.reason.video": "视频",
      "code.reason.odds": "赔率",
      "code.reason.market_update": "行情更新",
      "code.reason.manual_control": "手动控制",
      "code.reason.unknown": "未知事件",
      "code.reason.service_worker_rejected": "Service worker 拒绝事件",
      "code.reason.payload_too_large": "载荷过大",
      "code.reason.raw_payload_too_large": "原始载荷过大",
      "code.reason.binary_payload": "二进制载荷",
      "code.reason.invalid_envelope": "事件封装无效",
      "code.reason.unknown_structure": "未知载荷结构",
      "code.reason.invalid_raw": "原始事件无效",
      "code.reason.disabled_source": "数据源已禁用",
      "code.reason.invalid_json": "JSON 无效",
      "code.reason.invalid_candidate": "候选事件无效",
      "code.reason.invalid_page_origin": "页面来源无效",
      "code.reason.invalid_bridge_json": "桥接 JSON 无效",
      "code.reason.match_id_mismatch": "比赛 ID 不匹配",
      "code.reason.untrusted_match": "比赛未通过信任校验",
      "code.reason.missing_match_id": "缺少比赛 ID",
      "code.reason.invalid_odds": "赔率无效",
      "code.reason.invalid_manual_control": "手动控制无效",
      "code.reason.diagnostic_untrusted": "诊断信息不可信",
      "code.reason.max_nodes": "载荷节点数超出上限",
      "code.reason.max_depth": "载荷深度超出上限",
      "code.reason.non_json_value": "包含非 JSON 值",
      "code.reason.max_string_bytes": "字符串大小超出上限",
      "code.reason.cycle": "载荷存在循环引用",
      "code.reason.max_array_items": "数组元素数超出上限",
      "code.reason.max_object_keys": "对象属性数超出上限",
      "code.reason.metadata_untrusted_match": "元数据比赛未通过信任校验",
      "code.reason.capture_inactive": "采集未启用",
      "code.reason.no_events": "未分类出事件",
      "code.reason.rate_limited": "页面流量已限流",
      "code.reason.processing_error": "事件处理失败",
      "code.reason.invalid_sender": "发送方无效",
      "code.reason.invalid_diagnostic": "诊断信息无效",
      "code.eventType.match_list": "比赛列表",
      "code.eventType.video": "视频",
      "code.eventType.odds": "赔率",
      "code.eventType.market_update": "行情更新",
      "code.eventType.manual_control": "手动控制",
      "code.eventType.unknown": "未知事件",
    }),
  });

  function normalizeLanguage(value) {
    if (typeof value !== "string") return null;
    const normalized = value.trim().toLowerCase();
    if (normalized === "en" || normalized.startsWith("en-")) return "en";
    if (normalized === "zh" || normalized.startsWith("zh-")) return "zh-CN";
    return null;
  }

  function preferredLanguage(navigatorObject = globalThis.navigator) {
    const candidates = [
      ...(Array.isArray(navigatorObject?.languages) ? navigatorObject.languages : []),
      navigatorObject?.language,
    ];
    for (const candidate of candidates) {
      const language = normalizeLanguage(candidate);
      if (language) return language;
    }
    return DEFAULT_LANGUAGE;
  }

  function translate(language, key, fallback = key) {
    const resolved = normalizeLanguage(language) || DEFAULT_LANGUAGE;
    return messages[resolved]?.[key] ?? messages[DEFAULT_LANGUAGE]?.[key] ?? fallback;
  }

  function format(language, key, values) {
    let value = translate(language, key);
    for (const [name, replacement] of Object.entries(values || {})) {
      value = value.replaceAll(`{${name}}`, String(replacement));
    }
    return value;
  }

  function translateCode(language, category, code) {
    if (code === null || code === undefined || code === "") return "";
    const raw = String(code);
    return translate(language, `code.${category}.${raw}`, raw);
  }

  function setAttribute(element, name, value) {
    if (typeof element?.setAttribute === "function") element.setAttribute(name, value);
    else if (element) element[name] = value;
  }

  function translateDocument(documentObject, page, language) {
    const resolved = normalizeLanguage(language) || DEFAULT_LANGUAGE;
    if (documentObject?.documentElement) {
      setAttribute(documentObject.documentElement, "lang", resolved);
    }
    if (documentObject) documentObject.title = translate(resolved, `${page}.documentTitle`);
    if (typeof documentObject?.querySelectorAll !== "function") return;

    for (const element of documentObject.querySelectorAll("[data-i18n]")) {
      element.textContent = translate(resolved, element.dataset.i18n);
    }
    for (const element of documentObject.querySelectorAll("[data-i18n-title]")) {
      setAttribute(element, "title", translate(resolved, element.dataset.i18nTitle));
    }
    for (const element of documentObject.querySelectorAll("[data-i18n-aria-label]")) {
      setAttribute(element, "aria-label", translate(resolved, element.dataset.i18nAriaLabel));
    }
  }

  function createLanguageController({
    document: documentObject,
    page,
    select,
    chrome: chromeObject = globalThis.chrome,
    navigator: navigatorObject = globalThis.navigator,
    onLanguageChanged,
  }) {
    const storage = chromeObject?.storage?.local;
    const storageChanges = chromeObject?.storage?.onChanged;
    let language = preferredLanguage(navigatorObject);
    let disposed = false;

    const apply = (nextLanguage) => {
      if (disposed) return;
      language = normalizeLanguage(nextLanguage) || preferredLanguage(navigatorObject);
      if (select) select.value = language;
      translateDocument(documentObject, page, language);
      if (typeof onLanguageChanged === "function") onLanguageChanged(language);
    };

    apply(language);

    const ready = (async () => {
      if (typeof storage?.get !== "function") return language;
      try {
        const stored = await storage.get(STORAGE_KEY);
        apply(stored?.[STORAGE_KEY] ?? preferredLanguage(navigatorObject));
      } catch {
        apply(preferredLanguage(navigatorObject));
      }
      return language;
    })();

    if (typeof select?.addEventListener === "function") {
      select.addEventListener("change", async () => {
        const selected = normalizeLanguage(select.value);
        if (!selected) return;
        apply(selected);
        if (typeof storage?.set === "function") {
          try {
            await storage.set({[STORAGE_KEY]: selected});
          } catch {
            // The selection remains active for this page even if persistence is unavailable.
          }
        }
      });
    }

    const storageListener = (changes, areaName) => {
      if (areaName !== "local" || !Object.hasOwn(changes || {}, STORAGE_KEY)) return;
      apply(changes[STORAGE_KEY]?.newValue ?? preferredLanguage(navigatorObject));
    };
    if (typeof storageChanges?.addListener === "function") {
      storageChanges.addListener(storageListener);
    }

    return Object.freeze({
      ready,
      getLanguage: () => language,
      dispose() {
        disposed = true;
        if (typeof storageChanges?.removeListener === "function") {
          storageChanges.removeListener(storageListener);
        }
      },
    });
  }

  globalThis.RaybetI18n = Object.freeze({
    STORAGE_KEY,
    SUPPORTED_LANGUAGES,
    normalizeLanguage,
    preferredLanguage,
    translate,
    format,
    translateCode,
    translateDocument,
    createLanguageController,
  });
})();
