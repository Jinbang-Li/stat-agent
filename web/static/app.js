/* 县域经济统计分析 Agent —— 前端逻辑（无框架，无外链） */
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
    var sqlHtml = '<pre class="code">' + esc(r.sql || "（未生成 SQL）") + "</pre>";
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
      "<dt>SQL 来源</dt><dd>" + esc(r.sql_source) + "</dd>" +
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

  function init() {
    initSamples();
    initTabs();
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
