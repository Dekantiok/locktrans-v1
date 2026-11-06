/* Record with MediaRecorder, POST the blob, hand the transcript back.
   Audio only ever goes to our own transcription endpoint. */
(function () {
  'use strict';

  function csrfToken() {
    var m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    if (m) return decodeURIComponent(m[1]);
    var el = document.querySelector('input[name="csrfmiddlewaretoken"]');
    return el ? el.value : '';
  }

  function supported() {
    return !!(window.MediaRecorder && navigator.mediaDevices &&
              navigator.mediaDevices.getUserMedia);
  }

  // stop(send=true) finishes and uploads the take; stop(false) discards
  // it. A cancelled take must never reach transcription: cancelling means
  // "do not send this audio anywhere", so onstop drops the chunks.
  function recordOnce(onDone, onError) {
    if (!supported()) {
      onError(new Error('Voice recording is not supported in this browser.'));
      return function () {};
    }
    var recorder = null;
    var chunks = [];
    var stopped = false;
    var deliver = true;
    navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
      if (stopped) {
        stream.getTracks().forEach(function (t) { t.stop(); });
        return;
      }
      var mime = '';
      if (window.MediaRecorder.isTypeSupported) {
        if (MediaRecorder.isTypeSupported('audio/webm;codecs=opus')) mime = 'audio/webm;codecs=opus';
        else if (MediaRecorder.isTypeSupported('audio/webm')) mime = 'audio/webm';
        else if (MediaRecorder.isTypeSupported('audio/mp4')) mime = 'audio/mp4';
      }
      try {
        recorder = mime ? new MediaRecorder(stream, { mimeType: mime })
                        : new MediaRecorder(stream);
      } catch (e) {
        stream.getTracks().forEach(function (t) { t.stop(); });
        onError(e);
        return;
      }
      recorder.ondataavailable = function (ev) {
        if (deliver && ev.data && ev.data.size) chunks.push(ev.data);
      };
      recorder.onstop = function () {
        stream.getTracks().forEach(function (t) { t.stop(); });
        if (!deliver) return;
        onDone(new Blob(chunks, { type: recorder.mimeType || 'audio/webm' }));
      };
      recorder.start();
      recordOnce.active = recorder;
    }).catch(onError);
    var handle = {
      stop: function (send) {
        deliver = send !== false;
        stopped = true;
        var r = recordOnce.active;
        if (r && r.state !== 'inactive') r.stop();
        recordOnce.active = null;
      },
    };
    // Backwards-compatible callable: stop() sends, stop.cancel() discards.
    var fn = function () { handle.stop(true); };
    fn.cancel = function () { handle.stop(false); };
    return fn;
  }
  recordOnce.active = null;
  recordOnce.supported = supported;

  function postAudio(url, blob, onOk, onErr) {
    var fd = new FormData();
    fd.append('audio', blob, 'note.webm');
    fetch(url, {
      method: 'POST',
      headers: { 'X-CSRFToken': csrfToken() },
      body: fd,
    }).then(function (resp) { return resp.json().then(function (data) {
      return { status: resp.status, data: data };
    }); }).then(function (out) {
      if (out.status >= 200 && out.status < 300 && out.data && out.data.ok !== false) onOk(out.data);
      else onErr(new Error((out.data && (out.data.error || out.data.transcript)) || 'Transcription failed.'));
    }).catch(onErr);
  }

  function fmtElapsed(ms) {
    var s = Math.floor(ms / 1000);
    return Math.floor(s / 60) + ':' + ('0' + (s % 60)).slice(-2);
  }

  function showTyping() {
    var thread = document.getElementById('chat-thread');
    if (!thread) return;
    if (!document.getElementById('chat-typing')) {
      var row = document.createElement('div');
      row.className = 'd-flex justify-content-start';
      row.id = 'chat-typing';
      row.setAttribute('aria-live', 'polite');
      row.innerHTML = '<div class="rounded-3 px-3 py-2 bubble-theirs">' +
        '<span class="typing-dots"><span></span><span></span><span></span></span></div>';
      thread.appendChild(row);
    }
    thread.scrollTop = thread.scrollHeight;
  }

  function hideTyping() {
    var row = document.getElementById('chat-typing');
    if (row) row.remove();
  }

  function appendUserBubble(text) {
    var thread = document.getElementById('chat-thread');
    if (!thread) return;
    var old = document.getElementById('chat-optimistic');
    if (old) old.remove();
    var row = document.createElement('div');
    row.className = 'd-flex justify-content-end';
    row.id = 'chat-optimistic';
    var bubble = document.createElement('div');
    bubble.className = 'rounded-3 px-3 py-2 bubble-mine';
    bubble.textContent = text;
    row.appendChild(bubble);
    thread.appendChild(row);
    thread.scrollTop = thread.scrollHeight;
  }

  /* Sent message appears instantly, typing dots below it, input cleared
     for the follow-up, the server swap then replaces the whole thread. */
  function initChatIndicators() {
    if (!window.htmx) return;
    document.body.addEventListener('htmx:beforeRequest', function (event) {
      var form = document.getElementById('chat-form');
      if (form && event.detail && event.detail.elt === form) {
        var input = document.getElementById('chat-input');
        var text = input ? input.value.trim() : '';
        if (text) {
          appendUserBubble(/^\d{6}$/.test(text) ? '[one-time code withheld]' : text);
          if (input) input.value = '';
        }
        showTyping();
      }
    });
    var hide = function (event) {
      if (event.detail && event.detail.target &&
          event.detail.target.id === 'chat-result') hideTyping();
    };
    document.body.addEventListener('htmx:afterSwap', hide);
    document.body.addEventListener('htmx:responseError', hideTyping);
  }


  function initChatMic() {
    var mic = document.getElementById('chat-mic');
    var hint = document.getElementById('chat-voice-hint');
    var result = document.getElementById('chat-result');
    if (!mic || !result) return;
    if (!supported()) { mic.style.display = 'none'; return; }
    if (mic.dataset.voiceAttached) return;
    mic.dataset.voiceAttached = '1';
    var stop = null;
    var startedAt = 0;
    mic.title = 'Hold to record a voice note';
    mic.style.touchAction = 'none';
    mic.addEventListener('contextmenu', function (e) { e.preventDefault(); });
    var recTimer = null;
    function stopRecUI() {
      if (recTimer) clearInterval(recTimer);
      recTimer = null;
      mic.classList.remove('mic-recording');
    }
    function setIdle() {
      stop = null;
      stopRecUI();
      mic.innerHTML = '<svg class="tc-icon" aria-hidden="true" focusable="false" width="16" height="16" fill="currentColor" viewBox="0 0 16 16"><path d="M3.5 6.5A.5.5 0 0 1 4 7v1a4 4 0 0 0 8 0V7a.5.5 0 0 1 1 0v1a5 5 0 0 1-4.5 4.975V15h3a.5.5 0 0 1 0 1h-7a.5.5 0 0 1 0-1h3v-2.025A5 5 0 0 1 3 8V7a.5.5 0 0 1 .5-.5z"/><path d="M10 8a2 2 0 1 1-4 0V3a2 2 0 1 1 4 0v5zM8 0a3 3 0 0 0-3 3v5a3 3 0 0 0 6 0V3a3 3 0 0 0-3-3z"/></svg>';
      mic.disabled = false;
    }
    function uploadBlob(blob) {
      stop = null;
      appendUserBubble('🎤 Voice note');
      showTyping();
      if (hint) hint.textContent = '';
      var fd = new FormData();
      fd.append('audio', blob, 'note.webm');
      fetch('/assistant/voice/', {
        method: 'POST',
        headers: { 'X-CSRFToken': csrfToken() },
        body: fd,
      }).then(function (resp) { return resp.text(); }).then(function (html) {
        result.innerHTML = html;
        if (typeof scrollThread === 'function') scrollThread();
        else {
          var thread = document.getElementById('chat-thread');
          if (thread) thread.scrollTop = thread.scrollHeight;
        }
        if (hint) hint.textContent = '';
        setIdle();
      }).then(function () { hideTyping(); }).catch(function () {
        hideTyping();
        if (hint) hint.textContent = 'Voice failed, please type instead.';
        setIdle();
      });
    }
    mic.addEventListener('pointerdown', function (e) {
      e.preventDefault();
      if (stop || mic.disabled) return;
      if (hint) hint.textContent = '';
      mic.innerHTML = '<svg class="tc-icon" aria-hidden="true" focusable="false" width="16" height="16" fill="currentColor" viewBox="0 0 16 16"><path d="M8 15A7 7 0 1 1 8 1a7 7 0 0 1 0 14zm0 1A8 8 0 1 0 8 0a8 8 0 0 0 0 16z"/><path d="M11 8a3 3 0 1 1-6 0 3 3 0 0 1 6 0z"/></svg>';
      mic.classList.add('mic-recording');
      if (hint) hint.innerHTML = '<span class="rec-dot"></span><span class="rec-time">0:00</span>, release to send';
      try { mic.setPointerCapture(e.pointerId); } catch (err) {}
      startedAt = Date.now();
      var tickChat = function () {
        var el = hint ? hint.querySelector('.rec-time') : null;
        if (el) el.textContent = fmtElapsed(Date.now() - startedAt);
      };
      recTimer = setInterval(tickChat, 500);
      stop = recordOnce(uploadBlob, function () {
        if (hint) hint.textContent = 'Microphone unavailable.';
        setIdle();
      });
    });
    function releaseToSend() {
      if (!stop) return;
      if (Date.now() - startedAt < 400) {
        var s = stop; stop = null;
        stopRecUI();
        s.cancel();
        mic.innerHTML = '<svg class="tc-icon" aria-hidden="true" focusable="false" width="16" height="16" fill="currentColor" viewBox="0 0 16 16"><path d="M3.5 6.5A.5.5 0 0 1 4 7v1a4 4 0 0 0 8 0V7a.5.5 0 0 1 1 0v1a5 5 0 0 1-4.5 4.975V15h3a.5.5 0 0 1 0 1h-7a.5.5 0 0 1 0-1h3v-2.025A5 5 0 0 1 3 8V7a.5.5 0 0 1 .5-.5z"/><path d="M10 8a2 2 0 1 1-4 0V3a2 2 0 1 1 4 0v5zM8 0a3 3 0 0 0-3 3v5a3 3 0 0 0 6 0V3a3 3 0 0 0-3-3z"/></svg>';
        if (hint) hint.textContent = 'Hold the mic button while you speak.';
        return;
      }
      var send = stop; stop = null;
      stopRecUI();
      mic.disabled = true;
      mic.innerHTML = '<svg class="tc-icon" aria-hidden="true" focusable="false" width="16" height="16" fill="currentColor" viewBox="0 0 16 16"><path d="M2 1.5a.5.5 0 0 1 .5-.5h11a.5.5 0 0 1 0 1h-1v1a4.5 4.5 0 0 1-2.557 4.06c-.29.139-.443.377-.443.704v3.535c0 .327.154.565.443.704A4.5 4.5 0 0 1 12.5 15v1h1a.5.5 0 0 1 0 1h-11a.5.5 0 1 1 0-1h1v-1a4.5 4.5 0 0 1 2.557-4.06c.29-.139.443-.377.443-.704V7.7c0-.327-.154-.565-.443-.704A4.5 4.5 0 0 1 3.5 3V2h-1a.5.5 0 0 1 0-1zm2.5 1v1a3.5 3.5 0 0 0 1.989 3.158c.533.256 1.011.79 1.011 1.491v3.702c0 .701-.478 1.235-1.011 1.491A3.5 3.5 0 0 0 4.5 15v1h7v-1a3.5 3.5 0 0 0-1.989-3.158C9.978 11.586 9.5 10.86 9.5 10.159V7.45c0-.701.478-1.235 1.011-1.491A3.5 3.5 0 0 0 11.5 3V2h-7z"/></svg>';
      if (hint) hint.textContent = '';
      showTyping();
      send();
    }
    function cancelHold() {
      if (!stop) return;
      var s = stop; stop = null;
      stopRecUI();
      s.cancel();
      mic.innerHTML = '<svg class="tc-icon" aria-hidden="true" focusable="false" width="16" height="16" fill="currentColor" viewBox="0 0 16 16"><path d="M3.5 6.5A.5.5 0 0 1 4 7v1a4 4 0 0 0 8 0V7a.5.5 0 0 1 1 0v1a5 5 0 0 1-4.5 4.975V15h3a.5.5 0 0 1 0 1h-7a.5.5 0 0 1 0-1h3v-2.025A5 5 0 0 1 3 8V7a.5.5 0 0 1 .5-.5z"/><path d="M10 8a2 2 0 1 1-4 0V3a2 2 0 1 1 4 0v5zM8 0a3 3 0 0 0-3 3v5a3 3 0 0 0 6 0V3a3 3 0 0 0-3-3z"/></svg>';
      if (hint) hint.textContent = 'Recording discarded — nothing was sent.';
    }
    mic.addEventListener('pointerup', releaseToSend);
    mic.addEventListener('pointerleave', releaseToSend);
    mic.addEventListener('pointercancel', cancelHold);
  }

  /* Poll for updates pushed into this conversation (e.g. the other
     party shipped, funded, or released) and swap them in live. */
  function initChatPoll() {
    var result = document.getElementById('chat-result');
    if (!result || !document.getElementById('chat-form')) return;
    if (result.dataset.pollAttached) return;
    result.dataset.pollAttached = '1';
    setInterval(function () {
      if (document.hidden) return;
      var thread = document.getElementById('chat-thread');
      if (!thread) return;
      var since = thread.getAttribute('data-last-id') || '0';
      fetch('/assistant/poll/?since=' + encodeURIComponent(since), {
        headers: { 'X-Requested-With': 'XMLHttpRequest' },
      }).then(function (resp) {
        if (resp.status === 204) return null;
        return resp.text();
      }).then(function (html) {
        if (!html) return;
        result.innerHTML = html;
        var fresh = document.getElementById('chat-thread');
        if (fresh) fresh.scrollTop = fresh.scrollHeight;
      }).catch(function () {});
    }, 10000);
  }

  window.LocktransAIVoice = {
    supported: supported,
    recordOnce: recordOnce,
    postAudio: postAudio,
    initChatMic: initChatMic,
    initChatIndicators: initChatIndicators,
    showTyping: showTyping,
    hideTyping: hideTyping,
    appendUserBubble: appendUserBubble,
    initChatPoll: initChatPoll,
  };

  document.addEventListener('DOMContentLoaded', function () {
    initChatMic();
    initChatIndicators();
    initChatPoll();
  });
})();
