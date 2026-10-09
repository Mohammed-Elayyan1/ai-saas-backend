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
    SENTRY_DSN=... (اختياري — مراقبة أخطاء مجانية عبر sentry.io)
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
import jwt
import psycopg2
import psycopg2.extras
import requests
import sentry_sdk
import stripe
import uvicorn
from cryptography.x509 import load_pem_x509_certificate
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
from fastapi import Response as FastAPIResponse
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

load_dotenv()

# ---------------------------------------------------------------------------
# مراقبة الأخطاء (Sentry) — مجانية حتى 5000 خطأ/شهر، بدون بطاقة ائتمان.
# إذا SENTRY_DSN غير معرّف، يعمل الموقع بشكل طبيعي بدون أي مراقبة (اختياري تمامًا).
# ---------------------------------------------------------------------------
SENTRY_DSN = os.environ.get("SENTRY_DSN", "")
if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        environment=os.environ.get("SENTRY_ENVIRONMENT", "production"),
        traces_sample_rate=0.2,
        send_default_pii=False,
    )
    print("✅ Sentry مفعّل — الأخطاء غير المتوقعة سترسل تنبيهًا تلقائيًا.")
else:
    print(
        "⚠️  SENTRY_DSN غير مُعرّف — لا توجد مراقبة أخطاء تلقائية. "
        "راجع تعليمات الإعداد بأعلى هذا الملف."
    )

app = FastAPI(title="Enterprise AI SaaS Backend", version="4.0")


def _real_client_ip(request: Request) -> str:
    """يرجع عنوان IP الحقيقي للزائر، وليس عنوان الوسيط الداخلي لـ Railway.

    Railway (وأي منصة استضافة خلف reverse proxy) توصل الطلبات عبر طبقة
    وسيطة، فعنوان الاتصال المباشر (request.client.host) يتغيّر بكل طلب
    ولا يمثّل جهاز الزائر الفعلي. العنوان الحقيقي موجود برأس
    X-Forwarded-For الذي تضيفه هذه الطبقة تلقائيًا.
    """
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return get_remote_address(request)


# ---------------------------------------------------------------------------
# حدّ لعدد المحاولات لكل IP — يمنع هجمات تخمين كلمات السر وإغراق السيرفر
# بحسابات تجريبية وهمية. يعمل بالذاكرة مباشرة، بدون أي خدمة خارجية أو تكلفة.
# ---------------------------------------------------------------------------
limiter = Limiter(key_func=_real_client_ip)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

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


_WIDGET_JS_PATH = Path(__file__).parent / "widget.js"


@app.get("/widget.js")
async def serve_widget_js():
    """يخدّم ويدجت المحادثة القابل للتضمين مباشرة من نفس السيرفر، حتى
    تقدر الشركات تضيفه بسطر واحد بمواقعها بدون أي استضافة إضافية."""
    if not _WIDGET_JS_PATH.exists():
        raise HTTPException(status_code=404, detail="widget.js غير موجود.")
    return FastAPIResponse(
        content=_WIDGET_JS_PATH.read_text(encoding="utf-8"),
        media_type="application/javascript; charset=utf-8",
    )


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
# بريد المالك/الأدمن — يستقبل تنبيهات فورية عند فشل أي عملية تفعيل دفع
# (مثلاً انقطاع الإنترنت لحظة الدفع)، حتى ما تضيع فلوس عميل بصمت.
ADMIN_ALERT_EMAIL = os.environ.get("ADMIN_ALERT_EMAIL", SMTP_FROM)

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
PAID_PERIOD_DAYS = 30
RESET_TOKEN_TTL_MINUTES = 30

FIREBASE_PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "")

# قاعدة بيانات PostgreSQL حقيقية (Railway يضيف DATABASE_URL أوتوماتيكيًا
# بعد ربط Add-on من نوع Postgres). بدونها نرجع لملفات JSON محليًا فقط
# (تُفقد عند كل Redeploy — غير مناسبة لعملاء حقيقيين).
DATABASE_URL = os.environ.get("DATABASE_URL", "")

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
if not FIREBASE_PROJECT_ID:
    print(
        "⚠️  FIREBASE_PROJECT_ID غير مُعرّف — "
        "تسجيل الدخول بـ Google لن يعمل."
    )
if not DATABASE_URL:
    print(
        "⚠️  DATABASE_URL غير مُعرّف — سيتم استخدام ملفات JSON محلية، "
        "وستُفقد كل الحسابات عند أي Redeploy. اربط Add-on من نوع "
        "PostgreSQL على Railway قبل استقبال أي عميل حقيقي."
    )

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
# التخزين: PostgreSQL حقيقي إذا تم ربط DATABASE_URL، وإلا ملفات JSON
# محلية (احتياطي للتطوير فقط — تُفقد عند كل Redeploy).
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


def _get_pg_connection():
    """يفتح اتصالًا جديدًا بقاعدة بيانات Railway Postgres.

    sslmode="prefer" (بدل "require") لأن اتصال الشبكة الداخلية على
    Railway غالبًا لا يحتاج SSL، وتثبيت "require" قد يفشل الاتصال.
    """
    sslmode = os.environ.get("PGSSLMODE", "prefer")
    return psycopg2.connect(DATABASE_URL, sslmode=sslmode)


def _init_pg_schema() -> None:
    """ينشئ الجدولين إذا لم يكونا موجودين (يعمل مرة واحدة عند الإقلاع)."""
    if not DATABASE_URL:
        return
    with _get_pg_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS companies (
                api_key TEXT PRIMARY KEY,
                data JSONB NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_claims (
                request_id TEXT PRIMARY KEY,
                data JSONB NOT NULL
            )
            """
        )
        conn.commit()


def _load_companies() -> dict:
    """يحمّل كل حسابات الشركات من Postgres، أو من ملف JSON احتياطيًا."""
    if not DATABASE_URL:
        return _load_json(DB_FILE)
    with _get_pg_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT api_key, data FROM companies")
        return {row[0]: row[1] for row in cur.fetchall()}


def _load_pending() -> dict:
    """يحمّل طلبات PayPal المعلّقة من Postgres، أو من JSON احتياطيًا."""
    if not DATABASE_URL:
        return _load_json(PENDING_FILE)
    with _get_pg_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT request_id, data FROM pending_claims")
        return {row[0]: row[1] for row in cur.fetchall()}


def _save_db() -> None:
    """يحفظ بيانات الشركات (Postgres إن توفر، وإلا ملف JSON)."""
    if not DATABASE_URL:
        _save_json(DB_FILE, DB_COMPANIES)
        return
    with _get_pg_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM companies")
        for api_key, data in DB_COMPANIES.items():
            cur.execute(
                "INSERT INTO companies (api_key, data) VALUES (%s, %s)",
                (api_key, psycopg2.extras.Json(data)),
            )
        conn.commit()


def _save_pending() -> None:
    """يحفظ طلبات PayPal المعلّقة (Postgres إن توفر، وإلا ملف JSON)."""
    if not DATABASE_URL:
        _save_json(PENDING_FILE, PENDING_CLAIMS)
        return
    with _get_pg_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM pending_claims")
        for request_id, data in PENDING_CLAIMS.items():
            cur.execute(
                "INSERT INTO pending_claims (request_id, data) "
                "VALUES (%s, %s)",
                (request_id, psycopg2.extras.Json(data)),
            )
        conn.commit()


_init_pg_schema()
DB_COMPANIES: dict = _load_companies()  # api_key -> company record
PENDING_CLAIMS: dict = _load_pending()  # request_id -> claim


def current_month_key() -> str:
    """مفتاح الشهر الحالي بصيغة YYYY-MM."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _hash_password(
    password: str, salt_hex: str | None = None
) -> tuple[str, str]:
    """يشفّر كلمة السر بـ PBKDF2 (لا حاجة لمكتبات خارجية)."""
    salt = bytes.fromhex(salt_hex) if salt_hex else os.urandom(16)
    # 120k تكرار: يبقي كلمة السر محمية بقوة مع استجابة أسرع من 200k
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 120_000)
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


def paid_status(company: dict) -> dict:
    """يحسب الوقت المتبقي قبل تجديد الاشتراك المدفوع (شهر من الدفع)."""
    paid_until = company.get("paid_until")
    if not company.get("payment_method") or not paid_until:
        return {"is_paid": False, "paid_until": None}
    ends_at = datetime.fromisoformat(paid_until)
    remaining = (ends_at - datetime.now(timezone.utc)).total_seconds()
    return {
        "is_paid": True,
        "paid_until": paid_until,
        "paid_seconds_remaining": max(0, int(remaining)),
    }


_firebase_keys_cache: dict = {"keys": {}, "expires_at": 0}


def _get_firebase_public_keys() -> dict:
    """يجلب شهادات Google العامة للتحقق من توقيع رموز Firebase، ويخزنها."""
    now = time.time()
    cached = _firebase_keys_cache
    if cached["keys"] and cached["expires_at"] > now:
        return cached["keys"]

    resp = requests.get(
        "https://www.googleapis.com/robot/v1/metadata/x509/"
        "securetoken@system.gserviceaccount.com",
        timeout=10,
    )
    resp.raise_for_status()
    keys = {
        kid: load_pem_x509_certificate(pem.encode()).public_key()
        for kid, pem in resp.json().items()
    }
    _firebase_keys_cache["keys"] = keys
    _firebase_keys_cache["expires_at"] = now + 3600
    return keys


def _verify_firebase_id_token(id_token: str) -> dict:
    """يتحقق من توقيع رمز Google ID Token مباشرة (لا يمكن تزويره)."""
    invalid = HTTPException(
        status_code=401, detail="رمز تسجيل الدخول عبر Google غير صالح."
    )
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError as exc:
        raise invalid from exc

    public_key = _get_firebase_public_keys().get(header.get("kid"))
    if not public_key:
        raise invalid

    try:
        payload = jwt.decode(
            id_token,
            public_key,
            algorithms=["RS256"],
            audience=FIREBASE_PROJECT_ID,
            issuer=f"https://securetoken.google.com/{FIREBASE_PROJECT_ID}",
        )
    except jwt.PyJWTError as exc:
        raise invalid from exc

    if not payload.get("email_verified") or not payload.get("email"):
        raise HTTPException(
            status_code=401,
            detail="يجب تأكيد البريد الإلكتروني عبر Google أولاً.",
        )
    return payload


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


def send_password_reset_email(
    to_email: str, company_name: str, token: str
) -> None:
    """يرسل رمز إعادة تعيين كلمة السر للعميل (صالح لمدة محدودة فقط)."""
    if not (SMTP_HOST and SMTP_USER and SMTP_PASSWORD and to_email):
        print(
            f"⚠️  SMTP غير مُعدّ — رمز إعادة التعيين لـ {to_email}: {token}"
        )
        return
    body = (
        f"مرحبًا {company_name}،\n\n"
        "وصلنا طلب لإعادة تعيين كلمة سر حسابكم.\n"
        f"رمز إعادة التعيين الخاص بكم هو:\n\n{token}\n\n"
        f"هذا الرمز صالح لمدة {RESET_TOKEN_TTL_MINUTES} دقيقة فقط من إرسال "
        "هذا البريد. إذا لم تطلبوا هذا التغيير، تجاهلوا هذه الرسالة."
    )
    msg = MIMEText(body, _charset="utf-8")
    msg["Subject"] = "رمز إعادة تعيين كلمة السر"
    msg["From"] = SMTP_FROM
    msg["To"] = to_email
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM, [to_email], msg.as_string())
    except OSError as exc:
        print(f"⚠️  فشل إرسال بريد إعادة التعيين: {exc}")


def send_admin_alert(subject: str, body: str) -> None:
    """ينبّه الأدمن فورًا عند فشل تفعيل دفعة (شبكة/قاعدة بيانات)، حتى لا
    تضيع فلوس عميل بصمت. يطبع بالسجلات دائمًا، ويرسل بريدًا أيضًا إذا
    كانت إعدادات SMTP موجودة. يسجَّل أيضًا في Sentry (إذا كان مفعّلًا) ليظهر
    مع باقي الأخطاء بنفس اللوحة."""
    print(f"🚨 تنبيه أدمن: {subject}\n{body}")
    if SENTRY_DSN:
        sentry_sdk.capture_message(f"🚨 {subject}\n{body}", level="error")
    if not (SMTP_HOST and SMTP_USER and SMTP_PASSWORD and ADMIN_ALERT_EMAIL):
        return
    msg = MIMEText(body, _charset="utf-8")
    msg["Subject"] = f"🚨 تنبيه NexusAI: {subject}"
    msg["From"] = SMTP_FROM
    msg["To"] = ADMIN_ALERT_EMAIL
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM, [ADMIN_ALERT_EMAIL], msg.as_string())
    except OSError as exc:
        print(f"⚠️  فشل إرسال بريد تنبيه الأدمن: {exc}")


# ---------------------------------------------------------------------------
# طبقة الذكاء الاصطناعي (Gemini أو Claude)
# ---------------------------------------------------------------------------
def _company_files(company: dict) -> list[dict]:
    """يرجع قائمة ملفات الشركة، مع التوافق مع الحسابات القديمة التي
    كانت تخزّن ملفًا واحدًا فقط بحقل file_data."""
    files = company.get("files")
    if files:
        return files
    legacy = company.get("file_data")
    if legacy:
        return [{"name": "ملف مرفوع", "content": legacy}]
    return []


# الحد الأقصى الإجمالي لعدد الأحرف من كل ملفات الشركة التي تُرسل للذكاء
# الاصطناعي بكل سؤال — لضبط تكلفة وسرعة الاستجابة بغض النظر عن عدد الملفات.
MAX_TOTAL_FILE_CHARS = 12000


def _build_system_prompt(company_name: str, files: list[dict]) -> str:
    prompt = (
        f'أنت مساعد ذكاء اصطناعي خاص بشركة "{company_name}". '
        "أجب على أسئلة العملاء بالاعتماد فقط على المعلومات المتوفرة "
        "لك عن الشركة أدناه إن وجدت، وإن لم تكن كافية فأجب بعمومية "
        "مهذبة توضح أنك بحاجة لمزيد من البيانات. "
        "أجب بنفس لغة سؤال المستخدم (عربي أو إنجليزي)."
    )
    if files:
        remaining = MAX_TOTAL_FILE_CHARS
        chunks = []
        for f in files:
            if remaining <= 0:
                break
            content = f.get("content", "")[:remaining]
            remaining -= len(content)
            chunks.append(f"[ملف: {f.get('name', 'بدون اسم')}]\n{content}")
        prompt += "\n\nبيانات الشركة المرفوعة:\n" + "\n\n".join(chunks)
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
    question: str, company_name: str, files: list[dict]
) -> str:
    """يولّد رد الذكاء الاصطناعي أو رد احتياطي إن لم يوجد مفتاح."""
    if not GEMINI_API_KEY and not ANTHROPIC_API_KEY:
        base = f'شكرًا لسؤالكم: "{question}". '
        if files:
            base += (
                "بناءً على المستندات المرفوعة، يمكنني مساعدتكم بمزيد من "
                "التفاصيل بمجرد تفعيل الذكاء الاصطناعي الحقيقي."
            )
        else:
            base += "لم يتم رفع أي مستندات بعد لهذه الشركة."
        return base

    system_prompt = _build_system_prompt(company_name, files)
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

    paid_until = (
        datetime.now(timezone.utc) + timedelta(days=PAID_PERIOD_DAYS)
    ).isoformat()

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
            paid_until=paid_until,
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
        "paid_until": paid_until,
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
    """يرفع ملف نصي ويضيفه لملفات حساب الشركة، ضمن حد باقتها."""
    company = get_company(x_api_key)
    content_bytes = await file.read()
    if len(content_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail="الملف كبير جدًا (الحد الأقصى 2MB).",
        )

    plan = PLANS.get(company["plan"], PLANS["basic"])
    max_files = plan["max_files"]
    files = _company_files(company)
    if max_files is not None and len(files) >= max_files:
        raise HTTPException(
            status_code=403,
            detail=(
                f"وصلت للحد الأقصى ({max_files} ملف) لباقتكم. "
                "احذف ملفًا قديمًا أو رقّوا باقتكم لرفع المزيد."
            ),
        )

    files.append(
        {
            "name": file.filename or "ملف بدون اسم",
            "content": content_bytes.decode("utf-8", errors="ignore"),
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    company["files"] = files
    company["file_data"] = ""  # الحقل القديم لم يعد يُستخدم كمصدر للبيانات
    _save_db()
    return {
        "status": "success",
        "message": "تم رفع الملف وإضافته لملفات شركتكم بنجاح.",
        "files": [f["name"] for f in files],
        "files_count": len(files),
        "max_files": max_files,
    }


@app.get("/enterprise/files")
async def list_company_files(x_api_key: OptionalHeader = None):
    """يرجع قائمة الملفات المرفوعة لحساب الشركة وحد باقتها."""
    company = get_company(x_api_key)
    plan = PLANS.get(company["plan"], PLANS["basic"])
    files = _company_files(company)
    return {
        "files": [
            {"name": f["name"], "uploaded_at": f.get("uploaded_at")}
            for f in files
        ],
        "files_count": len(files),
        "max_files": plan["max_files"],
    }


@app.delete("/enterprise/files/{file_name}")
async def delete_company_file(
    file_name: str, x_api_key: OptionalHeader = None
):
    """يحذف ملفًا واحدًا بالاسم من حساب الشركة، لتحرير مكان ضمن الحد."""
    company = get_company(x_api_key)
    files = _company_files(company)
    remaining = [f for f in files if f["name"] != file_name]
    if len(remaining) == len(files):
        raise HTTPException(status_code=404, detail="الملف غير موجود.")
    company["files"] = remaining
    company["file_data"] = ""
    _save_db()
    return {"status": "success", "files_count": len(remaining)}


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
        "has_uploaded_file": bool(_company_files(company)),
        "files_count": len(_company_files(company)),
        "max_files": plan["max_files"],
        "trial_expired": is_trial_expired(company),
        **trial_status(company),
        **paid_status(company),
    }


# ---------------------------------------------------------------------------
# 6) نقطة الأسئلة
# ---------------------------------------------------------------------------
@app.post("/enterprise/ask")
@limiter.limit("20/minute")
async def ask_ai_assistant(
    request: Request, payload: QuestionRequest, x_api_key: OptionalHeader = None
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
        _company_files(company),
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
@limiter.limit("5/hour")
async def signup(request: Request, payload: SignupRequest):
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
@limiter.limit("10/minute")
async def login(request: Request, payload: LoginRequest):
    """يتحقق من الإيميل وكلمة السر، ويرجع مفتاح الحساب."""
    api_key, company = _find_company_by_email(payload.email)
    wrong_credentials = HTTPException(
        status_code=401, detail="البريد الإلكتروني أو كلمة السر غير صحيحة."
    )
    if not company or not company.get("password_hash"):
        if company and company.get("oauth_provider"):
            raise HTTPException(
                status_code=401,
                detail=(
                    "هذا الحساب مسجّل عبر Google. "
                    "استخدم زر المتابعة عبر Google."
                ),
            )
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
        **paid_status(company),
    }


class OAuthLoginRequest(BaseModel):
    """جسم طلب تسجيل الدخول عبر Google."""

    id_token: str


@app.post("/enterprise/oauth-login")
@limiter.limit("10/minute")
async def oauth_login(request: Request, payload: OAuthLoginRequest):
    """يتحقق من رمز Google الحقيقي، ويسجل الدخول أو ينشئ تجربة جديدة."""
    if not FIREBASE_PROJECT_ID:
        raise HTTPException(
            status_code=500, detail="تسجيل الدخول عبر Google غير مُفعّل."
        )

    claims = await run_in_threadpool(
        _verify_firebase_id_token, payload.id_token
    )
    email = claims["email"]
    name = claims.get("name") or email.split("@")[0]

    api_key, company = _find_company_by_email(email)
    if company:
        return {
            "status": "success",
            "company": company["name"],
            "plan": company["plan"],
            "api_key": api_key,
            **trial_status(company),
            **paid_status(company),
        }

    trial_ends_at = (
        datetime.now(timezone.utc) + timedelta(days=TRIAL_DAYS)
    ).isoformat()
    new_key = f"trial_key_{uuid.uuid4().hex}"
    DB_COMPANIES[new_key] = {
        "name": name,
        "plan": "pro",
        "email": email,
        "password_hash": None,
        "password_salt": None,
        "oauth_provider": "google",
        "file_data": "",
        "usage": {},
        "created_at": datetime.now(timezone.utc).isoformat(),
        "active": True,
        "trial": True,
        "trial_ends_at": trial_ends_at,
        "payment_method": None,
    }
    _save_db()
    await run_in_threadpool(send_api_key_email, email, name, "pro", new_key)
    return {
        "status": "success",
        "company": name,
        "plan": "pro",
        "api_key": new_key,
        "is_trial": True,
        "trial_ends_at": trial_ends_at,
        "seconds_remaining": TRIAL_DAYS * 86400,
    }


# ---------------------------------------------------------------------------
# استرجاع كلمة السر — رمز حقيقي عشوائي صالح لمدة محدودة، يُرسل بالبريد فقط
# ---------------------------------------------------------------------------
class ForgotPasswordRequest(BaseModel):
    """جسم طلب نسيت كلمة السر."""

    email: str


class ResetPasswordRequest(BaseModel):
    """جسم طلب تعيين كلمة سر جديدة بالرمز المُرسل."""

    token: str
    new_password: str


_GENERIC_RESET_RESPONSE = {
    "status": "success",
    "message": (
        "إذا كان هذا البريد مسجّلاً لدينا بكلمة سر، "
        "أرسلنا رمز إعادة التعيين إليه."
    ),
}


@app.post("/enterprise/forgot-password")
@limiter.limit("5/hour")
async def forgot_password(request: Request, payload: ForgotPasswordRequest):
    """يرسل رمز إعادة تعيين حقيقي، بدون الكشف عن وجود البريد أو عدمه."""
    _, company = _find_company_by_email(payload.email)
    if not company or not company.get("password_hash"):
        # لا نكشف إن كان الحساب غير موجود أو مسجّلاً عبر Google فقط
        return _GENERIC_RESET_RESPONSE

    token = secrets.token_urlsafe(32)
    company["reset_token_hash"] = hashlib.sha256(token.encode()).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(
        minutes=RESET_TOKEN_TTL_MINUTES
    )
    company["reset_token_expires_at"] = expires_at.isoformat()
    _save_db()

    await run_in_threadpool(
        send_password_reset_email, payload.email, company["name"], token
    )
    return _GENERIC_RESET_RESPONSE


@app.post("/enterprise/reset-password")
@limiter.limit("10/hour")
async def reset_password(request: Request, payload: ResetPasswordRequest):
    """يستبدل كلمة السر بعد التحقق من رمز حقيقي غير منتهٍ."""
    if len(payload.new_password) < 6:
        raise HTTPException(
            status_code=400,
            detail="كلمة السر يجب أن تكون 6 أحرف على الأقل.",
        )

    token_hash = hashlib.sha256(payload.token.encode()).hexdigest()
    for company in DB_COMPANIES.values():
        if company.get("reset_token_hash") != token_hash:
            continue

        expires_at = company.get("reset_token_expires_at")
        expired = not expires_at or datetime.now(
            timezone.utc
        ) > datetime.fromisoformat(expires_at)
        if expired:
            raise HTTPException(
                status_code=400,
                detail="انتهت صلاحية رمز إعادة التعيين. اطلب رمزًا جديدًا.",
            )

        password_hash, password_salt = _hash_password(payload.new_password)
        company["password_hash"] = password_hash
        company["password_salt"] = password_salt
        company.pop("reset_token_hash", None)
        company.pop("reset_token_expires_at", None)
        _save_db()
        return {
            "status": "success",
            "message": "تم تغيير كلمة السر بنجاح. سجّل دخولك الآن.",
        }

    raise HTTPException(
        status_code=400,
        detail="رمز إعادة التعيين غير صالح أو تم استخدامه مسبقًا.",
    )


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
    paid_until = (
        datetime.now(timezone.utc) + timedelta(days=PAID_PERIOD_DAYS)
    ).isoformat()

    existing_key, existing = _find_company_by_email(payer_email)
    if existing:
        existing.update(
            plan=plan,
            trial=False,
            payment_method="paypal",
            paypal_reference=reference,
            verified=True,
            active=True,
            paid_until=paid_until,
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
        "paid_until": paid_until,
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
        # غالبًا انقطاع إنترنت/شبكة لحظة الدفع. PayPal يعيد المحاولة
        # تلقائيًا لاحقًا، لكن ننبّه الأدمن فورًا كشبكة أمان إضافية.
        await run_in_threadpool(
            send_admin_alert,
            "فشل الاتصال بـ PayPal أثناء التحقق من دفعة",
            (
                f"تعذر الوصول لسيرفرات PayPal للتحقق من إشعار دفع "
                f"(خطأ: {exc}).\n"
                "PayPal سيعيد إرسال الإشعار تلقائيًا، لكن إذا لم يصلك "
                "بريد تفعيل للعميل خلال ساعات، راجع لوحة PayPal يدويًا "
                "وفعّل الحساب عبر /admin/companies إذا لزم."
            ),
        )
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

    try:
        await _activate_paypal_payment(payer_email, plan, reference)
    except (psycopg2.Error, OSError, KeyError) as exc:
        # دفعة حقيقية استُلمت ووُثّق تحققها من PayPal، لكن التفعيل نفسه
        # فشل (مثلاً قاعدة البيانات مش متاحة لحظة الدفع). ما نخسر العميل:
        # ننبّه الأدمن فورًا بكل التفاصيل اللازمة للتفعيل اليدوي، ونرجع
        # 500 حتى تعيد PayPal إرسال الإشعار تلقائيًا من جهتها كذلك.
        await run_in_threadpool(
            send_admin_alert,
            "دفعة PayPal حقيقية وصلت ولكن التفعيل فشل!",
            (
                "⚠️ عميل دفع فعليًا وتحقق PayPal من الدفعة بنجاح، لكن "
                f"تفعيل الحساب فشل تقنيًا (خطأ: {exc}).\n\n"
                f"البريد: {payer_email}\n"
                f"الباقة: {plan}\n"
                f"مبلغ الدفعة: {amount}\n"
                f"مرجع PayPal: {reference}\n\n"
                "فعّل الحساب يدويًا بأسرع وقت حتى لا يخسر العميل مفتاحه، "
                "أو انتظر — PayPal سيعيد إرسال الإشعار تلقائيًا."
            ),
        )
        raise HTTPException(
            status_code=500, detail="فشل تفعيل الحساب، سيُعاد المحاولة."
        ) from exc

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


if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
