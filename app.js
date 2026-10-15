/* Artifex — comportamento do navegador. Sem dependências; nunca usa innerHTML com dados do servidor. */
(function () {
  "use strict";
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var csrf = function () { var m = $('meta[name="csrf"]'); return m ? m.content : ""; };
  var el = function (tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  };

  /* ---------- Menu lateral no celular ---------- */
  var toggle = $("[data-menu-toggle]");
  function setMenu(open) {
    document.body.classList.toggle("menu-open", open);
    if (toggle) toggle.setAttribute("aria-expanded", open ? "true" : "false");
    var scrim = $(".scrim"); if (scrim) scrim.hidden = !open;
  }
  if (toggle) toggle.addEventListener("click", function () { setMenu(!document.body.classList.contains("menu-open")); });
  $$("[data-menu-close]").forEach(function (b) { b.addEventListener("click", function () { setMenu(false); }); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") setMenu(false); });
  $$(".sidebar a").forEach(function (a) { a.addEventListener("click", function () { setMenu(false); }); });

  /* ---------- Carrosséis horizontais ---------- */
  $$("[data-carousel]").forEach(function (c) {
    var track = $(".track", c), prev = $(".prev", c), next = $(".next", c);
    function update() {
      var max = track.scrollWidth - track.clientWidth - 2;
      if (prev) prev.hidden = track.scrollLeft <= 2;
      if (next) next.hidden = track.scrollLeft >= max;
    }
    if (prev) prev.addEventListener("click", function () { track.scrollBy({ left: -track.clientWidth * 0.9, behavior: "smooth" }); });
    if (next) next.addEventListener("click", function () { track.scrollBy({ left: track.clientWidth * 0.9, behavior: "smooth" }); });
    track.addEventListener("scroll", update, { passive: true });
    window.addEventListener("resize", update);
    track.addEventListener("keydown", function (e) {
      if (e.key === "ArrowRight") track.scrollBy({ left: 200, behavior: "smooth" });
      if (e.key === "ArrowLeft") track.scrollBy({ left: -200, behavior: "smooth" });
    });
    update();
  });

  /* ---------- Banner principal ---------- */
  $$("[data-hero]").forEach(function (h) {
    var slides = $$(".hero-slide", h), dots = $(".hero-dots", h), i = 0, timer = null;
    if (!slides.length) return;
    var buttons = [];
    function show(n) {
      i = (n + slides.length) % slides.length;
      slides.forEach(function (s, k) { s.classList.toggle("is-active", k === i); });
      buttons.forEach(function (b, k) { b.setAttribute("aria-current", k === i ? "true" : "false"); });
    }
    if (slides.length > 1) {
      slides.forEach(function (_, k) {
        var b = el("button"); b.type = "button"; b.setAttribute("aria-label", "Banner " + (k + 1));
        b.addEventListener("click", function () { show(k); restart(); });
        dots.appendChild(b); buttons.push(b);
      });
      var p = $(".prev", h), n = $(".next", h);
      if (p) { p.hidden = false; p.addEventListener("click", function () { show(i - 1); restart(); }); }
      if (n) { n.hidden = false; n.addEventListener("click", function () { show(i + 1); restart(); }); }
      var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      var restart = function () { if (timer) clearInterval(timer); if (!reduce) timer = setInterval(function () { show(i + 1); }, 6000); };
      h.addEventListener("mouseenter", function () { if (timer) clearInterval(timer); });
      h.addEventListener("mouseleave", restart);
      var sx = null;
      h.addEventListener("touchstart", function (e) { sx = e.touches[0].clientX; }, { passive: true });
      h.addEventListener("touchend", function (e) {
        if (sx == null) return;
        var dx = e.changedTouches[0].clientX - sx;
        if (Math.abs(dx) > 40) { show(dx < 0 ? i + 1 : i - 1); restart(); }
        sx = null;
      });
      restart();
    }
    show(0);
  });

  /* ---------- Busca com sugestões e histórico ---------- */
  $$("[data-search]").forEach(function (form) {
    var input = $("input[name=q]", form), box = $("[data-suggest]", form), timer = null, ctl = null;
    function close() { box.hidden = true; box.textContent = ""; }
    function row(item) {
      var a = el("a"); a.href = item.url;
      if (item.thumb) { var im = el("img"); im.src = item.thumb; im.alt = ""; a.appendChild(im); } else a.appendChild(el("span", "ph"));
      var t = el("span", "t"); t.appendChild(el("b", null, item.title));
      t.appendChild(el("span", "muted small", item.category + " · " + (item.free ? "Gratuito" : item.price)));
      a.appendChild(t); return a;
    }
    function render(items) {
      box.textContent = "";
      if (!items.length) { box.appendChild(el("div", "head", "Nenhum resultado")); }
      items.forEach(function (it) { box.appendChild(row(it)); });
      box.hidden = false;
    }
    function history() {
      fetch("/api/search-history", { headers: { Accept: "application/json" } }).then(function (r) { return r.json(); }).then(function (d) {
        if (!d.items.length) return close();
        box.textContent = "";
        var head = el("div", "head"); head.appendChild(el("span", null, "Pesquisas recentes"));
        var clr = el("button", null, "Limpar histórico"); clr.type = "button";
        clr.addEventListener("click", function () {
          fetch("/search/history/clear", { method: "POST", headers: { "X-CSRF-Token": csrf(), "X-Requested-With": "fetch" } }).then(close);
        });
        head.appendChild(clr); box.appendChild(head);
        d.items.forEach(function (q) {
          var b = el("button", null, q); b.type = "button";
          b.addEventListener("click", function () { input.value = q; form.submit(); });
          box.appendChild(b);
        });
        box.hidden = false;
      }).catch(close);
    }
    input.addEventListener("focus", function () { if (!input.value.trim()) history(); });
    input.addEventListener("input", function () {
      clearTimeout(timer);
      var q = input.value.trim();
      if (q.length < 2) { if (!q) history(); else close(); return; }
      timer = setTimeout(function () {
        if (ctl) ctl.abort();
        ctl = window.AbortController ? new AbortController() : null;
        fetch("/api/suggest?q=" + encodeURIComponent(q), { signal: ctl ? ctl.signal : undefined, headers: { Accept: "application/json" } })
          .then(function (r) { return r.json(); }).then(function (d) { if (input.value.trim() === q) render(d.items); }).catch(function () {});
      }, 200);
    });
    document.addEventListener("click", function (e) { if (!form.contains(e.target)) close(); });
    input.addEventListener("keydown", function (e) { if (e.key === "Escape") close(); });
  });

  /* ---------- Confirmações ---------- */
  document.addEventListener("submit", function (e) {
    var msg = e.target.getAttribute && e.target.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) e.preventDefault();
  });

  /* ---------- Galeria da página do recurso ---------- */
  $$("[data-gallery]").forEach(function (g) {
    var main = $("[data-gallery-main]", g);
    $$("[data-src]", g).forEach(function (b) {
      b.addEventListener("click", function () {
        main.textContent = "";
        var node;
        if (b.getAttribute("data-type") === "video") {
          node = el("video"); node.controls = true; node.preload = "metadata"; node.src = b.getAttribute("data-src");
        } else { node = el("img"); node.alt = ""; node.src = b.getAttribute("data-src"); }
        main.appendChild(node);
        $$("[data-src]", g).forEach(function (o) { o.setAttribute("aria-current", o === b ? "true" : "false"); });
      });
    });
  });

  /* ---------- Compartilhar ---------- */
  $$("[data-share]").forEach(function (b) {
    b.addEventListener("click", function () {
      var url = b.getAttribute("data-share") || location.href;
      if (navigator.share) { navigator.share({ title: document.title, url: url }).catch(function () {}); return; }
      if (navigator.clipboard) navigator.clipboard.writeText(url).then(function () { var o = b.textContent; b.textContent = "Link copiado!"; setTimeout(function () { b.textContent = o; }, 1800); });
    });
  });

  /* ---------- Upload com barra de progresso ---------- */
  $$("form[data-upload]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      var hasFile = $$("input[type=file]", form).some(function (i) { return i.files && i.files.length; });
      if (!hasFile || !window.XMLHttpRequest || !window.FormData) return;
      e.preventDefault();
      var fd = new FormData(form);
      if (e.submitter && e.submitter.name) fd.append(e.submitter.name, e.submitter.value);
      var bar = $("progress", form), label = $("[data-progress-label]", form), xhr = new XMLHttpRequest();
      $$("button, input[type=submit]", form).forEach(function (b) { b.disabled = true; });
      if (bar) { bar.hidden = false; bar.value = 0; }
      xhr.upload.addEventListener("progress", function (ev) {
        if (!ev.lengthComputable) return;
        var p = Math.round(ev.loaded / ev.total * 100);
        if (bar) bar.value = p;
        if (label) label.textContent = p < 100 ? "Enviando… " + p + "%" : "Processando e verificando os arquivos…";
      });
      xhr.addEventListener("load", function () {
        if (xhr.status === 413 || xhr.status === 400 || xhr.status >= 500) {
          if (label) label.textContent = xhr.status === 413 ? "O envio é maior que o limite permitido pelo servidor." : "Não foi possível concluir o envio. Recarregue a página e tente de novo.";
          $$("button, input[type=submit]", form).forEach(function (b) { b.disabled = false; });
          return;
        }
        window.location.href = xhr.responseURL || window.location.href;
      });
      xhr.addEventListener("error", function () {
        if (label) label.textContent = "Falha de conexão durante o envio. Tente novamente.";
        $$("button, input[type=submit]", form).forEach(function (b) { b.disabled = false; });
      });
      xhr.open("POST", form.action || window.location.href);
      xhr.setRequestHeader("X-CSRF-Token", csrf());
      xhr.send(fd);
    });
  });

  /* ---------- Contador de caracteres ---------- */
  $$("[data-count]").forEach(function (t) {
    var out = $(t.getAttribute("data-count"));
    function up() { if (out) out.textContent = t.value.length + "/" + t.maxLength; }
    t.addEventListener("input", up); up();
  });

  /* ---------- Preço pago/gratuito ---------- */
  $$("[data-price-mode]").forEach(function (wrap) {
    var radios = $$("input[type=radio]", wrap), box = $("[data-price-box]", wrap);
    function up() { var paid = radios.some(function (r) { return r.checked && r.value === "paid"; }); box.hidden = !paid; }
    radios.forEach(function (r) { r.addEventListener("change", up); }); up();
  });
})();
