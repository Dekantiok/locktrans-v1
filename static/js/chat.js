(function () {
    function scrollThread() {
        var thread = document.getElementById('chat-thread');
        if (thread) thread.scrollTop = thread.scrollHeight;
    }
    document.body.addEventListener('htmx:afterSwap', function (event) {
        if (event.detail.target && event.detail.target.id === 'chat-result') {
            scrollThread();
            var input = document.getElementById('chat-input');
            if (input) { input.value = ''; input.focus(); }
        }
    });
    scrollThread();
    if (window.LocktransVoice) LocktransVoice.initChatMic();
})();
