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
    sendBtn.classList.toggle('is-busy', busy > 0);
    sendBtn.setAttribute('aria-busy', busy > 0 ? 'true' : 'false');
  }

  // Typing indicator: one at a time, with staged labels for long waits.
  var typingEl = null;
  var typingTimers = [];

  var echoEl = null;

  function hideTyping() {
    typingTimers.forEach(clearTimeout);
    typingTimers = [];
    if (typingEl) { typingEl.remove(); typingEl = null; }
    // The server returns the real user bubble; drop the optimistic copy.
    if (echoEl) { echoEl.remove(); echoEl = null; }
  }

  // Show the user's own text at once, before the (possibly slow) answer arrives.
  function showEcho(text) {
    if (!text) return;
    var d = document.createElement('div');
    d.className = 'msg msg-user msg-pending';
    var b = document.createElement('div');
    b.className = 'bubble';
    b.textContent = text;
    d.appendChild(b);
    thread.appendChild(d);
    echoEl = d;
  }

  // stages: [[delayMs, label], ...] sorted by delay; first one is shown at once.
  function showTyping(stages) {
    hideTyping();
    if (!stages || !stages.length) return;
    var d = document.createElement('div');
    d.className = 'msg msg-bot msg-typing';
    d.setAttribute('role', 'status');
    d.setAttribute('aria-live', 'polite');
    var b = document.createElement('div');
    b.className = 'bubble bubble-typing';
    var dots = document.createElement('span');
    dots.className = 'typing-dots';
    dots.setAttribute('aria-hidden', 'true');
    for (var i = 0; i < 3; i++) dots.appendChild(document.createElement('span'));
    var label = document.createElement('span');
    label.className = 'typing-label';
    label.textContent = stages[0][1];
    b.appendChild(dots);
    b.appendChild(label);
    d.appendChild(b);
    thread.appendChild(d);
    typingEl = d;
    stages.slice(1).forEach(function (st) {
      typingTimers.push(setTimeout(function () {
        if (typingEl === d) label.textContent = st[1];
      }, st[0]));
    });
    scrollDown();
  }

  var SEND_STAGES = [[0, 'دارم فکر می\u200cکنم'], [4000, 'دارم وضعیت و لاگ\u200cها را بررسی می\u200cکنم'], [15000, 'هنوز دارم بررسی می\u200cکنم؛ کمی بیشتر طول می\u200cکشد']];
  var GITHUB_STAGES = [[0, 'دارم مخزن را از گیت\u200cهاب دریافت می\u200cکنم']].concat(SEND_STAGES.slice(1));
  var UPLOAD_STAGES = [[0, 'در حال بررسی فایل...'], [3000, 'دارم نوع پروژه را تشخیص می\u200cدهم']];
  var KEY_STAGES = [[0, 'دارم کلید را بررسی می\u200cکنم']];
  var SECRET_STAGES = [[0, 'دارم مقدار را امن نگه می‌دارم']];
  var secretEndpoint = '/chat/key';
  var APPROVE_STAGES = [[0, 'دارم درخواست را اعمال می\u200cکنم']];
  var RESET_STAGES = [[0, 'دارم گفتگو را بازنشانی می\u200cکنم']];

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
    secretEndpoint = c.endpoint === '/chat/secret' ? '/chat/secret' : '/chat/key';
    input.type = mode === 'password' ? 'password' : 'text';
    input.autocomplete = mode === 'password' ? 'off' : 'off';
    if (mode === 'password') {
      input.setAttribute('autocapitalize', 'off');
      input.setAttribute('spellcheck', 'false');
      // Persian placeholder stays right-to-left; the typed key/value flips to LTR.
      input.setAttribute('dir', input.value ? 'ltr' : 'rtl');
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
  function request(url, opts, stages, echo) {
    setBusy(true);
    opts = opts || {};
    showTyping(stages);
    if (echo) { showEcho(echo); if (typingEl) thread.appendChild(typingEl); scrollDown(); }
    return fetch(url, opts)
      .then(function (r) {
        return r.json().catch(function () { return null; }).then(function (data) {
          hideTyping();
          if (data && (data.messages || data.composer)) {
            apply(data);
            return r.ok;
          }
          throw new Error('bad response');
        });
      })
      .catch(function () {
        hideTyping();
        netErrorBubble();
        return false;
      })
      .then(function (ok) { setBusy(false); return ok; });
  }

  function post(url, body, stages, echo) {
    return request(url, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf() },
      body: JSON.stringify(body || {})
    }, stages, echo);
  }

  function submit() {
    if (busy) return;
    var val = input.value;
    if (mode === 'password') {
      var key = val.trim();
      if (!key) { showError('کلید را وارد کنید.'); input.focus(); return; }
      input.value = '';
      showError('');
      post(secretEndpoint, { key: key }, secretEndpoint === '/chat/secret' ? SECRET_STAGES : KEY_STAGES);
      key = null;
      return;
    }
    var text = val.trim();
    if (!text) return;
    input.value = '';
    showError('');
    post('/chat/send', { text: text }, /^https:\/\/github\.com\//i.test(text) ? GITHUB_STAGES : SEND_STAGES, text);
  }

  composerEl.addEventListener('submit', function (e) { e.preventDefault(); submit(); });
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); }
  });
  input.addEventListener('input', function () {
    if (mode !== 'password') return;
    input.setAttribute('dir', input.value ? 'ltr' : 'rtl');
    if (input.value) showError('');
  });

  // Delegated clicks: pills and approval buttons.
  thread.addEventListener('click', function (e) {
    var pill = e.target.closest('.pill');
    if (pill && !pill.disabled) {
      var row = pill.closest('.pills');
      var all = row ? row.querySelectorAll('.pill') : [pill];
      all.forEach(function (b) { b.disabled = true; });
      post('/chat/send', { text: pill.getAttribute('data-label'), value: pill.getAttribute('data-value') }, SEND_STAGES, pill.getAttribute('data-label'))
        .then(function (ok) { if (!ok) all.forEach(function (b) { b.disabled = false; }); });
      return;
    }
    var act = e.target.closest('button[data-decision]');
    if (act && !act.disabled) {
      var wrap = act.closest('.approval-actions') || act.parentNode;
      var btns = wrap.querySelectorAll('button');
      btns.forEach(function (b) { b.disabled = true; });
      post('/chat/approve', { action_id: act.getAttribute('data-action-id'), decision: act.getAttribute('data-decision') }, APPROVE_STAGES)
        .then(function (ok) { if (!ok) btns.forEach(function (b) { b.disabled = false; }); });
    }
  });

  if (resetBtn) {
    resetBtn.addEventListener('click', function () {
      if (busy) return;
      Object.keys(streams).forEach(function (k) { streams[k].close(); delete streams[k]; });
      post('/chat/reset', {}, RESET_STAGES);
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
      var fd = new FormData();
      fd.append('file', f);
      request('/chat/upload', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'X-CSRFToken': csrf() },
        body: fd
      }, UPLOAD_STAGES);
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
