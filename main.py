"""Enterprise AI SaaS Backend.

تثبيت المكتبات:
    pip install -r requirements.txt

ثم أنشئ ملف .env بجانب هذا الملف وعبّئ القيم الحقيقية (لا تكتبها هنا بالكود):
    STRIPE_SECRET_KEY=...
    STRIPE_WEBHOOK_SECRET=...
    ANTHROPIC_API_KEY=... (أو GEMINI_API_KEY)
    PAYPAL_CLIENT_ID=...
    PAYPAL_SECRET=...
    PAYPAL_WEBHOOK_ID=...
    ADMIN_TOKEN=...
"""

import hashlib
import json
import os
import secrets
import smtplib
import time
import uuid
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Annotated

import anthropic
import requests
import stripe
import uvicorn
from dotenv import load_dotenv
from fastapi import (
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

load_dotenv()

app = FastAPI(title="Enterprise AI SaaS Backend", version="4.0")

app.add_middleware(
    CORSMiddleware,
    # في الإنتاج: استبدلها برابط موقعك فقط
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

OptionalHeader = Annotated[str | None, Header()]


@app.get("/")
async def health_check():
    """نقطة فحص بسيطة للتأكد أن الخادم شغال."""
    return {"status": "ok", "service": "Enterprise AI SaaS Backend"}


# ---------------------------------------------------------------------------
# الإعدادات (كلها من متغيرات البيئة — لا تُكتب القيم الحقيقية هنا)
# ---------------------------------------------------------------------------
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

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
MAX_UPLOAD_BYTES = 2 * 1024 * 1024  # 2MB

# PayPal — للتفعيل التلقائي الحقيقي عبر Webhook (بدون موافقة يدوية)
PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID", "")
PAYPAL_SECRET = os.environ.get("PAYPAL_SECRET", "")
PAYPAL_WEBHOOK_ID = os.environ.get("PAYPAL_WEBHOOK_ID", "")
PAYPAL_API_BASE = os.environ.get(
    "PAYPAL_API_BASE", "https://api-m.paypal.com"  # Live
)
# يطابق كل باقة بمبلغها (لتحديد الباقة من المبلغ المدفوع فعليًا)
PLAN_PRICES_USD = {"basic": 29, "pro": 79, "enterprise": 199}
TRIAL_DAYS = 7

PAYPAL_LINKS = {
    "basic": "https://www.paypal.com/ncp/payment/23V3WQK4NVTG4",
    "pro": "https://www.paypal.com/ncp/payment/Z59KCWS6MZAC6",
    "enterprise": "https://www.paypal.com/ncp/payment/CS59KMKAETBFC",
}

if not ADMIN_TOKEN:
    print("⚠️  ADMIN_TOKEN غير مُعرّف — نقاط /admin/* مقفلة بالكامل.")
if not stripe.api_key:
    print("⚠️  STRIPE_SECRET_KEY غير مُعرّف — الدفع عبر البطاقات لن يعمل.")
if not STRIPE_WEBHOOK_SECRET:
    print(
        "⚠️  STRIPE_WEBHOOK_SECRET غير مُعرّف — "
        "webhook البطاقات سيرفض الطلبات."
    )
if not ANTHROPIC_API_KEY and not GEMINI_API_KEY:
    print("⚠️  لا يوجد مفتاح ذكاء اصطناعي — سيتم استخدام رد احتياطي.")
if not (PAYPAL_CLIENT_ID and PAYPAL_SECRET and PAYPAL_WEBHOOK_ID):
    print("⚠️  إعدادات PayPal (Client ID / Secret / Webhook ID) غير مكتملة.")

# ---------------------------------------------------------------------------
# الباقات (غيّر price_id لقيمك الحقيقية من لوحة Stripe)
# ---------------------------------------------------------------------------
PLANS: dict[str, dict] = {
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
        "max_files": None,
        "priority": "highest",
    },
}

# ---------------------------------------------------------------------------
# التخزين (ملفات JSON) — الخطوة التالية الحقيقية هي PostgreSQL
# ---------------------------------------------------------------------------
DB_FILE = Path(__file__).parent / "companies_db.json"
PENDING_FILE = Path(__file__).parent / "pending_claims.json"


def _load_json(path: Path) -> dict:
    """يقرأ ملف JSON ويرجع قاموسًا فارغًا عند أي مشكلة."""
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    return {}


def _save_json(path: Path, data: dict) -> None:
    """يكتب القاموس في ملف JSON."""
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _save_db() -> None:
    """يحفظ بيانات الشركات."""
    _save_json(DB_FILE, DB_COMPANIES)


def _save_pending() -> None:
    """يحفظ طلبات PayPal المعلّقة (نية الدفع قبل تأكيد PayPal)."""
    _save_json(PENDING_FILE, PENDING_CLAIMS)


DB_COMPANIES: dict = _load_json(DB_FILE)  # api_key -> company record
PENDING_CLAIMS: dict = _load_json(PENDING_FILE)  # request_id -> claim


def current_month_key() -> str:
    """مفتاح الشهر الحالي بصيغة YYYY-MM."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _hash_password(
    password: str, salt_hex: str | None = None
) -> tuple[str, str]:
    """يشفّر كلمة السر بـ PBKDF2 (لا حاجة لمكتبات خارجية)."""
    salt = bytes.fromhex(salt_hex) if salt_hex else os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return digest.hex(), salt.hex()


def _verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    """يتحقق من كلمة السر بمقارنة ثابتة الزمن."""
    computed, _ = _hash_password(password, salt_hex)
    return secrets.compare_digest(computed, hash_hex)


def _find_company_by_email(email: str) -> tuple[str | None, dict | None]:
    """يبحث عن حساب موجود بنفس الإيميل (لربط الدفع بحساب التجربة)."""
    email_norm = email.strip().lower()
    for api_key, company in DB_COMPANIES.items():
        if company.get("email", "").strip().lower() == email_norm:
            return api_key, company
    return None, None


def is_trial_expired(company: dict) -> bool:
    """يرجع True إذا انتهت الفترة التجريبية ولم يدفع العميل بعد."""
    if not company.get("trial") or company.get("payment_method"):
        return False
    ends_at = company.get("trial_ends_at")
    if not ends_at:
        return False
    return datetime.now(timezone.utc) > datetime.fromisoformat(ends_at)


def trial_status(company: dict) -> dict:
    """يحسب الوقت المتبقي من التجربة لعرضه كعدّاد بالواجهة."""
    if not company.get("trial") or company.get("payment_method"):
        return {
            "is_trial": False,
            "trial_ends_at": None,
            "seconds_remaining": 0,
        }
    ends_at = datetime.fromisoformat(company["trial_ends_at"])
    remaining = (ends_at - datetime.now(timezone.utc)).total_seconds()
    return {
        "is_trial": True,
        "trial_ends_at": company["trial_ends_at"],
        "seconds_remaining": max(0, int(remaining)),
    }


def get_company(api_key: str | None) -> dict:
    """يرجع الشركة المرتبطة بالمفتاح أو يرفع خطأ 401."""
    company = DB_COMPANIES.get(api_key) if api_key else None
    if not company:
        raise HTTPException(
            status_code=401,
            detail="مفتاح الـ API غير صالح أو غير موجود.",
        )
    return company


def enforce_plan_limits(company: dict) -> None:
    """يرفع خطأ 429 إذا تجاوزت الشركة حد باقتها الشهري."""
    plan = PLANS.get(company["plan"], PLANS["basic"])
    limit = plan["monthly_query_limit"]
    if limit is None:
        return

    used = company.setdefault("usage", {}).get(current_month_key(), 0)
    if used >= limit:
        raise HTTPException(
            status_code=429,
            detail=(
                f"وصلت للحد الأقصى ({limit} استفسار) لباقتكم هذا الشهر. "
                "يرجى الترقية للباقة الاحترافية للاستمرار."
            ),
        )


def record_usage(company: dict) -> None:
    """يزيد عداد الاستخدام الشهري للشركة."""
    month = current_month_key()
    usage = company.setdefault("usage", {})
    usage[month] = usage.get(month, 0) + 1
    _save_db()


# ---------------------------------------------------------------------------
# البريد الإلكتروني (اختياري)
# ---------------------------------------------------------------------------
def send_api_key_email(
    to_email: str, company_name: str, plan: str, api_key: str
) -> None:
    """يرسل مفتاح الـ API للعميل إذا كانت إعدادات SMTP موجودة."""
    if not (SMTP_HOST and SMTP_USER and SMTP_PASSWORD and to_email):
        return
    label = PLANS.get(plan, {}).get("label", plan)
    body = (
        f"مرحبًا {company_name}،\n\n"
        f"تم تفعيل اشتراككم بنجاح في {label}.\n"
        f"مفتاح الـ API الخاص بكم هو:\n\n{api_key}\n\n"
        "احتفظوا به في مكان آمن، وستحتاجونه لإرسال الطلبات "
        "إلى نقطة /enterprise/ask.\n\n"
        "شكرًا لاشتراككم معنا."
    )
    msg = MIMEText(body, _charset="utf-8")
    msg["Subject"] = "تم تفعيل اشتراككم — مفتاح الـ API الخاص بكم"
    msg["From"] = SMTP_FROM
    msg["To"] = to_email
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM, [to_email], msg.as_string())
    except OSError as exc:
        print(f"⚠️  فشل إرسال بريد مفتاح الـ API: {exc}")


# ---------------------------------------------------------------------------
# طبقة الذكاء الاصطناعي (Gemini أو Claude)
# ---------------------------------------------------------------------------
def _build_system_prompt(company_name: str, file_data: str) -> str:
    prompt = (
        f'أنت مساعد ذكاء اصطناعي خاص بشركة "{company_name}". '
        "أجب على أسئلة العملاء بالاعتماد فقط على المعلومات المتوفرة "
        "لك عن الشركة أدناه إن وجدت، وإن لم تكن كافية فأجب بعمومية "
        "مهذبة توضح أنك بحاجة لمزيد من البيانات. "
        "أجب بنفس لغة سؤال المستخدم (عربي أو إنجليزي)."
    )
    if file_data:
        prompt += f"\n\nبيانات الشركة المرفوعة:\n{file_data[:6000]}"
    return prompt


def _call_gemini(question: str, system_prompt: str) -> str:
    resp = requests.post(
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-2.0-flash:generateContent",
        headers={"x-goog-api-key": GEMINI_API_KEY},
        json={
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": question}]}],
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


def _call_anthropic(question: str, system_prompt: str) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        system=system_prompt,
        messages=[{"role": "user", "content": question}],
    )
    return "".join(getattr(block, "text", "") for block in response.content)


def generate_ai_answer(
    question: str, company_name: str, file_data: str
) -> str:
    """يولّد رد الذكاء الاصطناعي أو رد احتياطي إن لم يوجد مفتاح."""
    if not GEMINI_API_KEY and not ANTHROPIC_API_KEY:
        base = f'شكرًا لسؤالكم: "{question}". '
        if file_data:
            base += (
                "بناءً على المستند المرفوع، يمكنني مساعدتكم بمزيد من "
                "التفاصيل بمجرد تفعيل الذكاء الاصطناعي الحقيقي."
            )
        else:
            base += "لم يتم رفع أي مستندات بعد لهذه الشركة."
        return base

    system_prompt = _build_system_prompt(company_name, file_data)
    try:
        if GEMINI_API_KEY:
            return _call_gemini(question, system_prompt)
        return _call_anthropic(question, system_prompt)
    except (
        requests.RequestException,
        anthropic.APIError,
        KeyError,
        IndexError,
        ValueError,
    ) as exc:
        print(f"⚠️  خطأ من مزود الذكاء الاصطناعي: {exc}")
        return "عذرًا، حدث خطأ أثناء توليد الرد الذكي. حاول لاحقًا."


# ---------------------------------------------------------------------------
# نماذج البيانات
# ---------------------------------------------------------------------------
class QuestionRequest(BaseModel):
    """جسم طلب السؤال."""

    question: str


class PaypalClaim(BaseModel):
    """جسم طلب تسجيل نية الدفع عبر PayPal."""

    name: str
    email: str
    plan: str
    paypal_reference: str = ""


# ---------------------------------------------------------------------------
# 1) إنشاء جلسة الدفع عبر Stripe (بطاقات)
# ---------------------------------------------------------------------------
@app.post("/enterprise/create-checkout-session")
async def create_checkout_session(
    name: Annotated[str, Form()],
    email: Annotated[str, Form()],
    plan: Annotated[str, Form()],
):
    """ينشئ جلسة دفع Stripe للباقة المختارة."""
    if plan not in PLANS:
        raise HTTPException(
            status_code=400, detail="الباقة المختارة غير صالحة."
        )

    def _create():
        return stripe.checkout.Session.create(
            payment_method_types=["card"],
            customer_email=email,
            line_items=[{"price": PLANS[plan]["price_id"], "quantity": 1}],
            mode="subscription",
            metadata={"company_name": name, "plan": plan, "email": email},
            success_url=(
                f"{FRONTEND_URL}/success?session_id={{CHECKOUT_SESSION_ID}}"
            ),
            cancel_url=f"{FRONTEND_URL}/?canceled=true",
        )

    try:
        session = await run_in_threadpool(_create)
    except stripe.StripeError as exc:
        print(f"⚠️  فشل إنشاء جلسة Stripe: {exc}")
        raise HTTPException(
            status_code=500, detail="تعذر إنشاء جلسة الدفع. حاول لاحقًا."
        ) from exc
    return {"checkout_url": session.url, "session_id": session.id}


# ---------------------------------------------------------------------------
# 2) Webhook Stripe — التفعيل التلقائي بعد نجاح دفع البطاقة
# ---------------------------------------------------------------------------
async def _activate_from_session(obj: dict) -> None:
    """يرقّي حساب التجربة الموجود أو ينشئ حسابًا جديدًا (مرة لكل جلسة)."""
    session_id = obj["id"]
    if any(
        c.get("stripe_session_id") == session_id
        for c in DB_COMPANIES.values()
    ):
        return  # Stripe أعاد إرسال نفس الحدث

    metadata = obj.get("metadata") or {}
    name = metadata.get("company_name", "شركة بدون اسم")
    plan = metadata.get("plan", "basic")
    if plan not in PLANS:
        plan = "basic"
    email = metadata.get("email", "")

    existing_key, existing = _find_company_by_email(email)
    if existing:
        existing.update(
            plan=plan,
            trial=False,
            payment_method="stripe",
            stripe_session_id=session_id,
            stripe_customer_id=obj.get("customer"),
            stripe_subscription_id=obj.get("subscription"),
            active=True,
        )
        _save_db()
        await run_in_threadpool(
            send_api_key_email, email, existing["name"], plan, existing_key
        )
        print(f"✅ ترقية حساب (Stripe): {existing['name']} — {plan}")
        return

    api_key = f"ent_key_{uuid.uuid4().hex}"
    DB_COMPANIES[api_key] = {
        "name": name,
        "plan": plan,
        "email": email,
        "file_data": "",
        "usage": {},
        "created_at": datetime.now(timezone.utc).isoformat(),
        "trial": False,
        "stripe_session_id": session_id,
        "stripe_customer_id": obj.get("customer"),
        "stripe_subscription_id": obj.get("subscription"),
        "payment_method": "stripe",
        "active": True,
    }
    _save_db()
    await run_in_threadpool(send_api_key_email, email, name, plan, api_key)
    print(f"✅ تفعيل تلقائي (Stripe): {name} — {plan}")


def _deactivate_subscription(sub_id: str | None) -> None:
    """يوقف الحسابات المرتبطة باشتراك ملغى."""
    for company in DB_COMPANIES.values():
        if company.get("stripe_subscription_id") == sub_id:
            company["active"] = False
    _save_db()


@app.post("/webhook/stripe")
async def stripe_webhook(
    request: Request, stripe_signature: OptionalHeader = None
):
    """يستقبل أحداث Stripe بعد التحقق من التوقيع."""
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(
            status_code=500,
            detail="STRIPE_WEBHOOK_SECRET غير مُعدّ على السيرفر.",
        )

    payload = await request.body()
    try:
        event = stripe.Webhook.construct_event(
            payload, stripe_signature or "", STRIPE_WEBHOOK_SECRET
        )
    except (ValueError, stripe.SignatureVerificationError) as exc:
        raise HTTPException(
            status_code=400, detail="توقيع Webhook غير صالح."
        ) from exc

    obj = event["data"]["object"]
    if event["type"] == "checkout.session.completed":
        await _activate_from_session(obj)
    elif event["type"] == "customer.subscription.deleted":
        _deactivate_subscription(obj.get("id"))

    return {"status": "received"}


# ---------------------------------------------------------------------------
# 3) نتيجة الجلسة (صفحة الشكر) — خاصة بـ Stripe
# ---------------------------------------------------------------------------
@app.get("/enterprise/session/{session_id}")
async def get_session_result(session_id: str):
    """يرجع مفتاح الـ API بعد تفعيل جلسة Stripe."""
    for api_key, company in DB_COMPANIES.items():
        if company.get("stripe_session_id") == session_id:
            plan_info = PLANS.get(company["plan"], {})
            return {
                "status": "success",
                "company": company["name"],
                "plan": company["plan"],
                "plan_label": plan_info.get("label", company["plan"]),
                "api_key": api_key,
            }
    raise HTTPException(
        status_code=404,
        detail="لم يتم تفعيل الاشتراك بعد. حاول خلال لحظات.",
    )


# ---------------------------------------------------------------------------
# 4) رفع ملف بيانات الشركة
# ---------------------------------------------------------------------------
@app.post("/enterprise/upload")
async def upload_company_file(
    file: Annotated[UploadFile, File()],
    x_api_key: OptionalHeader = None,
):
    """يرفع ملف نصي ويربطه بحساب الشركة."""
    company = get_company(x_api_key)
    content_bytes = await file.read()
    if len(content_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail="الملف كبير جدًا (الحد الأقصى 2MB).",
        )
    company["file_data"] = content_bytes.decode("utf-8", errors="ignore")
    _save_db()
    return {
        "status": "success",
        "message": "تم رفع الملف وربطه بحساب شركتكم بنجاح.",
    }


# ---------------------------------------------------------------------------
# 5) معلومات الحساب
# ---------------------------------------------------------------------------
@app.get("/enterprise/me")
async def get_me(x_api_key: OptionalHeader = None):
    """يرجع معلومات الحساب، الاستخدام، وحالة التجربة (للعدّاد)."""
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
        "trial_expired": is_trial_expired(company),
        **trial_status(company),
    }


# ---------------------------------------------------------------------------
# 6) نقطة الأسئلة
# ---------------------------------------------------------------------------
@app.post("/enterprise/ask")
async def ask_ai_assistant(
    payload: QuestionRequest, x_api_key: OptionalHeader = None
):
    """يجيب على سؤال العميل مع تطبيق حدود الباقة."""
    company = get_company(x_api_key)

    if not company.get("active", True):
        raise HTTPException(
            status_code=403,
            detail="تم إلغاء الاشتراك. يرجى تجديد الباقة للمتابعة.",
        )

    if is_trial_expired(company):
        raise HTTPException(
            status_code=402,
            detail=(
                f"انتهت فترتك التجريبية ({TRIAL_DAYS} أيام). "
                "يرجى الاشتراك بأحد الباقات للاستمرار."
            ),
        )

    enforce_plan_limits(company)

    answer = await run_in_threadpool(
        generate_ai_answer,
        payload.question,
        company["name"],
        company.get("file_data", ""),
    )
    record_usage(company)

    return {
        "company": company["name"],
        "plan": company["plan"],
        "answer": answer,
    }


# ---------------------------------------------------------------------------
# تسجيل حقيقي: إنشاء حساب (تجربة مجانية 7 أيام) + تسجيل دخول بكلمة سر فعلية
# ---------------------------------------------------------------------------
class SignupRequest(BaseModel):
    """جسم طلب إنشاء حساب جديد."""

    name: str
    email: str
    password: str


class LoginRequest(BaseModel):
    """جسم طلب تسجيل الدخول."""

    email: str
    password: str


@app.post("/enterprise/signup")
async def signup(payload: SignupRequest):
    """ينشئ حسابًا حقيقيًا بكلمة سر، ويبدأ تجربة مجانية 7 أيام فورًا."""
    if len(payload.password) < 6:
        raise HTTPException(
            status_code=400, detail="كلمة السر يجب أن تكون 6 أحرف على الأقل."
        )
    existing_key, _ = _find_company_by_email(payload.email)
    if existing_key:
        raise HTTPException(
            status_code=409,
            detail="هذا البريد مسجّل مسبقًا. استخدم تسجيل الدخول.",
        )

    password_hash, password_salt = _hash_password(payload.password)
    trial_ends_at = (
        datetime.now(timezone.utc) + timedelta(days=TRIAL_DAYS)
    ).isoformat()

    api_key = f"trial_key_{uuid.uuid4().hex}"
    DB_COMPANIES[api_key] = {
        "name": payload.name,
        "plan": "pro",  # أثناء التجربة: كل المزايا مفتوحة لإقناع العميل
        "email": payload.email,
        "password_hash": password_hash,
        "password_salt": password_salt,
        "file_data": "",
        "usage": {},
        "created_at": datetime.now(timezone.utc).isoformat(),
        "active": True,
        "trial": True,
        "trial_ends_at": trial_ends_at,
        "payment_method": None,
    }
    _save_db()
    await run_in_threadpool(
        send_api_key_email, payload.email, payload.name, "pro", api_key
    )
    return {
        "status": "success",
        "company": payload.name,
        "api_key": api_key,
        "trial_ends_at": trial_ends_at,
        "trial_days": TRIAL_DAYS,
    }


@app.post("/enterprise/login")
async def login(payload: LoginRequest):
    """يتحقق من الإيميل وكلمة السر، ويرجع مفتاح الحساب."""
    api_key, company = _find_company_by_email(payload.email)
    wrong_credentials = HTTPException(
        status_code=401, detail="البريد الإلكتروني أو كلمة السر غير صحيحة."
    )
    if not company or "password_hash" not in company:
        raise wrong_credentials
    if not _verify_password(
        payload.password, company["password_salt"], company["password_hash"]
    ):
        raise wrong_credentials

    return {
        "status": "success",
        "company": company["name"],
        "plan": company["plan"],
        "api_key": api_key,
        **trial_status(company),
    }


# ---------------------------------------------------------------------------
# PayPal — تفعيل تلقائي حقيقي بدون أي موافقة يدوية
# يعتمد على Webhook حقيقي من سيرفرات PayPal نفسها (لا يمكن تزويره)
# ---------------------------------------------------------------------------
@app.get("/enterprise/paypal-links")
async def get_paypal_links():
    """يرجع روابط الدفع الثابتة للواجهة الأمامية."""
    return PAYPAL_LINKS


_paypal_token_cache: dict = {"token": None, "expires_at": 0}


def _get_paypal_access_token() -> str:
    """يجلب توكن PayPal، ويخزنه مؤقتًا حتى انتهاء صلاحيته."""
    now = time.time()
    cached = _paypal_token_cache
    if cached["token"] and cached["expires_at"] > now:
        return _paypal_token_cache["token"]

    resp = requests.post(
        f"{PAYPAL_API_BASE}/v1/oauth2/token",
        data={"grant_type": "client_credentials"},
        auth=(PAYPAL_CLIENT_ID, PAYPAL_SECRET),
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    _paypal_token_cache["token"] = data["access_token"]
    _paypal_token_cache["expires_at"] = now + data.get("expires_in", 3600) - 60
    return _paypal_token_cache["token"]


def _verify_paypal_webhook(headers: dict, body: dict) -> bool:
    """يتحقق من توقيع الإشعار مباشرة مع سيرفرات PayPal (لا يمكن تزويره)."""
    token = _get_paypal_access_token()
    payload = {
        "transmission_id": headers.get("paypal-transmission-id"),
        "transmission_time": headers.get("paypal-transmission-time"),
        "cert_url": headers.get("paypal-cert-url"),
        "auth_algo": headers.get("paypal-auth-algo"),
        "transmission_sig": headers.get("paypal-transmission-sig"),
        "webhook_id": PAYPAL_WEBHOOK_ID,
        "webhook_event": body,
    }
    resp = requests.post(
        f"{PAYPAL_API_BASE}/v1/notifications/verify-webhook-signature",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("verification_status") == "SUCCESS"


def _match_plan_by_amount(amount: float) -> str | None:
    """يحدد أي باقة تطابق المبلغ المدفوع فعليًا (بسماحية دولار واحد)."""
    for plan, price in PLAN_PRICES_USD.items():
        if abs(amount - price) < 1:
            return plan
    return None


async def _activate_paypal_payment(
    payer_email: str, plan: str, reference: str
) -> str:
    """يرقّي حساب التجربة الموجود، أو ينشئ حسابًا جديدًا إذا لم يوجد."""
    existing_key, existing = _find_company_by_email(payer_email)
    if existing:
        existing.update(
            plan=plan,
            trial=False,
            payment_method="paypal",
            paypal_reference=reference,
            verified=True,
            active=True,
        )
        _save_db()
        await run_in_threadpool(
            send_api_key_email,
            payer_email,
            existing["name"],
            plan,
            existing_key,
        )
        print(f"✅ ترقية حساب (PayPal): {existing['name']} — {plan}")
        return existing_key

    matched_claim, matched_request_id = None, None
    for request_id, claim in PENDING_CLAIMS.items():
        if claim["email"].strip().lower() == payer_email.strip().lower():
            matched_claim, matched_request_id = claim, request_id
            break

    name = (
        matched_claim["name"]
        if matched_claim
        else payer_email.split("@")[0]
    )

    api_key = f"ent_key_{uuid.uuid4().hex}"
    DB_COMPANIES[api_key] = {
        "name": name,
        "plan": plan,
        "email": payer_email,
        "file_data": "",
        "usage": {},
        "created_at": datetime.now(timezone.utc).isoformat(),
        "trial": False,
        "active": True,
        "payment_method": "paypal",
        "paypal_reference": reference,
        "verified": True,
    }
    _save_db()

    if matched_request_id:
        del PENDING_CLAIMS[matched_request_id]
        _save_pending()

    await run_in_threadpool(
        send_api_key_email, payer_email, name, plan, api_key
    )
    print(f"✅ تفعيل تلقائي (PayPal): {name} — {plan} — {payer_email}")
    return api_key


@app.post("/webhook/paypal")
async def paypal_webhook(request: Request):
    """يستقبل إشعار الدفع من PayPal ويفعّل الحساب فورًا عند التحقق الناجح."""
    if not (PAYPAL_CLIENT_ID and PAYPAL_SECRET and PAYPAL_WEBHOOK_ID):
        raise HTTPException(
            status_code=500, detail="إعدادات PayPal غير مكتملة."
        )

    body = await request.json()
    headers = {k.lower(): v for k, v in request.headers.items()}

    try:
        verified = await run_in_threadpool(
            _verify_paypal_webhook, headers, body
        )
    except requests.RequestException as exc:
        print(f"⚠️  فشل الاتصال بـ PayPal للتحقق: {exc}")
        raise HTTPException(
            status_code=502, detail="تعذر التحقق من الإشعار."
        ) from exc

    if not verified:
        raise HTTPException(status_code=400, detail="توقيع الإشعار غير صالح.")

    if body.get("event_type") != "PAYMENT.CAPTURE.COMPLETED":
        return {"status": "ignored"}

    resource = body.get("resource", {})
    amount = float(resource.get("amount", {}).get("value", 0))
    payer_email = (
        resource.get("payer", {}).get("email_address")
        or resource.get("payee", {}).get("email_address")
        or ""
    )
    reference = resource.get("id", "")

    plan = _match_plan_by_amount(amount)
    if not plan or not payer_email:
        print(f"⚠️  دفعة PayPal غير مطابقة لأي باقة: {amount} / {payer_email}")
        return {"status": "unmatched"}

    already = any(
        c.get("paypal_reference") == reference for c in DB_COMPANIES.values()
    )
    if already:
        return {"status": "already_processed"}

    await _activate_paypal_payment(payer_email, plan, reference)
    return {"status": "activated"}


@app.post("/enterprise/paypal/claim")
async def claim_paypal_subscription(claim: PaypalClaim):
    """يسجل نية الدفع، أو يرجع المفتاح فورًا إذا كانت الدفعة وصلت مسبقًا."""
    if claim.plan not in PLANS:
        raise HTTPException(status_code=400, detail="الباقة غير صالحة.")

    for api_key, company in DB_COMPANIES.items():
        if (
            company.get("payment_method") == "paypal"
            and company["email"].strip().lower() == claim.email.strip().lower()
            and company["plan"] == claim.plan
        ):
            return {
                "status": "success",
                "message": "تم تفعيل حسابكم بنجاح.",
                "api_key": api_key,
            }

    request_id = f"req_{uuid.uuid4().hex}"
    PENDING_CLAIMS[request_id] = {
        "name": claim.name,
        "email": claim.email,
        "plan": claim.plan,
        "paypal_reference": claim.paypal_reference,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_pending()

    return {
        "status": "pending",
        "message": (
            "تم استلام طلبكم. بعد تأكيد PayPal للدفعة (فوري تلقائيًا)، "
            "سيصلكم مفتاح API عبر البريد الإلكتروني."
        ),
        "request_id": request_id,
    }


# ---------------------------------------------------------------------------
# الإدارة (احتياطي يدوي نادر الاستخدام فقط)
# ---------------------------------------------------------------------------
def _check_admin(x_admin_token: str | None) -> None:
    """يتحقق من توكن الأدمن أو يرفع خطأ 403."""
    valid = (
        ADMIN_TOKEN
        and x_admin_token
        and secrets.compare_digest(x_admin_token, ADMIN_TOKEN)
    )
    if not valid:
        raise HTTPException(
            status_code=403,
            detail="غير مصرح لك بالوصول لهذه النقطة.",
        )


@app.get("/admin/companies")
async def admin_list_companies(x_admin_token: OptionalHeader = None):
    """يعرض كل الشركات."""
    _check_admin(x_admin_token)
    return DB_COMPANIES


@app.get("/admin/pending-claims")
async def admin_list_pending(x_admin_token: OptionalHeader = None):
    """يعرض الطلبات بانتظار تأكيد PayPal (حالة طبيعية، ليست مراجعة يدوية)."""
    _check_admin(x_admin_token)
    return PENDING_CLAIMS


@app.post("/admin/revoke/{api_key}")
async def admin_revoke(api_key: str, x_admin_token: OptionalHeader = None):
    """يوقف حسابًا مفعّلًا."""
    _check_admin(x_admin_token)
    company = get_company(api_key)
    company["active"] = False
    _save_db()
    return {
        "status": "success",
        "message": f"تم إيقاف حساب {company['name']}.",
    }


@app.post("/admin/activate/{api_key}")
async def admin_activate(api_key: str, x_admin_token: OptionalHeader = None):
    """إعادة تفعيل حساب موقوف."""
    _check_admin(x_admin_token)
    company = get_company(api_key)
    company["active"] = True
    _save_db()
    return {
        "status": "success",
        "message": f"تم تفعيل حساب {company['name']} بنجاح.",
    }


@app.delete("/admin/company/{api_key}")
async def admin_delete_company(api_key: str, x_admin_token: OptionalHeader = None):
    """حذف شركة أو حساب بشكل نهائي من النظام."""
    _check_admin(x_admin_token)
    if api_key in DB_COMPANIES:
        deleted_company = DB_COMPANIES.pop(api_key)
        _save_db()
        return {
            "status": "success",
            "message": f"تم حذف شركة {deleted_company.get('name')} نهائياً.",
        }
    raise HTTPException(status_code=404, detail="مفتاح الـ API غير موجود.")


if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
