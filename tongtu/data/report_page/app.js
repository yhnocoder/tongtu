/* 通途检验页逻辑（架构 §11）。
 *
 * 红线：本页只读产物包里已有的文件。任何需要服务端或 LLM 调用的功能一律归文枢，
 * 这里永不添加——不发请求、不存状态、不写盘。
 *
 * 经典脚本（非 ESM）：file:// 下浏览器按 CORS 拒绝加载 module 脚本，双击打开就白屏。
 * 同理这里不用 fetch 读同目录文件（file:// 下同样被拒），数据走 report-data.js。
 */
(function () {
  "use strict";

  var DATA = window.TONGTU_REPORT || {};
  var REPORT = DATA.report || {};
  var ANCHORS = (DATA.anchors && DATA.anchors.anchors) || [];
  var PDF_META = (DATA.anchors && DATA.anchors.pdf) || {};
  var BLOCKS = DATA.blocks || {};
  var FIGURES = (DATA.figures && DATA.figures.figures) || [];
  var VENDOR = "vendor/pdfjs/";

  var el = {
    pages: document.getElementById("pages"),
    status: document.getElementById("viewer-status"),
    topmeta: document.getElementById("topmeta"),
    detail: document.getElementById("detail"),
    detailTitle: document.getElementById("detail-title"),
    detailMeta: document.getElementById("detail-meta"),
    detailTex: document.getElementById("detail-tex"),
    showHotspots: document.getElementById("show-hotspots"),
    showDegraded: document.getElementById("show-degraded")
  };

  var TYPE_LABEL = {
    section: "章节",
    equation: "公式",
    figure: "图",
    table: "表",
    algorithm: "算法",
    block: "块",
    citation: "引用"
  };

  // ------------------------------------------------------------------ 小工具

  function h(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) {
        if (key === "class") node.className = attrs[key];
        else if (key === "text") node.textContent = attrs[key];
        else if (attrs[key] !== null && attrs[key] !== undefined) node.setAttribute(key, attrs[key]);
      });
    }
    (children || []).forEach(function (child) {
      node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    });
    return node;
  }

  function table(rows) {
    var body = h("tbody", null, rows.filter(Boolean).map(function (row) {
      return h("tr", null, [h("th", { text: row[0] }), h("td", { text: String(row[1]) })]);
    }));
    return h("table", { class: "kv" }, [body]);
  }

  function base64ToBytes(b64) {
    var raw = atob(b64);
    var out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }

  function loadScript(src) {
    return new Promise(function (resolve, reject) {
      var node = document.createElement("script");
      node.src = src;
      node.onload = resolve;
      node.onerror = function () { reject(new Error("加载失败：" + src)); };
      document.head.appendChild(node);
    });
  }

  // ------------------------------------------------------------------ 侧栏

  function renderOverview() {
    var panel = document.getElementById("panel-overview");
    var paper = REPORT.paper || {};
    var validation = REPORT.validation || {};
    var compile = REPORT.compile || {};
    var counts = {};
    ANCHORS.forEach(function (a) { counts[a.source] = (counts[a.source] || 0) + 1; });

    panel.appendChild(h("h3", { text: "论文" }));
    panel.appendChild(table([
      ["arXiv id", paper.arxiv_id || "—"],
      paper.title ? ["标题", paper.title] : null,
      ["状态", REPORT.status || "—"],
      ["契约版本", REPORT.contract_version || "—"],
      ["通途版本", REPORT.tongtu_version || "—"],
      ["生成时间", DATA.generated_at || "—"]
    ]));

    panel.appendChild(h("h3", { text: "校验统计" }));
    panel.appendChild(table([
      ["块总数", validation.chunks_total || 0],
      ["已翻译", validation.translated || 0],
      ["缓存命中", validation.cached || 0],
      ["回退", validation.fallback || 0],
      ["重试", validation.retries || 0],
      ["掩码往返自检", validation.mask_roundtrip_ok === false ? "未通过" : "通过"]
    ]));

    panel.appendChild(h("h3", { text: "编译" }));
    panel.appendChild(table([
      ["结果", compile.passed ? "通过" : "未通过"],
      ["引擎", compile.engine || "—"],
      ["编译次数", compile.passes || 0],
      ["原文 baseline", compile.baseline_passed === false ? "未通过" : "通过"],
      ["注入分支", (compile.inject && compile.inject.branch) || "—"],
      ["documentclass", (compile.inject && compile.inject.documentclass) || "—"]
    ]));

    panel.appendChild(h("h3", { text: "锚点来源" }));
    panel.appendChild(table([
      ["总数", ANCHORS.length],
      ["synctex（精确）", counts.synctex || 0],
      ["blocks（页级降级）", counts.blocks || 0],
      ["页数", PDF_META.page_count || 0]
    ]));
    if (!counts.synctex && ANCHORS.length) {
      panel.appendChild(h("p", {
        class: "note",
        text: "本包没有 zh.synctex.gz，全部锚点退化为页级：热区画整页虚线框，页码为估计值。"
      }));
    }

    var stages = REPORT.stages || [];
    if (stages.length) {
      panel.appendChild(h("h3", { text: "阶段" }));
      panel.appendChild(table(stages.map(function (s) {
        return [s.name, s.status + (s.duration_ms ? " · " + s.duration_ms + " ms" : "")];
      })));
    }

    var interventions = REPORT.agent_interventions || [];
    panel.appendChild(h("h3", { text: "agent 关节干预（" + interventions.length + "）" }));
    if (!interventions.length) {
      panel.appendChild(h("p", { class: "empty", text: "本次运行没有拉起任何 agent 关节。" }));
    } else {
      panel.appendChild(table(interventions.map(function (item) {
        return [item.joint + " · " + item.primitive, item.outcome + (item.action ? " · " + item.action : "")];
      })));
    }

    var artifacts = REPORT.artifacts || [];
    if (artifacts.length) {
      panel.appendChild(h("h3", { text: "产物包" }));
      panel.appendChild(table(artifacts.map(function (item) {
        var mark = item.schema_valid === null || item.schema_valid === undefined
          ? "—"
          : (item.schema_valid ? "schema 通过" : "schema 未通过");
        return [item.path, mark + " · " + (item.bytes || 0) + " B"];
      })));
    }
  }

  function anchorLabel(anchor) {
    return anchor.title || anchor.label || anchor.block_id || anchor.id;
  }

  function renderAnchorList() {
    var panel = document.getElementById("panel-anchors");
    if (!ANCHORS.length) {
      panel.appendChild(h("p", { class: "empty", text: "没有锚点。" }));
      return;
    }
    var list = h("ul", { class: "list" }, ANCHORS.map(function (anchor) {
      var item = h("li", { class: anchor.source === "synctex" ? "" : "degraded" }, [
        h("span", { class: "page-no", text: "p" + anchor.page }),
        h("span", { class: "kind", text: TYPE_LABEL[anchor.type] || anchor.type }),
        h("span", { class: "name", text: anchorLabel(anchor) })
      ]);
      item.addEventListener("click", function () { focusAnchor(anchor.id); });
      return item;
    }));
    panel.appendChild(list);
    panel.appendChild(h("p", {
      class: "note",
      text: "虚线热区 = 页级降级（没有 synctex 精确坐标）；实线 = synctex 定位。"
    }));
  }

  function renderFallbacks() {
    var panel = document.getElementById("panel-fallbacks");
    var fallbacks = REPORT.fallbacks || [];
    if (!fallbacks.length) {
      panel.appendChild(h("p", { class: "empty", text: "没有回退块——全篇都是译文。" }));
    } else {
      fallbacks.forEach(function (item) {
        var where = item.paragraphs && item.paragraphs.length
          ? "段落 " + item.paragraphs.join(", ")
          : "整块";
        panel.appendChild(h("div", { class: "fallback" }, [
          h("div", null, [h("code", { class: "mono", text: item.chunk_id }), " · " + where]),
          h("div", { class: "note", text: item.reason + (item.section ? " · " + item.section : "") }),
          item.detail ? h("div", { class: "note mono", text: item.detail }) : h("span")
        ]));
      });
    }
    var warnings = (REPORT.compile && REPORT.compile.warnings) || [];
    panel.appendChild(h("h3", { text: "编译警告（" + warnings.length + "）" }));
    if (!warnings.length) {
      panel.appendChild(h("p", { class: "empty", text: "无。" }));
    } else {
      panel.appendChild(h("ul", { class: "list" }, warnings.map(function (w) {
        return h("li", null, [h("span", { class: "name", text: w.kind + "：" + w.message })]);
      })));
    }
  }

  function renderFigures() {
    var panel = document.getElementById("panel-figures");
    if (!FIGURES.length) {
      panel.appendChild(h("p", { class: "empty", text: "没有预渲染图片。" }));
      return;
    }
    FIGURES.forEach(function (figure) {
      var caption = figure.caption || {};
      var card = h("div", { class: "figure-card" }, [
        h("img", { src: figure.render.path, alt: figure.id, loading: "lazy" }),
        h("div", { class: "cap" }, [
          h("b", { text: figure.id + (figure.label ? " · " + figure.label : "") }),
          " " + (caption.translation || caption.source || "")
        ])
      ]);
      panel.appendChild(card);
    });
  }

  // ------------------------------------------------------------------ 详情

  function showDetail(anchor) {
    var block = anchor.block_id ? BLOCKS[anchor.block_id] : null;
    el.detailTitle.textContent =
      (TYPE_LABEL[anchor.type] || anchor.type) + " · " + (anchor.label || anchor.id);
    var bits = ["第 " + anchor.page + " 页", "来源 " + anchor.source];
    if (anchor.confidence !== undefined) bits.push("置信度 " + anchor.confidence);
    if (anchor.chunk_id) bits.push("翻译块 " + anchor.chunk_id);
    if (block && block.environment) bits.push("环境 " + block.environment);
    if (anchor.title) bits.push(anchor.title);
    el.detailMeta.textContent = bits.join(" · ");
    el.detailTex.textContent = block
      ? block.tex
      : "（该锚点不对应掩码块——章节标题这类锚点直接来自 zh.tex，没有 blocks.json 条目）";
    el.detail.hidden = false;
  }

  function focusAnchor(id) {
    var anchor = ANCHORS.filter(function (a) { return a.id === id; })[0];
    if (!anchor) return;
    var nodes = document.querySelectorAll('[data-anchor="' + id + '"]');
    if (nodes.length) {
      nodes[0].scrollIntoView({ block: "center", behavior: "smooth" });
      Array.prototype.forEach.call(nodes, function (node) {
        node.classList.add("flash");
        setTimeout(function () { node.classList.remove("flash"); }, 900);
      });
    }
    showDetail(anchor);
  }

  // ------------------------------------------------------------------ PDF

  function anchorsOnPage(page) {
    return ANCHORS.filter(function (a) { return a.page === page; });
  }

  function drawOverlay(wrap, page, scale) {
    var overlay = h("div", { class: "overlay" });
    anchorsOnPage(page).forEach(function (anchor) {
      (anchor.rects || []).forEach(function (rect) {
        var hot = h("div", {
          class: "hot" + (anchor.source === "synctex" ? "" : " degraded"),
          "data-anchor": anchor.id,
          title: anchorLabel(anchor)
        });
        hot.style.left = rect.x * scale + "px";
        hot.style.top = rect.y * scale + "px";
        hot.style.width = Math.max(2, rect.w * scale) + "px";
        hot.style.height = Math.max(2, rect.h * scale) + "px";
        hot.addEventListener("click", function () { showDetail(anchor); });
        overlay.appendChild(hot);
      });
    });
    wrap.appendChild(overlay);
    applyToggles();
  }

  function applyToggles() {
    var showAll = el.showHotspots.checked;
    var showDegraded = el.showDegraded.checked;
    Array.prototype.forEach.call(document.querySelectorAll(".hot"), function (node) {
      var degraded = node.classList.contains("degraded");
      node.classList.toggle("hidden", !showAll || (degraded && !showDegraded));
    });
  }

  function pdfBytes() {
    // http(s) 场景：直接取同目录的 zh.pdf（省掉 base64 的 33% 体积）。
    // file:// 场景：fetch 会被浏览器拒绝，退回内嵌的 base64——两条路都要有。
    var name = (DATA.pdf && DATA.pdf.name) || "zh.pdf";
    var embedded = (DATA.pdf && DATA.pdf.base64) || "";
    if (location.protocol === "file:" || typeof fetch !== "function") {
      return Promise.resolve(embedded ? base64ToBytes(embedded) : null);
    }
    return fetch(name)
      .then(function (response) {
        if (!response.ok) throw new Error(String(response.status));
        return response.arrayBuffer();
      })
      .then(function (buffer) { return new Uint8Array(buffer); })
      .catch(function () { return embedded ? base64ToBytes(embedded) : null; });
  }

  function setupWorker() {
    var src = VENDOR + "pdf.worker.min.js";
    window.pdfjsLib.GlobalWorkerOptions.workerSrc = src;
    if (location.protocol !== "file:") return Promise.resolve();
    // file:// 下 new Worker("file://…") 一律被拒；把 worker 脚本注入主线程，
    // pdf.js 见到 globalThis.pdfjsWorker 就直接走主线程通道，不再尝试真 worker。
    return loadScript(src).catch(function () { /* 失败也能跑：pdf.js 自己会退化 */ });
  }

  function renderPdf() {
    if (!window.pdfjsLib) {
      el.status.textContent = "PDF.js 没加载起来（vendor/pdfjs/pdf.min.js 缺失？）。";
      return;
    }
    setupWorker()
      .then(pdfBytes)
      .then(function (bytes) {
        if (!bytes) throw new Error("产物包里没有 zh.pdf 数据");
        return window.pdfjsLib.getDocument({ data: bytes }).promise;
      })
      .then(function (doc) {
        el.status.textContent = "zh.pdf · " + doc.numPages + " 页";
        var width = el.pages.clientWidth || 800;
        var chain = Promise.resolve();
        for (var number = 1; number <= doc.numPages; number++) {
          chain = chain.then(renderPage.bind(null, doc, number, width));
        }
        return chain;
      })
      .catch(function (error) {
        el.status.textContent = "PDF 渲染失败：" + error.message;
      });
  }

  function renderPage(doc, number, width) {
    return doc.getPage(number).then(function (page) {
      var base = page.getViewport({ scale: 1 });
      var scale = Math.min(2, Math.max(0.4, (width - 24) / base.width));
      var viewport = page.getViewport({ scale: scale });
      var canvas = h("canvas");
      canvas.width = Math.floor(viewport.width);
      canvas.height = Math.floor(viewport.height);
      var wrap = h("div", { class: "page-wrap", "data-page": number }, [canvas]);
      el.pages.appendChild(wrap);
      var task;
      try {
        task = page.render({
          canvasContext: canvas.getContext("2d"),
          viewport: viewport
        }).promise;
      } catch (error) {
        task = Promise.reject(error);
      }
      return task
        .catch(function (error) {
          // 某一页画不出来不该连累其它页，更不该把热区一起吞掉——anchors 覆盖层是这页
          // 的主角（架构 §11：画不出热区就无法验收）。
          el.status.textContent = "第 " + number + " 页渲染失败：" + error.message;
        })
        .then(function () {
          // anchors 的坐标是「页面左上角为原点的 pt」，与未旋转页面的画布同向，
          // 故换算只是乘一个 scale。
          drawOverlay(wrap, number, scale);
        });
    });
  }

  // ------------------------------------------------------------------ 启动

  function renderTop() {
    var paper = REPORT.paper || {};
    var status = REPORT.status || "unknown";
    el.topmeta.appendChild(h("span", { class: "badge " + status, text: status }));
    el.topmeta.appendChild(document.createTextNode(
      " " + (paper.arxiv_id || "") + " · 锚点 " + ANCHORS.length +
      " · 回退 " + ((REPORT.validation || {}).fallback || 0) +
      " · 图 " + FIGURES.length
    ));
  }

  function wireTabs() {
    Array.prototype.forEach.call(document.querySelectorAll(".tab"), function (tab) {
      tab.addEventListener("click", function () {
        Array.prototype.forEach.call(document.querySelectorAll(".tab"), function (other) {
          other.classList.toggle("is-active", other === tab);
        });
        Array.prototype.forEach.call(document.querySelectorAll(".panel"), function (panel) {
          panel.classList.toggle("is-active", panel.id === "panel-" + tab.dataset.tab);
        });
      });
    });
  }

  renderTop();
  wireTabs();
  renderOverview();
  renderAnchorList();
  renderFallbacks();
  renderFigures();
  el.showHotspots.addEventListener("change", applyToggles);
  el.showDegraded.addEventListener("change", applyToggles);
  document.getElementById("detail-close").addEventListener("click", function () {
    el.detail.hidden = true;
  });
  renderPdf();
})();
