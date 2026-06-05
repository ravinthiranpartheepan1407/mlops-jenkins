"""
SocialRamp - Social Media Aggregator Backend
FastAPI + Real OAuth2 integrations

OAuth flows:
  - Facebook + Messenger  → single Meta OAuth  → /api/auth/facebook/connect
  - Instagram             → separate Meta OAuth → /api/auth/instagram/connect
  - WhatsApp              → separate Meta OAuth → /api/auth/whatsapp/connect
  - Gmail                 → Google OAuth        → /api/auth/gmail/connect

Meta App Setup:
  Use case 1: "Messenger" + "Manage everything on your Page"
    pages_messaging, pages_show_list, pages_manage_metadata,
    pages_manage_posts, pages_read_engagement, pages_read_user_content,
    pages_manage_engagement

  Use case 2: "Instagram — Manage messaging and content"
    instagram_basic, instagram_manage_messages,
    instagram_manage_comments, pages_show_list,
    pages_read_engagement, pages_manage_metadata

  Use case 3: "WhatsApp Business"
    whatsapp_business_messaging, whatsapp_business_management

In Meta App dashboard:
  Add OAuth redirects for all three callbacks:
    {BACKEND_URL}/api/auth/facebook/callback
    {BACKEND_URL}/api/auth/instagram/callback
    {BACKEND_URL}/api/auth/whatsapp/callback
"""

import os
import json
import base64
import uuid
import httpx
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from dotenv import load_dotenv

from fastapi import FastAPI, HTTPException, Depends, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse
from pydantic import BaseModel

from apscheduler.schedulers.background import BackgroundScheduler

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
import google.auth.transport.requests
import requests as pyrequests

from jose import JWTError, jwt

load_dotenv()
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = os.getenv("OAUTHLIB_INSECURE_TRANSPORT", "0")

app = FastAPI(title="SocialRamp API", version="4.0.0")

# ─── CORS ─────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.getenv("FRONTEND_URL", "http://localhost:3000")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
META_APP_ID                  = os.getenv("FACEBOOK_APP_ID")
META_APP_SECRET              = os.getenv("FACEBOOK_APP_SECRET")
GOOGLE_CLIENT_ID             = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET         = os.getenv("GOOGLE_CLIENT_SECRET")
WHATSAPP_PHONE_NUMBER_ID     = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
WHATSAPP_BUSINESS_ACCOUNT_ID = os.getenv("WHATSAPP_BUSINESS_ACCOUNT_ID")
FRONTEND_URL                 = os.getenv("FRONTEND_URL", "http://localhost:3000")
SECRET_KEY                   = os.getenv("SECRET_KEY", "change-this-secret")
BACKEND_URL                  = os.getenv("BACKEND_URL", "http://localhost:8000")
WHATSAPP_VERIFY_TOKEN        = os.getenv("WHATSAPP_VERIFY_TOKEN", "socialramp_verify_token")

META_GRAPH_URL   = "https://graph.facebook.com/v19.0"
WHATSAPP_API_URL = f"https://graph.facebook.com/v19.0/{WHATSAPP_PHONE_NUMBER_ID}"

FB_APP_ID     = os.getenv("FACEBOOK_APP_ID")
FB_APP_SECRET = os.getenv("FACEBOOK_APP_SECRET")
FB_REDIRECT   = "http://localhost:8000/automation/auth/facebook/callback"

users_store: dict = {}
posts_store: list = []
scheduler = BackgroundScheduler()
scheduler.start()

# In-memory stores (replace with DB in production)
token_store: Dict[str, Dict[str, Any]] = {}
oauth_state_store: Dict[str, str] = {}
whatsapp_webhook_store: Dict[str, List] = {"messages": []}

# ─── In-memory DB ────────────────────────────────────────────────
stores = {}
products = {}
orders = {}
reviewz = {}
carts = {}


# ─── JWT UTILS ────────────────────────────────────────────────────────────────
def create_session_token(session_id: str) -> str:
    expire = datetime.utcnow() + timedelta(days=7)
    return jwt.encode({"sub": session_id, "exp": expire}, SECRET_KEY, algorithm="HS256")


def get_session_id(request: Request) -> str:
    token = request.cookies.get("session_token") or request.headers.get("X-Session-Token")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        return payload["sub"]
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid session")


def get_platform_token(session_id: str, platform: str) -> Optional[Dict]:
    return token_store.get(session_id, {}).get(platform)


def set_platform_token(session_id: str, platform: str, data: Dict):
    if session_id not in token_store:
        token_store[session_id] = {}
    token_store[session_id][platform] = data


def _normalise_ts(ts):
    if not ts:
        return ""
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).isoformat()
    except Exception:
        return str(ts)


# ─── MODELS ───────────────────────────────────────────────────────────────────
class SendMessageRequest(BaseModel):
    platform: str
    recipient_id: str
    message: str


class ReplyDMRequest(BaseModel):
    platform: str
    thread_id: str
    recipient_id: str
    message: str


class ReplyCommentRequest(BaseModel):
    platform: str
    comment_id: str
    post_id: str
    message: str


class PostCommentRequest(BaseModel):
    platform: str
    post_id: str
    message: str


class SendEmailRequest(BaseModel):
    to: str
    subject: str
    body: str
    thread_id: Optional[str] = None


class PostCreate(BaseModel):
    message: str
    page_id: str
    session_token: str
    scheduled_time: Optional[str] = None
    post_type: str = "post"
    image_url: Optional[str] = None


# ─── ECOMMERCE Models ──────────────────────────────────────────────────────
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
    page_config: Optional[str] = None  # ← add this


class Product(BaseModel):
    store_id: str
    name: str
    description: Optional[str] = ""
    price: float
    stock: int = 0
    category: Optional[str] = ""
    image_url: Optional[str] = ""
    images: Optional[List[str]] = []  # ← add this


class ProductUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    price: Optional[float] = None
    stock: Optional[int] = None
    category: Optional[str] = None
    image_url: Optional[str] = None
    images: Optional[List[str]] = None  # ← add this

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

class CartItem(BaseModel):
    product_id: str
    quantity: int


# ─── AUTOMATION ──────────────────────────────────────────────────────────────────


async def fb_get(path: str, token: str, params: dict = {}):
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"https://graph.facebook.com/v19.0/{path}",
            params={"access_token": token, **params}
        )
        return r.json()


async def fb_post_req(path: str, token: str, data: dict = {}):
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"https://graph.facebook.com/v19.0/{path}",
            data={"access_token": token, **data}
        )
        return r.json()


@app.get("/automation/auth/facebook")
def facebook_login():
    scopes = "pages_manage_posts,pages_read_engagement,pages_show_list"
    url = (
        f"https://www.facebook.com/v19.0/dialog/oauth"
        f"?client_id={FB_APP_ID}"
        f"&redirect_uri={FB_REDIRECT}"
        f"&scope={scopes}"
        f"&response_type=code"
    )
    return RedirectResponse(url)


@app.get("/automation/auth/facebook/callback")
async def facebook_callback(code: str):
    async with httpx.AsyncClient() as client:
        token_res = await client.get(
            "https://graph.facebook.com/v19.0/oauth/access_token",
            params={
                "client_id": FB_APP_ID,
                "client_secret": FB_APP_SECRET,
                "redirect_uri": FB_REDIRECT,
                "code": code,
            }
        )
        token_data = token_res.json()
        access_token = token_data.get("access_token")
        if not access_token:
            raise HTTPException(400, "Failed to get access token")

        profile = await fb_get("me", access_token, {"fields": "id,name,email,picture"})
        pages_data = await fb_get("me/accounts", access_token)
        pages = pages_data.get("data", [])

        session_token = str(uuid.uuid4())
        users_store[session_token] = {
            "user": profile,
            "access_token": access_token,
            "pages": pages,
            "connected_at": datetime.utcnow().isoformat(),
        }

    return RedirectResponse(
        f"{FRONTEND_URL}/dashboard?session={session_token}&name={profile.get('name', '')}"
    )


@app.get("/automation/auth/me")
def get_me(session_token: str):
    user_data = users_store.get(session_token)
    if not user_data:
        raise HTTPException(401, "Invalid or expired session")
    return {
        "user": user_data["user"],
        "pages": user_data["pages"],
        "connected_at": user_data["connected_at"],
    }


@app.post("/automation/auth/logout")
def logout(session_token: str):
    users_store.pop(session_token, None)
    return {"message": "Logged out"}


async def _publish_fb(page_token, page_id, message, post_type, image_url):
    if post_type == "story" and image_url:
        return await fb_post_req(f"{page_id}/photos", page_token, {
            "url": image_url, "caption": message,
        })
    data = {"message": message}
    if image_url:
        data["link"] = image_url
    return await fb_post_req(f"{page_id}/feed", page_token, data)


def sync_publish(post_id, message, page_id, page_token, post_type, image_url):
    import asyncio
    loop = asyncio.new_event_loop()
    result = loop.run_until_complete(
        _publish_fb(page_token, page_id, message, post_type, image_url)
    )
    loop.close()
    for post in posts_store:
        if post["id"] == post_id:
            post["status"] = "published" if "id" in result else "failed"
            post["fb_post_id"] = result.get("id")


@app.post("/automation/posts/publish")
async def publish_post(payload: PostCreate):
    user_data = users_store.get(payload.session_token)
    if not user_data:
        raise HTTPException(401, "Invalid session")

    page = next((p for p in user_data["pages"] if p["id"] == payload.page_id), None)
    if not page:
        raise HTTPException(404, "Page not found")

    page_token = page["access_token"]
    post_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()

    if payload.scheduled_time:
        scheduled_dt = datetime.fromisoformat(payload.scheduled_time.replace("Z",""))
        record = {
            "id": post_id,
            "message": payload.message,
            "page_id": payload.page_id,
            "page_name": page.get("name", ""),
            "status": "scheduled",
            "created_at": now,
            "scheduled_time": payload.scheduled_time,
            "post_type": payload.post_type,
            "image_url": payload.image_url,
            "fb_post_id": None,
        }
        posts_store.append(record)
        scheduler.add_job(
            sync_publish, "date", run_date=scheduled_dt,
            args=[post_id, payload.message, payload.page_id, page_token,
                  payload.post_type, payload.image_url],
            id=post_id,
        )
        return record

    fb_result = await _publish_fb(
        page_token, payload.page_id, payload.message,
        payload.post_type, payload.image_url
    )
    record = {
        "id": post_id,
        "message": payload.message,
        "page_id": payload.page_id,
        "page_name": page.get("name", ""),
        "status": "published" if "id" in fb_result else "failed",
        "created_at": now,
        "scheduled_time": None,
        "post_type": payload.post_type,
        "image_url": payload.image_url,
        "fb_post_id": fb_result.get("id"),
    }
    posts_store.append(record)
    return record


@app.get("/automation/posts")
def list_posts(session_token: str):
    if session_token not in users_store:
        raise HTTPException(401, "Invalid session")
    return {"posts": posts_store}


@app.delete("/automation/posts/{post_id}")
def delete_post(post_id: str, session_token: str):
    global posts_store
    if session_token not in users_store:
        raise HTTPException(401, "Invalid session")
    posts_store = [p for p in posts_store if p["id"] != post_id]
    try:
        scheduler.remove_job(post_id)
    except Exception:
        pass
    return {"message": "Removed"}


@app.get("/automation/health")
def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


# ─── INSTAGRAM ───────────────────────────────────────────────────────────────

@app.get("/automation/auth/instagram")
def instagram_login():
    scopes = "instagram_basic,instagram_content_publish,pages_show_list,pages_read_engagement"
    url = (
        f"https://www.facebook.com/v19.0/dialog/oauth"
        f"?client_id={FB_APP_ID}"
        f"&redirect_uri={os.getenv('IG_REDIRECT', 'http://localhost:8000/automation/auth/instagram/callback')}"
        f"&scope={scopes}"
        f"&response_type=code"
    )
    return RedirectResponse(url)


@app.get("/automation/auth/instagram/callback")
async def instagram_callback(code: str):
    IG_REDIRECT = os.getenv("IG_REDIRECT", "http://localhost:8000/automation/auth/instagram/callback")
    async with httpx.AsyncClient() as client:
        token_res = await client.get(
            "https://graph.facebook.com/v19.0/oauth/access_token",
            params={
                "client_id": FB_APP_ID,
                "client_secret": FB_APP_SECRET,
                "redirect_uri": IG_REDIRECT,
                "code": code,
            }
        )
        token_data = token_res.json()
        access_token = token_data.get("access_token")
        if not access_token:
            raise HTTPException(400, "Failed to get Instagram access token")

        # Get Facebook pages, then find linked IG business accounts
        pages_data = await fb_get("me/accounts", access_token)
        pages = pages_data.get("data", [])

        ig_accounts = []
        for page in pages:
            page_token = page["access_token"]
            ig_res = await fb_get(
                f"{page['id']}",
                page_token,
                {"fields": "instagram_business_account{id,name,username,profile_picture_url}"}
            )
            ig = ig_res.get("instagram_business_account")
            if ig:
                ig_accounts.append({**ig, "page_token": page_token, "page_id": page["id"]})

        profile = await fb_get("me", access_token, {"fields": "id,name,picture"})
        session_token = str(uuid.uuid4())
        users_store[session_token] = {
            "user": profile,
            "access_token": access_token,
            "pages": [],
            "ig_accounts": ig_accounts,
            "connected_at": datetime.utcnow().isoformat(),
            "platform": "instagram",
        }

    return RedirectResponse(
        f"{FRONTEND_URL}/dashboard?session={session_token}&platform=instagram"
    )


@app.get("/automation/auth/instagram/accounts")
def get_ig_accounts(session_token: str):
    user_data = users_store.get(session_token)
    if not user_data:
        raise HTTPException(401, "Invalid session")
    return {"accounts": user_data.get("ig_accounts", [])}


async def _publish_ig(ig_user_id: str, page_token: str, message: str, post_type: str, media_url: str):
    """Two-step IG publish: create container → publish"""
    if post_type == "story":
        if not media_url:
            raise HTTPException(400, "Stories require a media_url")
        # Detect video vs image by extension
        is_video = any(media_url.lower().endswith(ext) for ext in [".mp4", ".mov", ".avi"])
        container_data = {
            "caption": message,
            "media_type": "STORIES",
        }
        if is_video:
            container_data["video_url"] = media_url
        else:
            container_data["image_url"] = media_url

    elif media_url:
        is_video = any(media_url.lower().endswith(ext) for ext in [".mp4", ".mov", ".avi"])
        container_data = {
            "caption": message,
            "media_type": "VIDEO" if is_video else "IMAGE",
        }
        if is_video:
            container_data["video_url"] = media_url
        else:
            container_data["image_url"] = media_url
    else:
        raise HTTPException(400, "Instagram requires a media_url")

    async with httpx.AsyncClient() as client:
        # Step 1: Create container
        container_res = await client.post(
            f"https://graph.facebook.com/v19.0/{ig_user_id}/media",
            data={"access_token": page_token, **container_data}
        )
        container = container_res.json()
        container_id = container.get("id")
        if not container_id:
            return container  # return error as-is

        # Step 2: Publish container
        publish_res = await client.post(
            f"https://graph.facebook.com/v19.0/{ig_user_id}/media_publish",
            data={"access_token": page_token, "creation_id": container_id}
        )
        return publish_res.json()


class IGPostCreate(BaseModel):
    message: str
    ig_user_id: str
    session_token: str
    post_type: str = "post"      # "post" | "story"
    media_url: Optional[str] = None
    scheduled_time: Optional[str] = None


@app.post("/automation/instagram/publish")
async def publish_ig_post(payload: IGPostCreate):
    user_data = users_store.get(payload.session_token)
    if not user_data:
        raise HTTPException(401, "Invalid session")

    ig_accounts = user_data.get("ig_accounts", [])
    account = next((a for a in ig_accounts if a["id"] == payload.ig_user_id), None)
    if not account:
        raise HTTPException(404, "Instagram account not found")

    page_token = account["page_token"]
    post_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()

    record = {
        "id": post_id,
        "platform": "instagram",
        "message": payload.message,
        "ig_user_id": payload.ig_user_id,
        "ig_username": account.get("username", ""),
        "status": "scheduled" if payload.scheduled_time else "pending",
        "created_at": now,
        "scheduled_time": payload.scheduled_time,
        "post_type": payload.post_type,
        "media_url": payload.media_url,
        "fb_post_id": None,
    }

    if payload.scheduled_time:
        scheduled_dt = datetime.fromisoformat(payload.scheduled_time.replace("Z", ""))
        record["status"] = "scheduled"
        posts_store.append(record)

        def sync_ig(post_id, ig_user_id, page_token, message, post_type, media_url):
            import asyncio
            loop = asyncio.new_event_loop()
            result = loop.run_until_complete(
                _publish_ig(ig_user_id, page_token, message, post_type, media_url)
            )
            loop.close()
            for post in posts_store:
                if post["id"] == post_id:
                    post["status"] = "published" if "id" in result else "failed"
                    post["fb_post_id"] = result.get("id")

        scheduler.add_job(
            sync_ig, "date", run_date=scheduled_dt,
            args=[post_id, payload.ig_user_id, page_token,
                  payload.message, payload.post_type, payload.media_url],
            id=post_id,
        )
        return record

    result = await _publish_ig(
        payload.ig_user_id, page_token,
        payload.message, payload.post_type, payload.media_url
    )
    record["status"] = "published" if "id" in result else "failed"
    record["fb_post_id"] = result.get("id")
    posts_store.append(record)
    return record


# ─── GMAIL ───────────────────────────────────────────────────────────────────

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]
GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GMAIL_REDIRECT = os.getenv("GMAIL_REDIRECT", "http://localhost:8000/automation/auth/gmail/callback")

gmail_flow_store: dict = {}   # state → Flow


@app.get("/automation/auth/gmail")
def gmail_login():
    flow: Flow = Flow.from_client_config(
        {
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [GMAIL_REDIRECT],
            }
        },
        scopes=GMAIL_SCOPES,
    )
    flow.redirect_uri = GMAIL_REDIRECT
    auth_url, state = flow.authorization_url(
        access_type="offline", include_granted_scopes="false", prompt="consent"
    )
    gmail_flow_store[state] = flow
    return RedirectResponse(auth_url)


@app.get("/automation/auth/gmail/callback")
def gmail_callback(code: str, state: str):
    flow: Flow = gmail_flow_store.pop(state, None)
    if not flow:
        raise HTTPException(400, "Invalid OAuth state")

    os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
    flow.fetch_token(code=code)
    creds: Credentials = flow.credentials

    import google.oauth2.credentials
    creds.refresh(google.auth.transport.requests.Request())
    userinfo = pyrequests.get(
        "https://www.googleapis.com/oauth2/v2/userinfo",
        headers={"Authorization": f"Bearer {creds.token}"}
    ).json()
    email = userinfo.get("email", "unknown")

    session_token = str(uuid.uuid4())
    users_store[session_token] = {
        "user": {"id": email, "name": email, "email": email},
        "gmail_creds": {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": list(creds.scopes),
        },
        "pages": [],
        "connected_at": datetime.utcnow().isoformat(),
        "platform": "gmail",
    }
    return RedirectResponse(
        f"{FRONTEND_URL}/dashboard?session={session_token}&platform=gmail"
    )


# Update GmailSend model
class GmailSend(BaseModel):
    session_token: str
    to: str
    subject: str
    body: str
    cc: Optional[str] = None
    bcc: Optional[str] = None
    scheduled_time: Optional[str] = None


def _build_gmail_service(creds_dict: dict):
    from google.oauth2.credentials import Credentials as GCreds
    creds = GCreds(
        token=creds_dict["token"],
        refresh_token=creds_dict["refresh_token"],
        token_uri=creds_dict["token_uri"],
        client_id=creds_dict["client_id"],
        client_secret=creds_dict["client_secret"],
        scopes=creds_dict["scopes"],
    )
    return build("gmail", "v1", credentials=creds)


# Update _send_gmail function
def _send_gmail(service, to: str, subject: str, body: str, cc: str = None, bcc: str = None):
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    def split_addrs(s):
        return [a.strip() for a in s.split(",") if a.strip()] if s else []

    to_list  = split_addrs(to)
    cc_list  = split_addrs(cc)
    bcc_list = split_addrs(bcc)

    def build_raw(to_header, cc_header=None):
        msg = MIMEMultipart("alternative")
        msg["To"]      = to_header
        msg["Subject"] = subject
        if cc_header:
            msg["Cc"] = cc_header
        msg.attach(MIMEText(body, "plain", "utf-8"))
        return base64.urlsafe_b64encode(msg.as_bytes()).decode()

    results = []

    # Main send — To + Cc recipients
    main_raw = build_raw(
        to_header=", ".join(to_list),
        cc_header=", ".join(cc_list) if cc_list else None,
    )
    result = service.users().messages().send(
        userId="me", body={"raw": main_raw}
    ).execute()
    results.append(result)

    # BCC: each recipient gets their own send where they are in To
    # so Gmail API actually delivers to them — no Bcc header ever added
    for addr in bcc_list:
        bcc_raw = build_raw(to_header=addr)  # only this addr in To, no Cc
        service.users().messages().send(
            userId="me", body={"raw": bcc_raw}
        ).execute()

    return results[0]


@app.post("/automation/gmail/send")
async def send_gmail(payload: GmailSend):
    user_data = users_store.get(payload.session_token)
    if not user_data or "gmail_creds" not in user_data:
        raise HTTPException(401, "Gmail not connected")

    post_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    record = {
        "id": post_id,
        "platform": "gmail",
        "to": payload.to,
        "cc": payload.cc,
        "bcc": payload.bcc,
        "subject": payload.subject,
        "body": payload.body,
        "status": "scheduled" if payload.scheduled_time else "pending",
        "created_at": now,
        "scheduled_time": payload.scheduled_time,
    }

    if payload.scheduled_time:
        scheduled_dt = datetime.fromisoformat(payload.scheduled_time.replace("Z", ""))
        posts_store.append(record)
        creds_dict = user_data["gmail_creds"]

        def sync_gmail(post_id, creds_dict, to, subject, body, cc, bcc):
            svc = _build_gmail_service(creds_dict)
            result = _send_gmail(svc, to, subject, body, cc, bcc)
            for post in posts_store:
                if post["id"] == post_id:
                    post["status"] = "sent" if result.get("id") else "failed"

        scheduler.add_job(
            sync_gmail, "date", run_date=scheduled_dt,
            args=[post_id, creds_dict, payload.to, payload.subject, payload.body, payload.cc, payload.bcc],
            id=post_id,
        )
        return record

    service = _build_gmail_service(user_data["gmail_creds"])
    result = _send_gmail(service, payload.to, payload.subject, payload.body, payload.cc, payload.bcc)
    record["status"] = "sent" if result.get("id") else "failed"
    posts_store.append(record)
    return record


# ─── YOUTUBE ─────────────────────────────────────────────────────────────────

YOUTUBE_SCOPES  = ["https://www.googleapis.com/auth/youtube.upload"]
YT_REDIRECT     = os.getenv("YT_REDIRECT", "http://localhost:8000/automation/auth/youtube/callback")
yt_flow_store: dict = {}


@app.get("/automation/auth/youtube")
def youtube_login():
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [YT_REDIRECT],
            }
        },
        scopes=YOUTUBE_SCOPES,
    )
    flow.redirect_uri = YT_REDIRECT
    auth_url, state = flow.authorization_url(
        access_type="offline", include_granted_scopes="true", prompt="consent"
    )
    yt_flow_store[state] = flow
    return RedirectResponse(auth_url)


@app.get("/automation/auth/youtube/callback")
def youtube_callback(code: str, state: str):
    flow = yt_flow_store.pop(state, None)
    if not flow:
        raise HTTPException(400, "Invalid OAuth state")
    flow.fetch_token(code=code)
    creds = flow.credentials

    session_token = str(uuid.uuid4())
    users_store[session_token] = {
        "user": {"id": session_token, "name": "YouTube User"},
        "yt_creds": {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": list(creds.scopes),
        },
        "pages": [],
        "connected_at": datetime.utcnow().isoformat(),
        "platform": "youtube",
    }
    return RedirectResponse(
        f"{FRONTEND_URL}/dashboard?session={session_token}&platform=youtube"
    )


class YouTubeUpload(BaseModel):
    session_token: str
    title: str
    description: str
    video_url: str                  # publicly accessible URL to the video file
    post_type: str = "video"        # "video" | "short"
    privacy: str = "public"         # "public" | "private" | "unlisted"
    scheduled_time: Optional[str] = None


def _build_yt_service(creds_dict: dict):
    from google.oauth2.credentials import Credentials as GCreds
    creds = GCreds(
        token=creds_dict["token"],
        refresh_token=creds_dict["refresh_token"],
        token_uri=creds_dict["token_uri"],
        client_id=creds_dict["client_id"],
        client_secret=creds_dict["client_secret"],
        scopes=creds_dict["scopes"],
    )
    return build("youtube", "v3", credentials=creds)


async def _upload_youtube(creds_dict: dict, title: str, description: str,
                          video_url: str, post_type: str, privacy: str):
    from googleapiclient.http import MediaIoBaseUpload
    import io

    # Download video bytes from public URL
    async with httpx.AsyncClient(follow_redirects=True, timeout=120) as client:
        r = await client.get(video_url)
        video_bytes = r.content

    youtube = _build_yt_service(creds_dict)

    # Shorts: title must contain #Shorts; category 22 = People & Blogs
    if post_type == "short":
        title = title if "#Shorts" in title else f"{title} #Shorts"

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "categoryId": "22",
        },
        "status": {"privacyStatus": privacy},
    }

    media = MediaIoBaseUpload(io.BytesIO(video_bytes), mimetype="video/*", resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        _, response = request.next_chunk()

    return response


@app.post("/automation/youtube/upload")
async def upload_youtube(payload: YouTubeUpload):
    user_data = users_store.get(payload.session_token)
    if not user_data or "yt_creds" not in user_data:
        raise HTTPException(401, "YouTube not connected")

    post_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    record = {
        "id": post_id,
        "platform": "youtube",
        "title": payload.title,
        "description": payload.description,
        "video_url": payload.video_url,
        "post_type": payload.post_type,
        "privacy": payload.privacy,
        "status": "scheduled" if payload.scheduled_time else "pending",
        "created_at": now,
        "scheduled_time": payload.scheduled_time,
        "yt_video_id": None,
    }

    if payload.scheduled_time:
        scheduled_dt = datetime.fromisoformat(payload.scheduled_time.replace("Z", ""))
        posts_store.append(record)
        creds_dict = user_data["yt_creds"]

        def sync_yt(post_id, creds_dict, title, description, video_url, post_type, privacy):
            import asyncio
            loop = asyncio.new_event_loop()
            result = loop.run_until_complete(
                _upload_youtube(creds_dict, title, description, video_url, post_type, privacy)
            )
            loop.close()
            for post in posts_store:
                if post["id"] == post_id:
                    post["status"] = "published" if result.get("id") else "failed"
                    post["yt_video_id"] = result.get("id")

        scheduler.add_job(
            sync_yt, "date", run_date=scheduled_dt,
            args=[post_id, user_data["yt_creds"], payload.title, payload.description,
                  payload.video_url, payload.post_type, payload.privacy],
            id=post_id,
        )
        return record

    result = await _upload_youtube(
        user_data["yt_creds"], payload.title, payload.description,
        payload.video_url, payload.post_type, payload.privacy
    )
    record["status"] = "published" if result.get("id") else "failed"
    record["yt_video_id"] = result.get("id")
    posts_store.append(record)
    return record


# ─── SESSION ──────────────────────────────────────────────────────────────────
@app.post("/api/session/create")
async def create_session(response: Response):
    import uuid
    session_id = str(uuid.uuid4())
    token = create_session_token(session_id)
    response.set_cookie(
        key="session_token", value=token,
        httponly=True, secure=False, samesite="lax", max_age=604800,
    )
    return {"session_id": session_id, "token": token}


@app.get("/api/session/connections")
async def get_connections(session_id: str = Depends(get_session_id)):
    platforms = token_store.get(session_id, {})
    return {
        "facebook":  "facebook"  in platforms,   # Facebook Page (Manage everything)
        "messenger": "messenger" in platforms,   # Messenger DMs
        "instagram": "instagram" in platforms,   # Instagram (separate OAuth)
        "whatsapp":  "whatsapp"  in platforms,   # WhatsApp (separate OAuth)
        "gmail":     "gmail"     in platforms,   # Gmail
    }


# ═════════════════════════════════════════════════════════════════════════════
# OAUTH 1: FACEBOOK + MESSENGER
# Scopes cover: Messenger DMs + full Page management
# ═════════════════════════════════════════════════════════════════════════════

FACEBOOK_MESSENGER_SCOPES = [
    "pages_messaging",           # Send/receive Messenger messages
    "pages_show_list",           # Enumerate managed pages
    "pages_manage_metadata",     # Subscribe webhooks
    "pages_manage_posts",        # Create/delete page posts
    "pages_read_engagement",     # Read posts, comments, likes
    "pages_read_user_content",   # Read user posts on page
    "pages_manage_engagement",   # Reply to comments, like posts
    "email",
]


@app.get("/api/auth/facebook/connect")
async def facebook_connect(session_id: str = Depends(get_session_id)):
    """Initiate Facebook + Messenger OAuth."""
    scope_str = ",".join(FACEBOOK_MESSENGER_SCOPES)
    redirect_uri = f"{BACKEND_URL}/api/auth/facebook/callback"
    url = (
        f"https://www.facebook.com/v19.0/dialog/oauth"
        f"?client_id={META_APP_ID}"
        f"&redirect_uri={redirect_uri}"
        f"&scope={scope_str}"
        f"&state={session_id}"
        f"&response_type=code"
    )
    return {"auth_url": url}


@app.get("/api/auth/facebook/callback")
async def facebook_callback(code: str, state: str, request: Request):
    session_id = state
    redirect_uri = f"{BACKEND_URL}/api/auth/facebook/callback"

    async with httpx.AsyncClient(follow_redirects=True) as client:
        token_resp = await client.get(
            f"{META_GRAPH_URL}/oauth/access_token",
            params={
                "client_id": META_APP_ID, "client_secret": META_APP_SECRET,
                "redirect_uri": redirect_uri, "code": code,
            },
        )
        token_data = token_resp.json()
        if "error" in token_data:
            return RedirectResponse(f"{FRONTEND_URL}?error=facebook_auth_failed")

        user_token = token_data["access_token"]

        ll_resp = await client.get(
            f"{META_GRAPH_URL}/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": META_APP_ID, "client_secret": META_APP_SECRET,
                "fb_exchange_token": user_token,
            },
        )
        ll_data    = ll_resp.json()
        long_token = ll_data.get("access_token", user_token)

        me_resp = await client.get(
            f"{META_GRAPH_URL}/me",
            params={"fields": "id,name,picture", "access_token": long_token},
        )
        me_data = me_resp.json()

        # ── KEY FIX: fetch pages with explicit fields so access_token is included ──
        pages_resp = await client.get(
            f"{META_GRAPH_URL}/me/accounts",
            params={
                "fields": "id,name,access_token,category,tasks",
                "access_token": long_token,
            },
        )
        pages_data = pages_resp.json()
        pages = pages_data.get("data", [])

        print(f"[Facebook OAuth] user={me_data.get('name')} pages={[p.get('name') for p in pages]}")

        if not pages:
            print(f"[Facebook OAuth] WARNING: no pages returned. Raw: {pages_data}")

        base_data = {
            "access_token": long_token,
            "user_id": me_data.get("id"),
            "name": me_data.get("name"),
            "picture": me_data.get("picture", {}).get("data", {}).get("url"),
            "pages": pages,
        }

        set_platform_token(session_id, "messenger", base_data)
        set_platform_token(session_id, "facebook", base_data)

    session_token = create_session_token(session_id)
    return RedirectResponse(f"{FRONTEND_URL}?connected=facebook&token={session_token}")


# ═════════════════════════════════════════════════════════════════════════════
# OAUTH 2: INSTAGRAM (separate flow)
# ═════════════════════════════════════════════════════════════════════════════

INSTAGRAM_SCOPES = [
    "instagram_basic",
    "instagram_manage_messages",
    "instagram_manage_comments",
    "pages_show_list",
    "pages_read_engagement",
    "pages_manage_metadata",
    "email",
]


@app.get("/api/auth/instagram/connect")
async def instagram_connect(session_id: str = Depends(get_session_id)):
    """Initiate Instagram-only OAuth."""
    scope_str = ",".join(INSTAGRAM_SCOPES)
    redirect_uri = f"{BACKEND_URL}/api/auth/instagram/callback"
    url = (
        f"https://www.facebook.com/v19.0/dialog/oauth"
        f"?client_id={META_APP_ID}"
        f"&redirect_uri={redirect_uri}"
        f"&scope={scope_str}"
        f"&state={session_id}"
        f"&response_type=code"
    )
    return {"auth_url": url}


@app.get("/api/auth/instagram/callback")
async def instagram_callback(code: str, state: str, request: Request):
    session_id = state
    redirect_uri = f"{BACKEND_URL}/api/auth/instagram/callback"

    async with httpx.AsyncClient(follow_redirects=True) as client:
        token_resp = await client.get(
            f"{META_GRAPH_URL}/oauth/access_token",
            params={
                "client_id": META_APP_ID, "client_secret": META_APP_SECRET,
                "redirect_uri": redirect_uri, "code": code,
            },
        )
        token_data = token_resp.json()
        if "error" in token_data:
            return RedirectResponse(f"{FRONTEND_URL}?error=instagram_auth_failed")

        user_token = token_data["access_token"]

        ll_resp = await client.get(
            f"{META_GRAPH_URL}/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": META_APP_ID, "client_secret": META_APP_SECRET,
                "fb_exchange_token": user_token,
            },
        )
        long_token = ll_resp.json().get("access_token", user_token)

        # ── Fetch pages with explicit fields ──
        pages_resp = await client.get(
            f"{META_GRAPH_URL}/me/accounts",
            params={
                "fields": "id,name,access_token,instagram_business_account",
                "access_token": long_token,
            },
        )
        pages = pages_resp.json().get("data", [])
        print(f"[Instagram OAuth] pages found: {[p.get('name') for p in pages]}")

        ig_connected = False
        for page in pages:
            page_token = page.get("access_token")
            page_id    = page.get("id")

            ig_account = page.get("instagram_business_account")
            if not ig_account:
                # fallback: fetch page fields separately
                ig_resp = await client.get(
                    f"{META_GRAPH_URL}/{page_id}",
                    params={"fields": "instagram_business_account", "access_token": page_token},
                )
                ig_account = ig_resp.json().get("instagram_business_account")

            if ig_account:
                ig_id = ig_account["id"]
                ig_detail = (await client.get(
                    f"{META_GRAPH_URL}/{ig_id}",
                    params={
                        "fields": "id,username,profile_picture_url,name",
                        "access_token": page_token,
                    },
                )).json()

                set_platform_token(session_id, "instagram", {
                    # ── access_token must be the PAGE token, not user token ──
                    "access_token": page_token,
                    "ig_user_id":   ig_id,
                    "username":     ig_detail.get("username"),
                    "name":         ig_detail.get("name"),
                    "picture":      ig_detail.get("profile_picture_url"),
                    "page_id":      page_id,
                    # ── also store pages list so debug endpoint shows it ──
                    "pages": [{
                        "id":           page_id,
                        "name":         page.get("name"),
                        "access_token": page_token,
                    }],
                })
                print(f"[Instagram OAuth] linked IG account: {ig_detail.get('username')} via page {page_id}")
                ig_connected = True
                break

        if not ig_connected:
            print(f"[Instagram OAuth] No IG business account found. Pages: {pages}")
            return RedirectResponse(f"{FRONTEND_URL}?error=no_instagram_business_account")

    session_token = create_session_token(session_id)
    return RedirectResponse(f"{FRONTEND_URL}?connected=instagram&token={session_token}")


# ═════════════════════════════════════════════════════════════════════════════
# OAUTH 3: WHATSAPP (separate flow)
# ═════════════════════════════════════════════════════════════════════════════

WHATSAPP_SCOPES = [
    "business_management",
    "whatsapp_business_messaging",
    "whatsapp_business_management",
]


@app.get("/api/debug/whatsapp")
async def debug_whatsapp(session_id: str = Depends(get_session_id)):
    token_data = get_platform_token(session_id, "whatsapp")
    if not token_data:
        return {"error": "WhatsApp not connected"}

    async with httpx.AsyncClient() as client:
        # Verify the phone number ID is valid
        resp = await client.get(
            f"{META_GRAPH_URL}/{token_data['phone_number_id']}",
            headers={"Authorization": f"Bearer {token_data['access_token']}"},
        )
    return {
        "stored": {k: v for k, v in token_data.items() if k != "access_token"},
        "phone_number_check": resp.json(),
        "webhook_messages_count": len(whatsapp_webhook_store.get("messages", [])),
    }


@app.get("/api/auth/whatsapp/connect")
async def whatsapp_connect(session_id: str = Depends(get_session_id)):
    """Initiate WhatsApp Business OAuth."""
    scope_str = ",".join(WHATSAPP_SCOPES)
    redirect_uri = f"{BACKEND_URL}/api/auth/whatsapp/callback"
    url = (
        f"https://www.facebook.com/v19.0/dialog/oauth"
        f"?client_id={META_APP_ID}"
        f"&redirect_uri={redirect_uri}"
        f"&scope={scope_str}"
        f"&state={session_id}"
        f"&response_type=code"
    )
    return {"auth_url": url}


@app.get("/api/auth/whatsapp/callback")
async def whatsapp_callback(code: str, state: str, request: Request):
    session_id = state
    redirect_uri = f"{BACKEND_URL}/api/auth/whatsapp/callback"

    async with httpx.AsyncClient(follow_redirects=True) as client:
        token_resp = await client.get(
            f"{META_GRAPH_URL}/oauth/access_token",
            params={
                "client_id": META_APP_ID, "client_secret": META_APP_SECRET,
                "redirect_uri": redirect_uri, "code": code,
            },
        )
        token_data = token_resp.json()
        if "error" in token_data:
            return RedirectResponse(f"{FRONTEND_URL}?error=whatsapp_auth_failed")

        user_token = token_data["access_token"]

        ll_resp = await client.get(
            f"{META_GRAPH_URL}/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": META_APP_ID, "client_secret": META_APP_SECRET,
                "fb_exchange_token": user_token,
            },
        )
        long_token = ll_resp.json().get("access_token", user_token)

        # ── NEW: Dynamically fetch the WABA and phone number ID from the token ──
        phone_number_id = WHATSAPP_PHONE_NUMBER_ID  # fallback to env
        business_account_id = WHATSAPP_BUSINESS_ACCOUNT_ID

        try:
            # Get the WhatsApp Business Accounts this token can access
            waba_resp = await client.get(
                f"{META_GRAPH_URL}/me/businesses",
                params={"access_token": long_token},
            )
            businesses = waba_resp.json().get("data", [])
            print(f"[WhatsApp OAuth] businesses: {businesses}")

            # Try to get WABAs
            waba_list_resp = await client.get(
                f"{META_GRAPH_URL}/{WHATSAPP_BUSINESS_ACCOUNT_ID}",
                params={
                    "fields": "id,name,phone_numbers",
                    "access_token": long_token,
                },
            )
            waba_data = waba_list_resp.json()
            print(f"[WhatsApp OAuth] WABA data: {waba_data}")

            # Fetch phone numbers under the WABA
            phones_resp = await client.get(
                f"{META_GRAPH_URL}/{WHATSAPP_BUSINESS_ACCOUNT_ID}/phone_numbers",
                params={
                    "fields": "id,display_phone_number,verified_name,status",
                    "access_token": long_token,
                },
            )
            phones_data = phones_resp.json()
            print(f"[WhatsApp OAuth] phone numbers: {phones_data}")

            phones = phones_data.get("data", [])
            if phones:
                phone_number_id = phones[0]["id"]
                print(f"[WhatsApp OAuth] using phone_number_id: {phone_number_id}")

        except Exception as e:
            print(f"[WhatsApp OAuth] error fetching phone numbers: {e}")

        set_platform_token(session_id, "whatsapp", {
            "access_token": long_token,
            "phone_number_id": phone_number_id,
            "business_account_id": business_account_id,
        })

    session_token = create_session_token(session_id)
    return RedirectResponse(f"{FRONTEND_URL}?connected=whatsapp&token={session_token}")


@app.post("/api/debug/whatsapp/seed")
async def seed_whatsapp_message(session_id: str = Depends(get_session_id)):
    """Temporary: seed a fake message to test inbox rendering."""
    whatsapp_webhook_store["messages"].append({
        "id": "test_msg_001",
        "from": "919876543210",
        "to": None,
        "timestamp": datetime.utcnow().isoformat(),
        "type": "text",
        "text": "Hello, I saw your product!",
        "is_sent": False,
        "platform": "whatsapp",
    })
    return {"seeded": True, "total": len(whatsapp_webhook_store["messages"])}


# ═════════════════════════════════════════════════════════════════════════════
# GMAIL OAUTH
# ═════════════════════════════════════════════════════════════════════════════

GMAIL_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    # NEW
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/business.manage",
]


class ReplyYouTubeCommentRequest(BaseModel):
    comment_id: str
    text: str


class ReplyReviewRequest(BaseModel):
    account_id: str
    location_id: str
    review_id: str
    comment: str


def build_google_service(session_id: str, api: str, version: str):
    """Build any Google API service using the stored Gmail/Google OAuth credentials."""
    token_data = get_platform_token(session_id, "gmail")
    if not token_data:
        raise HTTPException(status_code=401, detail="Google account not connected")
    creds = Credentials(
        token=token_data["token"],
        refresh_token=token_data["refresh_token"],
        token_uri=token_data["token_uri"],
        client_id=token_data["client_id"],
        client_secret=token_data["client_secret"],
        scopes=token_data["scopes"],
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(google.auth.transport.requests.Request())
        token_data["token"] = creds.token
        set_platform_token(session_id, "gmail", token_data)
    return build(api, version, credentials=creds)


def get_gmail_flow(redirect_uri: str) -> Flow:
    client_config = {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }
    }
    return Flow.from_client_config(client_config, scopes=GMAIL_SCOPES, redirect_uri=redirect_uri)


@app.get("/api/auth/gmail/connect")
async def gmail_connect(session_id: str = Depends(get_session_id)):
    redirect_uri = f"{BACKEND_URL}/api/auth/gmail/callback"
    flow = get_gmail_flow(redirect_uri)
    auth_url, state = flow.authorization_url(
        access_type="offline", include_granted_scopes="true",
        state=session_id, prompt="consent",
    )
    if flow.code_verifier:
        oauth_state_store[session_id] = flow.code_verifier
    return {"auth_url": auth_url}


@app.get("/api/auth/gmail/callback")
async def gmail_callback(code: str, state: str, request: Request):
    session_id = state
    redirect_uri = f"{BACKEND_URL}/api/auth/gmail/callback"
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
    os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"]  = "1"

    flow = get_gmail_flow(redirect_uri)
    code_verifier = oauth_state_store.pop(session_id, None)
    if code_verifier:
        flow.code_verifier = code_verifier
    flow.fetch_token(code=code, code_verifier=code_verifier)

    creds   = flow.credentials
    service = build("gmail", "v1", credentials=creds)
    profile = service.users().getProfile(userId="me").execute()

    set_platform_token(session_id, "gmail", {
        "token": creds.token, "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri, "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes or GMAIL_SCOPES),
        "email": profile.get("emailAddress"),
    })

    session_token = create_session_token(session_id)
    return RedirectResponse(f"{FRONTEND_URL}?connected=gmail&token={session_token}")


def build_gmail_service(session_id: str):
    token_data = get_platform_token(session_id, "gmail")
    if not token_data:
        raise HTTPException(status_code=401, detail="Gmail not connected")
    creds = Credentials(
        token=token_data["token"], refresh_token=token_data["refresh_token"],
        token_uri=token_data["token_uri"], client_id=token_data["client_id"],
        client_secret=token_data["client_secret"], scopes=token_data["scopes"],
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(google.auth.transport.requests.Request())
        token_data["token"] = creds.token
        set_platform_token(session_id, "gmail", token_data)
    return build("gmail", "v1", credentials=creds)


# ═════════════════════════════════════════════════════════════════════════════
# YOUTUBE
# ═════════════════════════════════════════════════════════════════════════════

@app.get("/api/youtube/channel")
async def get_youtube_channel(session_id: str = Depends(get_session_id)):
    youtube = build_google_service(session_id, "youtube", "v3")
    resp = youtube.channels().list(part="snippet,statistics", mine=True).execute()
    items = resp.get("items", [])
    if not items:
        raise HTTPException(status_code=404, detail="No YouTube channel found for this account")
    ch = items[0]
    return {
        "id":          ch["id"],
        "title":       ch["snippet"]["title"],
        "description": ch["snippet"].get("description", ""),
        "thumbnail":   ch["snippet"]["thumbnails"].get("default", {}).get("url"),
        "subscribers": ch["statistics"].get("subscriberCount", "0"),
        "video_count": ch["statistics"].get("videoCount", "0"),
        "view_count":  ch["statistics"].get("viewCount", "0"),
    }


@app.get("/api/youtube/videos")
async def get_youtube_videos(max_results: int = 20, session_id: str = Depends(get_session_id)):
    youtube = build_google_service(session_id, "youtube", "v3")
    # Get uploads playlist ID
    ch_resp = youtube.channels().list(part="contentDetails", mine=True).execute()
    items = ch_resp.get("items", [])
    if not items:
        raise HTTPException(status_code=404, detail="No YouTube channel found")
    uploads_playlist = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

    pl_resp = youtube.playlistItems().list(
        part="snippet,contentDetails",
        playlistId=uploads_playlist,
        maxResults=max_results,
    ).execute()

    video_ids = [i["contentDetails"]["videoId"] for i in pl_resp.get("items", [])]
    if not video_ids:
        return {"videos": []}

    stats_resp = youtube.videos().list(
        part="statistics,snippet",
        id=",".join(video_ids),
    ).execute()

    videos = []
    for v in stats_resp.get("items", []):
        videos.append({
            "id":            v["id"],
            "title":         v["snippet"]["title"],
            "description":   v["snippet"].get("description", ""),
            "thumbnail":     v["snippet"]["thumbnails"].get("medium", {}).get("url"),
            "published_at":  v["snippet"].get("publishedAt"),
            "view_count":    v["statistics"].get("viewCount", "0"),
            "like_count":    v["statistics"].get("likeCount", "0"),
            "comment_count": v["statistics"].get("commentCount", "0"),
        })
    return {"videos": videos}


@app.get("/api/youtube/videos/{video_id}/comments")
async def get_youtube_comments(video_id: str, max_results: int = 50, session_id: str = Depends(get_session_id)):
    youtube = build_google_service(session_id, "youtube", "v3")
    resp = youtube.commentThreads().list(
        part="snippet,replies",
        videoId=video_id,
        maxResults=max_results,
        order="relevance",
    ).execute()

    comments = []
    for item in resp.get("items", []):
        top = item["snippet"]["topLevelComment"]
        top_snip = top["snippet"]
        replies = []
        for r in item.get("replies", {}).get("comments", []):
            rs = r["snippet"]
            replies.append({
                "id":           r["id"],
                "text":         rs["textDisplay"],
                "author":       rs["authorDisplayName"],
                "author_photo": rs.get("authorProfileImageUrl"),
                "like_count":   rs.get("likeCount", 0),
                "published_at": rs.get("publishedAt"),
                "is_mine":      rs.get("authorChannelId", {}).get("value") == top_snip.get("authorChannelId", {}).get("value"),
            })
        comments.append({
            "id":            top["id"],
            "text":          top_snip["textDisplay"],
            "author":        top_snip["authorDisplayName"],
            "author_photo":  top_snip.get("authorProfileImageUrl"),
            "like_count":    top_snip.get("likeCount", 0),
            "reply_count":   item["snippet"].get("totalReplyCount", 0),
            "published_at":  top_snip.get("publishedAt"),
            "replies":       replies,
        })
    return {"comments": comments}


@app.post("/api/youtube/comments/reply")
async def reply_youtube_comment(req: ReplyYouTubeCommentRequest, session_id: str = Depends(get_session_id)):
    youtube = build_google_service(session_id, "youtube", "v3")
    resp = youtube.comments().insert(
        part="snippet",
        body={
            "snippet": {
                "parentId": req.comment_id,
                "textOriginal": req.text,
            }
        },
    ).execute()
    return {"id": resp["id"], "text": resp["snippet"]["textDisplay"]}


# ═════════════════════════════════════════════════════════════════════════════
# GOOGLE BUSINESS PROFILE (Ratings & Reviews)
# requires: https://www.googleapis.com/auth/business.manage scope
# and the "My Business Account Management API" + "My Business Reviews API"
# enabled in your Google Cloud Console project
# ═════════════════════════════════════════════════════════════════════════════

GBP_BASE         = "https://mybusinessaccountmanagement.googleapis.com/v1"
GBP_REVIEWS_BASE = "https://mybusinessreviews.googleapis.com/v1"


async def _gbp_get(url: str, creds: Credentials, params: dict = None):
    """Authenticated GET against the Google Business Profile REST API."""
    headers = {"Authorization": f"Bearer {creds.token}"}
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers, params=params or {})
    data = resp.json()
    if "error" in data:
        raise HTTPException(status_code=400, detail=data["error"].get("message", "GBP API error"))
    return data


async def _gbp_put(url: str, creds: Credentials, body: dict):
    headers = {"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient() as client:
        resp = await client.put(url, headers=headers, json=body)
    data = resp.json()
    if "error" in data:
        raise HTTPException(status_code=400, detail=data["error"].get("message", "GBP API error"))
    return data


def _get_google_creds(session_id: str) -> Credentials:
    token_data = get_platform_token(session_id, "gmail")
    if not token_data:
        raise HTTPException(status_code=401, detail="Google account not connected")
    creds = Credentials(
        token=token_data["token"],
        refresh_token=token_data["refresh_token"],
        token_uri=token_data["token_uri"],
        client_id=token_data["client_id"],
        client_secret=token_data["client_secret"],
        scopes=token_data["scopes"],
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(google.auth.transport.requests.Request())
        token_data["token"] = creds.token
        set_platform_token(session_id, "gmail", token_data)
    return creds


async def _gbp_request(method: str, url: str, creds: Credentials, **kwargs):
    headers = {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient() as client:
        resp = await getattr(client, method)(url, headers=headers, **kwargs)
    data = resp.json()
    if isinstance(data, dict) and "error" in data:
        raise HTTPException(
            status_code=data["error"].get("code", 400),
            detail=data["error"].get("message", "GBP API error"),
        )
    return data
# ✅ This already works for patch — just call it with method="patch"


@app.get("/api/reviews/accounts")
async def get_gbp_accounts(session_id: str = Depends(get_session_id)):
    creds = _get_google_creds(session_id)
    accounts = []
    seen_ids = set()

    try:
        data = await _gbp_request("get", f"{GBP_BASE}/accounts", creds)
        for a in data.get("accounts", []):
            acct_id = a.get("name", "").split("/")[-1]
            if acct_id and acct_id not in seen_ids:
                seen_ids.add(acct_id)
                accounts.append({
                    "id":           acct_id,
                    "resource_name": a.get("name"),
                    "account_name": a.get("accountName") or a.get("name"),
                    "type":         a.get("type", ""),
                })
    except HTTPException as e:
        print(f"[GBP accounts.list] {e.status_code}: {e.detail}")

    return {"accounts": accounts}


@app.get("/api/reviews/accounts/{account_id}/locations")
async def get_gbp_locations(account_id: str, session_id: str = Depends(get_session_id)):
    creds = _get_google_creds(session_id)
    data = await _gbp_request(
        "get",
        f"{GBP_INFO_BASE}/accounts/{account_id}/locations",
        creds,
        params={"readMask": "name,title"},  # readMask is required by this API
    )
    locations = []
    for loc in data.get("locations", []):
        loc_id = loc.get("name", "").split("/")[-1]
        locations.append({
            "id":            loc_id,
            "resource_name": loc.get("name"),
            "title":         loc.get("title", loc_id),
        })
    return {"locations": locations}


@app.get("/api/reviews/accounts/{account_id}/locations/{location_id}/reviews")
async def get_gbp_reviews(account_id: str, location_id: str, page_size: int = 20,
                          session_id: str = Depends(get_session_id)):
    creds = _get_google_creds(session_id)
    # New API uses just "locations/{name}" not "accounts/.../locations/..."
    data = await _gbp_request(
        "get",
        f"{GBP_REVIEWS_BASE}/locations/{location_id}/reviews",
        creds,
        params={"pageSize": page_size},
    )
    reviews = []
    for r in data.get("reviews", []):
        # New API: review name is like "locations/123/reviews/abc"
        review_id = r.get("name", "").split("/")[-1]
        reviews.append({
            "id":             review_id,
            "resource_name":  r.get("name"),
            "reviewer":       r.get("reviewer", {}).get("displayName", "Anonymous"),
            "reviewer_photo": r.get("reviewer", {}).get("profilePhotoUrl"),
            "star_rating":    r.get("starRating"),
            "comment":        r.get("comment", ""),
            "create_time":    r.get("createTime"),
            "update_time":    r.get("updateTime"),
            "reply":          r.get("reviewReply", {}).get("comment"),
            "reply_time":     r.get("reviewReply", {}).get("updateTime"),
        })
    return {
        "reviews":        reviews,
        "average_rating": data.get("averageRating"),
        "total_count":    data.get("totalReviewCount"),
    }


# NEW reply endpoint
@app.put("/api/reviews/accounts/{account_id}/locations/{location_id}/reviews/{review_id}/reply")
async def reply_to_review(account_id: str, location_id: str, review_id: str,
                          req: ReplyReviewRequest,
                          session_id: str = Depends(get_session_id)):
    creds = _get_google_creds(session_id)
    # New API: PATCH locations/{locationId}/reviews/{reviewId}/reply
    return await _gbp_request(
        "patch",
        f"{GBP_REVIEWS_BASE}/locations/{location_id}/reviews/{review_id}/reply",
        creds,
        json={"comment": req.comment},
    )


# ═════════════════════════════════════════════════════════════════════════════
# MESSENGER INBOX & MESSAGING
# ═════════════════════════════════════════════════════════════════════════════

@app.get("/api/inbox/messenger")
async def get_messenger_inbox(session_id: str = Depends(get_session_id)):
    token_data = get_platform_token(session_id, "messenger")
    if not token_data:
        raise HTTPException(status_code=401, detail="Messenger not connected")

    pages = token_data.get("pages", [])
    if not pages:
        raise HTTPException(status_code=400, detail="No pages found — re-connect Facebook and make sure to select your Page during auth")

    threads = []
    async with httpx.AsyncClient() as client:
        for page in pages:
            page_id    = page["id"]
            page_token = page.get("access_token")

            if not page_token:
                print(f"[Messenger] page {page_id} has no access_token, skipping")
                continue

            conv_resp = await client.get(
                f"{META_GRAPH_URL}/{page_id}/conversations",
                params={
                    "fields": (
                        "id,participants,updated_time,"
                        "messages{id,message,from,created_time,attachments,sticker}"
                    ),
                    "access_token": page_token,
                },
            )
            result = conv_resp.json()

            if "error" in result:
                print(f"[Messenger] page {page_id} error: {result['error']}")
                raise HTTPException(
                    status_code=400,
                    detail=f"Meta API error for page {page_id}: {result['error'].get('message', 'unknown')}"
                )

            for conv in result.get("data", []):
                participants = conv.get("participants", {}).get("data", [])
                other_party  = next((p for p in participants if p["id"] != page_id), None)
                msgs = []
                for msg in conv.get("messages", {}).get("data", []):
                    sender_id = msg.get("from", {}).get("id")
                    msgs.append({
                        "id":         msg["id"],
                        "text":       msg.get("message", ""),
                        "sender":     msg.get("from", {}).get("name"),
                        "sender_id":  sender_id,
                        "is_sent":    sender_id == page_id,
                        "timestamp":  _normalise_ts(msg.get("created_time")),
                        "attachments": msg.get("attachments", {}).get("data", []),
                    })
                msgs.sort(key=lambda m: m["timestamp"])
                threads.append({
                    "id":           conv["id"],
                    "platform":     "messenger",
                    "page_id":      page_id,
                    "page_name":    page.get("name"),
                    "page_token":   page_token,
                    "updated_at":   _normalise_ts(conv.get("updated_time")),
                    "participant":  other_party,
                    "messages":     msgs,
                    "last_message": msgs[-1] if msgs else None,
                })

    threads.sort(key=lambda t: t["updated_at"], reverse=True)
    return {"threads": threads}


@app.post("/api/messages/messenger/send")
async def send_messenger_message(req: SendMessageRequest, session_id: str = Depends(get_session_id)):
    token_data = get_platform_token(session_id, "messenger")
    if not token_data:
        raise HTTPException(status_code=401, detail="Messenger not connected")

    pages = token_data.get("pages", [])
    if not pages:
        raise HTTPException(status_code=400, detail="No managed pages found")
    page = pages[0]

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{META_GRAPH_URL}/{page['id']}/messages",
            params={"access_token": page["access_token"]},
            json={
                "recipient": {"id": req.recipient_id},
                "message":   {"text": req.message},
                "messaging_type": "RESPONSE",
            },
        )
        data = resp.json()
        if "error" in data:
            raise HTTPException(status_code=400, detail=data["error"].get("message"))
        return data


@app.post("/api/messages/messenger/reply")
async def reply_messenger_dm(req: ReplyDMRequest, session_id: str = Depends(get_session_id)):
    token_data = get_platform_token(session_id, "messenger")
    if not token_data:
        raise HTTPException(status_code=401, detail="Messenger not connected")

    pages = token_data.get("pages", [])
    page  = pages[0] if pages else None
    if not page:
        raise HTTPException(status_code=400, detail="No managed pages found")

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{META_GRAPH_URL}/{page['id']}/messages",
            params={"access_token": page["access_token"]},
            json={
                "recipient": {"id": req.recipient_id},
                "message":   {"text": req.message},
                "messaging_type": "RESPONSE",
            },
        )
        data = resp.json()
        if "error" in data:
            raise HTTPException(status_code=400, detail=data["error"].get("message"))
        return {"thread_id": req.thread_id, **data}


# ═════════════════════════════════════════════════════════════════════════════
# FACEBOOK PAGE POSTS & COMMENTS
# ═════════════════════════════════════════════════════════════════════════════

@app.get("/api/posts/facebook")
async def get_facebook_posts(session_id: str = Depends(get_session_id)):
    # Use "facebook" token store (set during facebook OAuth)
    token_data = get_platform_token(session_id, "facebook")
    if not token_data:
        raise HTTPException(status_code=401, detail="Facebook not connected")

    all_posts = []
    async with httpx.AsyncClient() as client:
        for page in token_data.get("pages", []):
            page_id    = page["id"]
            page_token = page["access_token"]
            resp = await client.get(
                f"{META_GRAPH_URL}/{page_id}/posts",
                params={
                    "fields": (
                        "id,message,story,created_time,"
                        "likes.summary(true),comments.summary(true),"
                        "full_picture,permalink_url"
                    ),
                    "access_token": page_token,
                },
            )
            for post in resp.json().get("data", []):
                post["page_name"] = page.get("name")
                post["page_id"]   = page_id
                all_posts.append(post)

    return {"posts": all_posts}


@app.get("/api/posts/facebook/{post_id}/comments")
async def get_facebook_comments(post_id: str, session_id: str = Depends(get_session_id)):
    token_data = get_platform_token(session_id, "facebook")
    if not token_data:
        raise HTTPException(status_code=401, detail="Facebook not connected")

    page_token = token_data.get("pages", [{}])[0].get("access_token", token_data["access_token"])

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{META_GRAPH_URL}/{post_id}/comments",
            params={
                "fields": (
                    "id,message,from,created_time,like_count,"
                    "comments{id,message,from,created_time}"
                ),
                "access_token": page_token,
            },
        )
    return {"comments": resp.json().get("data", [])}


# Alias for the frontend which uses platform="messenger" for FB page posts
@app.get("/api/posts/messenger/{post_id}/comments")
async def get_messenger_post_comments(post_id: str, session_id: str = Depends(get_session_id)):
    return await get_facebook_comments(post_id, session_id)


# ═════════════════════════════════════════════════════════════════════════════
# INSTAGRAM INBOX, POSTS & COMMENTS
# ═════════════════════════════════════════════════════════════════════════════

# @app.get("/api/inbox/instagram")
# async def get_instagram_inbox(session_id: str = Depends(get_session_id)):
#     token_data = get_platform_token(session_id, "instagram")
#     if not token_data:
#         raise HTTPException(status_code=401, detail="Instagram not connected")
#
#     page_id      = token_data["page_id"]        # ← use page_id, not ig_user_id
#     ig_id        = token_data["ig_user_id"]
#     access_token = token_data["access_token"]   # this is already the page token
#     threads      = []
#
#     async with httpx.AsyncClient() as client:
#         for folder in ["primary", "general", "requests"]:
#             conv_resp = await client.get(
#                 f"{META_GRAPH_URL}/{page_id}/conversations",  # ← page_id, not ig_id
#                 params={
#                     "platform": "instagram",
#                     "folder": folder,
#                     "fields": (
#                         "id,updated_time,participants,"
#                         "messages{id,message,from,created_time,attachments}"
#                     ),
#                     "access_token": access_token,
#                 },
#             )
#             result = conv_resp.json()
#             if "error" in result:
#                 print(f"[Instagram inbox] folder={folder} error={result['error']}")
#                 continue
#
#             for conv in result.get("data", []):
#                 participants = conv.get("participants", {}).get("data", [])
#                 other_party  = next((p for p in participants if p["id"] != ig_id), None)
#                 msgs = []
#                 for msg in conv.get("messages", {}).get("data", []):
#                     sender_id = msg.get("from", {}).get("id")
#                     msgs.append({
#                         "id":          msg["id"],
#                         "text":        msg.get("message", ""),
#                         "sender":      msg.get("from", {}).get("username") or msg.get("from", {}).get("name"),
#                         "sender_id":   sender_id,
#                         "is_sent":     sender_id == ig_id,
#                         "timestamp":   _normalise_ts(msg.get("created_time")),
#                         "attachments": msg.get("attachments", {}).get("data", []),
#                     })
#                 msgs.sort(key=lambda m: m["timestamp"])
#                 threads.append({
#                     "id":           conv["id"],
#                     "platform":     "instagram",
#                     "folder":       folder,
#                     "updated_at":   _normalise_ts(conv.get("updated_time")),
#                     "participant":  other_party,
#                     "messages":     msgs,
#                     "last_message": msgs[-1] if msgs else None,
#                 })
#
#     threads.sort(key=lambda t: t["updated_at"], reverse=True)
#     return {"threads": threads}
#
#
# @app.post("/api/messages/instagram/send")
# async def send_instagram_dm(req: SendMessageRequest, session_id: str = Depends(get_session_id)):
#     token_data = get_platform_token(session_id, "instagram")
#     if not token_data:
#         raise HTTPException(status_code=401, detail="Instagram not connected")
#
#     async with httpx.AsyncClient() as client:
#         resp = await client.post(
#             f"{META_GRAPH_URL}/{token_data['page_id']}/messages",
#             params={"access_token": token_data["access_token"], "platform": "instagram"},
#             json={
#                 "recipient": {"id": req.recipient_id},
#                 "message": {"text": req.message},
#             },
#         )
#         data = resp.json()
#         if "error" in data:
#             raise HTTPException(status_code=400, detail=data["error"].get("message"))
#         return data
#
#
# @app.post("/api/messages/instagram/reply")
# async def reply_instagram_dm(req: ReplyDMRequest, session_id: str = Depends(get_session_id)):
#     token_data = get_platform_token(session_id, "instagram")
#     if not token_data:
#         raise HTTPException(status_code=401, detail="Instagram not connected")
#
#     async with httpx.AsyncClient() as client:
#         resp = await client.post(
#             f"{META_GRAPH_URL}/{token_data['ig_user_id']}/messages",
#             params={"access_token": token_data["access_token"]},
#             json={
#                 "recipient": {"id": req.recipient_id},
#                 "message":   {"text": req.message},
#             },
#         )
#         data = resp.json()
#         if "error" in data:
#             raise HTTPException(status_code=400, detail=data["error"].get("message"))
#         return {"thread_id": req.thread_id, **data}


@app.get("/api/posts/instagram")
async def get_instagram_posts(session_id: str = Depends(get_session_id)):
    token_data = get_platform_token(session_id, "instagram")
    if not token_data:
        raise HTTPException(status_code=401, detail="Instagram not connected")

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{META_GRAPH_URL}/{token_data['ig_user_id']}/media",
            params={
                "fields": (
                    "id,caption,media_type,media_url,thumbnail_url,"
                    "timestamp,like_count,comments_count,permalink"
                ),
                "access_token": token_data["access_token"],
            },
        )
    return {"posts": resp.json().get("data", [])}


@app.get("/api/posts/instagram/stories")
async def get_instagram_stories(session_id: str = Depends(get_session_id)):
    token_data = get_platform_token(session_id, "instagram")
    if not token_data:
        raise HTTPException(status_code=401, detail="Instagram not connected")

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{META_GRAPH_URL}/{token_data['ig_user_id']}/stories",
            params={
                "fields": "id,media_type,media_url,thumbnail_url,timestamp,permalink",
                "access_token": token_data["access_token"],
            },
        )
    return {"stories": resp.json().get("data", [])}


@app.get("/api/posts/instagram/{post_id}/comments")
async def get_instagram_comments(post_id: str, session_id: str = Depends(get_session_id)):
    token_data = get_platform_token(session_id, "instagram")
    if not token_data:
        raise HTTPException(status_code=401, detail="Instagram not connected")

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{META_GRAPH_URL}/{post_id}/comments",
            params={
                "fields": (
                    "id,text,username,timestamp,like_count,"
                    "replies{id,text,username,timestamp,from}"
                ),
                "access_token": token_data["access_token"],
            },
        )
    return {"comments": resp.json().get("data", [])}


@app.post("/api/comments/reply")
async def reply_to_comment(req: ReplyCommentRequest, session_id: str = Depends(get_session_id)):
    if req.platform == "instagram":
        token_data = get_platform_token(session_id, "instagram")
        if not token_data:
            raise HTTPException(status_code=401, detail="Instagram not connected")
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{META_GRAPH_URL}/{req.comment_id}/replies",
                params={"access_token": token_data["access_token"]},
                json={"message": req.message},
            )
            data = resp.json()
            if "error" in data:
                raise HTTPException(status_code=400, detail=data["error"].get("message"))
            return data
    else:
        # Facebook page comments
        token_data = get_platform_token(session_id, "facebook")
        if not token_data:
            raise HTTPException(status_code=401, detail="Facebook not connected")
        page_token = token_data.get("pages", [{}])[0].get("access_token", token_data["access_token"])
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{META_GRAPH_URL}/{req.comment_id}/comments",
                params={"access_token": page_token},
                json={"message": req.message},
            )
            data = resp.json()
            if "error" in data:
                raise HTTPException(status_code=400, detail=data["error"].get("message"))
            return data


@app.post("/api/comments/post")
async def post_comment(req: PostCommentRequest, session_id: str = Depends(get_session_id)):
    if req.platform == "instagram":
        token_data = get_platform_token(session_id, "instagram")
        if not token_data:
            raise HTTPException(status_code=401, detail="Instagram not connected")
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{META_GRAPH_URL}/{req.post_id}/comments",
                params={"access_token": token_data["access_token"]},
                json={"message": req.message},
            )
            data = resp.json()
            if "error" in data:
                raise HTTPException(status_code=400, detail=data["error"].get("message"))
            return data
    else:
        token_data = get_platform_token(session_id, "facebook")
        if not token_data:
            raise HTTPException(status_code=401, detail="Facebook not connected")
        page_token = token_data.get("pages", [{}])[0].get("access_token", token_data["access_token"])
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{META_GRAPH_URL}/{req.post_id}/comments",
                params={"access_token": page_token},
                json={"message": req.message},
            )
            data = resp.json()
            if "error" in data:
                raise HTTPException(status_code=400, detail=data["error"].get("message"))
            return data


# ═════════════════════════════════════════════════════════════════════════════
# WHATSAPP BUSINESS
# ═════════════════════════════════════════════════════════════════════════════

@app.get("/api/inbox/whatsapp")
async def get_whatsapp_messages(session_id: str = Depends(get_session_id)):
    token_data = get_platform_token(session_id, "whatsapp")
    if not token_data:
        raise HTTPException(status_code=401, detail="WhatsApp not connected")

    raw = whatsapp_webhook_store.get("messages", [])
    by_sender: Dict[str, List] = {}
    for msg in raw:
        key = msg.get("from") or msg.get("to", "unknown")
        by_sender.setdefault(key, []).append(msg)

    threads = []
    for phone, msgs in by_sender.items():
        msgs_sorted = sorted(msgs, key=lambda m: m.get("timestamp", ""))
        threads.append({
            "id":           f"wa_{phone}",
            "platform":     "whatsapp",
            "participant":  {"id": phone, "name": phone},
            "messages":     msgs_sorted,
            "last_message": msgs_sorted[-1] if msgs_sorted else None,
            "updated_at":   msgs_sorted[-1].get("timestamp", "") if msgs_sorted else "",
        })

    threads.sort(key=lambda t: t["updated_at"], reverse=True)
    return {"threads": threads}


@app.post("/api/messages/whatsapp/send")
async def send_whatsapp_message(req: SendMessageRequest, session_id: str = Depends(get_session_id)):
    token_data = get_platform_token(session_id, "whatsapp")
    if not token_data:
        raise HTTPException(status_code=401, detail="WhatsApp not connected")

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{WHATSAPP_API_URL}/messages",
            headers={
                "Authorization": f"Bearer {token_data['access_token']}",
                "Content-Type": "application/json",
            },
            json={
                "messaging_product": "whatsapp",
                "to":   req.recipient_id,
                "type": "text",
                "text": {"body": req.message},
            },
        )
        data = resp.json()
        if "error" in data:
            raise HTTPException(status_code=400, detail=data["error"].get("message"))

    whatsapp_webhook_store["messages"].append({
        "id":        data.get("messages", [{}])[0].get("id", "sent"),
        "from":      None,
        "to":        req.recipient_id,
        "timestamp": datetime.utcnow().isoformat(),
        "type":      "text",
        "text":      req.message,
        "is_sent":   True,
        "platform":  "whatsapp",
    })
    return data


@app.post("/api/webhooks/whatsapp")
async def whatsapp_webhook_receive(request: Request):
    body = await request.json()
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for msg in value.get("messages", []):
                whatsapp_webhook_store["messages"].append({
                    "id":        msg["id"],
                    "from":      msg["from"],
                    "to":        None,
                    "timestamp": _normalise_ts(msg.get("timestamp")),
                    "type":      msg.get("type"),
                    "text":      msg.get("text", {}).get("body", ""),
                    "is_sent":   False,
                    "platform":  "whatsapp",
                })
    return {"status": "ok"}


# ═════════════════════════════════════════════════════════════════════════════
# GMAIL
# ═════════════════════════════════════════════════════════════════════════════

@app.get("/api/inbox/gmail")
async def get_gmail_inbox(max_results: int = 20, session_id: str = Depends(get_session_id)):
    service = build_gmail_service(session_id)
    result  = service.users().messages().list(
        userId="me", labelIds=["INBOX"], maxResults=max_results
    ).execute()

    messages = []
    for msg_ref in result.get("messages", []):
        msg = service.users().messages().get(
            userId="me", id=msg_ref["id"], format="metadata",
            metadataHeaders=["From", "To", "Subject", "Date"],
        ).execute()
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        messages.append({
            "id":        msg["id"],
            "thread_id": msg["threadId"],
            "snippet":   msg.get("snippet", ""),
            "from":      headers.get("From", ""),
            "to":        headers.get("To", ""),
            "subject":   headers.get("Subject", ""),
            "date":      headers.get("Date", ""),
            "labels":    msg.get("labelIds", []),
            "platform":  "gmail",
        })

    return {"messages": messages}


@app.get("/api/inbox/gmail/{message_id}")
async def get_gmail_message(message_id: str, session_id: str = Depends(get_session_id)):
    service = build_gmail_service(session_id)
    msg = service.users().messages().get(userId="me", id=message_id, format="full").execute()

    def decode_body(payload):
        if payload.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
        for part in payload.get("parts", []):
            if part.get("mimeType") in ("text/plain", "text/html"):
                data = part.get("body", {}).get("data", "")
                if data:
                    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        return ""

    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    return {
        "id":        msg["id"],
        "thread_id": msg["threadId"],
        "from":      headers.get("From", ""),
        "to":        headers.get("To", ""),
        "subject":   headers.get("Subject", ""),
        "date":      headers.get("Date", ""),
        "body":      decode_body(msg.get("payload", {})),
        "platform":  "gmail",
    }


@app.post("/api/email/send")
async def send_email(req: SendEmailRequest, session_id: str = Depends(get_session_id)):
    import email.mime.text
    import email.mime.multipart

    service      = build_gmail_service(session_id)
    token_data   = get_platform_token(session_id, "gmail")
    sender_email = token_data.get("email", "me")

    mime_msg            = email.mime.multipart.MIMEMultipart()
    mime_msg["to"]      = req.to
    mime_msg["from"]    = sender_email
    mime_msg["subject"] = req.subject
    mime_msg.attach(email.mime.text.MIMEText(req.body, "plain"))

    raw  = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode()
    body = {"raw": raw}
    if req.thread_id:
        body["threadId"] = req.thread_id

    result = service.users().messages().send(userId="me", body=body).execute()
    return {"message_id": result["id"], "thread_id": result.get("threadId")}


# ═════════════════════════════════════════════════════════════════════════════
# DISCONNECT
# ═════════════════════════════════════════════════════════════════════════════

@app.delete("/api/auth/{platform}/disconnect")
async def disconnect_platform(platform: str, session_id: str = Depends(get_session_id)):
    if session_id in token_store:
        if platform == "facebook":
            # Disconnecting facebook also removes messenger (same OAuth)
            token_store[session_id].pop("facebook", None)
            token_store[session_id].pop("messenger", None)
        else:
            token_store[session_id].pop(platform, None)
    return {"disconnected": platform}


# ═════════════════════════════════════════════════════════════════════════════
# DEBUG
# ═════════════════════════════════════════════════════════════════════════════

@app.get("/api/debug/instagram")
async def debug_instagram(session_id: str = Depends(get_session_id)):
    token_data = get_platform_token(session_id, "instagram")
    if not token_data:
        return {"error": "Instagram not connected"}
    async with httpx.AsyncClient() as client:
        me = (await client.get(
            f"{META_GRAPH_URL}/{token_data['ig_user_id']}",
            params={
                "fields": "id,username,name,biography,followers_count,media_count",
                "access_token": token_data["access_token"],
            },
        )).json()
    return {"stored_keys": list(token_data.keys()), "me": me}


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

# ─── Order / Checkout Endpoints ───────────────────────────────────
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
    reviewz[review_id] = {
        "id": review_id,
        **review.dict(),
        "created_at": datetime.now().isoformat(),
    }
    return reviewz[review_id]

@app.get("/reviewz")
def list_reviewz(store_id: Optional[str] = None):
    all_reviewz = list(reviewz.values())
    if store_id:
        all_reviewz = [r for r in all_reviewz if r["store_id"] == store_id]
    return all_reviewz

@app.get("/reviewz/stats/{store_id}")
def review_stats(store_id: str):
    store_reviewz = [r for r in reviewz.values() if r["store_id"] == store_id]
    if not store_reviewz:
        return {"average_rating": 0, "total_reviewz": 0, "rating_breakdown": {}}
    avg = sum(r["rating"] for r in store_reviewz) / len(store_reviewz)
    breakdown = {str(i): sum(1 for r in store_reviewz if r["rating"] == i) for i in range(1, 6)}
    return {
        "average_rating": round(avg, 2),
        "total_reviewz": len(store_reviewz),
        "rating_breakdown": breakdown,
    }

# ─── Analytics ────────────────────────────────────────────────────
@app.get("/analytics/{store_id}")
def get_analytics(store_id: str):
    if store_id not in stores:
        raise HTTPException(status_code=404, detail="Store not found")
    store_orders = [o for o in orders.values() if o["store_id"] == store_id]
    store_products = [p for p in products.values() if p["store_id"] == store_id]
    store_reviewz = [r for r in reviewz.values() if r["store_id"] == store_id]
    total_revenue = sum(o["total"] for o in store_orders)
    avg_rating = (sum(r["rating"] for r in store_reviewz) / len(store_reviewz)) if store_reviewz else 0
    return {
        "total_orders": len(store_orders),
        "total_revenue": round(total_revenue, 2),
        "total_products": len(store_products),
        "total_reviewz": len(store_reviewz),
        "average_rating": round(avg_rating, 2),
        "recent_orders": store_orders[-5:][::-1],
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
