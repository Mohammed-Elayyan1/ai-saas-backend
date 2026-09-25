"""
Enterprise AI SaaS Backend — نسخة كاملة وجاهزة للعمل الفعلي
=================================================================
هذا الملف يغطي:
  1. دفع حقيقي عبر Stripe + تفعيل تلقائي فوري بعد نجاح الدفع (Webhook)
  2. توليد وإرسال مفتاح الـ API فورًا للعميل (على الشاشة + بريد إلكتروني اختياري)
  3. تفعيل مميزات كل باقة فورًا (حدود استخدام مختلفة لكل باقة)
  4. ردود ذكاء اصطناعي حقيقية (عبر Claude API) بدل النص الوهمي السابق
  5. تخزين يبقى بعد إعادة تشغيل السيرفر (ملف JSON بسيط بدل قاموس بالذاكرة فقط)

قبل التشغيل ثبّت المكتبات:
    pip install fastapi uvicorn stripe anthropic python-multipart python-dotenv

ثم أنشئ ملف .env بجانب هذا الملف (شوف .env.example المرفق) وعبّئ القيم الحقيقية.
"""

import os
import json
import uuid
import smtplib
from email.mime.text import MIMEText
from pathlib import Path
from datetime import datetime, timezone

import stripe
from fastapi import FastAPI, HTTPException, Header, Form, UploadFile, File, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Enterprise AI SaaS Backend", version="3.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # في الإنتاج: استبدلها برابط موقعك فقط، مثل ["https://yourdomain.com"]
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def health_check():
    """نقطة فحص بسيطة — تفيد Railway/Render للتأكد إن الخادم شغال، ولمنع 404 عند زيارة الرابط الأساسي مباشرة."""
    return {"status": "ok", "service": "Enterprise AI SaaS Backend"}

# =============================================================================
# الإعدادات — كلها من متغيرات البيئة، ولا شيء حساس مكتوب بالكود
# =============================================================================
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:8501")

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER)

# رمز إداري بسيط لحماية نقاط الإدارة (تفعيل/إلغاء اشتراكات PayPal يدويًا)
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
if not ADMIN_TOKEN:
    print("⚠️  ADMIN_TOKEN غير مُعرّف — نقاط لوحة الإدارة (/admin/*) ستكون غير محمية. عرّفه في .env قبل النشر الفعلي.")

# روابط دفع PayPal الثابتة الخاصة بك (PayPal.Me / Buy-Now links).
# هذه روابط "بدون ربط برمجي" — أي لا يوجد webhook تلقائي منها مثل Stripe،
# لذلك التفعيل يعتمد على نموذج "المطالبة" أدناه (/enterprise/paypal/claim).
PAYPAL_LINKS = {
    "basic": "https://www.paypal.com/ncp/payment/23V3WQK4NVTG4",
    "pro": "https://www.paypal.com/ncp/payment/Z59KCWS6MZAC6",
    "enterprise": "https://www.paypal.com/ncp/payment/CS59KMKAETBFC",
}

if not stripe.api_key:
    print("⚠️  STRIPE_SECRET_KEY غير مُعرّف — الدفع لن يعمل حتى تضيفه في .env")
if not ANTHROPIC_API_KEY and not GEMINI_API_KEY:
    print("⚠️  لا يوجد ANTHROPIC_API_KEY ولا GEMINI_API_KEY — سيتم استخدام رد احتياطي بسيط بدل الذكاء الاصطناعي الحقيقي")

# =============================================================================
# باقات الأسعار + المميزات والحدود المرتبطة بكل باقة
# غيّر price_id هون لتطابق القيم الحقيقية من لوحة تحكم Stripe عندك
# =============================================================================
PLANS = {
    "basic": {
        "price_id": "price_1NxBasicPlanIDExample",
        "label": "الباقة الأساسية",
        "monthly_query_limit": 500,
        "max_files": 1,
        "priority": "normal",
    },
    "pro": {
        "price_id": "price_1NxProPlanIDExample",
        "label": "الباقة الاحترافية",
        "monthly_query_limit": None,  # None = غير محدود
        "max_files": 10,
        "priority": "high",
    },
    "enterprise": {
        "price_id": "price_1NxEnterprisePlanIDExample",
        "label": "باقة المؤسسات",
        "monthly_query_limit": None,
        "max_files": None,  # غير محدود
        "priority": "highest",
    },
}

# =============================================================================
# طبقة تخزين بسيطة تعتمد على ملف JSON، حتى لا تضيع بيانات الشركات
# كل مرة يعاد فيها تشغيل السيرفر (كما كان يحدث مع القاموس بالذاكرة فقط).
# هذا حل مؤقت لطيف؛ الخطوة التالية الحقيقية هي الانتقال إلى PostgreSQL.
# =============================================================================
DB_FILE = Path(__file__).parent / "companies_db.json"


def _load_db() -> dict:
    if DB_FILE.exists():
        try:
            return json.loads(DB_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_db(data: dict) -> None:
    DB_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


DB_COMPANIES: dict = _load_db()          # api_key -> company record
PENDING_SESSIONS: dict = {}              # stripe session_id -> api_key (مؤقت بالذاكرة، لا يحتاج بقاء طويل)


def current_month_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def get_company(api_key: str) -> dict:
    company = DB_COMPANIES.get(api_key)
    if not company:
        raise HTTPException(status_code=401, detail="مفتاح الـ API غير صالح أو غير موجود.")
    return company


def enforce_plan_limits(company: dict) -> None:
    """يرفع خطأ 429 إذا تجاوزت الشركة الحد الشهري المسموح لباقتها."""
    plan = PLANS.get(company["plan"], PLANS["basic"])
    limit = plan["monthly_query_limit"]
    if limit is None:
        return  # باقة غير محدودة

    month = current_month_key()
    usage = company.setdefault("usage", {})
    used = usage.get(month, 0)
    if used >= limit:
        raise HTTPException(
            status_code=429,
            detail=f"وصلت للحد الأقصى ({limit} استفسار) لباقتكم هذا الشهر. يرجى الترقية للباقة الاحترافية للاستمرار.",
        )


def record_usage(company: dict) -> None:
    month = current_month_key()
    usage = company.setdefault("usage", {})
    usage[month] = usage.get(month, 0) + 1
    _save_db(DB_COMPANIES)


# =============================================================================
# إرسال البريد الإلكتروني (اختياري) — إذا كانت إعدادات SMTP موجودة،
# يُرسل مفتاح الـ API تلقائيًا للعميل فور تفعيل اشتراكه، حتى لو أغلق المتصفح.
# =============================================================================
def send_api_key_email(to_email: str, company_name: str, plan: str, api_key: str) -> None:
    if not (SMTP_HOST and SMTP_USER and SMTP_PASSWORD and to_email):
        return
    try:
        body = (
            f"مرحبًا {company_name}،\n\n"
            f"تم تفعيل اشتراككم بنجاح في {PLANS.get(plan, {}).get('label', plan)}.\n"
            f"مفتاح الـ API الخاص بكم هو:\n\n{api_key}\n\n"
            f"احتفظوا به في مكان آمن، وستحتاجونه لإرسال الطلبات إلى نقطة /enterprise/ask.\n\n"
            f"شكرًا لاشتراككم معنا."
        )
        msg = MIMEText(body, _charset="utf-8")
        msg["Subject"] = "تم تفعيل اشتراككم — مفتاح الـ API الخاص بكم"
        msg["From"] = SMTP_FROM
        msg["To"] = to_email

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM, [to_email], msg.as_string())
    except Exception as e:
        print(f"⚠️  فشل إرسال بريد مفتاح الـ API: {e}")


# =============================================================================
# طبقة الذكاء الاصطناعي الحقيقية — تدعم Gemini (مجاني) أو Claude (Anthropic)
# أي مفتاح موجود بمتغيرات البيئة يُستخدم تلقائيًا. تعطي ردًا مبنيًا فعليًا
# على سؤال العميل + بيانات ملفه المرفوع (إن وجد).
# =============================================================================
def _build_system_prompt(company_name: str, file_data: str) -> str:
    prompt = (
        f"أنت مساعد ذكاء اصطناعي خاص بشركة \"{company_name}\". "
        "أجب على أسئلة العملاء بالاعتماد فقط على المعلومات المتوفرة لك عن الشركة أدناه إن وجدت، "
        "وإن لم تكن كافية فأجب بعمومية مهذبة توضح أنك بحاجة لمزيد من البيانات. "
        "أجب بنفس لغة سؤال المستخدم (عربي أو إنجليزي)."
    )
    if file_data:
        prompt += f"\n\nبيانات الشركة المرفوعة:\n{file_data[:6000]}"
    return prompt


def _call_gemini(question: str, system_prompt: str) -> str:
    import requests
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    )
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": question}]}],
    }
    resp = requests.post(url, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


def _call_anthropic(question: str, system_prompt: str) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        system=system_prompt,
        messages=[{"role": "user", "content": question}],
    )
    return "".join(block.text for block in response.content if hasattr(block, "text"))


def generate_ai_answer(question: str, company_name: str, file_data: str) -> str:
    if not GEMINI_API_KEY and not ANTHROPIC_API_KEY:
        # رد احتياطي بسيط في حال عدم إعداد أي مفتاح ذكاء اصطناعي بعد
        base = f"شكرًا لسؤالكم: \"{question}\". "
        if file_data:
            base += "بناءً على المستند المرفوع، يمكنني مساعدتكم بمزيد من التفاصيل بمجرد تفعيل الذكاء الاصطناعي الحقيقي."
        else:
            base += "لم يتم رفع أي مستندات بعد لهذه الشركة."
        return base

    system_prompt = _build_system_prompt(company_name, file_data)

    try:
        if GEMINI_API_KEY:
            return _call_gemini(question, system_prompt)
        return _call_anthropic(question, system_prompt)
    except Exception as e:
        return f"عذرًا، حدث خطأ أثناء توليد الرد الذكي: {e}"


# =============================================================================
# نماذج البيانات
# =============================================================================
class QuestionRequest(BaseModel):
    question: str


# =============================================================================
# 1) إنشاء جلسة الدفع — الآن تطلب إيميل العميل أيضًا لإرسال المفتاح لاحقًا
# =============================================================================
@app.post("/enterprise/create-checkout-session")
async def create_checkout_session(
    name: str = Form(...),
    email: str = Form(...),
    plan: str = Form(...),
):
    if plan not in PLANS:
        raise HTTPException(status_code=400, detail="الباقة المختارة غير صالحة.")

    try:
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            customer_email=email,
            line_items=[{"price": PLANS[plan]["price_id"], "quantity": 1}],
            mode="subscription",
            metadata={"company_name": name, "plan": plan, "email": email},
            success_url=f"{FRONTEND_URL}/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{FRONTEND_URL}/?canceled=true",
        )
        return {"checkout_url": checkout_session.url, "session_id": checkout_session.id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# 2) Webhook — القلب الفعلي للتفعيل التلقائي. يُستدعى من Stripe مباشرة،
#    فور نجاح الدفع الحقيقي، ولا يمكن تزويره من المتصفح.
# =============================================================================
@app.post("/webhook/stripe")
async def stripe_webhook(request: Request, stripe_signature: str = Header(None)):
    payload = await request.body()

    try:
        if STRIPE_WEBHOOK_SECRET:
            event = stripe.Webhook.construct_event(payload, stripe_signature, STRIPE_WEBHOOK_SECRET)
        else:
            event = json.loads(payload)  # وضع تجريبي فقط بدون تحقق توقيع
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"توقيع Webhook غير صالح: {e}")

    event_type = event["type"]
    obj = event["data"]["object"]

    if event_type == "checkout.session.completed":
        session_id = obj["id"]
        metadata = obj.get("metadata", {}) or {}
        name = metadata.get("company_name", "شركة بدون اسم")
        plan = metadata.get("plan", "basic")
        email = metadata.get("email", "")

        api_key = f"ent_key_{uuid.uuid4().hex}"
        DB_COMPANIES[api_key] = {
            "name": name,
            "plan": plan,
            "email": email,
            "file_data": "",
            "usage": {},
            "created_at": datetime.now(timezone.utc).isoformat(),
            "stripe_customer_id": obj.get("customer"),
            "stripe_subscription_id": obj.get("subscription"),
            "active": True,
        }
        _save_db(DB_COMPANIES)
        PENDING_SESSIONS[session_id] = api_key

        # إرسال المفتاح فورًا بالبريد (إن كانت إعدادات SMTP مُفعّلة)
        send_api_key_email(email, name, plan, api_key)
        print(f"✅ تفعيل تلقائي فوري: {name} — {plan} — {api_key}")

    elif event_type in ("customer.subscription.deleted",):
        sub_id = obj.get("id")
        for company in DB_COMPANIES.values():
            if company.get("stripe_subscription_id") == sub_id:
                company["active"] = False
        _save_db(DB_COMPANIES)

    return {"status": "received"}


# =============================================================================
# 3) جلب نتيجة الجلسة فورًا — تستخدمها صفحة "شكرًا على اشتراكك" لعرض المفتاح
#    مباشرة للعميل بدون أي تدخل يدوي منك.
# =============================================================================
@app.get("/enterprise/session/{session_id}")
async def get_session_result(session_id: str):
    api_key = PENDING_SESSIONS.get(session_id)
    if not api_key:
        # الـ webhook قد يستغرق ثوانٍ قليلة — الواجهة يجب أن تعيد المحاولة كل ثانيتين مثلاً
        raise HTTPException(status_code=404, detail="لم يتم تفعيل الاشتراك بعد. حاول خلال لحظات.")

    company = DB_COMPANIES[api_key]
    return {
        "status": "success",
        "company": company["name"],
        "plan": company["plan"],
        "plan_label": PLANS.get(company["plan"], {}).get("label", company["plan"]),
        "api_key": api_key,
    }


# =============================================================================
# 4) رفع/تحديث ملف بيانات الشركة بعد التفعيل (يتطلب مفتاح API صالح)
# =============================================================================
@app.post("/enterprise/upload")
async def upload_company_file(x_api_key: str = Header(None), file: UploadFile = File(...)):
    company = get_company(x_api_key)
    content_bytes = await file.read()
    company["file_data"] = content_bytes.decode("utf-8", errors="ignore")
    _save_db(DB_COMPANIES)
    return {"status": "success", "message": "تم رفع الملف وربطه بحساب شركتكم بنجاح."}


# =============================================================================
# 5) معلومات الحساب الحالي — مفيدة للوحة تحكم الفرونت إند
# =============================================================================
@app.get("/enterprise/me")
async def get_me(x_api_key: str = Header(None)):
    company = get_company(x_api_key)
    plan = PLANS.get(company["plan"], PLANS["basic"])
    used = company.get("usage", {}).get(current_month_key(), 0)
    return {
        "company": company["name"],
        "plan": company["plan"],
        "plan_label": plan["label"],
        "active": company.get("active", True),
        "monthly_query_limit": plan["monthly_query_limit"],
        "queries_used_this_month": used,
        "has_uploaded_file": bool(company.get("file_data")),
    }


# =============================================================================
# 6) نقطة الأسئلة — الآن برد ذكاء اصطناعي حقيقي + تطبيق حدود الباقة فعليًا
# =============================================================================
@app.post("/enterprise/ask")
async def ask_ai_assistant(payload: QuestionRequest, x_api_key: str = Header(None)):
    company = get_company(x_api_key)

    if not company.get("active", True):
        raise HTTPException(status_code=403, detail="تم إلغاء الاشتراك. يرجى تجديد الباقة للمتابعة.")

    enforce_plan_limits(company)

    answer = generate_ai_answer(payload.question, company["name"], company.get("file_data", ""))
    record_usage(company)

    return {
        "company": company["name"],
        "plan": company["plan"],
        "answer": answer,
    }


# =============================================================================
# تسجيل يدوي مباشر بدون دفع — مفيد للتجربة المجانية فقط، وليس بديلاً عن Stripe
# =============================================================================
@app.post("/enterprise/register-trial")
async def register_trial(name: str = Form(...), email: str = Form(...)):
    api_key = f"trial_key_{uuid.uuid4().hex}"
    DB_COMPANIES[api_key] = {
        "name": name,
        "plan": "basic",
        "email": email,
        "file_data": "",
        "usage": {},
        "created_at": datetime.now(timezone.utc).isoformat(),
        "active": True,
        "trial": True,
    }
    _save_db(DB_COMPANIES)
    send_api_key_email(email, name, "basic", api_key)
    return {"status": "success", "company": name, "plan": "basic", "api_key": api_key}


# =============================================================================
# روابط PayPal الثابتة — الفرونت إند يجلبها من هنا بدل كتابتها يدويًا بالـ HTML
# =============================================================================
@app.get("/enterprise/paypal-links")
async def get_paypal_links():
    return PAYPAL_LINKS


# =============================================================================
# مطالبة تفعيل عبر PayPal — النسخة الآمنة: بدل ما نصدر مفتاح API فورًا،
# نخزن الطلب كـ "بانتظار المراجعة" فقط. أنت (الأدمن) تراجع حساب PayPal يدويًا،
# وبس لما تتأكد إن الدفعة وصلت فعليًا، تضغط "موافقة" ووقتها بس يتولد
# المفتاح ويوصل للعميل (بالبريد إذا فعّلت SMTP، أو تاخده من لوحة الإدارة
# وترسله يدويًا). العميل ما رح ياخد أي مفتاح قبل موافقتك.
# =============================================================================
PENDING_FILE = Path(__file__).parent / "pending_claims.json"


def _load_pending() -> dict:
    if PENDING_FILE.exists():
        try:
            return json.loads(PENDING_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_pending(data: dict) -> None:
    PENDING_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


PENDING_CLAIMS: dict = _load_pending()  # request_id -> {name, email, plan, paypal_reference, submitted_at}


class PaypalClaim(BaseModel):
    name: str
    email: str
    plan: str
    paypal_reference: str = ""


@app.post("/enterprise/paypal/claim")
async def claim_paypal_subscription(claim: PaypalClaim):
    if claim.plan not in PLANS:
        raise HTTPException(status_code=400, detail="الباقة غير صالحة.")

    request_id = f"req_{uuid.uuid4().hex}"
    PENDING_CLAIMS[request_id] = {
        "name": claim.name,
        "email": claim.email,
        "plan": claim.plan,
        "paypal_reference": claim.paypal_reference,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_pending(PENDING_CLAIMS)

    return {
        "status": "pending",
        "message": "تم استلام طلبكم بنجاح. بعد التحقق من الدفعة سيصلكم مفتاح API عبر البريد الإلكتروني خلال ساعات العمل.",
        "request_id": request_id,
    }


def _check_admin(x_admin_token: str) -> None:
    if not ADMIN_TOKEN or x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="غير مصرح لك بالوصول لهذه النقطة.")


@app.get("/admin/companies")
async def admin_list_companies(x_admin_token: str = Header(None)):
    _check_admin(x_admin_token)
    return DB_COMPANIES


@app.get("/admin/pending-claims")
async def admin_list_pending(x_admin_token: str = Header(None)):
    """اعرض كل الطلبات بانتظار المراجعة — راجعها مقابل حساب PayPal قبل الموافقة."""
    _check_admin(x_admin_token)
    return PENDING_CLAIMS


@app.post("/admin/approve-claim/{request_id}")
async def admin_approve_claim(request_id: str, x_admin_token: str = Header(None)):
    """استخدمها فقط بعد ما تتأكد يدويًا من لوحة PayPal إن الدفعة وصلت فعليًا.
    هنا فقط يتولد مفتاح API الحقيقي ويُرسل للعميل."""
    _check_admin(x_admin_token)
    claim = PENDING_CLAIMS.get(request_id)
    if not claim:
        raise HTTPException(status_code=404, detail="الطلب غير موجود أو تمت معالجته مسبقًا.")

    api_key = f"ent_key_{uuid.uuid4().hex}"
    DB_COMPANIES[api_key] = {
        "name": claim["name"],
        "plan": claim["plan"],
        "email": claim["email"],
        "file_data": "",
        "usage": {},
        "created_at": datetime.now(timezone.utc).isoformat(),
        "active": True,
        "payment_method": "paypal",
        "paypal_reference": claim.get("paypal_reference", ""),
        "verified": True,
    }
    _save_db(DB_COMPANIES)

    del PENDING_CLAIMS[request_id]
    _save_pending(PENDING_CLAIMS)

    send_api_key_email(claim["email"], claim["name"], claim["plan"], api_key)

    return {
        "status": "success",
        "message": f"تم تفعيل {claim['name']} وإرسال المفتاح.",
        "company": claim["name"],
        "api_key": api_key,  # يظهر لك هنا أيضًا لترسله يدويًا إن لم يكن SMTP مفعّلاً
    }


@app.post("/admin/reject-claim/{request_id}")
async def admin_reject_claim(request_id: str, x_admin_token: str = Header(None)):
    """استخدمها إذا تبيّن أن الطلب غير حقيقي (لم تصل أي دفعة مطابقة)."""
    _check_admin(x_admin_token)
    if request_id not in PENDING_CLAIMS:
        raise HTTPException(status_code=404, detail="الطلب غير موجود.")
    removed = PENDING_CLAIMS.pop(request_id)
    _save_pending(PENDING_CLAIMS)
    return {"status": "success", "message": f"تم رفض طلب {removed['name']}."}


@app.post("/admin/revoke/{api_key}")
async def admin_revoke(api_key: str, x_admin_token: str = Header(None)):
    """استخدمها لإيقاف حساب مفعّل مسبقًا (مثلاً عند إلغاء الاشتراك)."""
    _check_admin(x_admin_token)
    company = get_company(api_key)
    company["active"] = False
    _save_db(DB_COMPANIES)
    return {"status": "success", "message": f"تم إيقاف حساب {company['name']}."}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)