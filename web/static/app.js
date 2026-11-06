/* 经济数据分析 Agent —— 前端逻辑（无框架，无外链） */
(function () {
  "use strict";

  var SAMPLES = [
    "贫困县和非贫困县的居民收入有显著差异吗",
    "居民收入和农业总产值是否显著相关",
    "不同地形类型的县收入差异显著吗",
    "预测未来三年全县平均 GDP",
    "找出产值异常偏高的县",
    "把县按经济特征聚类"
  ];
  var STAGES = [
    ["intent_ms", "意图识别"],
    ["sql_ms", "生成 SQL"],
    ["load_ms", "取数"],
    ["route_ms", "方法路由"],
    ["analysis_ms", "统计计算"],
    ["charts_ms", "绘图"],
    ["report_ms", "撰写报告"]
  ];

  var $ = function (id) { return document.getElementById(id); };

  /* ---------------- 工具 ---------------- */
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function fmtMs(v) {
    if (v == null) return "";
    return v >= 1000 ? (v / 1000).toFixed(2) + "s" : Math.round(v) + "ms";
  }

  function inline(s) {
    return esc(s)
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/`([^`]+)`/g, "<code>$1</code>");
  }

  /* 极简 markdown：只覆盖报告模板用到的语法（# / ## / - / 空行 / 行内加粗） */
  function markdown(md) {
    var out = [], i, line, inList = false;
    var lines = String(md || "").replace(/\r/g, "").split("\n");
    function closeList() { if (inList) { out.push("</ul>"); inList = false; } }
    for (i = 0; i < lines.length; i++) {
      line = lines[i];
      if (/^#\s+/.test(line)) { closeList(); out.push("<h1>" + inline(line.replace(/^#\s+/, "")) + "</h1>"); }
      else if (/^##\s+/.test(line)) { closeList(); out.push("<h2>" + inline(line.replace(/^##\s+/, "")) + "</h2>"); }
      else if (/^###\s+/.test(line)) { closeList(); out.push("<h3>" + inline(line.replace(/^###\s+/, "")) + "</h3>"); }
      else if (/^\s*[-*]\s+/.test(line)) {
        if (!inList) { out.push("<ul>"); inList = true; }
        out.push("<li>" + inline(line.replace(/^\s*[-*]\s+/, "")) + "</li>");
      }
      else if (!line.trim()) { closeList(); }
      else { closeList(); out.push("<p>" + inline(line) + "</p>"); }
    }
    closeList();
    return out.join("\n");
  }

  /* ---------------- 顶栏状态 ---------------- */
  function loadHealth() {
    fetch("/api/health").then(function (r) { return r.json(); }).then(function (h) {
      var pills = [];
      pills.push('<span class="pill">' + esc(h.datasource || "未知数据源") +
        (h.tables && h.tables.length ? " · " + h.tables.length + " 表" : "") + "</span>");
      if (h.llm_online) {
        pills.push('<span class="pill ok">模型在线 · ' + esc(h.model || "") + "</span>");
      } else {
        pills.push('<span class="pill warn">离线模式 · 规则兜底</span>');
      }
      if (h.schema_blocking) {
        pills.push('<span class="pill bad">Schema 有阻断问题</span>');
      } else {
        pills.push('<span class="pill ok">Schema 校验通过</span>');
      }
      $("pills").innerHTML = pills.join("");
    }).catch(function () {
      $("pills").innerHTML = '<span class="pill bad">服务未连接</span>';
    });
  }

  /* ---------------- 提交 ---------------- */
  function submit() {
    if (pendingFile) return submitFile(pendingFile);
    var q = $("q").value.trim();
    if (!q) { $("q").focus(); return; }
    var btn = $("ask");
    btn.disabled = true; btn.textContent = "分析中…";
    hideError();
    showLoading();
    fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: q, session_id: sessionId() })
    }).then(function (r) {
      return r.json().then(function (body) { return { status: r.status, body: body }; });
    }).then(function (res) {
      if (res.status !== 200) { showError(res.body && res.body.error, res.body && res.body.trace_id); return; }
      render(res.body);
    }).catch(function (e) {
      showError("请求失败：" + e.message + "（请确认服务仍在运行）");
    }).then(function () {
      btn.disabled = false; btn.textContent = "分析";
    });
  }

  function submitFile(file) {
    var q = $("q").value.trim();
    var btn = $("ask");
    btn.disabled = true; btn.textContent = "分析中…";
    hideError();
    showLoading();
    var fd = new FormData();
    fd.append("file", file);
    fd.append("question", q);
    fd.append("session_id", sessionId());
    fetch("/api/excel", { method: "POST", body: fd }).then(function (r) {
      return r.json().then(function (body) { return { status: r.status, body: body }; });
    }).then(function (res) {
      if (res.status !== 200) { showError(res.body && res.body.error, res.body && res.body.trace_id); return; }
      clearPending();
      render(res.body);
    }).catch(function (e) {
      showError("上传失败：" + e.message);
    }).then(function () {
      btn.disabled = false;
      btn.textContent = pendingFile ? "分析这个文件" : "分析";
    });
  }

  /* ---------------- 文件选择 / 拖拽 ---------------- */
  var ACCEPT = [".xlsx", ".xlsm", ".xls", ".csv"];
  var MAX_BYTES = 20 * 1024 * 1024;
  var pendingFile = null;

  function extOf(name) {
    var i = (name || "").lastIndexOf(".");
    return i < 0 ? "" : name.slice(i).toLowerCase();
  }

  function fmtSize(n) {
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / 1024 / 1024).toFixed(1) + " MB";
  }

  /* 只登记文件，不立刻上传：先让人看清文件名和大小，再点"分析" */
  function setPendingFile(f) {
    if (!f) return false;
    var ext = extOf(f.name);
    if (ACCEPT.indexOf(ext) < 0) {
      showError("不支持 " + (ext || "这种") + " 文件；可用格式：" + ACCEPT.join(" / "));
      return false;
    }
    if (!f.size) { showError("这个文件是空的（0 字节）"); return false; }
    if (f.size > MAX_BYTES) {
      showError("文件 " + fmtSize(f.size) + " 超过 20 MB 上限");
      return false;
    }
    pendingFile = f;
    hideError();
    $("fcbadge").textContent = ext.replace(".", "").toUpperCase();
    $("fcname").textContent = f.name;
    $("fcsize").textContent = fmtSize(f.size);
    $("filechip").classList.remove("hidden");
    $("ask").textContent = "分析这个文件";
    return true;
  }

  function clearPending() {
    pendingFile = null;
    $("filechip").classList.add("hidden");
    $("fcname").textContent = "";
    $("fcsize").textContent = "";
    $("ask").textContent = "分析";
    $("file").value = "";
  }

  function sessionId() {
    try {
      var k = "stat_agent_sid", v = localStorage.getItem(k);
      if (!v) {
        v = "web-" + Math.random().toString(36).slice(2, 10);
        localStorage.setItem(k, v);
      }
      return v;
    } catch (e) { return "web"; }
  }

  /* ---------------- 渲染 ---------------- */
  function showLoading() {
    var box = $("result");
    box.classList.remove("hidden");
    $("stages").innerHTML = '<span class="stage">正在执行分析链路…</span>';
    $("charts").innerHTML = "";
    $("report").innerHTML =
      '<div class="skeleton loading-line" style="width:40%"></div>' +
      '<div class="skeleton loading-line" style="width:88%"></div>' +
      '<div class="skeleton loading-line" style="width:76%"></div>' +
      '<div class="skeleton loading-line" style="width:82%"></div>';
    $("footnote").innerHTML = "";
  }

  function render(r) {
    hasResult = true;
    renderStages(r);
    renderCharts(r.charts || []);
    $("report").innerHTML = markdown((r.report || {}).markdown || "") +
      '<div class="closing">本报告的统计量由 Python（scipy / statsmodels）计算，' +
      '模型只负责理解问题与组织文字；报告生成后已逐项核对数字来源。</div>';
    renderTabs(r);
    renderFootnote(r);
    $("result").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function renderStages(r) {
    var t = r.timings || {}, html = [], rejected = !!(r.plan || {}).reject;
    STAGES.forEach(function (s, idx) {
      var ms = t[s[0]];
      if (ms == null) return;
      var cls = "stage" + (rejected && s[0] === "route_ms" ? " reject" : "");
      html.push('<span class="' + cls + '"><b>' + s[1] + "</b>" +
        '<span class="ms">' + fmtMs(ms) + "</span></span>");
      if (idx < STAGES.length - 1) html.push('<span class="arrow">›</span>');
    });
    if (t.total_ms != null) {
      html.push('<span class="stage"><b>合计</b><span class="ms">' + fmtMs(t.total_ms) + "</span></span>");
    }
    (r.degraded || []).forEach(function (d) {
      html.push('<span class="stage" style="border-color:#f2ddb4;background:#fdf3e0;color:#9a6200">降级 ' + esc(d) + "</span>");
    });
    $("stages").innerHTML = html.join("");
  }

  function renderCharts(charts) {
    var box = $("charts");
    if (!charts.length) { box.innerHTML = ""; return; }
    box.innerHTML = charts.map(function (c) {
      return '<div class="chart">' + (c.svg || "") + "</div>";
    }).join("");
  }

  function renderTabs(r) {
    var plan = r.plan || {}, checks = r.sql_checks || [];

    /* SQL */
    var isExcel = (r.data_source || "sql").indexOf("excel") === 0;
    var sqlHtml = r.sql
      ? '<pre class="code">' + esc(r.sql) + "</pre>"
      : '<div class="empty">' + (isExcel
          ? "本次没有生成 SQL：数据来自上传的表格文件，不需要查询数据库。"
          : "本次没有生成 SQL。") + "</div>";
    sqlHtml += '<div style="margin-top:10px">';
    sqlHtml += '<span class="tag">来源：' + esc(r.sql_source || "-") + "</span>";
    if (r.sql_note) sqlHtml += '<span class="tag">' + esc(r.sql_note) + "</span>";
    sqlHtml += "</div>";
    if (checks.length) {
      sqlHtml += '<div style="margin-top:12px">';
      checks.forEach(function (c) {
        var ok = c.passed !== false && c.ok !== false;
        sqlHtml += '<div class="check"><span class="dot ' + (ok ? "ok" : "bad") + '"></span><div>' +
          "<b>" + esc(c.layer) + "</b> " + (c.note ? '<span class="note">' + esc(c.note) + "</span>" : "") +
          "</div></div>";
      });
      sqlHtml += "</div>";
    }
    $("panel-sql").innerHTML = sqlHtml;

    /* 数据预览 */
    var cols = r.columns || [], rows = r.rows || [];
    if (!rows.length) {
      $("panel-data").innerHTML = '<div class="empty">本次没有返回数据行。</div>';
    } else {
      var preview = rows.slice(0, 100);
      var h = '<div class="empty" style="margin-bottom:8px">共 ' + r.n_rows + " 行" +
        (rows.length > preview.length ? "，预览前 " + preview.length + " 行" : "") + "。</div>";
      h += '<div class="table-wrap"><table class="grid"><thead><tr>';
      cols.forEach(function (c) { h += "<th>" + esc(c) + "</th>"; });
      h += "</tr></thead><tbody>";
      preview.forEach(function (row) {
        h += "<tr>";
        row.forEach(function (v) {
          var num = typeof v === "number";
          h += "<td" + (num ? ' class="num"' : "") + ">" +
            esc(num ? (Math.round(v * 100) / 100) : (v == null ? "—" : v)) + "</td>";
        });
        h += "</tr>";
      });
      h += "</tbody></table></div>";
      $("panel-data").innerHTML = h;
    }

    /* 决策轨迹 */
    var p = '<div class="kv">' +
      "<dt>意图</dt><dd>" + esc((r.intent || {}).label || (r.intent || {}).intent || "-") + "</dd>" +
      "<dt>统计方法</dt><dd>" + esc(plan.label || plan.method || "-") + "</dd>" +
      "<dt>数据结构</dt><dd>" + esc(((plan.structure || {}).kind) || "-") + "</dd>" +
      "<dt>有效样本</dt><dd>" + esc(r.n_rows == null ? "-" : r.n_rows) + "</dd>" +
      "</div>";
    if (plan.reject) {
      p += '<div class="check" style="margin-top:12px"><span class="dot bad"></span><div>' +
        "<b>拒绝建模</b> " + esc(plan.reason || "") +
        (plan.suggestion ? '<div class="note">建议：' + esc(plan.suggestion) + "</div>" : "") +
        "</div></div>";
    }
    if ((plan.steps || []).length) {
      p += '<div style="margin-top:14px">';
      plan.steps.forEach(function (s) {
        p += '<div class="check"><span class="dot ' + (s.ok === false ? "bad" : "ok") + '"></span><div>' +
          "<b>" + esc(s.step) + "</b> <span class=\"note\">" + esc(s.detail) + "</span></div></div>";
      });
      p += "</div>";
    }
    if ((plan.warnings || []).length) {
      p += '<div style="margin-top:12px">';
      plan.warnings.forEach(function (w) {
        p += '<div class="check"><span class="tag warn">提示</span><span class="note">' + esc(w) + "</span></div>";
      });
      p += "</div>";
    }
    if ((plan.notes || []).length) {
      p += '<div style="margin-top:12px">';
      plan.notes.forEach(function (n) {
        p += '<div class="check"><span class="tag">数据</span><span class="note">' + esc(n) + "</span></div>";
      });
      p += "</div>";
    }
    $("panel-plan").innerHTML = p;

    /* 运行信息 */
    var tok = r.tokens || {};
    var kv = '<div class="kv">' +
      "<dt>trace_id</dt><dd>" + esc(r.trace_id) + "</dd>" +
      "<dt>会话</dt><dd>" + esc(r.session_id) + "</dd>" +
      "<dt>提问者</dt><dd>" + esc(r.actor) + "</dd>" +
      "<dt>时间</dt><dd>" + esc(r.ts) + "</dd>" +
      "<dt>数据来源</dt><dd>" + esc(r.data_source || "sql") + "</dd>" +
      "<dt>SQL 来源</dt><dd>" + esc(r.sql_source || "-") + "</dd>" +
      "<dt>返回行数</dt><dd>" + esc(r.n_rows) + "</dd>" +
      "<dt>Token</dt><dd>" + esc((tok.prompt_tokens || 0) + " 输入 / " + (tok.completion_tokens || 0) + " 输出") + "</dd>" +
      "<dt>审计</dt><dd>已写入 data/audit.db（哈希链可校验）</dd>" +
      "</div>";
    if (r.error) {
      kv += '<div class="check" style="margin-top:10px"><span class="dot bad"></span><span>' + esc(r.error) + "</span></div>";
    }
    $("panel-meta").innerHTML = kv;
  }

  function renderFootnote(r) {
    var flags = ((r.report || {}).flags) || [];
    var bits = [];
    bits.push("trace_id " + r.trace_id);
    if ((r.report || {}).source) {
      bits.push("报告来源：" + ({ llm: "模型撰写（数字已核对）", template: "规则模板",
        rejected: "拒绝建模说明" }[r.report.source] || r.report.source));
    }
    if (flags.length) bits.push("标记：" + flags.join("、"));
    bits.push("耗时 " + fmtMs((r.timings || {}).total_ms));
    $("footnote").innerHTML = bits.map(esc).join("　·　");
  }

  /* ---------------- 历史记录 ---------------- */
  var hasResult = false;

  function showView(name) {
    $("askview").classList.toggle("hidden", name !== "ask");
    $("historyview").classList.toggle("hidden", name !== "history");
    if (name === "ask") {
      if (hasResult) $("result").classList.remove("hidden");
    } else {
      $("result").classList.add("hidden");
      $("errorbox").classList.add("hidden");
    }
    Array.prototype.forEach.call(document.querySelectorAll(".viewtab"), function (b) {
      b.classList.toggle("active", b.getAttribute("data-view") === name);
    });
    if (name === "history") loadHistory();
  }

  function loadHistory() {
    $("histlist").innerHTML = '<div class="empty">加载中…</div>';
    fetch("/api/history?limit=20").then(function (r) { return r.json(); }).then(function (d) {
      var items = d.items || [];
      $("histsub").textContent = d.total
        ? "共 " + d.total + " 条，显示最近 " + items.length + " 条。点开可回看当时的完整报告与图表。"
        : "还没有记录。回到「分析」提一个问题，这里就会出现。";
      renderHistoryList(items);
    }).catch(function (e) {
      $("histlist").innerHTML = '<div class="empty">加载失败：' + esc(e.message) + "</div>";
    });
  }

  function renderHistoryList(items) {
    if (!items.length) { $("histlist").innerHTML = '<div class="empty">暂无历史记录。</div>'; return; }
    $("histlist").innerHTML = items.map(function (it) {
      var badges = [];
      if (it.data_source && it.data_source !== "sql") badges.push('<span class="tag">Excel</span>');
      if (it.degraded) badges.push('<span class="tag warn">降级</span>');
      if (it.ok === 0) badges.push('<span class="tag bad">未完成</span>');
      return '<div class="hist-item" data-id="' + it.id + '">' +
        '<div class="hist-top"><span class="hist-id">#' + it.id + "</span>" +
        '<span class="hist-time">' + esc(it.ts) + "</span>" +
        '<span class="hist-intent">' + esc(it.intent_label || it.intent || "-") + "</span>" +
        badges.join("") + "</div>" +
        '<div class="hist-q">' + esc(it.question) + "</div>" +
        '<div class="hist-sum">' + esc(it.summary || "") + "</div>" +
        '<div class="hist-meta">' + esc(it.method || "未建模") + " · " +
        esc(it.n_rows) + " 行 · " + fmtMs(it.latency_ms) + "</div>" +
        "</div>";
    }).join("");
  }

  function openHistory(id) {
    $("histlist").innerHTML = '<div class="empty">加载中…</div>';
    fetch("/api/history/" + id).then(function (r) { return r.json(); }).then(function (it) {
      if (it.error) { $("histlist").innerHTML = '<div class="empty">' + esc(it.error) + "</div>"; return; }
      var charts = (it.charts || []).map(function (c) { return '<div class="chart">' + (c.svg || "") + "</div>"; }).join("");
      var html = '<button class="ghost" id="histback">← 返回列表</button>';
      html += '<div class="hist-detail">';
      html += '<h2>' + esc(it.question || "（无问题）") + "</h2>";
      html += '<div class="kv">' +
        "<dt>时间</dt><dd>" + esc(it.ts) + "</dd>" +
        "<dt>意图</dt><dd>" + esc(it.intent_label || it.intent || "-") + "</dd>" +
        "<dt>统计方法</dt><dd>" + esc(it.method || "-") + "</dd>" +
        "<dt>数据来源</dt><dd>" + esc(it.data_source || "sql") + "</dd>" +
        "<dt>样本 / 耗时</dt><dd>" + esc(it.n_rows) + " 行 · " + fmtMs(it.latency_ms) + "</dd>" +
        "<dt>trace_id</dt><dd>" + esc(it.trace_id || "-") + "</dd>" +
        "</div>";
      if (it.degraded) html += '<div class="check"><span class="tag warn">降级</span><span class="note">' + esc(it.degraded) + "</span></div>";
      if (charts) html += '<div class="charts">' + charts + "</div>";
      html += '<div class="report">' + markdown((it.report || {}).markdown || "") + "</div>";
      if (it.sql) html += "<h3>执行过的 SQL</h3><pre class=\"code\">" + esc(it.sql) + "</pre>";
      html += "</div>";
      $("histlist").innerHTML = html;
      $("histback").addEventListener("click", loadHistory);
      window.scrollTo({ top: 0, behavior: "smooth" });
    }).catch(function (e) {
      $("histlist").innerHTML = '<div class="empty">加载失败：' + esc(e.message) + "</div>";
    });
  }

  function initHistory() {
    $("histlist").addEventListener("click", function (e) {
      var t = e.target.closest(".hist-item");
      if (!t) return;
      openHistory(t.getAttribute("data-id"));
    });
    $("histrefresh").addEventListener("click", loadHistory);
    $("viewtabs").addEventListener("click", function (e) {
      var t = e.target.closest(".viewtab");
      if (t) showView(t.getAttribute("data-view"));
    });
  }

  function showError(msg, traceId) {
    var box = $("errorbox");
    box.classList.remove("hidden");
    box.innerHTML = "<b>分析未能完成</b>" + esc(msg || "未知错误") +
      (traceId ? '<div style="margin-top:6px;font-size:12px">trace_id：' + esc(traceId) + "</div>" : "");
    $("result").classList.add("hidden");
  }

  function hideError() { $("errorbox").classList.add("hidden"); }

  /* ---------------- 事件 ---------------- */
  function initSamples() {
    $("samples").innerHTML = SAMPLES.slice(0, 4).map(function (s) {
      return '<span class="chip" data-q="' + esc(s) + '">' + esc(s) + "</span>";
    }).join("");
    $("samples").addEventListener("click", function (e) {
      var t = e.target.closest(".chip");
      if (!t) return;
      $("q").value = t.getAttribute("data-q");
      $("q").focus();
    });
  }

  function initTabs() {
    $("tabbar").addEventListener("click", function (e) {
      var t = e.target.closest(".tab");
      if (!t) return;
      Array.prototype.forEach.call(document.querySelectorAll(".tab"), function (b) {
        b.classList.toggle("active", b === t);
      });
      ["sql", "data", "plan", "meta"].forEach(function (k) {
        $("panel-" + k).classList.toggle("hidden", k !== t.getAttribute("data-tab"));
      });
    });
  }

  function initUpload() {
    var dz = $("dropzone"), input = $("file");

    dz.addEventListener("click", function () { input.click(); });
    dz.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); }
    });

    input.addEventListener("change", function (e) {
      var f = e.target.files && e.target.files[0];
      if (f) setPendingFile(f);
      e.target.value = "";              // 允许再次选中同一个文件
    });

    $("fcx").addEventListener("click", function (e) {
      e.stopPropagation();
      clearPending();
    });

    ["dragenter", "dragover"].forEach(function (name) {
      dz.addEventListener(name, function (e) {
        e.preventDefault(); e.stopPropagation();
        dz.classList.add("dragover");
      });
    });
    ["dragleave", "dragend"].forEach(function (name) {
      dz.addEventListener(name, function (e) {
        e.preventDefault(); e.stopPropagation();
        // 在子元素之间移动也会触发 dragleave，用 relatedTarget 判一下免得闪
        if (name === "dragleave" && e.relatedTarget && dz.contains(e.relatedTarget)) return;
        dz.classList.remove("dragover");
      });
    });
    dz.addEventListener("drop", function (e) {
      e.preventDefault(); e.stopPropagation();
      dz.classList.remove("dragover");
      var dt = e.dataTransfer;
      var f = dt && dt.files && dt.files[0];
      if (f) setPendingFile(f);
    });

    // 拖到页面其它地方时，别让浏览器直接打开这个文件
    ["dragover", "drop"].forEach(function (name) {
      window.addEventListener(name, function (e) { e.preventDefault(); });
    });
  }

  function init() {
    initSamples();
    initTabs();
    initHistory();
    initUpload();
    loadHealth();
    $("ask").addEventListener("click", submit);
    $("q").addEventListener("keydown", function (e) {
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); submit(); }
    });
    // 带 ?q=... 打开可直接看到结果，方便分享链接与截图
    var m = /[?&]q=([^&]*)/.exec(location.search);
    if (m) {
      var text = decodeURIComponent(m[1].replace(/\+/g, " "));
      if (text) { $("q").value = text; submit(); }
    }
  }

  document.addEventListener("DOMContentLoaded", init);
})();
