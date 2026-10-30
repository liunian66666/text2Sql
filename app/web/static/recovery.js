/*
 * 页面刷新的任务恢复状态机。
 *
 * 这里只放纯逻辑：不碰 DOM、不发请求、不读全局状态，目的是让「刷新后该做什么」
 * 可以脱离浏览器被测试（见 tests/js/recovery.test.mjs）。
 * chat.js 负责按返回的 action 渲染。
 *
 * 设计约束（对应需求文档）：
 *   - 一个标签页同一时刻只有一个活动任务；
 *   - 只有 404 / 终态 / 用户点「新建对话」才清理记录；
 *   - 网络错误不等于任务失败，不能顺手清记录；
 *   - 记录里只存恢复页面必需的非敏感字段。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.DeepEyeRecovery = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  const STORAGE_KEY = "deepeye.active_task";
  const CONVERSATION_KEY = "deepeye.conversation_id";
  const ACTIVE_STATUSES = ["queued", "running"];
  const TERMINAL_STATUSES = ["success", "empty_result", "error", "interrupted"];
  const RESTORE_START_MESSAGE = "正在恢复上一次任务……";
  const MAX_QUESTION_LENGTH = 2000;
  const REQUEST_ID_PATTERN = /^[A-Za-z0-9_-]{4,64}$/;

  const ACTIONS = {
    resume_events: "resume_events",
    render_result: "render_result",
    render_error: "render_error",
    interrupted: "interrupted",
    discard: "discard",
  };

  function text(value, limit) {
    if (typeof value !== "string") return "";
    return value.slice(0, limit == null ? MAX_QUESTION_LENGTH : limit);
  }

  function isActiveStatus(status) {
    return ACTIVE_STATUSES.includes(status);
  }

  function isTerminalStatus(status) {
    return TERMINAL_STATUSES.includes(status);
  }

  /* 记录只保留白名单字段，任何多出来的键（尤其是误存的响应体）都会被丢掉。 */
  function sanitizeRecord(raw) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
    const requestId = text(raw.request_id, 64).trim();
    if (!REQUEST_ID_PATTERN.test(requestId)) return null;
    const conversationId = text(raw.conversation_id, 128).trim();
    if (!conversationId) return null;
    const sequence = Number.isInteger(raw.sequence) && raw.sequence >= 0 ? raw.sequence : 0;
    return {
      request_id: requestId,
      conversation_id: conversationId,
      question: text(raw.question),
      database_id: text(raw.database_id, 64).trim() || "auto",
      created_at: text(raw.created_at, 64),
      sequence,
    };
  }

  function readRecord(storage) {
    if (!storage) return null;
    let raw;
    try {
      raw = storage.getItem(STORAGE_KEY);
    } catch (error) {
      return null;
    }
    if (!raw) return null;
    try {
      return sanitizeRecord(JSON.parse(raw));
    } catch (error) {
      return null;
    }
  }

  function writeRecord(storage, record) {
    const clean = sanitizeRecord(record);
    if (!storage || !clean) return null;
    try {
      storage.setItem(STORAGE_KEY, JSON.stringify(clean));
    } catch (error) {
      return null;
    }
    return clean;
  }

  function clearRecord(storage) {
    if (!storage) return;
    try {
      storage.removeItem(STORAGE_KEY);
    } catch (error) {
      /* 存储被禁用时无需处理：本来也没有可恢复的记录 */
    }
  }

  /*
   * 把 GET /api/query/active 返回的任务翻译成本地记录。
   * 新标签页的 sessionStorage 是空的，只能靠服务端说“现在有任务在跑”，
   * 因此这条路径不读本地状态，字段同样走白名单，不整包存响应。
   */
  function recordFromTask(task, fallbackConversationId) {
    if (!task || typeof task !== "object" || Array.isArray(task)) return null;
    const request = task.request && typeof task.request === "object" ? task.request : {};
    return sanitizeRecord({
      request_id: task.id,
      conversation_id: request.conversation_id || fallbackConversationId || "",
      question: request.message || request.question || "",
      database_id: request.database_id || "auto",
      created_at: task.created_at || "",
      sequence: 0,
    });
  }

  function readConversationId(storage) {
    if (!storage) return "";
    try {
      const value = storage.getItem(CONVERSATION_KEY);
      return typeof value === "string" ? text(value, 128).trim() : "";
    } catch (error) {
      return "";
    }
  }

  function writeConversationId(storage, conversationId) {
    if (!storage || typeof conversationId !== "string" || !conversationId) return "";
    try {
      storage.setItem(CONVERSATION_KEY, conversationId);
    } catch (error) {
      return "";
    }
    return conversationId;
  }

  /*
   * 状态 → 动作。未知状态（含接口 404 之外的异常值）一律当作失效记录处理：
   * 清理并回到欢迎页，绝不自动重新提交。
   */
  function planForStatus(status) {
    switch (status) {
      case "queued":
        return {
          action: ACTIONS.resume_events,
          status,
          headline: "任务仍在排队",
          message: "任务仍在排队，已恢复进度连接",
          busy: true,
          clearRecord: false,
          canSubmit: false,
        };
      case "running":
        return {
          action: ACTIONS.resume_events,
          status,
          headline: "任务仍在后台执行",
          message: "任务仍在后台执行，已恢复进度连接",
          busy: true,
          clearRecord: false,
          canSubmit: false,
        };
      case "success":
      case "empty_result":
        return {
          action: ACTIONS.render_result,
          status,
          headline: "分析完成",
          message: "上一次任务已经完成，结果已恢复",
          busy: false,
          clearRecord: false,
          canSubmit: true,
        };
      case "error":
        return {
          action: ACTIONS.render_error,
          status,
          headline: "分析未完成",
          message: "上一次任务执行失败，已恢复失败信息",
          busy: false,
          clearRecord: false,
          canSubmit: true,
        };
      case "interrupted":
        return {
          action: ACTIONS.interrupted,
          status,
          headline: "任务已中断",
          message: "后端在任务执行期间重启，本次任务未完成，请重新提交",
          busy: false,
          clearRecord: true,
          canSubmit: true,
        };
      default:
        return {
          action: ACTIONS.discard,
          status: status || "unknown",
          headline: "任务记录已失效",
          message: "任务记录已失效，请重新提交",
          busy: false,
          clearRecord: true,
          canSubmit: true,
        };
    }
  }

  /* 有限次重连：超过上限就停止，避免失效任务无限重试。 */
  function shouldReconnect(attempts, max) {
    return Number(attempts) < Number(max);
  }

  return {
    STORAGE_KEY,
    CONVERSATION_KEY,
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    RESTORE_START_MESSAGE,
    ACTIONS,
    sanitizeRecord,
    readRecord,
    writeRecord,
    clearRecord,
    recordFromTask,
    readConversationId,
    writeConversationId,
    planForStatus,
    isActiveStatus,
    isTerminalStatus,
    shouldReconnect,
  };
});
