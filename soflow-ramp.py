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

from __future__ import annotations

# ── Standard Library ──────────────────────────────────────────────────────────
import os
import io
import re
import ssl
import json
import uuid
import base64
import random
import string
import pathlib
import platform
import smtplib
import difflib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from typing import Optional, List, Dict, Any

# ── Third-Party: Web Framework ────────────────────────────────────────────────
from fastapi import FastAPI, HTTPException, Depends, Request, Response, Query, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, field_validator
from fastapi.responses import JSONResponse, FileResponse

# ── Third-Party: HTTP Clients ─────────────────────────────────────────────────
import httpx
import requests
from bs4 import BeautifulSoup

# ── Third-Party: Auth & Security ──────────────────────────────────────────────
from jose import JWTError, jwt

# ── Third-Party: Google ───────────────────────────────────────────────────────
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
import google.auth.transport.requests
from fastapi.exceptions import RequestValidationError

# ── Third-Party: Scheduler ────────────────────────────────────────────────────
from apscheduler.schedulers.background import BackgroundScheduler

# ── Third-Party: Payments ─────────────────────────────────────────────────────
import razorpay

# ── Third-Party: ML / Vision ──────────────────────────────────────────────────
from fastai.vision.all import PILImage

# ── Third-Party: Azure / Phi-4 ───────────────────────────────────────────────
from azure.ai.inference import ChatCompletionsClient
from azure.ai.inference.models import UserMessage, SystemMessage
from azure.core.credentials import AzureKeyCredential

# ── PDF invoice generation libs ────────────────────────────────────
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
)
from reportlab.lib.enums import TA_RIGHT, TA_LEFT, TA_CENTER
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont


from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders


# ── Environment ───────────────────────────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv()

load_dotenv()
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = os.getenv("OAUTHLIB_INSECURE_TRANSPORT", "0")

app = FastAPI(title="SocialRamp API", version="4.0.0")

# ─── CORS ─────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
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
FRONTEND_URL                 = "http://localhost:3000"
SECRET_KEY                   = os.getenv("SECRET_KEY", "change-this-secret")
BACKEND_URL                  = os.getenv("BACKEND_URL", "http://localhost:8000")
WHATSAPP_VERIFY_TOKEN        = os.getenv("WHATSAPP_VERIFY_TOKEN", "socialramp_verify_token")

META_GRAPH_URL   = "https://graph.facebook.com/v19.0"
WHATSAPP_API_URL = f"https://graph.facebook.com/v19.0/{WHATSAPP_PHONE_NUMBER_ID}"

FB_APP_ID     = os.getenv("FACEBOOK_APP_ID")
FB_APP_SECRET = os.getenv("FACEBOOK_APP_SECRET")
FB_REDIRECT   = "http://localhost:8000/automation/auth/facebook/callback"

AZURE_API_URL = os.environ.get("API_URL")
AZURE_API_KEY = os.environ.get("API_KEY")
AZURE_MODEL   = "Phi-4"

# ── Razorpay config ───────────────────────────────────────────────────────────
RAZORPAY_KEY_ID     = os.getenv("RAZORPAY_KEY_ID")
# RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")

users_store: dict = {}
posts_store: list = []
scheduler = BackgroundScheduler()
scheduler.start()

# In-memory stores (replace with DB in production)
token_store: Dict[str, Dict[str, Any]] = {}
oauth_state_store: Dict[str, str] = {}
whatsapp_webhook_store: Dict[str, List] = {"messages": []}

# ─── In-memory DB ────────────────────────────────────────────────
stores   = {}
products = {}
orders   = {}
reviews  = {}

FRONTEND_URL       = os.getenv("FRONTEND_URL", "http://localhost:3000")

# ─── SMTP settings (for OTP emails) ─────────────────────────────
SMTP_HOST     = os.getenv("SMTP_HOST", "mail.privateemail.com")
SMTP_PORT     = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER     = os.getenv("EMAIL_USER")
SMTP_PASSWORD = os.getenv("EMAIL_PASSWORD")
SMTP_FROM     = os.getenv("SMTP_FROM", SMTP_USER)


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
        f"{FRONTEND_URL}/dashboard/home?session={session_token}&name={profile.get('name', '')}"
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
        f"{FRONTEND_URL}/dashboard/home?session={session_token}&platform=instagram"
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
    userinfo = requests.get(
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
        f"{FRONTEND_URL}/dashboard/home?session={session_token}&platform=gmail"
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
        f"{FRONTEND_URL}/dashboard/home?session={session_token}&platform=youtube"
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


# ═══════════════════════════════════════════════════════════════════
#  GST INVOICE GENERATION  (merged in directly — no separate module)
# ═══════════════════════════════════════════════════════════════════

DEFAULT_GST_RATE = 18.0  # used only if a product has no gst_rate set

# ── Font registration ──────────────────────────────────────────────
# Base PDF fonts (Helvetica) have no glyph for ₹ and render it as a
# black box. DejaVu Sans ships on most Linux systems and includes it.
# If unavailable, fall back to "Rs." text so nothing breaks silently.
_RUPEE_FONT_REGULAR = "Helvetica"
_RUPEE_FONT_BOLD = "Helvetica-Bold"
_RUPEE_SYMBOL = "Rs. "

_DEJAVU_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_DEJAVU_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

if os.path.exists(_DEJAVU_REGULAR) and os.path.exists(_DEJAVU_BOLD):
    try:
        pdfmetrics.registerFont(TTFont("DejaVuSans", _DEJAVU_REGULAR))
        pdfmetrics.registerFont(TTFont("DejaVuSans-Bold", _DEJAVU_BOLD))
        _RUPEE_FONT_REGULAR = "DejaVuSans"
        _RUPEE_FONT_BOLD = "DejaVuSans-Bold"
        _RUPEE_SYMBOL = "\u20b9"  # ₹ glyph is safe to use with this font
    except Exception:
        pass


def fmt_inr(amount: float) -> str:
    """Format a number as an INR amount string using whichever rupee
    representation is safe for the registered font."""
    return f"{_RUPEE_SYMBOL}{amount:,.2f}"


# State code -> name map (first 2 digits of GSTIN identify the state).
# Used to decide CGST+SGST (intra-state) vs IGST (inter-state).
GST_STATE_CODES = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab",
    "04": "Chandigarh", "05": "Uttarakhand", "06": "Haryana", "07": "Delhi",
    "08": "Rajasthan", "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim",
    "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur",
    "15": "Mizoram", "16": "Tripura", "17": "Meghalaya", "18": "Assam",
    "19": "West Bengal", "20": "Jharkhand", "21": "Odisha",
    "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
    "26": "Dadra and Nagar Haveli and Daman and Diu", "27": "Maharashtra",
    "29": "Karnataka", "30": "Goa", "31": "Lakshadweep", "32": "Kerala",
    "33": "Tamil Nadu", "34": "Puducherry", "35": "Andaman and Nicobar Islands",
    "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh", "97": "Other Territory",
}

STATE_NAME_TO_CODE = {v.lower(): k for k, v in GST_STATE_CODES.items()}


def _state_code_from_gstin(gstin: str) -> str:
    if not gstin or len(gstin) < 2:
        return ""
    return gstin[:2]


def _state_code_from_name(name: str) -> str:
    if not name:
        return ""
    return STATE_NAME_TO_CODE.get(name.strip().lower(), "")


def compute_invoice_tax(line_items, seller_gstin: str, delivery_state: str):
    """
    line_items: list of dicts with keys:
        product_name, quantity, unit_price, subtotal, hsn_code, gst_rate
    seller_gstin: store's GSTIN (e.g. "33ABCDE1234F1Z5"). Empty/None means
        the seller is NOT GST-registered, in which case NO tax is charged
        on any line item regardless of any gst_rate set on the product —
        an unregistered seller cannot legally collect GST.
    delivery_state: buyer's delivery state name (e.g. "Tamil Nadu")

    Returns (enriched_items, totals, tax_type) where tax_type is
    "CGST_SGST" (intra-state), "IGST" (inter-state), or "UNREGISTERED"
    (seller not GST-registered — bill of supply, zero tax).
    """
    seller_gstin = (seller_gstin or "").strip()

    if not seller_gstin:
        # Bill of supply: no GST charged at all.
        enriched = []
        grand_subtotal = 0.0
        for item in line_items:
            taxable_value = float(item["subtotal"])
            grand_subtotal += taxable_value
            enriched.append({
                "product_name": item["product_name"],
                "hsn_code": item.get("hsn_code") or "—",
                "quantity": item["quantity"],
                "unit_price": float(item["unit_price"]),
                "taxable_value": taxable_value,
                "gst_rate": 0.0,
                "cgst_rate": 0.0, "cgst_amt": 0.0,
                "sgst_rate": 0.0, "sgst_amt": 0.0,
                "igst_rate": 0.0, "igst_amt": 0.0,
                "line_total": taxable_value,
            })
        totals = {
            "subtotal": grand_subtotal, "cgst": 0.0, "sgst": 0.0, "igst": 0.0,
            "total_tax": 0.0, "grand_total": grand_subtotal,
        }
        return enriched, totals, "UNREGISTERED"

    seller_state_code = _state_code_from_gstin(seller_gstin)
    buyer_state_code = _state_code_from_name(delivery_state)

    # If we can't determine the buyer's state, default to intra-state
    # (CGST+SGST) since that's the more common case for most small
    # sellers and avoids under-charging IGST incorrectly.
    same_state = (not buyer_state_code) or (seller_state_code == buyer_state_code)
    tax_type = "CGST_SGST" if same_state else "IGST"

    enriched = []
    grand_subtotal = 0.0
    grand_cgst = 0.0
    grand_sgst = 0.0
    grand_igst = 0.0

    for item in line_items:
        rate = item.get("gst_rate")
        if rate is None or rate == "":
            rate = DEFAULT_GST_RATE
        rate = float(rate)

        taxable_value = float(item["subtotal"])
        tax_amount = taxable_value * rate / 100.0

        row = {
            "product_name": item["product_name"],
            "hsn_code": item.get("hsn_code") or "—",
            "quantity": item["quantity"],
            "unit_price": float(item["unit_price"]),
            "taxable_value": taxable_value,
            "gst_rate": rate,
        }

        if tax_type == "CGST_SGST":
            cgst = tax_amount / 2.0
            sgst = tax_amount / 2.0
            row["cgst_rate"] = rate / 2.0
            row["sgst_rate"] = rate / 2.0
            row["cgst_amt"] = cgst
            row["sgst_amt"] = sgst
            row["igst_rate"] = 0.0
            row["igst_amt"] = 0.0
            grand_cgst += cgst
            grand_sgst += sgst
        else:
            row["cgst_rate"] = 0.0
            row["sgst_rate"] = 0.0
            row["cgst_amt"] = 0.0
            row["sgst_amt"] = 0.0
            row["igst_rate"] = rate
            row["igst_amt"] = tax_amount
            grand_igst += tax_amount

        row["line_total"] = taxable_value + tax_amount
        grand_subtotal += taxable_value
        enriched.append(row)

    totals = {
        "subtotal": grand_subtotal,
        "cgst": grand_cgst,
        "sgst": grand_sgst,
        "igst": grand_igst,
        "total_tax": grand_cgst + grand_sgst + grand_igst,
        "grand_total": grand_subtotal + grand_cgst + grand_sgst + grand_igst,
    }
    return enriched, totals, tax_type


def number_to_words_inr(amount: float) -> str:
    """Simple INR amount-in-words for invoice footer (Indian numbering)."""
    try:
        from num2words import num2words
        rupees = int(amount)
        paise = round((amount - rupees) * 100)
        words = num2words(rupees, lang="en_IN").replace(",", "").title()
        if paise:
            words += f" Rupees and {num2words(paise, lang='en_IN').title()} Paise"
        else:
            words += " Rupees"
        return words + " Only"
    except Exception:
        return f"INR {amount:,.2f} Only"


def generate_invoice_pdf(*, order: dict, store: dict, output_path: str) -> str:
    """
    Build a GST-compliant tax invoice PDF for a confirmed order.

    order: the order dict as stored in `orders[order_id]`, expected keys:
        id, customer_name, customer_email, customer_phone, address,
        delivery_city, delivery_state, delivery_pincode, items, total,
        created_at, razorpay_payment_id / payment_method
        items: list of {product_id, product_name, quantity, unit_price, subtotal}

    store: the store dict, expected keys:
        id, name, gstin, address (or business_address), email, owner

    output_path: full path to write the PDF to.

    Returns output_path.
    """
    # Pull per-product hsn_code / gst_rate from the order's line items
    # (snapshotted at purchase time — see create_razorpay_order below).
    items_for_tax = []
    for li in order["items"]:
        items_for_tax.append({
            "product_name": li["product_name"],
            "quantity": li["quantity"],
            "unit_price": li["unit_price"],
            "subtotal": li["subtotal"],
            "hsn_code": li.get("hsn_code", ""),
            "gst_rate": li.get("gst_rate", None),
        })

    enriched_items, totals, tax_type = compute_invoice_tax(
        items_for_tax,
        seller_gstin=store.get("gstin", ""),
        delivery_state=order.get("delivery_state", ""),
    )

    doc = SimpleDocTemplate(
        output_path, pagesize=A4,
        topMargin=14 * mm, bottomMargin=14 * mm,
        leftMargin=14 * mm, rightMargin=14 * mm,
    )
    styles = getSampleStyleSheet()
    story = []

    title_style = ParagraphStyle("InvTitle", parent=styles["Title"], fontSize=16,
                                 alignment=TA_CENTER, spaceAfter=2, fontName=_RUPEE_FONT_BOLD)
    small = ParagraphStyle("Small", parent=styles["Normal"], fontSize=8.5, leading=11,
                           fontName=_RUPEE_FONT_REGULAR)
    small_right = ParagraphStyle("SmallRight", parent=small, alignment=TA_RIGHT)
    h2 = ParagraphStyle("H2", parent=styles["Heading2"], fontSize=11, spaceAfter=4, fontName=_RUPEE_FONT_BOLD)

    is_registered = bool((store.get("gstin") or "").strip())

    story.append(Paragraph("TAX INVOICE" if is_registered else "INVOICE (Seller Not GST Registered)", title_style))
    story.append(Spacer(1, 4))

    # ── Seller / Invoice meta block ──────────────────────────────
    seller_lines = [f"<b>{store.get('name', 'Store')}</b>"]
    if store.get("address"):
        seller_lines.append(store["address"])
    if is_registered:
        seller_lines.append(f"GSTIN: {store['gstin']}")
    if store.get("email"):
        seller_lines.append(store["email"])
    seller_para = Paragraph("<br/>".join(seller_lines), small)

    invoice_no = f"INV-{order['id'].upper()}"
    created_at = order.get("created_at", "")
    try:
        date_str = datetime.fromisoformat(created_at).strftime("%d %b %Y")
    except Exception:
        date_str = created_at[:10] if created_at else datetime.now().strftime("%d %b %Y")

    meta_lines = [
        f"<b>Invoice No:</b> {invoice_no}",
        f"<b>Invoice Date:</b> {date_str}",
        f"<b>Order ID:</b> {order['id']}",
    ]
    if order.get("razorpay_payment_id"):
        meta_lines.append(f"<b>Payment Ref:</b> {order['razorpay_payment_id']}")
    meta_para = Paragraph("<br/>".join(meta_lines), small_right)

    header_table = Table([[seller_para, meta_para]], colWidths=[100 * mm, 80 * mm])
    header_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 10))

    # ── Bill To / Ship To ────────────────────────────────────────
    buyer_lines = [
        "<b>Bill To / Ship To:</b>",
        order.get("customer_name", ""),
    ]
    addr_bits = [order.get("address", "")]
    city_state_pin = ", ".join(filter(None, [
        order.get("delivery_city", ""), order.get("delivery_state", ""), order.get("delivery_pincode", "")
    ]))
    if city_state_pin:
        addr_bits.append(city_state_pin)
    buyer_lines.append(", ".join(filter(None, addr_bits)))
    if order.get("customer_phone"):
        buyer_lines.append(f"Phone: {order['customer_phone']}")
    if order.get("customer_email"):
        buyer_lines.append(order["customer_email"])

    story.append(Paragraph("<br/>".join(filter(None, buyer_lines)), small))
    story.append(Spacer(1, 4))

    place_of_supply = order.get("delivery_state", "") or "—"
    tax_type_label = {
        "CGST_SGST": "CGST + SGST",
        "IGST": "IGST",
        "UNREGISTERED": "No GST (Bill of Supply)",
    }.get(tax_type, tax_type)
    story.append(Paragraph(f"<b>Place of Supply:</b> {place_of_supply} &nbsp;&nbsp; "
                           f"<b>Tax Type:</b> {tax_type_label}", small))
    story.append(Spacer(1, 10))

    # ── Line items table ─────────────────────────────────────────
    rupee_label = _RUPEE_SYMBOL.strip() if _RUPEE_SYMBOL != "Rs. " else "Rs"
    if tax_type == "CGST_SGST":
        head = ["#", "Item", "HSN", "Qty", f"Rate ({rupee_label})", f"Taxable ({rupee_label})",
                "CGST %", f"CGST ({rupee_label})", "SGST %", f"SGST ({rupee_label})", f"Total ({rupee_label})"]
    elif tax_type == "IGST":
        head = ["#", "Item", "HSN", "Qty", f"Rate ({rupee_label})", f"Taxable ({rupee_label})",
                "IGST %", f"IGST ({rupee_label})", f"Total ({rupee_label})"]
    else:  # UNREGISTERED — no tax columns
        head = ["#", "Item", "HSN", "Qty", f"Rate ({rupee_label})", f"Amount ({rupee_label})"]

    rows = [head]
    for i, it in enumerate(enriched_items, start=1):
        if tax_type == "CGST_SGST":
            rows.append([
                str(i), it["product_name"], it["hsn_code"], str(it["quantity"]),
                f"{it['unit_price']:.2f}", f"{it['taxable_value']:.2f}",
                f"{it['cgst_rate']:.1f}%", f"{it['cgst_amt']:.2f}",
                f"{it['sgst_rate']:.1f}%", f"{it['sgst_amt']:.2f}",
                f"{it['line_total']:.2f}",
            ])
        elif tax_type == "IGST":
            rows.append([
                str(i), it["product_name"], it["hsn_code"], str(it["quantity"]),
                f"{it['unit_price']:.2f}", f"{it['taxable_value']:.2f}",
                f"{it['igst_rate']:.1f}%", f"{it['igst_amt']:.2f}",
                f"{it['line_total']:.2f}",
            ])
        else:  # UNREGISTERED
            rows.append([
                str(i), it["product_name"], it["hsn_code"], str(it["quantity"]),
                f"{it['unit_price']:.2f}", f"{it['line_total']:.2f}",
            ])

    if tax_type == "CGST_SGST":
        col_widths = [8 * mm, 42 * mm, 16 * mm, 10 * mm, 18 * mm, 20 * mm, 14 * mm, 16 * mm, 14 * mm, 16 * mm, 18 * mm]
    elif tax_type == "IGST":
        col_widths = [8 * mm, 55 * mm, 18 * mm, 10 * mm, 20 * mm, 22 * mm, 16 * mm, 18 * mm, 20 * mm]
    else:  # UNREGISTERED
        col_widths = [10 * mm, 80 * mm, 24 * mm, 14 * mm, 28 * mm, 28 * mm]

    item_table = Table(rows, colWidths=col_widths, repeatRows=1)
    item_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e293b")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("FONTNAME", (0, 0), (-1, -1), _RUPEE_FONT_REGULAR),
        ("FONTNAME", (0, 0), (-1, 0), _RUPEE_FONT_BOLD),
        ("ALIGN", (3, 0), (-1, -1), "RIGHT"),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(item_table)
    story.append(Spacer(1, 8))

    # ── Totals block ──────────────────────────────────────────────
    total_rows = [["Taxable Value", fmt_inr(totals['subtotal'])]]
    if tax_type == "CGST_SGST":
        total_rows.append(["CGST", fmt_inr(totals['cgst'])])
        total_rows.append(["SGST", fmt_inr(totals['sgst'])])
    elif tax_type == "IGST":
        total_rows.append(["IGST", fmt_inr(totals['igst'])])
    # UNREGISTERED: no tax rows at all — subtotal IS the grand total.
    total_rows.append(["Shipping", "Free"])
    total_rows.append(["Grand Total", fmt_inr(totals['grand_total'])])

    totals_table = Table(total_rows, colWidths=[40 * mm, 30 * mm])
    totals_table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("FONTNAME", (0, 0), (-1, -1), _RUPEE_FONT_REGULAR),
        ("FONTNAME", (0, -1), (-1, -1), _RUPEE_FONT_BOLD),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("LINEABOVE", (0, -1), (-1, -1), 0.8, colors.black),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))

    wrapper = Table([[Paragraph("", small), totals_table]], colWidths=[120 * mm, 70 * mm])
    wrapper.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(wrapper)
    story.append(Spacer(1, 10))

    story.append(Paragraph(f"<b>Amount in Words:</b> {number_to_words_inr(totals['grand_total'])}", small))
    story.append(Spacer(1, 10))

    if not is_registered:
        story.append(Paragraph(
            "Note: Seller is not registered under GST. This is a bill of supply, "
            "no GST has been charged on this order.", small
        ))
        story.append(Spacer(1, 6))

    story.append(Paragraph(
        "This is a system-generated invoice fro Brandmake.click and does not require a physical signature.",
        ParagraphStyle("Footer", parent=small, textColor=colors.HexColor("#888888"),
                       alignment=TA_LEFT, fontName=_RUPEE_FONT_REGULAR)
    ))

    doc.build(story)
    return output_path


def build_invoice_for_order(order: dict, store: dict, out_dir: str = "/tmp/invoices") -> str:
    """Convenience wrapper: builds the PDF and returns its file path."""
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"invoice_{order['id']}.pdf")
    # Unregistered seller still gets an invoice — just with zero GST (bill of supply)
    return generate_invoice_pdf(order=order, store=store, output_path=out_path)


def send_invoice_email(order: dict, store: dict, pdf_path: str):
    to_email = order.get("customer_email")
    if not to_email:
        return

    if not SMTP_USER or not SMTP_PASSWORD or not SMTP_HOST:
        print(f"[DEV] Would email invoice for order {order['id']} to {to_email} (SMTP not configured)")
        return

    from_addr = SMTP_FROM or SMTP_USER

    try:
        msg = MIMEMultipart()
        msg["Subject"] = f"Your invoice for order #{order['id']} — {store.get('name', 'Store')}"
        msg["From"]    = from_addr
        msg["To"]      = to_email

        body = (
            f"Hi {order.get('customer_name', '')},\n\n"
            f"Thank you for your order from {store.get('name', 'our store')}!\n"
            f"Your order #{order['id']} has been confirmed. Please find your "
            f"GST invoice attached.\n\n"
            f"Order total: Rs. {order['total']:.2f}\n\n"
            f"— {store.get('name', 'Store')}"
        )
        msg.attach(MIMEText(body, "plain"))

        with open(pdf_path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition",
            f"attachment; filename=invoice_{order['id']}.pdf",
        )
        msg.attach(part)

        with smtplib.SMTP('mail.privateemail.com', 587) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(from_addr, [to_email], msg.as_string())

        print(f"[invoice] Email sent to {to_email} for order {order['id']}")

    except Exception as e:
        print(f"[invoice] SMTP send failed for order {order['id']}: {e}")

    to_email = order.get("customer_email")
    if not to_email:
        return

    if not SMTP_USER or not SMTP_PASSWORD or not SMTP_HOST:
        print(f"[DEV] Would email invoice for order {order['id']} to {to_email} (SMTP not configured)")
        return

    from_addr = SMTP_FROM or SMTP_USER

    try:
        msg = MIMEMultipart()
        msg["Subject"] = f"Your invoice for order #{order['id']} — {store.get('name', 'Store')}"
        msg["From"]    = from_addr
        msg["To"]      = to_email

        body = (
            f"Hi {order.get('customer_name', '')},\n\n"
            f"Thank you for your order from {store.get('name', 'our store')}!\n"
            f"Your order #{order['id']} has been confirmed. Please find your "
            f"GST invoice attached.\n\n"
            f"Order total: Rs. {order['total']:.2f}\n\n"
            f"— {store.get('name', 'Store')}"
        )
        msg.attach(MIMEText(body, "plain"))

        with open(pdf_path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition",
            f"attachment; filename=invoice_{order['id']}.pdf",
        )
        msg.attach(part)

        with smtplib.SMTP('mail.privateemail.com', 587) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(from_addr, [to_email], msg.as_string())

        print(f"[invoice] Email sent to {to_email} for order {order['id']}")

    except Exception as e:
        print(f"[invoice] SMTP send failed for order {order['id']}: {e}")
        raise


# ─── Store Endpoints ─────────────────────────────────────────────
# ─── KYC Models ──────────────────────────────────────────────────
class GSTINRequest(BaseModel):
    gstin: str  # frontend sends lowercase "gstin"

    model_config = {"extra": "allow"}  # ignore any extra fields silently


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
    # if not RAPIDAPI_KEY:
    #     print(f"[DEV] Mock GSTIN lookup for: {gstin_val}")
    #     return {"gstin": gstin_val, "business_name": "SAMPLE BUSINESS PVT LTD", "status": "VALID"}

    # Live RapidAPI (GST Insights) call
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.get(
                f"https://gst-insights-api.p.rapidapi.com/getGSTStatus/{gstin_val}",
                headers={
                    "x-rapidapi-key": os.getenv("RAPID_KEY"),
                    "x-rapidapi-host": "gst-insights-api.p.rapidapi.com",
                    "Content-Type": "application/json",
                },
            )
            data = res.json()
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"RapidAPI unreachable: {str(e)}")

    print(f"[RapidAPI GSTIN] status={res.status_code} body={data}")

    if res.status_code != 200:
        msg = data.get("message") or data.get("error") or f"RapidAPI error {res.status_code}"
        raise HTTPException(status_code=400, detail=msg)

    gst_data = data.get("data") or data

    business_name = gst_data.get("legalName") or ""
    is_active = gst_data.get("isActive")
    cf_status = str(gst_data.get("status") or "").upper()

    if not business_name or (is_active is False) or cf_status == "INACTIVE":
        raise HTTPException(status_code=400, detail="GSTIN is invalid or not registered")

    return {"gstin": gstin_val, "business_name": business_name, "status": "VALID"}


def _decode_cloudflare_email(hex_string: str) -> str:
    try:
        xor_key = int(hex_string[:2], 16)
        return "".join(
            chr(int(hex_string[i:i + 2], 16) ^ xor_key)
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

class Store(BaseModel):
    name: str
    description: Optional[str] = ""
    owner: Optional[str] = ""
    gstin: Optional[str] = ""  # store's GSTIN, set during KYC — used for GST invoices
    address: Optional[str] = ""  # registered business address, shown on invoice


class StoreUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    owner: Optional[str] = None
    gstin: Optional[str] = None
    address: Optional[str] = None


@app.post("/stores")
def create_store(store: Store):
    store_id = str(uuid.uuid4())[:8]
    stores[store_id] = {
        "id": store_id,
        "name": store.name,
        "description": store.description,
        "owner": store.owner,
        "gstin": store.gstin,
        "address": store.address,
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
    gst_rate: Optional[float] = 18.0  # GST % for this product (5/12/18/28 etc.)


class ProductUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    price: Optional[float] = None
    stock: Optional[int] = None
    category: Optional[str] = None
    image_url: Optional[str] = None
    images: Optional[List[str]] = None
    hsn_code: Optional[str] = None
    gst_rate: Optional[float] = None


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
            "hsn_code": product.get("hsn_code", ""),
            "gst_rate": product.get("gst_rate", 18.0),
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
    store_orders = [o for o in orders.values() if o["store_id"] == store_id]
    store_products = [p for p in products.values() if p["store_id"] == store_id]
    store_reviews = [r for r in reviews.values() if r["store_id"] == store_id]
    total_revenue = sum(o["total"] for o in store_orders)
    avg_rating = (sum(r["rating"] for r in store_reviews) / len(store_reviews)) if store_reviews else 0
    return {
        "total_orders": len(store_orders),
        "total_revenue": round(total_revenue, 2),
        "total_products": len(store_products),
        "total_reviews": len(store_reviews),
        "average_rating": round(avg_rating, 2),
        "recent_orders": store_orders[-5:][::-1],
    }


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
    delivery_city: Optional[str] = ""
    delivery_state: Optional[str] = ""  # needed to decide CGST/SGST vs IGST
    delivery_pincode: Optional[str] = ""
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
        "key_id": payload.key_id,
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

    order_id = str(uuid.uuid4())[:8]
    total = 0.0
    line_items = []
    for item in req.items:
        p = products.get(item.product_id)
        if not p:
            raise HTTPException(status_code=404, detail=f"Product {item.product_id} not found")
        if p["stock"] < item.quantity:
            raise HTTPException(status_code=400, detail=f"Insufficient stock for {p['name']}")
        subtotal = p["price"] * item.quantity
        total += subtotal
        products[item.product_id]["stock"] -= item.quantity
        line_items.append({
            "product_id": item.product_id,
            "product_name": p["name"],
            "quantity": item.quantity,
            "unit_price": p["price"],
            "subtotal": subtotal,
            "hsn_code": p.get("hsn_code", ""),  # snapshot at purchase time
            "gst_rate": p.get("gst_rate", 18.0),  # snapshot at purchase time
        })

    orders[order_id] = {
        "id": order_id,
        "store_id": req.store_id,
        "customer_name": req.customer_name,
        "customer_email": req.customer_email,
        "customer_phone": req.customer_phone,
        "address": req.address,
        "delivery_city": req.delivery_city,
        "delivery_state": req.delivery_state,
        "delivery_pincode": req.delivery_pincode,
        "items": line_items,
        "total": round(total, 2),
        "status": "confirmed",
        "payment_method": "razorpay",
        "razorpay_payment_id": req.payment_id,
        "created_at": datetime.now().isoformat(),
    }

    # ── Generate the GST invoice and email it to the customer ──────
    # Wrapped in try/except so a PDF or SMTP failure never blocks the
    # order confirmation itself — the customer can always re-download
    # the invoice later via GET /orders/{order_id}/invoice, which
    # regenerates it fresh on demand.
    try:
        store = stores[req.store_id]
        pdf_path = build_invoice_for_order(orders[order_id], store)
        orders[order_id]["invoice_path"] = pdf_path
        send_invoice_email(orders[order_id], store, pdf_path)
    except Exception as e:
        print(f"[invoice] Failed to generate/send invoice for order {order_id}: {e}")

    return orders[order_id]


@app.get("/orders/{order_id}/invoice")
def download_invoice(order_id: str):
    """On-demand GST invoice download — also used by the 'Download
    Invoice' button on the Cart success screen in the frontend."""
    if order_id not in orders:
        raise HTTPException(status_code=404, detail="Order not found")
    order = orders[order_id]
    store = stores.get(order["store_id"])
    if not store:
        raise HTTPException(status_code=404, detail="Store not found")

    pdf_path = build_invoice_for_order(order, store)
    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        filename=f"invoice_{order_id}.pdf",
    )


from fastapi.exceptions import RequestValidationError


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    body = await request.body()
    print(f"[422] Path: {request.url.path} | Body: {body.decode()} | Errors: {exc.errors()}")
    return JSONResponse(
        status_code=422,
        content={"detail": exc.errors(), "body_received": body.decode()},
    )


# ── In-memory store (replace with your DB layer) ──────────────────────────────
chatbot_configs: dict = {}
razorpay_keys: dict = {}
return_complaints: dict = {}



# ═══════════════════════════════════════════════════════════════════════════════
# Schemas
# ═══════════════════════════════════════════════════════════════════════════════

class FAQ(BaseModel):
    question: str
    answer:   str

class ChatbotConfig(BaseModel):
    bot_name: str       = "Store Assistant"
    greeting: str       = "Hi! How can I help you today?"
    faqs:     List[FAQ] = []
    linked_store_id: Optional[str] = ""   # product/ecommerce store this chatbot sells from

class ChatRequest(BaseModel):
    store_id: Optional[str] = ""
    message:  str

    @field_validator("message")
    @classmethod
    def message_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("message cannot be empty")
        return v.strip()

class RefundRequest(BaseModel):
    store_id: str
    order_id: str
    amount:   float
    reason:   Optional[str] = "Customer requested refund"

# REPLACE ReturnRequests:
class ReturnRequest(BaseModel):
    store_id:      str
    order_id:      str
    reason:        Optional[str] = ""
    image_url:     Optional[str] = ""       # ✅ URL instead of base64
    image_caption: Optional[str] = ""
    image_base64:  Optional[str] = ""       # kept for backwards compat

# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def allow_self_signed_https():
    if not os.environ.get("PYTHONHTTPSVERIFY") and getattr(ssl, "_create_unverified_context", None):
        ssl._create_default_https_context = ssl._create_unverified_context


def get_phi4_client() -> Optional[ChatCompletionsClient]:
    if not AZURE_API_URL or not AZURE_API_KEY:
        print("[Phi-4] Missing API_URL or API_KEY environment variables.")
        return None
    try:
        return ChatCompletionsClient(
            endpoint=AZURE_API_URL,
            credential=AzureKeyCredential(AZURE_API_KEY),
        )
    except Exception as e:
        print(f"[Phi-4] Client init error: {e}")
        return None


def phi4_complete(prompt: str, system: str = "") -> str:
    """Call Phi-4 via Azure AI Inference SDK."""
    client = get_phi4_client()
    if not client:
        return "I'm having trouble connecting to my AI backend. Please try again later."
    try:
        msgs = []
        if system:
            msgs.append(SystemMessage(content=system))
        msgs.append(UserMessage(content=prompt))
        response = client.complete(
            messages=msgs,
            model=AZURE_MODEL,
            max_tokens=512,
            temperature=0.4,
            top_p=1,
            presence_penalty=0.0,
            frequency_penalty=0.0,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"AI error: {str(e)}"


# ── Stopwords excluded from overlap scoring to avoid false FAQ matches ─────────
STOPWORDS = {
    "what", "is", "are", "the", "a", "an", "your", "my", "do", "does",
    "how", "when", "where", "which", "who", "can", "i", "you", "we",
    "it", "this", "that", "for", "of", "to", "in", "on", "at", "be",
    "will", "have", "has", "about", "and", "or", "not", "no", "yes",
    "me", "us", "them", "they", "he", "she", "its", "our", "their",
    "from", "with", "by", "please", "tell", "get", "give", "want",
}


def faq_match(faqs: List[FAQ], question: str) -> Optional[str]:
    """
    Match a customer question to the closest FAQ entry.

    Uses meaningful-word overlap (stopwords excluded) combined with
    fuzzy sequence matching. Requires a combined score >= 0.5 to
    avoid false positives from common words like 'what is your'.
    """
    if not faqs:
        return None

    q_lower  = question.lower()
    q_words  = set(re.findall(r"\w+", q_lower)) - STOPWORDS

    if not q_words:
        return None

    best_score  = 0.0
    best_answer = None

    for faq in faqs:
        faq_lower = faq.question.lower()
        faq_words = set(re.findall(r"\w+", faq_lower)) - STOPWORDS

        if not faq_words:
            continue

        overlap_count = len(faq_words & q_words)
        word_score    = overlap_count / max(len(faq_words), 1)

        seq_score = difflib.SequenceMatcher(None, q_lower, faq_lower).ratio()

        combined = seq_score * 0.3 + word_score * 0.7

        if combined > best_score:
            best_score  = combined
            best_answer = faq.answer

    return best_answer if best_score >= 0.5 else None


def detect_defects(caption: str) -> dict:
    """Classify caption for defect keywords."""
    DEFECT_KEYWORDS = [
        "damage", "damaged", "crack", "cracked", "broken", "defect", "defective",
        "scratch", "scratched", "tear", "torn", "worn", "stain", "stained",
        "missing", "dent", "dented", "fault", "faulty", "chip", "chipped",
        "discolor", "discoloured", "rust", "rusted", "bend", "bent",
    ]
    caption_lower = caption.lower()
    found = [kw for kw in DEFECT_KEYWORDS if kw in caption_lower]
    return {
        "defects_detected":    len(found) > 0,
        "keywords_found":      found,
        "eligible_for_return": len(found) > 0,
    }


# def get_razorpay_client(store_id: str):
#     creds = razorpay_keys.get(store_id)
#     if creds:
#         return razorpay.Client(auth=(creds["key_id"], creds["key_secret"]))
#     if RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET:
#         return razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
#     return None

# ═══════════════════════════════════════════════════════════════════════════════
# Chatbot Config Endpoints
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/chatbot/config/{store_id}")
async def get_chatbot_config(store_id: str):
    """Return the chatbot configuration for a store."""
    return chatbot_configs.get(store_id, {
        "bot_name": "Store Assistant",
        "greeting": "Hi! How can I help you today?",
        "faqs":     [],
        "linked_store_id": "",
    })


@app.put("/chatbot/config/{store_id}")
async def put_chatbot_config(store_id: str, config: ChatbotConfig):
    """Save / overwrite chatbot configuration for a store."""
    chatbot_configs[store_id] = config.model_dump()
    return {"success": True, "message": "Chatbot configuration saved."}

# ═══════════════════════════════════════════════════════════════════════════════
# Chat Endpoint — FAQ RAG + Phi-4 fallback
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/chatbot/chat")
async def chatbot_chat(req: ChatRequest):
    """
    1. Try to match against store FAQ knowledge base (strict stopword-aware matching).
    2. If no FAQ match, call Phi-4 with the full FAQ context so it can reason
       over the knowledge base and give a helpful, grounded answer.
    """
    cfg  = chatbot_configs.get(req.store_id, {})
    faqs: List[FAQ] = [FAQ(**f) for f in cfg.get("faqs", [])]

    # ── Step 1: Direct FAQ match (fast path) ──────────────────────────────────
    faq_answer = faq_match(faqs, req.message)
    if faq_answer:
        return {"reply": faq_answer, "source": "faq"}

    # ── Step 2: Phi-4 with FAQ knowledge base as context ──────────────────────
    faq_context = "\n".join(
        f"Q: {f.question}\nA: {f.answer}" for f in faqs
    ) if faqs else "No FAQ data available."

    system_prompt = f"""You are {cfg.get('bot_name', 'Store Assistant')}, a helpful and friendly customer support chatbot for this store.

You have access to the store's FAQ Knowledge Base below. Use it as your primary source of truth when answering customer questions.

GUIDELINES:
1. Read the FAQ Knowledge Base carefully. If the customer's question is covered — even partially or by implication — answer using that information. You may rephrase it naturally and concisely.
2. If the FAQ contains related or adjacent information that helps answer the question, use it to give a helpful response.
3. If the FAQ genuinely does not contain any relevant information, respond with: "I don't have specific information on that. Please contact our support team for further assistance."
4. Do NOT invent store-specific details (prices, policies, products, timelines) that are not present in the FAQ.
5. Keep responses short, clear, and friendly. No unnecessary filler or greetings.

FAQ Knowledge Base:
{faq_context}"""

    reply = phi4_complete(req.message, system=system_prompt)
    return {"reply": reply, "source": "ai"}

# ═══════════════════════════════════════════════════════════════════════════════
# Razorpay Refund Endpoint
# ═══════════════════════════════════════════════════════════════════════════════

# @app.post("/chatbot/refund")
# async def initiate_refund(req: RefundRequest):
#     """
#     Initiate a Razorpay refund for a given payment ID.
#     Pass the Razorpay payment_id (pay_XXXX) in the order_id field.
#     If an order_id (order_XXXX) is passed, we look up the captured payment first.
#     Amount is in INR — converted to paise internally.
#     """
#     rzp = get_razorpay_client(req.store_id)
#     if not rzp:
#         raise HTTPException(
#             status_code=503,
#             detail="Razorpay not configured for this store. Please add your API credentials in the Payments tab.",
#         )
#
#     payment_id = req.order_id.strip()
#
#     if not payment_id.startswith("pay_"):
#         if payment_id.startswith("order_"):
#             try:
#                 payments = rzp.order.payments(payment_id)
#                 items    = payments.get("items", [])
#                 if not items:
#                     raise HTTPException(status_code=404, detail="No payments found for this order ID.")
#                 captured = [p for p in items if p.get("status") == "captured"]
#                 if not captured:
#                     raise HTTPException(
#                         status_code=400,
#                         detail="No captured payment found for this order. Refund cannot be processed.",
#                     )
#                 payment_id = captured[0]["id"]
#             except HTTPException:
#                 raise HTTPException(status_code=400, detail="Invalid payment ID. Must start with 'pay_' or 'order_'.")
#             except Exception as e:
#                 raise HTTPException(status_code=400, detail=f"Could not resolve order to payment: {str(e)}")
#         else:
#             raise HTTPException(status_code=400, detail="Invalid payment ID. Must start with 'pay_' or 'order_'.")
#
#     amount_paise = int(req.amount * 100)
#     if amount_paise <= 0:
#         raise HTTPException(status_code=400, detail="Refund amount must be greater than 0.")
#
#     try:
#         refund = rzp.payment.refund(payment_id, {
#             "amount": amount_paise,
#             "speed":  "optimum",
#             "notes": {
#                 "reason":   req.reason or "Customer requested refund",
#                 "store_id": req.store_id,
#             },
#         })
#         return {
#             "success":    True,
#             "refund_id":  refund.get("id"),
#             "payment_id": refund.get("payment_id"),
#             "amount":     refund.get("amount", amount_paise) / 100,
#             "status":     refund.get("status"),
#             "speed":      refund.get("speed_processed"),
#             "message":    f"Refund of ₹{req.amount:.2f} initiated successfully.",
#         }
#     except razorpay.errors.BadRequestError as e:
#         raise HTTPException(status_code=400, detail=f"Razorpay error: {str(e)}")
#     except Exception as e:
#         raise HTTPException(status_code=500, detail=f"Refund failed: {str(e)}")

# ═══════════════════════════════════════════════════════════════════════════════
# Product Return + Image Verification Endpoint
# ═══════════════════════════════════════════════════════════════════════════════

MODELSLAB_KEY = os.environ.get("VID_KEY")


async def get_caption_from_modelslab(image_url: str) -> Optional[str]:
    if not MODELSLAB_KEY:
        print("[ModelsLab] ❌ VID_KEY not set")
        return None
    if not image_url:
        print("[ModelsLab] ❌ No image URL provided")
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.post(
                "https://modelslab.com/api/v6/image_editing/caption",
                json={"init_image": image_url, "length": "short", "key": MODELSLAB_KEY},
            )
            data = res.json()
            print(f"[ModelsLab] Raw response: {data}")

            # output is a URL to a JSON file — fetch it
            output = data.get("output")
            if isinstance(output, list) and output:
                json_url = output[0]
                json_res = await client.get(json_url)
                caption_data = json_res.json()
                print(f"[ModelsLab] Caption JSON: {caption_data}")
                if isinstance(caption_data, list) and caption_data:
                    caption = caption_data[0].get("caption") or caption_data[0].get("output") or str(caption_data[0])
                else:
                    caption = caption_data.get("caption") or caption_data.get("output") or str(caption_data)
                return caption
            elif isinstance(output, str):
                return output

            return data.get("caption")
    except Exception as e:
        print(f"[ModelsLab] Error: {e}")
        return None



def _resolve_order(store_id: str, raw_order_id: str):
    """
    Customer may type: internal UUID (92ce16db), Razorpay payment ID (pay_xxx),
    or a partial/wrong string. Try all resolution strategies.
    """
    raw = raw_order_id.strip()

    # 1. Direct key match
    o = orders.get(raw)
    if o and o.get("store_id") == store_id:
        return o

    # 2. Match by razorpay_payment_id
    o = next((v for v in orders.values()
              if v.get("store_id") == store_id
              and v.get("razorpay_payment_id") == raw), None)
    if o:
        return o

    # 3. Match by id field (in case key differs)
    o = next((v for v in orders.values()
              if v.get("store_id") == store_id
              and v.get("id") == raw), None)
    if o:
        return o

    # 4. Case-insensitive partial match on id
    o = next((v for v in orders.values()
              if v.get("store_id") == store_id
              and raw.lower() in v.get("id", "").lower()), None)
    return o


@app.post("/chatbot/return")
async def submit_return(req: ReturnRequest):
    caption = req.image_caption or ""
    print(f"[Return] image_url: '{req.image_url}'")

    if not caption and req.image_url:
        caption = await get_caption_from_modelslab(req.image_url) or ""
        print(f"[Return] caption after ModelsLab: '{caption}'")

    defect_analysis = detect_defects(caption)
    print(f"[Return] defect_analysis: {defect_analysis}")

    if caption:
        verdict_prompt = (
            f"A customer wants to return a product from order {req.order_id}.\n"
            f"Reason given: {req.reason or 'Not specified'}\n"
            f"AI image analysis of the product photo: {caption}\n"
            f"Defect keywords found: {', '.join(defect_analysis['keywords_found']) or 'none'}\n\n"
            "Based on this, write a SHORT (2 sentences max) customer-facing message:\n"
            "- If defects are present: confirm the return is approved and mention next steps.\n"
            "- If no defects: inform the customer the item appears undamaged; flag for manual review.\n"
            "Be empathetic and professional."
        )
        verdict_message = phi4_complete(verdict_prompt)
    else:
        verdict_message = (
            "No visible damage or defects were found in your product photo. Your return request has been rejected."
            if not defect_analysis["defects_detected"]
            else "Defects detected in your product image. Your return request has been approved. Please await pickup instructions."
        )

    # Resolve actual order
    resolved_order = _resolve_order(req.store_id, req.order_id)
    actual_order_id = resolved_order["id"] if resolved_order else req.order_id
    razorpay_payment_id = resolved_order.get("razorpay_payment_id", "") if resolved_order else ""

    # ── Persist the complaint so admin can view it ─────────────────────────────
    import time
    complaint = {
        "store_id":         req.store_id,
        "reason":           req.reason or "",
        "image_url":        req.image_url or "",
        "image_caption":    caption,
        "order_id":         actual_order_id,  # ← real internal UUID, not what customer typed
        "razorpay_payment_id": razorpay_payment_id,
        "customer_typed_order_id": req.order_id,  # ← preserve what they typed for reference
        "defects_detected": defect_analysis["defects_detected"],
        "defect_keywords":  defect_analysis["keywords_found"],
        "eligible":         defect_analysis["eligible_for_return"],
        "status":           "approved" if defect_analysis["eligible_for_return"] else "manual_review",
        "verdict_message":  verdict_message,
        "submitted_at":     time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    return_complaints.setdefault(req.store_id, []).append(complaint)

    return {
        "success":          True,
        "order_id":         req.order_id,
        "eligible":         defect_analysis["eligible_for_return"],
        "defects_detected": defect_analysis["defects_detected"],
        "defect_keywords":  defect_analysis["keywords_found"],
        "image_caption":    caption,
        "message":          verdict_message,
        "status":           complaint["status"],
    }


# ── Add this NEW endpoint right after the one above ──────────────────────────

@app.get("/chatbot/returns/{store_id}")
async def get_return_complaints(store_id: str):
    """Return all submitted return complaints for a store, newest first."""
    complaints = return_complaints.get(store_id, [])
    return {"complaints": list(reversed(complaints))}


# ═══════════════════════════════════════════════════════════════════════════════
# Razorpay per-store key management
# ═══════════════════════════════════════════════════════════════════════════════

class RazorpayKeyPayload(BaseModel):
    key_id:     str
    key_secret: Optional[str] = ""


@app.get("/razorpay/connection/{store_id}")
async def get_razorpay_connection(store_id: str):
    creds = razorpay_keys.get(store_id)
    if creds:
        return {"connected": True, "key_id": creds["key_id"]}
    return {"connected": False}


# @app.put("/razorpay/connection/{store_id}")
# async def put_razorpay_connection(store_id: str, payload: RazorpayKeyPayload):
#     razorpay_keys[store_id] = {
#         "key_id":     payload.key_id,
#         "key_secret": payload.key_secret or RAZORPAY_KEY_SECRET,
#     }
#     return {"success": True, "key_id": payload.key_id}


@app.delete("/razorpay/connection/{store_id}")
async def delete_razorpay_connection(store_id: str):
    razorpay_keys.pop(store_id, None)
    return {"success": True}


# ═══════════════════════════════════════════════════════════════════════════════
# Skin Analysis
# ═══════════════════════════════════════════════════════════════════════════════


# Handle different OS paths
plt = platform.system()
if plt == "Linux":
    pathlib.WindowsPath = pathlib.PosixPath

learn = None
labels = None

def load_model():
    global learn, labels
    if learn is None:
        from fastai.vision.all import load_learner
        learn = load_learner("./export_new.pkl")
        labels = learn.dls.vocab


LABEL_DISPLAY_NAMES = {
    "BLACKHEADS": "Blackheads",
    "DARK CIRCLES ON FACE": "Dark Circles",
    "DARK SPOTS ON FACE": "Pigmentation",
    "DRY SKIN": "Dryness",
    "DULL SKIN": "Dullness",
    "EYE BAGS": "Eye Bags",
    "FACE REDNESS": "Redness",
    "FOREHEAD WRINKLES": "Wrinkles",
    "HORMONAL ACNE": "Acne",
    "LARGE PORES ON FACE": "Large Pores",
    "OILY SKIN": "Oily Skin",
    "RAZOR BUMPS": "Razor Bumps",
    "ROUGH TEXTURE ON FACE": "Rough Texture",
    "SEBACEOUS FILAMENTS": "Clogged Pores",
    "UNDER-EYE WRINKLES": "Fine Lines",
}


@app.post("/predict")
async def predict(file: UploadFile = File(...)):
    try:
        load_model()  # <-- add this line, ensures model is loaded before use

        from fastai.vision.all import PILImage

        contents = await file.read()
        img_bytes = io.BytesIO(contents)
        img = PILImage.create(img_bytes)

        pred, pred_idx, probs = learn.predict(img)

        results = {
            LABEL_DISPLAY_NAMES.get(str(labels[i]), str(labels[i])): float(probs[i])
            for i in range(len(labels))
        }
        sorted_results = sorted(results.items(), key=lambda x: x[1], reverse=True)

        return JSONResponse({
            "top_predictions": [
                {"label": label, "probability": round(prob, 4)}
                for label, prob in sorted_results[:5]
            ],
            "all_predictions": {k: round(v, 4) for k, v in results.items()}
        })

    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


SHIPROCKET_EMAIL = os.getenv("SHIPROCKET_EMAIL")
SHIPROCKET_PASSWORD = os.getenv("SHIPROCKET_PASSWORD")
SHIPROCKET_BASE = "https://apiv2.shiprocket.in/v1/external"

# In-memory: shiprocket_tokens[store_id] = { token, expires_at }
shiprocket_tokens = {}
# In-memory: shiprocket_connections[store_id] = { email, password, pickup_location, ... }
shiprocket_connections = {}
# In-memory: shipments[order_id] = { sr_order_id, awb, courier, status, tracking_url, ... }
shipments = {}
# In-memory: return_shipments[return_id] = { ... }
return_shipments = {}


# ─── Models ──────────────────────────────────────────────────────

class ShiprocketConnect(BaseModel):
    email: str
    password: str


class ShipOrderRequest(BaseModel):
    order_id: str  # your internal order id
    pickup_postcode: str
    pickup_address: str
    pickup_city: str
    pickup_state: str
    pickup_country: str = "India"
    pickup_name: str  # seller/pickup contact name
    pickup_phone: str
    weight: float  # kg
    length: float  # cm
    breadth: float  # cm
    height: float  # cm
    courier_id: Optional[int] = None  # if None → auto-select cheapest


class UpdateAddressRequest(BaseModel):
    order_id: str
    new_address: str
    new_city: str
    new_state: str
    new_pincode: str
    new_country: str = "India"


class ReturnRequest(BaseModel):
    order_id: str  # your internal order id
    reason: str = "Customer return request"
    pickup_postcode: str  # customer's pincode (pickup FROM customer)
    pickup_address: str
    pickup_city: str
    pickup_state: str
    pickup_name: str  # customer name
    pickup_phone: str
    weight: float
    length: float
    breadth: float
    height: float


# ─── Helpers ─────────────────────────────────────────────────────

async def _get_sr_token(store_id: str) -> str:
    """Get or refresh Shiprocket JWT for this store."""
    conn = shiprocket_connections.get(store_id)
    if not conn:
        raise HTTPException(status_code=400, detail="Shiprocket not connected for this store")

    cached = shiprocket_tokens.get(store_id)
    if cached:
        from datetime import timezone
        expiry = datetime.fromisoformat(cached["expires_at"])
        if datetime.now() < expiry:
            return cached["token"]

    async with httpx.AsyncClient(timeout=20) as client:
        res = await client.post(
            f"{SHIPROCKET_BASE}/auth/login",
            json={"email": conn["email"], "password": conn["password"]},
        )
        data = res.json()

    token = data.get("token")
    if not token:
        raise HTTPException(status_code=401, detail=f"Shiprocket auth failed: {data.get('message', 'unknown')}")

    # Token valid for ~24h; cache for 23h
    from datetime import timedelta
    shiprocket_tokens[store_id] = {
        "token": token,
        "expires_at": (datetime.now() + timedelta(hours=23)).isoformat(),
    }
    return token


async def _sr_get(store_id: str, path: str, params: dict = None):
    token = await _get_sr_token(store_id)
    async with httpx.AsyncClient(timeout=20) as client:
        res = await client.get(
            f"{SHIPROCKET_BASE}{path}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            params=params or {},
        )
        return res.status_code, res.json()


async def _sr_post(store_id: str, path: str, payload: dict):
    token = await _get_sr_token(store_id)
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.post(
            f"{SHIPROCKET_BASE}{path}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
        )
        return res.status_code, res.json()


def _send_tracking_email(customer_email: str, customer_name: str, order_id: str,
                         awb: str, courier: str, tracking_url: str):
    """Non-fatal: logs in dev if no SMTP configured."""
    if not SMTP_USER or not SMTP_PASSWORD:
        print(f"[DEV] Tracking email → {customer_email}")
        print(f"      Order: {order_id} | AWB: {awb} | Courier: {courier}")
        print(f"      Track: {tracking_url}")
        return

    try:
        body = (
            f"Hi {customer_name},\n\n"
            f"Your order #{order_id} has been shipped!\n\n"
            f"Courier  : {courier}\n"
            f"AWB No.  : {awb}\n"
            f"Track    : {tracking_url}\n\n"
            f"Thank you for shopping with us!\n"
        )
        msg = MIMEText(body)
        msg["Subject"] = f"Your Order #{order_id} Has Been Shipped 🚚"
        msg["From"] = SMTP_FROM
        msg["To"] = customer_email
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo();
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM, [customer_email], msg.as_string())
    except Exception as e:
        print(f"[WARN] Could not send tracking email: {e}")


# ═══════════════════════════════════════════════════════════════════
#  SHIPROCKET ENDPOINTS
# ═══════════════════════════════════════════════════════════════════

# ── 1. Connect Shiprocket account for a store ─────────────────────
@app.post("/shiprocket/connect/{store_id}")
async def shiprocket_connect(store_id: str, req: ShiprocketConnect):
    if store_id not in stores:
        raise HTTPException(status_code=404, detail="Store not found")
    shiprocket_connections[store_id] = {
        "email": req.email,
        "password": req.password,
        "connected_at": datetime.now().isoformat(),
    }
    # Validate credentials immediately
    await _get_sr_token(store_id)
    return {"connected": True, "email": req.email}


@app.get("/shiprocket/connection/{store_id}")
async def shiprocket_get_connection(store_id: str):
    print(f"[SR] SHIPROCKET_EMAIL={SHIPROCKET_EMAIL!r}")
    print(f"[SR] SHIPROCKET_PASSWORD={SHIPROCKET_PASSWORD!r}")
    print(f"[SR] store_id={store_id!r}")
    print(f"[SR] already in connections: {store_id in shiprocket_connections}")

    if store_id not in shiprocket_connections:
        if SHIPROCKET_EMAIL and SHIPROCKET_PASSWORD:
            shiprocket_connections[store_id] = {
                "email": SHIPROCKET_EMAIL,
                "password": SHIPROCKET_PASSWORD,
                "connected_at": datetime.now().isoformat(),
            }
            try:
                await _get_sr_token(store_id)
                print(f"[SR] token fetch SUCCESS")
            except Exception as e:
                print(f"[SR] token fetch FAILED: {e}")
                shiprocket_connections.pop(store_id, None)
                return {"connected": False}
        else:
            print(f"[SR] env vars missing, returning not connected")
            return {"connected": False}

    conn = shiprocket_connections[store_id]
    print(f"[SR] returning connected=True email={conn['email']!r}")
    return {"connected": True, "email": conn["email"], "connected_at": conn["connected_at"]}


@app.delete("/shiprocket/connection/{store_id}")
def shiprocket_disconnect(store_id: str):
    shiprocket_connections.pop(store_id, None)
    shiprocket_tokens.pop(store_id, None)
    return {"disconnected": True}


# ── 2. List pickup addresses saved in Shiprocket ──────────────────
@app.get("/shiprocket/pickup-addresses/{store_id}")
async def list_pickup_addresses(store_id: str):
    status, data = await _sr_get(store_id, "/settings/company/pickup")
    if status != 200:
        raise HTTPException(status_code=502, detail=data.get("message", "Failed to fetch pickup addresses"))
    return data.get("data", {}).get("shipping_address", [])


# ── 3. Get courier serviceability & rates (sorted cheapest first) ─
@app.get("/shiprocket/couriers/{store_id}")
async def get_couriers(
        store_id: str,
        pickup_postcode: str,
        delivery_postcode: str,
        weight: float,
        cod: int = 0,
        cod_amount: float = 0,
):
    status, data = await _sr_get(store_id, "/courier/serviceability/", params={
        "pickup_postcode": pickup_postcode,
        "delivery_postcode": delivery_postcode,
        "weight": weight,
        "cod": cod,
        "cod_amount": cod_amount if cod else 0,
    })
    if status != 200:
        raise HTTPException(status_code=502, detail=data.get("message", "Failed to fetch couriers"))

    available = data.get("data", {}).get("available_courier_companies", [])
    # Sort by rate ascending (cheapest first)
    available.sort(key=lambda x: float(x.get("rate", 9999)))
    return {"couriers": available}


class PickupAddressRequest(BaseModel):
    pickup_location: str      # the label, e.g. "Primary"
    name: str
    email: str
    phone: str
    address: str
    address_2: Optional[str] = ""
    city: str
    state: str
    country: str = "India"
    pin_code: str


@app.post("/shiprocket/pickup-address/{store_id}")
async def create_pickup_address(store_id: str, req: PickupAddressRequest):
    sr_status, data = await _sr_post(store_id, "/settings/company/addpickup", req.dict())
    print(f"[Shiprocket] create pickup: status={sr_status} data={data}")
    if sr_status not in (200, 201):
        raise HTTPException(status_code=502, detail=data.get("message", "Failed to create pickup address"))
    return data


# ── 4. Create shipment & assign courier ───────────────────────────
@app.post("/shiprocket/ship/{store_id}")
async def ship_order(store_id: str, req: ShipOrderRequest):
    print(f"[Shiprocket] ship_order called store_id={store_id} req={req}")  # ← add this
    order = orders.get(req.order_id)
    print(f"[Shiprocket] order lookup: {order}")  # ← add this
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order["store_id"] != store_id:
        print(f"[Shiprocket] store mismatch: order.store_id={order['store_id']} != store_id={store_id}")  # ← add this
        raise HTTPException(status_code=403, detail="Order does not belong to this store")

    # ── Fetch real pickup location name from Shiprocket ──
    sr_status_pl, pl_data = await _sr_get(store_id, "/settings/company/pickup")
    print(f"[Shiprocket] pickup locations: {pl_data}")

    pickup_location_name = None
    if sr_status_pl == 200:
        addresses = pl_data.get("data", {}).get("shipping_address", [])
        if addresses:
            # Use first active pickup location
            active = next((a for a in addresses if a.get("is_primary_location") == 1), None)
            pickup_location_name = (active or addresses[0]).get("pickup_location", "")

    if not pickup_location_name:
        raise HTTPException(status_code=400,
                            detail="No pickup location found in your Shiprocket account. Add one at Settings → Manage Pickup Addresses.")

    print(f"[Shiprocket] using pickup_location='{pickup_location_name}'")

    # --- Step 1: Create Shiprocket order ---
    # resolve delivery address fields with fallbacks
    delivery_address = order.get("address") or req.pickup_address
    delivery_city = order.get("delivery_city") or req.pickup_city
    delivery_pincode = order.get("delivery_pincode") or req.pickup_postcode
    delivery_state = order.get("delivery_state") or req.pickup_state

    billing_phone_raw = re.sub(r"\D", "", order.get("customer_phone") or "")
    billing_phone = billing_phone_raw[:10] if len(billing_phone_raw) >= 10 else None

    if not billing_phone:
        raise HTTPException(status_code=400, detail="Customer phone number is missing on this order. Cannot ship without a valid 10-digit phone.")


    sr_order_payload = {
        "order_id": req.order_id,
        "order_date": order["created_at"][:10],
        "pickup_location": pickup_location_name,
        "billing_customer_name": order["customer_name"],
        "billing_last_name": "",
        "billing_address": delivery_address,
        "billing_address_2": "",  # required key, even if empty string
        "billing_city": delivery_city,
        "billing_pincode": delivery_pincode,
        "billing_state": delivery_state,
        "billing_country": "India",
        "billing_email": order["customer_email"],
        "billing_phone": billing_phone,
        "shipping_is_billing": True,
        "order_items": [
            {
                "name": item["product_name"],
                "sku": item["product_id"],
                "units": item["quantity"],
                "selling_price": item["unit_price"],
            }
            for item in order["items"]
        ],
        "payment_method": "Prepaid",
        "sub_total": order["total"],
        "length": req.length,
        "breadth": req.breadth,
        "height": req.height,
        "weight": req.weight,
    }

    try:
        sr_status, sr_data = await _sr_post(store_id, "/orders/create/adhoc", sr_order_payload)
        print(f"[Shiprocket] create order status={sr_status} response={sr_data}")
    except Exception as e:
        print(f"[Shiprocket] _sr_post exception: {type(e).__name__}: {e}")
        raise HTTPException(status_code=502, detail=f"Shiprocket request failed: {str(e)}")

    if sr_status not in (200, 201) or not sr_data.get("order_id"):
        raise HTTPException(status_code=502, detail=sr_data.get("message", "Shiprocket order creation failed"))

    sr_order_id = sr_data["order_id"]
    sr_shipment_id = sr_data.get("shipment_id")

    # --- Step 2: Assign courier ---
    assign_payload: dict = {"shipment_id": [sr_shipment_id]}
    if req.courier_id:
        assign_payload["courier_id"] = req.courier_id

    _, assign_data = await _sr_post(store_id, "/courier/assign/awb", assign_payload)
    awb = assign_data.get("response", {}).get("data", {}).get("awb_code", "")
    courier_name = assign_data.get("response", {}).get("data", {}).get("courier_name", "")

    # --- Step 3: Schedule pickup ---
    _, pickup_data = await _sr_post(store_id, "/courier/generate/pickup", {
        "shipment_id": [sr_shipment_id],
    })
    pickup_scheduled = pickup_data.get("pickup_status", 1) == 1

    tracking_url = f"https://shiprocket.co/tracking/{awb}" if awb else ""

    # Store shipment record
    shipments[req.order_id] = {
        "order_id": req.order_id,
        "sr_order_id": sr_order_id,
        "sr_shipment_id": sr_shipment_id,
        "awb": awb,
        "courier": courier_name,
        "tracking_url": tracking_url,
        "status": "pickup_scheduled" if pickup_scheduled else "assigned",
        "pickup_scheduled": pickup_scheduled,
        "created_at": datetime.now().isoformat(),
        "store_id": store_id,
    }

    # Update order status
    orders[req.order_id]["status"] = "shipped"
    orders[req.order_id]["tracking_url"] = tracking_url
    orders[req.order_id]["awb"] = awb
    orders[req.order_id]["courier"] = courier_name

    # --- Step 4: Email customer ---
    _send_tracking_email(
        customer_email=order["customer_email"],
        customer_name=order["customer_name"],
        order_id=req.order_id,
        awb=awb,
        courier=courier_name,
        tracking_url=tracking_url,
    )

    return {
        "success": True,
        "sr_order_id": sr_order_id,
        "sr_shipment_id": sr_shipment_id,
        "awb": awb,
        "courier": courier_name,
        "tracking_url": tracking_url,
        "pickup_scheduled": pickup_scheduled,
    }


# ── 5. Track shipment ─────────────────────────────────────────────
@app.get("/shiprocket/track/{order_id}")
async def track_shipment(order_id: str):
    shipment = shipments.get(order_id)
    if not shipment:
        raise HTTPException(status_code=404, detail="Shipment not found for this order")

    store_id = shipment["store_id"]
    awb = shipment.get("awb", "")
    if not awb:
        return {**shipment, "tracking_data": None}

    status, data = await _sr_get(store_id, f"/courier/track/awb/{awb}")
    tracking_data = data.get("tracking_data") if status == 200 else None

    return {
        **shipment,
        "tracking_data": tracking_data,
    }


# ── 6. Get shipment details for an order ──────────────────────────
@app.get("/shiprocket/shipment/{order_id}")
def get_shipment(order_id: str):
    shipment = shipments.get(order_id)
    if not shipment:
        return {"exists": False}
    return {**shipment, "exists": True}


# ── 7. Update delivery address (before shipped) ───────────────────
@app.put("/shiprocket/update-address/{store_id}")
async def update_delivery_address(store_id: str, req: UpdateAddressRequest):
    order = orders.get(req.order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order["store_id"] != store_id:
        raise HTTPException(status_code=403, detail="Order does not belong to this store")
    if order.get("status") == "shipped":
        raise HTTPException(status_code=400, detail="Cannot update address after order is shipped")

    # Persist to local order record
    orders[req.order_id]["address"] = req.new_address
    orders[req.order_id]["delivery_city"] = req.new_city
    orders[req.order_id]["delivery_state"] = req.new_state
    orders[req.order_id]["delivery_pincode"] = req.new_pincode
    orders[req.order_id]["delivery_country"] = req.new_country

    # If Shiprocket order already created, patch it
    shipment = shipments.get(req.order_id)
    if shipment and shipment.get("sr_order_id"):
        _, data = await _sr_post(store_id, f"/orders/address/update", {
            "order_id": shipment["sr_order_id"],
            "shipping_address": req.new_address,
            "shipping_city": req.new_city,
            "shipping_state": req.new_state,
            "shipping_pincode": req.new_pincode,
            "shipping_country": req.new_country,
        })
        if not data.get("status"):
            print(f"[WARN] Shiprocket address update failed: {data}")

    return {"updated": True, "order_id": req.order_id}


# ── 8. Initiate return shipment ───────────────────────────────────
@app.post("/shiprocket/return/{store_id}")
async def create_return(store_id: str, req: ReturnRequest):
    order = orders.get(req.order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order["store_id"] != store_id:
        raise HTTPException(status_code=403, detail="Order does not belong to this store")

    store = stores[store_id]

    return_payload = {
        "order_id": f"RET-{req.order_id}-{str(uuid.uuid4())[:4]}",
        "order_date": datetime.now().strftime("%Y-%m-%d"),
        # Pickup FROM customer
        "pickup_customer_name": req.pickup_name,
        "pickup_last_name": "",
        "pickup_address": req.pickup_address,
        "pickup_address_2": "",
        "pickup_city": req.pickup_city,
        "pickup_state": req.pickup_state,
        "pickup_country": "India",
        "pickup_pincode": req.pickup_postcode,
        "pickup_email": order["customer_email"],
        "pickup_phone": req.pickup_phone,
        # Deliver TO seller (store)
        "shipping_customer_name": store.get("name", "Seller"),
        "shipping_last_name": "",
        "shipping_address": store.get("pickup_address", order.get("address", "")),
        "shipping_city": store.get("pickup_city", ""),
        "shipping_pincode": store.get("pickup_pincode", req.pickup_postcode),
        "shipping_state": store.get("pickup_state", req.pickup_state),
        "shipping_country": "India",
        "shipping_email": store.get("owner_email", order["customer_email"]),
        "shipping_phone": store.get("owner_phone", req.pickup_phone),
        "order_items": [
            {
                "name": item["product_name"],
                "sku": item["product_id"],
                "units": item["quantity"],
                "selling_price": item["unit_price"],
            }
            for item in order["items"]
        ],
        "payment_method": "Prepaid",
        "sub_total": order["total"],
        "length": req.length,
        "breadth": req.breadth,
        "height": req.height,
        "weight": req.weight,
    }

    sr_status, sr_data = await _sr_post(store_id, "/orders/create/return", return_payload)
    if sr_status not in (200, 201):
        raise HTTPException(status_code=502, detail=sr_data.get("message", "Return creation failed"))

    return_id = str(uuid.uuid4())[:8]
    sr_order_id = sr_data.get("order_id")
    sr_shipment_id = sr_data.get("shipment_id")

    # Assign courier (auto-cheapest)
    awb = ""
    courier_name = ""
    if sr_shipment_id:
        _, assign_data = await _sr_post(store_id, "/courier/assign/awb", {"shipment_id": [sr_shipment_id]})
        awb = assign_data.get("response", {}).get("data", {}).get("awb_code", "")
        courier_name = assign_data.get("response", {}).get("data", {}).get("courier_name", "")

    tracking_url = f"https://shiprocket.co/tracking/{awb}" if awb else ""

    return_shipments[return_id] = {
        "return_id": return_id,
        "order_id": req.order_id,
        "sr_order_id": sr_order_id,
        "sr_shipment_id": sr_shipment_id,
        "awb": awb,
        "courier": courier_name,
        "tracking_url": tracking_url,
        "reason": req.reason,
        "status": "return_initiated",
        "created_at": datetime.now().isoformat(),
        "store_id": store_id,
    }

    # Mark original order as return-initiated
    orders[req.order_id]["return_status"] = "return_initiated"
    orders[req.order_id]["return_id"] = return_id
    orders[req.order_id]["return_tracking"] = tracking_url

    return {
        "success": True,
        "return_id": return_id,
        "sr_order_id": sr_order_id,
        "awb": awb,
        "courier": courier_name,
        "tracking_url": tracking_url,
    }


# ── 9. List returns for a store ───────────────────────────────────
@app.get("/shiprocket/returns/{store_id}")
def list_returns(store_id: str):
    return [r for r in return_shipments.values() if r["store_id"] == store_id]


# ── 10. Shiprocket webhook (tracking updates) ─────────────────────
@app.post("/shiprocket/webhook")
async def shiprocket_webhook(request: dict):
    """
    Shiprocket sends POST with tracking events.
    Configure: Shiprocket Dashboard → Settings → API → Webhooks
    URL: https://yourdomain.com/shiprocket/webhook
    """
    awb = request.get("awb", "")
    current_status = request.get("current_status", "")
    sr_order_id = str(request.get("sr_order_id", ""))

    # Find matching shipment
    for order_id, s in shipments.items():
        if s.get("awb") == awb or str(s.get("sr_order_id", "")) == sr_order_id:
            shipments[order_id]["status"] = current_status
            shipments[order_id]["last_updated"] = datetime.now().isoformat()
            shipments[order_id]["tracking_events"] = request.get("scans", [])
            orders[order_id]["shipping_status"] = current_status
            break

    return {"received": True}


# ─── Razorpay Refund ──────────────────────────────────────────────
import hmac, hashlib, base64

class RazorpayRefundRequest(BaseModel):
    order_id: str
    payment_id: Optional[str] = None  # if provided, skips order lookup for auth


# @app.post("/razorpay/refund/{store_id}")
# async def razorpay_refund(store_id: str, req: RazorpayRefundRequest):
#     conn = razorpay_connections.get(store_id)
#     if not conn:
#         raise HTTPException(status_code=400, detail="Razorpay not connected for this store")
#
#     # Resolve order using the same helper
#     order = _resolve_order(store_id, req.order_id)
#     if not order:
#         raise HTTPException(
#             status_code=404,
#             detail=(
#                 f"Order '{req.order_id}' not found for this store. "
#                 "Use the 8-character Order ID shown in the Orders tab (e.g. 92ce16db), "
#                 "or the Razorpay Payment ID (pay_xxx)."
#             )
#         )
#
#     payment_id = req.payment_id or order.get("razorpay_payment_id")
#     if not payment_id:
#         raise HTTPException(
#             status_code=400,
#             detail="No Razorpay payment ID linked to this order. Was it paid via Razorpay?"
#         )
#
#     if order.get("refund_status") == "refunded":
#         raise HTTPException(status_code=400, detail="This order has already been refunded.")
#
#     key_secret = os.getenv("RAZORPAY_KEY_SECRET", "")
#     if not key_secret:
#         raise HTTPException(status_code=400, detail="RAZORPAY_KEY_SECRET missing from server .env")
#
#     credentials = base64.b64encode(f"{conn['key_id']}:{key_secret}".encode()).decode()
#     amount_paise = int(round(order["total"] * 100))
#
#     try:
#         async with httpx.AsyncClient(timeout=20) as client:
#             res = await client.post(
#                 f"https://api.razorpay.com/v1/payments/{payment_id}/refund",
#                 headers={"Authorization": f"Basic {credentials}", "Content-Type": "application/json"},
#                 json={
#                     "amount": amount_paise,
#                     "notes": {"order_id": order["id"], "store_id": store_id, "reason": "Customer return approved"},
#                 },
#             )
#             data = res.json()
#     except httpx.RequestError as e:
#         raise HTTPException(status_code=502, detail=f"Razorpay unreachable: {e}")
#
#     if res.status_code not in (200, 201):
#         msg = data.get("error", {}).get("description") or f"Razorpay error {res.status_code}"
#         raise HTTPException(status_code=400, detail=msg)
#
#     orders[order["id"]]["refund_status"] = "refunded"
#     orders[order["id"]]["razorpay_refund_id"] = data.get("id", "")
#     orders[order["id"]]["refunded_at"] = datetime.now().isoformat()
#
#     return {
#         "success": True,
#         "refund_id": data.get("id"),
#         "amount": order["total"],
#         "payment_id": payment_id,
#         "internal_order_id": order["id"],
#         "status": data.get("status"),
#     }



if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
