from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from typing import Optional, List
import uuid, httpx, os, smtplib, random, string
from email.mime.text import MIMEText
from datetime import datetime
from dotenv import load_dotenv
# Add alongside existing imports
import re
import requests
from bs4 import BeautifulSoup

load_dotenv()

app = FastAPI(title="E-Commerce Builder API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── In-memory DB ────────────────────────────────────────────────
stores   = {}
products = {}
orders   = {}
reviews  = {}

# Zoho OAuth tokens keyed by store_id
zoho_connections = {}

FRONTEND_URL       = os.getenv("FRONTEND_URL", "http://localhost:3000")

# ─── Cashfree KYC credentials ────────────────────────────────────
CASHFREE_CLIENT_ID     = os.getenv("CASHFREE_CLIENT_ID")
CASHFREE_CLIENT_SECRET = os.getenv("CASHFREE_CLIENT_SECRET")
CASHFREE_BASE_URL      = os.getenv("CASHFREE_BASE_URL", "https://api.cashfree.com/verification")

# ─── SMTP settings (for OTP emails) ─────────────────────────────
SMTP_HOST     = os.getenv("SMTP_HOST", "mail.privateemail.com")
SMTP_PORT     = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER     = os.getenv("EMAIL_USER")
SMTP_PASSWORD = os.getenv("EMAIL_PASSWORD")
SMTP_FROM     = os.getenv("SMTP_FROM", SMTP_USER)

# Zoho env vars
ZOHO_CLIENT_ID     = os.getenv("ZOHO_CLIENT_ID", "")
ZOHO_CLIENT_SECRET = os.getenv("ZOHO_CLIENT_SECRET", "")
ZOHO_REDIRECT_URI  = os.getenv("ZOHO_REDIRECT_URI", f"{FRONTEND_URL}/api/zoho/callback")
ZOHO_ACCOUNTS_URL  = os.getenv("ZOHO_ACCOUNTS_URL", "https://accounts.zoho.in")
ZOHO_PAYMENTS_URL  = os.getenv("ZOHO_PAYMENTS_URL", "https://payments.zoho.in/api/v1")
ZOHO_SCOPES        = "ZohoPayments.fullaccess.all"

# ─── Models ──────────────────────────────────────────────────────
class Store(BaseModel):
    name: str
    description: Optional[str] = ""
    owner: Optional[str] = ""

class StoreUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    owner: Optional[str] = None
    theme: Optional[dict] = None
    hero_text: Optional[str] = None
    hero_subtitle: Optional[str] = None
    banner_color: Optional[str] = None
    page_config: Optional[str] = None

class Product(BaseModel):
    store_id: str
    name: str
    description: Optional[str] = ""
    price: float
    stock: int = 0
    category: Optional[str] = ""
    image_url: Optional[str] = ""
    images: Optional[List[str]] = []
    hsn_code: Optional[str] = ""

class ProductUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    price: Optional[float] = None
    stock: Optional[int] = None
    category: Optional[str] = None
    image_url: Optional[str] = None
    images: Optional[List[str]] = None
    hsn_code: Optional[str] = None

class OrderItem(BaseModel):
    product_id: str
    quantity: int

class Order(BaseModel):
    store_id: str
    customer_name: str
    customer_email: str
    items: List[OrderItem]
    address: Optional[str] = ""

class Review(BaseModel):
    store_id: str
    product_id: Optional[str] = None
    customer_name: str
    rating: int
    comment: Optional[str] = ""

class ZohoPaymentSessionRequest(BaseModel):
    store_id: str
    customer_name: str
    customer_email: str
    customer_phone: Optional[str] = ""
    address: Optional[str] = ""
    items: List[OrderItem]

class ZohoVerifyRequest(BaseModel):
    store_id: str
    payments_session_id: str
    payment_id: str
    customer_name: str
    customer_email: str
    address: Optional[str] = ""
    items: List[OrderItem]

# ─── KYC Models ──────────────────────────────────────────────────
class GSTINRequest(BaseModel):
    gstin: str                  # frontend sends lowercase "gstin"

    model_config = {"extra": "allow"}   # ignore any extra fields silently

class CINRequest(BaseModel):
    cin: str
    company_name: Optional[str] = ""

    model_config = {"extra": "allow"}

class SendOTPRequest(BaseModel):
    email: str
    otp: str

    model_config = {"extra": "allow"}

# ─── Validation error handler → readable JSON instead of raw 422 ──
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc):
    errors = exc.errors()
    detail = "; ".join(
        f"{' → '.join(str(loc) for loc in e['loc'])}: {e['msg']}"
        for e in errors
    )
    return JSONResponse(
        status_code=422,
        content={"detail": detail, "raw_errors": errors},
    )

# ═══════════════════════════════════════════════════════════════════
#  KYC ENDPOINTS
# ═══════════════════════════════════════════════════════════════════

@app.post("/kyc/gstin")
async def verify_gstin(req: GSTINRequest):
    gstin_val = req.gstin.strip().upper()

    if len(gstin_val) != 15:
        raise HTTPException(status_code=400, detail="GSTIN must be exactly 15 characters")

    # Dev / mock mode
    if not CASHFREE_CLIENT_ID or not CASHFREE_CLIENT_SECRET:
        print(f"[DEV] Mock GSTIN lookup for: {gstin_val}")
        return {"gstin": gstin_val, "business_name": "SAMPLE BUSINESS PVT LTD", "status": "VALID"}

    # Live Cashfree call — correct headers per Cashfree docs
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.post(
                f"{CASHFREE_BASE_URL}/gstin",
                json={"GSTIN": gstin_val, "business_name": ""},
                headers={
                    "x-client-id": CASHFREE_CLIENT_ID,
                    "x-client-secret": CASHFREE_CLIENT_SECRET,
                    "x-api-version": "2023-08-01",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            data = res.json()
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Cashfree unreachable: {str(e)}")

    print(f"[Cashfree GSTIN] status={res.status_code} body={data}")

    if res.status_code != 200:
        msg = data.get("message") or data.get("error") or f"Cashfree error {res.status_code}"
        raise HTTPException(status_code=400, detail=msg)

    # Response shape varies — check all known nested locations
    gstin_data = data.get("gstin_data") or data.get("data") or data
    business_name = (
        gstin_data.get("legal_name_of_business")
        or gstin_data.get("business_name")
        or gstin_data.get("legalNameOfBusiness")
        or gstin_data.get("legal_name")
        or gstin_data.get("trade_name")
        or data.get("legal_name_of_business")
        or data.get("business_name")
        or ""
    )

    cf_status = str(gstin_data.get("status") or data.get("status") or "").upper()
    if cf_status == "INVALID":
        raise HTTPException(status_code=400, detail="GSTIN is invalid or not registered")

    return {"gstin": gstin_val, "business_name": business_name, "status": "VALID"}


def _decode_cloudflare_email(hex_string: str) -> str:
    try:
        xor_key = int(hex_string[:2], 16)
        return "".join(
            chr(int(hex_string[i:i+2], 16) ^ xor_key)
            for i in range(2, len(hex_string), 2)
        )
    except Exception:
        return ""


def _scrape_cin(company_name: str, cin: str) -> dict:
    formatted_name = (
        company_name.strip().upper()
        .replace(" ", "-").replace(".", "").replace(",", "")
    )
    clean_cin = cin.strip().upper()
    url = f"https://www.zaubacorp.com/company/{formatted_name}/{clean_cin}"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }

    resp = requests.get(url, headers=headers, timeout=15)
    if resp.status_code != 200:
        return {}

    soup = BeautifulSoup(resp.text, "html.parser")

    # Decode Cloudflare-obfuscated email
    email = ""
    cf_tag = soup.find(class_="__cf_email__")
    if cf_tag and cf_tag.get("data-cfemail"):
        email = _decode_cloudflare_email(cf_tag["data-cfemail"])
    else:
        match = re.search(r'data-cfemail="([a-f0-9]+)"', resp.text)
        if match:
            email = _decode_cloudflare_email(match.group(1))

    # Parse directors table
    directors = []
    for table in soup.find_all("table"):
        txt = table.text
        if "DIN" in txt and "Director Name" in txt and "Designation" in txt:
            for row in table.find_all("tr")[1:]:
                cols = row.find_all("td")
                if len(cols) >= 2:
                    name = cols[1].text.strip()
                    if name and "Unlisted" not in name:
                        directors.append(name)
            break

    # Detect mismatch: zaubacorp page title / h1 contains the real company name
    page_company = ""
    h1 = soup.find("h1")
    if h1:
        page_company = h1.text.strip()

    return {
        "email": email,
        "directors": directors,
        "page_company": page_company,
    }


@app.post("/kyc/cin")
def verify_cin(req: CINRequest):
    cin_val = req.cin.strip().upper()

    # company_name is passed from the frontend (auto-filled from GSTIN business name)
    company_name = getattr(req, "company_name", "").strip()

    try:
        data = _scrape_cin(company_name, cin_val)
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Could not reach zaubacorp: {str(e)}")

    if not data:
        raise HTTPException(status_code=400, detail="CIN not found or invalid.")

    # If we got a page company name back, do a loose mismatch check
    page_company = data.get("page_company", "")
    if company_name and page_company:
        def _norm(s):
            return re.sub(r"[^a-z0-9]", "", s.lower())
        # Accept if either name contains the other (handles abbreviations)
        if _norm(company_name) not in _norm(page_company) and _norm(page_company) not in _norm(company_name):
            raise HTTPException(
                status_code=400,
                detail=f"Business Name and CIN do not match. CIN belongs to: {page_company}"
            )

    return {
        "status": "VALID",
        "cin": cin_val,
        "company_name": page_company or company_name,
        "cin_status": "ACTIVE",
        "email": data.get("email", ""),
        "directors": data.get("directors", []),
    }


@app.post("/kyc/send-otp")
def send_otp_email(req: SendOTPRequest):
    """
    Sends OTP to the email via SMTP.
    OTP validation is client-side via localStorage — no DB required.
    Prints OTP to console in dev mode (no SMTP credentials).
    """
    if not SMTP_USER or not SMTP_PASSWORD:
        print(f"[DEV] OTP for {req.email}: {req.otp}")
        return {"sent": True, "dev_mode": True}

    try:
        body = (
            f"Hello,\n\n"
            f"Your verification OTP for store creation is:\n\n"
            f"  {req.otp}\n\n"
            f"This code expires in 10 minutes. Do not share it with anyone.\n\n"
            f"— E-Commerce Builder Team"
        )
        msg = MIMEText(body)
        msg["Subject"] = "Your Store Verification OTP"
        msg["From"] = SMTP_FROM
        msg["To"] = req.email

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM, [req.email], msg.as_string())

        return {"sent": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to send OTP email: {str(e)}")


# ─── Store Endpoints ─────────────────────────────────────────────
@app.post("/stores")
def create_store(store: Store):
    store_id = str(uuid.uuid4())[:8]
    stores[store_id] = {
        "id": store_id,
        "name": store.name,
        "description": store.description,
        "owner": store.owner,
        "theme": {},
        "hero_text": f"Welcome to {store.name}",
        "hero_subtitle": store.description,
        "banner_color": "#6366f1",
        "created_at": datetime.now().isoformat(),
    }
    return stores[store_id]

@app.get("/stores")
def list_stores():
    return list(stores.values())

@app.get("/stores/{store_id}")
def get_store(store_id: str):
    if store_id not in stores:
        raise HTTPException(status_code=404, detail="Store not found")
    return stores[store_id]

@app.put("/stores/{store_id}")
def update_store(store_id: str, update: StoreUpdate):
    if store_id not in stores:
        raise HTTPException(status_code=404, detail="Store not found")
    for k, v in update.dict(exclude_none=True).items():
        stores[store_id][k] = v
    return stores[store_id]

@app.delete("/stores/{store_id}")
def delete_store(store_id: str):
    if store_id not in stores:
        raise HTTPException(status_code=404, detail="Store not found")
    del stores[store_id]
    return {"message": "Store deleted"}

# ─── Product Endpoints ────────────────────────────────────────────
@app.post("/products")
def create_product(product: Product):
    if product.store_id not in stores:
        raise HTTPException(status_code=404, detail="Store not found")
    product_id = str(uuid.uuid4())[:8]
    products[product_id] = {
        "id": product_id,
        **product.dict(),
        "created_at": datetime.now().isoformat(),
    }
    return products[product_id]

@app.get("/products")
def list_products(store_id: Optional[str] = None):
    all_products = list(products.values())
    if store_id:
        all_products = [p for p in all_products if p["store_id"] == store_id]
    return all_products

@app.get("/products/{product_id}")
def get_product(product_id: str):
    if product_id not in products:
        raise HTTPException(status_code=404, detail="Product not found")
    return products[product_id]

@app.put("/products/{product_id}")
def update_product(product_id: str, update: ProductUpdate):
    if product_id not in products:
        raise HTTPException(status_code=404, detail="Product not found")
    for k, v in update.dict(exclude_none=True).items():
        products[product_id][k] = v
    return products[product_id]

@app.delete("/products/{product_id}")
def delete_product(product_id: str):
    if product_id not in products:
        raise HTTPException(status_code=404, detail="Product not found")
    del products[product_id]
    return {"message": "Product deleted"}

# ─── Order Endpoints ──────────────────────────────────────────────
@app.post("/orders")
def create_order(order: Order):
    if order.store_id not in stores:
        raise HTTPException(status_code=404, detail="Store not found")
    order_id = str(uuid.uuid4())[:8]
    total = 0
    line_items = []
    for item in order.items:
        if item.product_id not in products:
            raise HTTPException(status_code=404, detail=f"Product {item.product_id} not found")
        product = products[item.product_id]
        if product["stock"] < item.quantity:
            raise HTTPException(status_code=400, detail=f"Insufficient stock for {product['name']}")
        products[item.product_id]["stock"] -= item.quantity
        subtotal = product["price"] * item.quantity
        total += subtotal
        line_items.append({
            "product_id": item.product_id,
            "product_name": product["name"],
            "quantity": item.quantity,
            "unit_price": product["price"],
            "subtotal": subtotal,
        })
    orders[order_id] = {
        "id": order_id,
        "store_id": order.store_id,
        "customer_name": order.customer_name,
        "customer_email": order.customer_email,
        "address": order.address,
        "items": line_items,
        "total": total,
        "status": "confirmed",
        "created_at": datetime.now().isoformat(),
    }
    return orders[order_id]

@app.get("/orders")
def list_orders(store_id: Optional[str] = None):
    all_orders = list(orders.values())
    if store_id:
        all_orders = [o for o in all_orders if o["store_id"] == store_id]
    return all_orders

# ─── Review Endpoints ─────────────────────────────────────────────
@app.post("/reviewz")
def create_review(review: Review):
    if review.store_id not in stores:
        raise HTTPException(status_code=404, detail="Store not found")
    review_id = str(uuid.uuid4())[:8]
    reviews[review_id] = {
        "id": review_id,
        **review.dict(),
        "created_at": datetime.now().isoformat(),
    }
    return reviews[review_id]

@app.get("/reviewz")
def list_reviews(store_id: Optional[str] = None):
    all_reviews = list(reviews.values())
    if store_id:
        all_reviews = [r for r in all_reviews if r["store_id"] == store_id]
    return all_reviews

@app.get("/reviewz/stats/{store_id}")
def review_stats(store_id: str):
    store_reviews = [r for r in reviews.values() if r["store_id"] == store_id]
    if not store_reviews:
        return {"average_rating": 0, "total_reviews": 0, "rating_breakdown": {}}
    avg = sum(r["rating"] for r in store_reviews) / len(store_reviews)
    breakdown = {str(i): sum(1 for r in store_reviews if r["rating"] == i) for i in range(1, 6)}
    return {
        "average_rating": round(avg, 2),
        "total_reviews": len(store_reviews),
        "rating_breakdown": breakdown,
    }

# ─── Analytics ────────────────────────────────────────────────────
@app.get("/analytics/{store_id}")
def get_analytics(store_id: str):
    if store_id not in stores:
        raise HTTPException(status_code=404, detail="Store not found")
    store_orders   = [o for o in orders.values() if o["store_id"] == store_id]
    store_products = [p for p in products.values() if p["store_id"] == store_id]
    store_reviews  = [r for r in reviews.values() if r["store_id"] == store_id]
    total_revenue  = sum(o["total"] for o in store_orders)
    avg_rating     = (sum(r["rating"] for r in store_reviews) / len(store_reviews)) if store_reviews else 0
    return {
        "total_orders": len(store_orders),
        "total_revenue": round(total_revenue, 2),
        "total_products": len(store_products),
        "total_reviews": len(store_reviews),
        "average_rating": round(avg_rating, 2),
        "recent_orders": store_orders[-5:][::-1],
    }

# ═══════════════════════════════════════════════════════════════════
#  ZOHO PAYMENTS  ── OAuth + Payment Session + Verification
# ═══════════════════════════════════════════════════════════════════

@app.get("/zoho/connect")
def zoho_connect(store_id: str = Query(...)):
    if store_id not in stores:
        raise HTTPException(status_code=404, detail="Store not found")
    auth_url = (
        f"{ZOHO_ACCOUNTS_URL}/oauth/v2/auth"
        f"?response_type=code"
        f"&client_id={ZOHO_CLIENT_ID}"
        f"&scope={ZOHO_SCOPES}"
        f"&redirect_uri={ZOHO_REDIRECT_URI}"
        f"&access_type=offline"
        f"&state={store_id}"
        f"&prompt=consent"
    )
    return {"auth_url": auth_url}


@app.get("/zoho/callback")
async def zoho_callback(code: str = Query(...), state: str = Query(...)):
    store_id = state
    if store_id not in stores:
        return RedirectResponse(f"{FRONTEND_URL}/ecommerce/admin?store={store_id}&zoho_error=store_not_found")

    async with httpx.AsyncClient() as client:
        token_res = await client.post(
            f"{ZOHO_ACCOUNTS_URL}/oauth/v2/token",
            params={
                "code":          code,
                "client_id":     ZOHO_CLIENT_ID,
                "client_secret": ZOHO_CLIENT_SECRET,
                "redirect_uri":  ZOHO_REDIRECT_URI,
                "grant_type":    "authorization_code",
            },
        )
        token_data = token_res.json()

    access_token  = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")

    if not access_token:
        error = token_data.get("error", "oauth_failed")
        return RedirectResponse(f"{FRONTEND_URL}/ecommerce/admin?store={store_id}&zoho_error={error}")

    account_id  = None
    zoho_email  = None
    async with httpx.AsyncClient() as client:
        info_res = await client.get(
            f"{ZOHO_ACCOUNTS_URL}/oauth/v2/usersapi",
            headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
        )
        if info_res.status_code == 200:
            info = info_res.json()
            zoho_email = info.get("Email") or info.get("email")

    zoho_connections[store_id] = {
        "access_token":  access_token,
        "refresh_token": refresh_token,
        "account_id":    account_id,
        "zoho_email":    zoho_email,
        "connected_at":  datetime.now().isoformat(),
    }

    return RedirectResponse(f"{FRONTEND_URL}/ecommerce/admin?store={store_id}&zoho_connected=1")


async def _get_valid_access_token(store_id: str) -> str:
    conn = zoho_connections.get(store_id)
    if not conn:
        raise HTTPException(status_code=400, detail="Zoho Payments not connected for this store")

    async with httpx.AsyncClient() as client:
        res = await client.post(
            f"{ZOHO_ACCOUNTS_URL}/oauth/v2/token",
            params={
                "refresh_token": conn["refresh_token"],
                "client_id":     ZOHO_CLIENT_ID,
                "client_secret": ZOHO_CLIENT_SECRET,
                "grant_type":    "refresh_token",
            },
        )
        data = res.json()

    new_token = data.get("access_token")
    if not new_token:
        raise HTTPException(status_code=401, detail="Failed to refresh Zoho access token")

    zoho_connections[store_id]["access_token"] = new_token
    return new_token


@app.get("/zoho/connection/{store_id}")
def get_zoho_connection(store_id: str):
    conn = zoho_connections.get(store_id)
    if not conn:
        return {"connected": False}
    return {
        "connected":   True,
        "account_id":  conn.get("account_id"),
        "zoho_email":  conn.get("zoho_email"),
        "connected_at": conn.get("connected_at"),
    }


@app.put("/zoho/connection/{store_id}/account")
def set_zoho_account_id(store_id: str, payload: dict):
    if store_id not in zoho_connections:
        raise HTTPException(status_code=400, detail="Zoho not connected")
    zoho_connections[store_id]["account_id"] = payload.get("account_id")
    return {"ok": True}


@app.delete("/zoho/disconnect/{store_id}")
async def zoho_disconnect(store_id: str):
    conn = zoho_connections.pop(store_id, None)
    if conn and conn.get("refresh_token"):
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{ZOHO_ACCOUNTS_URL}/oauth/v2/token/revoke",
                params={"token": conn["refresh_token"]},
            )
    return {"disconnected": True}


@app.post("/zoho/payment-session")
async def create_zoho_payment_session(req: ZohoPaymentSessionRequest):
    conn = zoho_connections.get(req.store_id)
    if not conn:
        raise HTTPException(status_code=400, detail="Zoho Payments not connected for this store")
    if not conn.get("account_id"):
        raise HTTPException(status_code=400, detail="Zoho account_id not set. Please update it in Admin > Payments.")

    total = 0.0
    for item in req.items:
        p = products.get(item.product_id)
        if not p:
            raise HTTPException(status_code=404, detail=f"Product {item.product_id} not found")
        if p["stock"] < item.quantity:
            raise HTTPException(status_code=400, detail=f"Insufficient stock for {p['name']}")
        total += p["price"] * item.quantity

    access_token = await _get_valid_access_token(req.store_id)
    account_id   = conn["account_id"]

    item_names = []
    for item in req.items:
        p = products.get(item.product_id, {})
        item_names.append(f"{p.get('name','Item')} x{item.quantity}")
    description = ", ".join(item_names)[:500]

    payload = {
        "amount":      round(total, 2),
        "currency":    "INR",
        "description": description,
        "configurations": {
            "allowed_payment_methods": ["upi", "cards", "netbanking", "wallet"],
            "hosted_page_parameters": {
                "phone_country_code": "IN",
                "phone":  req.customer_phone or "",
                "name":   req.customer_name,
                "email":  req.customer_email,
                "description": description,
                "success_url": f"{FRONTEND_URL}/ecommerce/cart?store={req.store_id}&payment=success",
                "failure_url": f"{FRONTEND_URL}/ecommerce/cart?store={req.store_id}&payment=failed",
            },
        },
        "meta_data": [{"key": "store_id", "value": req.store_id}],
    }

    async with httpx.AsyncClient() as client:
        res = await client.post(
            f"{ZOHO_PAYMENTS_URL}/paymentsessions",
            params={"account_id": account_id},
            headers={
                "Authorization": f"Zoho-oauthtoken {access_token}",
                "Content-Type":  "application/json",
            },
            json=payload,
        )
        data = res.json()

    if res.status_code not in (200, 201) or data.get("code") != 0:
        raise HTTPException(status_code=502, detail=data.get("message", "Zoho payment session failed"))

    session = data["payments_session"]
    return {
        "payments_session_id": session["payments_session_id"],
        "amount":              session["amount"],
        "access_key":          session.get("access_key"),
        "account_id":          account_id,
        "total":               round(total, 2),
    }


@app.post("/zoho/verify-payment")
async def verify_zoho_payment(req: ZohoVerifyRequest):
    conn = zoho_connections.get(req.store_id)
    if not conn:
        raise HTTPException(status_code=400, detail="Zoho not connected")

    access_token = await _get_valid_access_token(req.store_id)
    account_id   = conn["account_id"]

    async with httpx.AsyncClient() as client:
        res = await client.get(
            f"{ZOHO_PAYMENTS_URL}/paymentsessions/{req.payments_session_id}",
            params={"account_id": account_id},
            headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
        )
        data = res.json()

    if res.status_code != 200 or data.get("code") != 0:
        raise HTTPException(status_code=502, detail="Could not verify payment with Zoho")

    session = data.get("payments_session", {})
    status  = session.get("status", "")

    if status != "succeeded":
        raise HTTPException(status_code=400, detail=f"Payment not successful (status: {status})")

    order_id   = str(uuid.uuid4())[:8]
    total      = 0.0
    line_items = []
    for item in req.items:
        p = products.get(item.product_id)
        if not p:
            continue
        subtotal = p["price"] * item.quantity
        total   += subtotal
        products[item.product_id]["stock"] = max(0, p["stock"] - item.quantity)
        line_items.append({
            "product_id":   item.product_id,
            "product_name": p["name"],
            "quantity":     item.quantity,
            "unit_price":   p["price"],
            "subtotal":     subtotal,
        })

    orders[order_id] = {
        "id":             order_id,
        "store_id":       req.store_id,
        "customer_name":  req.customer_name,
        "customer_email": req.customer_email,
        "address":        req.address,
        "items":          line_items,
        "total":          round(total, 2),
        "status":         "confirmed",
        "payment_method": "zoho_payments",
        "zoho_session_id": req.payments_session_id,
        "zoho_payment_id": req.payment_id,
        "created_at":     datetime.now().isoformat(),
    }

    return orders[order_id]


# ═══════════════════════════════════════════════════════════════════
#  RAZORPAY  ── Key storage + Order creation
# ═══════════════════════════════════════════════════════════════════

razorpay_connections = {}

class RazorpayConnection(BaseModel):
    key_id: str

class RazorpayOrderRequest(BaseModel):
    store_id: str
    payment_id: str
    customer_name: str
    customer_email: str
    customer_phone: Optional[str] = ""
    address: Optional[str] = ""
    items: List[OrderItem]

@app.get("/razorpay/connection/{store_id}")
def get_razorpay_connection(store_id: str):
    conn = razorpay_connections.get(store_id)
    if not conn:
        return {"connected": False}
    return {"connected": True, "key_id": conn["key_id"]}

@app.put("/razorpay/connection/{store_id}")
def upsert_razorpay_connection(store_id: str, payload: RazorpayConnection):
    razorpay_connections[store_id] = {
        "key_id":       payload.key_id,
        "connected_at": datetime.now().isoformat(),
    }
    return {"connected": True, "key_id": payload.key_id}

@app.delete("/razorpay/connection/{store_id}")
def delete_razorpay_connection(store_id: str):
    razorpay_connections.pop(store_id, None)
    return {"disconnected": True}

@app.post("/orders/razorpay")
def create_razorpay_order(req: RazorpayOrderRequest):
    if req.store_id not in stores:
        raise HTTPException(status_code=404, detail="Store not found")
    if req.store_id not in razorpay_connections:
        raise HTTPException(status_code=400, detail="Razorpay not connected for this store")

    order_id   = str(uuid.uuid4())[:8]
    total      = 0.0
    line_items = []
    for item in req.items:
        p = products.get(item.product_id)
        if not p:
            raise HTTPException(status_code=404, detail=f"Product {item.product_id} not found")
        if p["stock"] < item.quantity:
            raise HTTPException(status_code=400, detail=f"Insufficient stock for {p['name']}")
        subtotal = p["price"] * item.quantity
        total   += subtotal
        products[item.product_id]["stock"] -= item.quantity
        line_items.append({
            "product_id":   item.product_id,
            "product_name": p["name"],
            "quantity":     item.quantity,
            "unit_price":   p["price"],
            "subtotal":     subtotal,
        })

    orders[order_id] = {
        "id":             order_id,
        "store_id":       req.store_id,
        "customer_name":  req.customer_name,
        "customer_email": req.customer_email,
        "address":        req.address,
        "items":          line_items,
        "total":          round(total, 2),
        "status":         "confirmed",
        "payment_method": "razorpay",
        "razorpay_payment_id": req.payment_id,
        "created_at":     datetime.now().isoformat(),
    }
    return orders[order_id]
