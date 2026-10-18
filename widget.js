/*
 * NexusAI — ويدجت المحادثة القابل للتضمين.
 * ضع هذا السطر قبل إغلاق </body> بموقعكم (انسخه جاهزًا من لوحة التحكم):
 *
 *   <script src="https://YOUR-BACKEND-DOMAIN/widget.js" data-token="pub_..."></script>
 *
 * data-token هو "التوكن العام": آمن لوضعه بكود الموقع لأنه يسمح فقط بطرح
 * الأسئلة وترك رقم زبون. أما مفتاح الـ API الخاص فلا يوضع هنا أبدًا.
 *
 * لا حاجة لأي تثبيت أو خادم إضافي — الملف يُخدَّم مباشرة من نفس سيرفر NexusAI.
 *
 * يدعم: ترحيب مخصّص، متابعة سياق المحادثة، ونموذج "اترك رقمك" الذي يصل لصاحب العمل.
 */
(function () {
    var scriptTag = document.currentScript;
    var token = scriptTag ? scriptTag.getAttribute("data-token") : null;
    var apiBase = scriptTag
        ? new URL(scriptTag.src).origin
        : "";
    var lang = (scriptTag && scriptTag.getAttribute("data-lang")) ||
        (document.documentElement.lang || "ar").slice(0, 2);
    var isAr = lang !== "en";

    if (!token || token.indexOf("pub_") !== 0) {
        console.warn("NexusAI widget: data-token مفقود. انسخ سطر التضمين الجديد من لوحة التحكم.");
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
            leadLink: "📞 اترك رقمك ليتواصل معك فريقنا",
            leadTitle: "اترك بياناتك وسنتواصل معك:",
            leadName: "الاسم",
            leadPhone: "رقم الهاتف / واتساب (مع رمز الدولة)",
            leadSend: "إرسال",
            leadNeedPhone: "الرجاء كتابة رقم الهاتف.",
            leadFail: "تعذر الإرسال، حاول مرة أخرى.",
            leadThanks: "شكرًا لك! وصلتنا بياناتك وسيتواصل معك فريقنا قريبًا. ✅",
            privacy: "محادثتك تُحفظ ويطّلع عليها صاحب الموقع فقط. لا تكتب كلمات سر أو بيانات بطاقات.",
            leadNote: "بياناتك تصل لصاحب الموقع فقط ليتواصل معك.",
        }
        : {
            bubble: "💬",
            title: "AI Assistant",
            placeholder: "Type your question...",
            send: "Send",
            greeting: "Hi! How can I help you today?",
            thinking: "Typing...",
            error: "Couldn't send your question, try again.",
            leadLink: "📞 Leave your number and we'll contact you",
            leadTitle: "Leave your details and we'll get back to you:",
            leadName: "Name",
            leadPhone: "Phone / WhatsApp (with country code)",
            leadSend: "Send",
            leadNeedPhone: "Please enter your phone number.",
            leadFail: "Couldn't send, please try again.",
            leadThanks: "Thank you! We received your details and will contact you soon. ✅",
            privacy: "Your chat is saved and visible to the site owner only. Don't share passwords or card details.",
            leadNote: "Your details go to the site owner only, to contact you.",
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
        "#nexusai-widget-messages .msg{margin-bottom:10px;line-height:1.5;white-space:pre-wrap;}" +
        "#nexusai-widget-messages .user{color:#fff;font-weight:600;}" +
        "#nexusai-widget-messages .bot{color:#cfc9ff;}" +
        "#nexusai-widget-leadlink{background:none;border:none;color:#a99bff;cursor:pointer;font-size:12px;" +
        "padding:4px 12px;text-align:" + (isAr ? "right" : "left") + ";font-family:inherit;}" +
        ".nexusai-leadbox{background:rgba(109,74,255,.18);border:1px solid rgba(109,74,255,.5);" +
        "border-radius:10px;padding:10px;display:flex;flex-direction:column;gap:6px;margin-bottom:10px;}" +
        ".nexusai-leadbox input{background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.18);" +
        "border-radius:6px;padding:7px;color:#fff;font-size:13px;}" +
        ".nexusai-leadbox button{background:#6d4aff;color:#fff;border:none;border-radius:6px;padding:7px;" +
        "font-size:13px;cursor:pointer;}" +
        ".nexusai-leadbox .err{color:#ffb4b4;font-size:12px;}" +
        "#nexusai-widget-privacy{font-size:10px;color:#9d96c9;padding:4px 12px 0;line-height:1.4;}" +
        "#nexusai-widget-privacy a{color:inherit;}" +
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
        '<div id="nexusai-widget-header"></div>' +
        '<div id="nexusai-widget-messages"></div>' +
        '<button id="nexusai-widget-leadlink" type="button"></button>' +
        '<div id="nexusai-widget-privacy"></div>' +
        '<div id="nexusai-widget-inputrow">' +
        '<input id="nexusai-widget-input" type="text">' +
        '<button id="nexusai-widget-sendbtn"></button>' +
        "</div>";

    document.body.appendChild(bubble);
    document.body.appendChild(panel);

    var messagesEl = panel.querySelector("#nexusai-widget-messages");
    var inputEl = panel.querySelector("#nexusai-widget-input");
    var sendBtn = panel.querySelector("#nexusai-widget-sendbtn");
    var leadLinkEl = panel.querySelector("#nexusai-widget-leadlink");
    panel.querySelector("#nexusai-widget-header").textContent = T.title;
    inputEl.placeholder = T.placeholder;
    sendBtn.textContent = T.send;
    leadLinkEl.textContent = T.leadLink;
    panel.querySelector("#nexusai-widget-privacy").textContent = T.privacy;

    var opened = false;
    var greeting = T.greeting;
    var configLoaded = false;
    // معرّف جلسة عشوائي: يربط أسئلة نفس الزائر ببعضها (للمتابعات ولسجل صاحب العمل)
    var sid = "w" + Math.random().toString(36).slice(2, 12) + Date.now().toString(36);
    var leadSent = false;
    var leadBox = null;

    function addMessage(text, who) {
        var div = document.createElement("div");
        div.className = "msg " + who;
        div.textContent = text;
        messagesEl.appendChild(div);
        messagesEl.scrollTop = messagesEl.scrollHeight;
        return div;
    }

    function loadConfig() {
        if (configLoaded) return Promise.resolve();
        configLoaded = true;
        return fetch(apiBase + "/public/config/" + encodeURIComponent(token))
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (cfg) { if (cfg && cfg.greeting) greeting = cfg.greeting; })
            .catch(function () { });
    }

    bubble.addEventListener("click", function () {
        opened = !opened;
        panel.style.display = opened ? "flex" : "none";
        if (opened && !messagesEl.hasChildNodes()) {
            // الترحيب المخصّص (إن وُجد) يُجلب مرة واحدة؛ وإن فشل نعرض الترحيب الافتراضي.
            loadConfig().then(function () {
                if (!messagesEl.hasChildNodes()) addMessage(greeting, "bot");
            });
        }
    });

    function showLeadForm() {
        if (leadSent) return;
        if (leadBox) { leadBox.scrollIntoView({ block: "nearest" }); return; }
        var box = document.createElement("div");
        box.className = "nexusai-leadbox";
        leadBox = box;
        var title = document.createElement("div");
        title.textContent = T.leadTitle;
        var nameIn = document.createElement("input");
        nameIn.placeholder = T.leadName;
        nameIn.maxLength = 80;
        var phoneIn = document.createElement("input");
        phoneIn.placeholder = T.leadPhone;
        phoneIn.type = "tel";
        phoneIn.maxLength = 30;
        phoneIn.dir = "ltr";
        var err = document.createElement("div");
        err.className = "err";
        var go = document.createElement("button");
        go.type = "button";
        go.textContent = T.leadSend;
        go.addEventListener("click", function () {
            err.textContent = "";
            if (!phoneIn.value.trim()) { err.textContent = T.leadNeedPhone; return; }
            go.disabled = true;
            fetch(apiBase + "/public/lead/" + encodeURIComponent(token), {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ name: nameIn.value, phone: phoneIn.value, session_id: sid, channel: "widget" }),
            })
                .then(function (r) {
                    return r.json().catch(function () { return null; }).then(function (d) {
                        if (r.ok) {
                            leadSent = true;
                            leadLinkEl.style.display = "none";
                            messagesEl.removeChild(box);
                            leadBox = null;
                            addMessage(T.leadThanks, "bot");
                        } else {
                            err.textContent = (d && d.detail) || T.leadFail;
                            go.disabled = false;
                        }
                    });
                })
                .catch(function () { err.textContent = T.leadFail; go.disabled = false; });
        });
        var note = document.createElement("div");
        note.style.cssText = "font-size:11px;opacity:.7";
        note.textContent = T.leadNote;
        box.appendChild(title);
        box.appendChild(nameIn);
        box.appendChild(phoneIn);
        box.appendChild(note);
        box.appendChild(err);
        box.appendChild(go);
        messagesEl.appendChild(box);
        messagesEl.scrollTop = messagesEl.scrollHeight;
        phoneIn.focus();
    }
    leadLinkEl.addEventListener("click", showLeadForm);

    async function sendQuestion() {
        var question = inputEl.value.trim();
        if (!question) return;
        addMessage(question, "user");
        inputEl.value = "";
        var thinkingDiv = addMessage(T.thinking, "bot");

        try {
            var res = await fetch(apiBase + "/public/ask/" + encodeURIComponent(token), {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ question: question, session_id: sid, channel: "widget" }),
            });
            var data = await res.json().catch(function () { return null; });
            thinkingDiv.textContent =
                res.ok && data && data.answer ? data.answer : (data && data.detail) || T.error;
            if (res.ok && data && data.show_lead_form) showLeadForm();
        } catch (e) {
            thinkingDiv.textContent = T.error;
        }
    }

    sendBtn.addEventListener("click", sendQuestion);
    inputEl.addEventListener("keydown", function (e) {
        if (e.key === "Enter") sendQuestion();
    });
})();