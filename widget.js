/*
 * NexusAI — ويدجت المحادثة القابل للتضمين.
 * ضع هذا السطر قبل إغلاق </body> بموقعكم، وغيّر data-api-key لمفتاحكم الخاص:
 *
 *   <script src="https://YOUR-BACKEND-DOMAIN/widget.js" data-api-key="YOUR_API_KEY"></script>
 *
 * لا حاجة لأي تثبيت أو خادم إضافي — الملف يُخدَّم مباشرة من نفس سيرفر NexusAI.
 */
(function () {
    var scriptTag = document.currentScript;
    var apiKey = scriptTag ? scriptTag.getAttribute("data-api-key") : null;
    var apiBase = scriptTag
        ? new URL(scriptTag.src).origin
        : "";
    var lang = (scriptTag && scriptTag.getAttribute("data-lang")) ||
        (document.documentElement.lang || "ar").slice(0, 2);
    var isAr = lang !== "en";

    if (!apiKey) {
        console.warn("NexusAI widget: data-api-key مفقود على سطر التضمين.");
        return;
    }

    var T = isAr
        ? {
            bubble: "💬",
            title: "مساعد الشركة الذكي",
            placeholder: "اكتب سؤالك هنا...",
            send: "إرسال",
            greeting: "أهلًا! كيف أقدر أساعدك اليوم؟",
            thinking: "...جاري الكتابة",
            error: "تعذر إرسال سؤالك، حاول مرة أخرى.",
        }
        : {
            bubble: "💬",
            title: "AI Assistant",
            placeholder: "Type your question...",
            send: "Send",
            greeting: "Hi! How can I help you today?",
            thinking: "Typing...",
            error: "Couldn't send your question, try again.",
        };

    var style = document.createElement("style");
    style.textContent =
        "#nexusai-widget-bubble{position:fixed;bottom:20px;" +
        (isAr ? "left" : "right") +
        ":20px;width:56px;height:56px;border-radius:50%;background:#6d4aff;" +
        "color:#fff;font-size:26px;border:none;cursor:pointer;box-shadow:0 6px 18px rgba(0,0,0,.25);z-index:999999;}" +
        "#nexusai-widget-panel{position:fixed;bottom:88px;" +
        (isAr ? "left" : "right") +
        ":20px;width:320px;max-width:90vw;height:420px;max-height:70vh;background:#1b1530;" +
        "border:1px solid rgba(255,255,255,.12);border-radius:14px;display:none;flex-direction:column;" +
        "overflow:hidden;z-index:999999;font-family:system-ui,-apple-system,Segoe UI,Arial,sans-serif;direction:" +
        (isAr ? "rtl" : "ltr") +
        ";box-shadow:0 10px 30px rgba(0,0,0,.35);}" +
        "#nexusai-widget-header{background:#6d4aff;color:#fff;padding:12px 14px;font-size:14px;font-weight:600;}" +
        "#nexusai-widget-messages{flex:1;overflow-y:auto;padding:12px;font-size:13px;color:#eae6ff;}" +
        "#nexusai-widget-messages .msg{margin-bottom:10px;line-height:1.5;}" +
        "#nexusai-widget-messages .user{color:#fff;font-weight:600;}" +
        "#nexusai-widget-messages .bot{color:#cfc9ff;}" +
        "#nexusai-widget-inputrow{display:flex;border-top:1px solid rgba(255,255,255,.1);padding:8px;gap:6px;}" +
        "#nexusai-widget-input{flex:1;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.12);" +
        "border-radius:8px;padding:8px;color:#fff;font-size:13px;}" +
        "#nexusai-widget-sendbtn{background:#6d4aff;color:#fff;border:none;border-radius:8px;padding:8px 12px;" +
        "font-size:13px;cursor:pointer;}";
    document.head.appendChild(style);

    var bubble = document.createElement("button");
    bubble.id = "nexusai-widget-bubble";
    bubble.textContent = T.bubble;

    var panel = document.createElement("div");
    panel.id = "nexusai-widget-panel";
    panel.innerHTML =
        '<div id="nexusai-widget-header">' + T.title + "</div>" +
        '<div id="nexusai-widget-messages"></div>' +
        '<div id="nexusai-widget-inputrow">' +
        '<input id="nexusai-widget-input" type="text" placeholder="' + T.placeholder + '">' +
        '<button id="nexusai-widget-sendbtn">' + T.send + "</button>" +
        "</div>";

    document.body.appendChild(bubble);
    document.body.appendChild(panel);

    var messagesEl = panel.querySelector("#nexusai-widget-messages");
    var inputEl = panel.querySelector("#nexusai-widget-input");
    var sendBtn = panel.querySelector("#nexusai-widget-sendbtn");
    var opened = false;

    function addMessage(text, who) {
        var div = document.createElement("div");
        div.className = "msg " + who;
        div.textContent = text;
        messagesEl.appendChild(div);
        messagesEl.scrollTop = messagesEl.scrollHeight;
    }

    bubble.addEventListener("click", function () {
        opened = !opened;
        panel.style.display = opened ? "flex" : "none";
        if (opened && !messagesEl.hasChildNodes()) {
            addMessage(T.greeting, "bot");
        }
    });

    async function sendQuestion() {
        var question = inputEl.value.trim();
        if (!question) return;
        addMessage(question, "user");
        inputEl.value = "";
        var thinkingDiv = document.createElement("div");
        thinkingDiv.className = "msg bot";
        thinkingDiv.textContent = T.thinking;
        messagesEl.appendChild(thinkingDiv);
        messagesEl.scrollTop = messagesEl.scrollHeight;

        try {
            var res = await fetch(apiBase + "/enterprise/ask", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "x-api-key": apiKey,
                },
                body: JSON.stringify({ question: question }),
            });
            var data = await res.json().catch(function () { return null; });
            thinkingDiv.textContent =
                res.ok && data && data.answer ? data.answer : (data && data.detail) || T.error;
        } catch (e) {
            thinkingDiv.textContent = T.error;
        }
    }

    sendBtn.addEventListener("click", sendQuestion);
    inputEl.addEventListener("keydown", function (e) {
        if (e.key === "Enter") sendQuestion();
    });
})();