(function () {
  'use strict';

  var NET_ERR = 'ارتباط برقرار نشد؛ دوباره امتحان کنید.';
  var thread = document.getElementById('thread');
  var composerEl = document.getElementById('composer');
  var input = document.getElementById('composer-input');
  var sendBtn = document.getElementById('send-btn');
  var attachBtn = document.getElementById('attach-btn');
  var fileInput = document.getElementById('file-input');
  var labelEl = document.getElementById('composer-label');
  var errEl = document.getElementById('composer-error');
  var resetBtn = document.getElementById('reset-btn');
  if (!thread || !composerEl) return;

  var MAX_ZIP_MB = parseFloat(composerEl.getAttribute('data-max-zip-mb')) || 50;
  window.MAX_ZIP_MB = MAX_ZIP_MB;
  var mode = 'text';
  var busy = 0;
  var streams = {};

  function csrf() {
    var m = document.querySelector('meta[name="csrf-token"]');
    return m ? m.getAttribute('content') : '';
  }

  function scrollDown() {
    window.scrollTo(0, document.documentElement.scrollHeight);
  }

  function setBusy(on) {
    busy += on ? 1 : -1;
    if (busy < 0) busy = 0;
    sendBtn.disabled = busy > 0;
  }

  function showError(text) {
    errEl.textContent = text || '';
    errEl.hidden = !text;
  }

  function netErrorBubble() {
    var d = document.createElement('div');
    d.className = 'msg msg-bot';
    var b = document.createElement('div');
    b.className = 'bubble bubble-error';
    b.setAttribute('role', 'alert');
    b.textContent = NET_ERR;
    d.appendChild(b);
    thread.appendChild(d);
    scrollDown();
  }

  function applyComposer(c) {
    if (!c) return;
    mode = c.mode === 'password' ? 'password' : 'text';
    input.type = mode === 'password' ? 'password' : 'text';
    input.autocomplete = mode === 'password' ? 'off' : 'off';
    if (mode === 'password') {
      input.setAttribute('autocapitalize', 'off');
      input.setAttribute('spellcheck', 'false');
      input.setAttribute('dir', 'ltr');
    } else {
      input.removeAttribute('autocapitalize');
      input.removeAttribute('spellcheck');
      input.removeAttribute('dir');
    }
    if (typeof c.placeholder === 'string') input.placeholder = c.placeholder;
    if (labelEl) labelEl.textContent = (mode === 'password' && c.label) ? c.label : (c.label || 'پیام');
    if (attachBtn) attachBtn.hidden = !c.attach;
    showError('');
  }

  function upsert(m) {
    var tpl = document.createElement('template');
    tpl.innerHTML = (m.html || '').trim();
    var node = tpl.content.firstElementChild;
    if (!node) return;
    var existing = document.getElementById('msg-' + m.id);
    if (existing) existing.replaceWith(node);
    else thread.appendChild(node);
  }

  function startStreams() {
    var items = thread.querySelectorAll('.progress[data-done="0"]');
    items.forEach(function (el) {
      var id = el.getAttribute('data-job-id');
      if (!id || streams[id]) return;
      var es = new EventSource('/chat/stream?job=' + encodeURIComponent(id));
      streams[id] = es;
      es.addEventListener('message', function (ev) {
        var data;
        try { data = JSON.parse(ev.data); } catch (e) { return; }
        upsert(data);
        scrollDown();
      });
      es.addEventListener('progress', function (ev) {
        var data;
        try { data = JSON.parse(ev.data); } catch (e) { return; }
        upsert({ id: data.message_id, html: data.html });
        scrollDown();
        if (data.done) {
          es.close();
          delete streams[id];
        }
        startStreams();
      });
      es.onerror = function () {
        // EventSource auto-reconnects; stop if the block is already finished.
        var cur = thread.querySelector('.progress[data-job-id="' + id + '"]');
        if (!cur || cur.getAttribute('data-done') === '1') {
          es.close();
          delete streams[id];
        }
      };
    });
  }

  function apply(data) {
    if (data.reset) thread.innerHTML = '';
    (data.messages || []).forEach(upsert);
    applyComposer(data.composer);
    scrollDown();
    startStreams();
  }

  // Returns a promise resolving true on success, false on failure.
  function request(url, opts) {
    setBusy(true);
    opts = opts || {};
    return fetch(url, opts)
      .then(function (r) {
        return r.json().catch(function () { return null; }).then(function (data) {
          if (data && (data.messages || data.composer)) {
            apply(data);
            return r.ok;
          }
          throw new Error('bad response');
        });
      })
      .catch(function () {
        netErrorBubble();
        return false;
      })
      .then(function (ok) { setBusy(false); return ok; });
  }

  function post(url, body) {
    return request(url, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf() },
      body: JSON.stringify(body || {})
    });
  }

  function submit() {
    if (busy) return;
    var val = input.value;
    if (mode === 'password') {
      var key = val.trim();
      if (!key) { showError('کلید را وارد کنید.'); input.focus(); return; }
      input.value = '';
      showError('');
      post('/chat/key', { key: key });
      key = null;
      return;
    }
    var text = val.trim();
    if (!text) return;
    input.value = '';
    showError('');
    post('/chat/send', { text: text });
  }

  composerEl.addEventListener('submit', function (e) { e.preventDefault(); submit(); });
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); }
  });
  input.addEventListener('input', function () { if (mode === 'password' && input.value) showError(''); });

  // Delegated clicks: pills and approval buttons.
  thread.addEventListener('click', function (e) {
    var pill = e.target.closest('.pill');
    if (pill && !pill.disabled) {
      var row = pill.closest('.pills');
      var all = row ? row.querySelectorAll('.pill') : [pill];
      all.forEach(function (b) { b.disabled = true; });
      post('/chat/send', { text: pill.getAttribute('data-label'), value: pill.getAttribute('data-value') })
        .then(function (ok) { if (!ok) all.forEach(function (b) { b.disabled = false; }); });
      return;
    }
    var act = e.target.closest('button[data-decision]');
    if (act && !act.disabled) {
      var wrap = act.closest('.approval-actions') || act.parentNode;
      var btns = wrap.querySelectorAll('button');
      btns.forEach(function (b) { b.disabled = true; });
      post('/chat/approve', { action_id: act.getAttribute('data-action-id'), decision: act.getAttribute('data-decision') })
        .then(function (ok) { if (!ok) btns.forEach(function (b) { b.disabled = false; }); });
    }
  });

  if (resetBtn) {
    resetBtn.addEventListener('click', function () {
      if (busy) return;
      Object.keys(streams).forEach(function (k) { streams[k].close(); delete streams[k]; });
      post('/chat/reset', {});
    });
  }

  // Upload
  if (attachBtn && fileInput) {
    attachBtn.addEventListener('click', function () { fileInput.click(); });
    fileInput.addEventListener('change', function () {
      var f = fileInput.files && fileInput.files[0];
      fileInput.value = '';
      if (!f || busy) return;
      var msg = 'فقط فایل zip تا ' + MAX_ZIP_MB + ' مگابایت';
      if (!/\.zip$/i.test(f.name) || f.size > MAX_ZIP_MB * 1024 * 1024) {
        showError(msg);
        return;
      }
      showError('');
      var pending = document.createElement('div');
      pending.className = 'msg msg-bot';
      pending.innerHTML = '<div class="bubble bubble-pending">در حال بررسی فایل...</div>';
      thread.appendChild(pending);
      scrollDown();
      var fd = new FormData();
      fd.append('file', f);
      request('/chat/upload', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'X-CSRFToken': csrf() },
        body: fd
      }).then(function () { pending.remove(); });
    });
  }

  // Initial state
  try {
    var st = document.getElementById('composer-state');
    if (st) applyComposer(JSON.parse(st.textContent));
  } catch (e) { /* ignore */ }
  scrollDown();
  startStreams();
})();
