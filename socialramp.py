"""
SocialRamp - Social Media Aggregator Backend
FastAPI + Real OAuth2 integrations

SETUP:
1. pip install fastapi uvicorn httpx python-dotenv google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client requests-oauthlib cryptography python-jose[cryptography] passlib[bcrypt]
2. Create a .env file with the variables listed below
3. Run: uvicorn main:app --reload --port 8000

.env variables needed:
    META_APP_ID=your_meta_app_id
    META_APP_SECRET=your_meta_app_secret
    GOOGLE_CLIENT_ID=your_google_client_id
    GOOGLE_CLIENT_SECRET=your_google_client_secret
    WHATSAPP_PHONE_NUMBER_ID=your_whatsapp_phone_number_id
    WHATSAPP_BUSINESS_ACCOUNT_ID=your_whatsapp_business_account_id
    FRONTEND_URL=http://localhost:3000
    SECRET_KEY=your_random_secret_key_for_jwt
"""

import os
import json
import base64
import httpx
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from dotenv import load_dotenv

from fastapi import FastAPI, HTTPException, Depends, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse
from pydantic import BaseModel

# Google Gmail
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
import google.auth.transport.requests

# JWT for session tokens
from jose import JWTError, jwt
from passlib.context import CryptContext

load_dotenv()
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = os.getenv("OAUTHLIB_INSECURE_TRANSPORT", "0")


app = FastAPI(title="SocialRamp API", version="1.0.0")

# ─── CORS ────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.getenv("FRONTEND_URL", "http://localhost:3000")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── CONFIG ──────────────────────────────────────────────────────────────────
META_APP_ID = os.getenv("FACEBOOK_APP_ID")
META_APP_SECRET = os.getenv("FACEBOOK_APP_SECRET")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
WHATSAPP_BUSINESS_ACCOUNT_ID = os.getenv("WHATSAPP_BUSINESS_ACCOUNT_ID")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")
SECRET_KEY = os.getenv("SECRET_KEY", "change-this-secret")
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")

META_GRAPH_URL = "https://graph.facebook.com/v19.0"
WHATSAPP_API_URL = f"https://graph.facebook.com/v19.0/{WHATSAPP_PHONE_NUMBER_ID}"

# In-memory token store (replace with DB in production)
# Structure: { session_id: { platform: { access_token, refresh_token, ... } } }
token_store: Dict[str, Dict[str, Any]] = {}

# ─── JWT UTILS ───────────────────────────────────────────────────────────────
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

# ─── MODELS ──────────────────────────────────────────────────────────────────
class SendMessageRequest(BaseModel):
    platform: str
    recipient_id: str
    message: str

class ReplyCommentRequest(BaseModel):
    platform: str
    comment_id: str
    post_id: str
    message: str

class SendEmailRequest(BaseModel):
    to: str
    subject: str
    body: str
    thread_id: Optional[str] = None

# ─── SESSION ENDPOINT ─────────────────────────────────────────────────────────
@app.post("/api/session/create")
async def create_session(response: Response):
    import uuid
    session_id = str(uuid.uuid4())
    token = create_session_token(session_id)
    response.set_cookie(
        key="session_token",
        value=token,
        httponly=True,
        secure=False,  # Set True in production with HTTPS
        samesite="lax",
        max_age=604800,
    )
    return {"session_id": session_id, "token": token}

@app.get("/api/session/connections")
async def get_connections(session_id: str = Depends(get_session_id)):
    platforms = token_store.get(session_id, {})
    return {
        "instagram": "instagram" in platforms,
        "facebook": "facebook" in platforms,
        "whatsapp": "whatsapp" in platforms,
        "gmail": "gmail" in platforms,
    }

# ═════════════════════════════════════════════════════════════════════════════
# FACEBOOK & INSTAGRAM OAUTH (Meta)
# ═════════════════════════════════════════════════════════════════════════════

@app.get("/api/auth/facebook/connect")
async def facebook_connect(session_id: str = Depends(get_session_id)):
    """Initiate Facebook OAuth. Scopes cover FB Pages + Instagram Basic Display."""
    scopes = [
        "pages_show_list",
        "pages_read_engagement",
        # "pages_manage_posts",
        # "pages_messaging",
        "instagram_basic",
        "instagram_manage_messages",
        "instagram_manage_comments",
        "public_profile",
    ]
    scope_str = ",".join(scopes)
    redirect_uri = f"{BACKEND_URL}/api/auth/facebook/callback"
    state = session_id  # Pass session_id as state
    url = (
        f"https://www.facebook.com/v19.0/dialog/oauth"
        f"?client_id={META_APP_ID}"
        f"&redirect_uri={redirect_uri}"
        f"&scope={scope_str}"
        f"&state={state}"
        f"&response_type=code"
    )
    return {"auth_url": url}

@app.get("/api/auth/facebook/callback")
async def facebook_callback(code: str, state: str, request: Request):
    """Exchange code for access token. Meta returns both FB + IG tokens."""
    session_id = state
    redirect_uri = f"{BACKEND_URL}/api/auth/facebook/callback"

    async with httpx.AsyncClient() as client:
        # Exchange code for access token
        token_resp = await client.get(
            f"{META_GRAPH_URL}/oauth/access_token",
            params={
                "client_id": META_APP_ID,
                "client_secret": META_APP_SECRET,
                "redirect_uri": redirect_uri,
                "code": code,
            },
        )
        token_data = token_resp.json()
        if "error" in token_data:
            return RedirectResponse(f"{FRONTEND_URL}?error=facebook_auth_failed")

        user_token = token_data["access_token"]

        # Get long-lived token
        ll_resp = await client.get(
            f"{META_GRAPH_URL}/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": META_APP_ID,
                "client_secret": META_APP_SECRET,
                "fb_exchange_token": user_token,
            },
        )
        ll_data = ll_resp.json()
        long_lived_token = ll_data.get("access_token", user_token)

        # Get user info
        me_resp = await client.get(
            f"{META_GRAPH_URL}/me",
            params={"fields": "id,name,picture", "access_token": long_lived_token},
        )
        me_data = me_resp.json()

        # Get pages (for posting and messaging)
        pages_resp = await client.get(
            f"{META_GRAPH_URL}/me/accounts",
            params={"access_token": long_lived_token},
        )
        pages_data = pages_resp.json()
        pages = pages_data.get("data", [])

        # Store Facebook token
        set_platform_token(session_id, "facebook", {
            "access_token": long_lived_token,
            "user_id": me_data.get("id"),
            "name": me_data.get("name"),
            "picture": me_data.get("picture", {}).get("data", {}).get("url"),
            "pages": pages,
        })

        # Get Instagram business accounts linked to pages
        for page in pages:
            page_token = page.get("access_token")
            page_id = page.get("id")
            ig_resp = await client.get(
                f"{META_GRAPH_URL}/{page_id}",
                params={
                    "fields": "instagram_business_account",
                    "access_token": page_token,
                },
            )
            ig_data = ig_resp.json()
            ig_account = ig_data.get("instagram_business_account")
            if ig_account:
                ig_id = ig_account["id"]
                # Get IG account details
                ig_detail_resp = await client.get(
                    f"{META_GRAPH_URL}/{ig_id}",
                    params={
                        "fields": "id,username,profile_picture_url,name",
                        "access_token": page_token,
                    },
                )
                ig_detail = ig_detail_resp.json()
                set_platform_token(session_id, "instagram", {
                    "access_token": page_token,
                    "ig_user_id": ig_id,
                    "username": ig_detail.get("username"),
                    "name": ig_detail.get("name"),
                    "picture": ig_detail.get("profile_picture_url"),
                    "page_id": page_id,
                })
                break  # Use first IG account found

        # WhatsApp also uses Meta token — store it
        set_platform_token(session_id, "whatsapp", {
            "access_token": long_lived_token,
            "phone_number_id": WHATSAPP_PHONE_NUMBER_ID,
            "business_account_id": WHATSAPP_BUSINESS_ACCOUNT_ID,
        })

    return RedirectResponse(f"{FRONTEND_URL}?connected=facebook")


# ═════════════════════════════════════════════════════════════════════════════
# GMAIL OAUTH
# ═════════════════════════════════════════════════════════════════════════════

GMAIL_SCOPES = [
    "openid",  # ← Add this
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]

# In-memory store for OAuth states (add near token_store at the top)
oauth_state_store: Dict[str, str] = {}  # state -> code_verifier or serialized flow state

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
        access_type="offline",
        include_granted_scopes="true",
        state=session_id,
        prompt="consent",
    )

    # Persist code_verifier for PKCE handshake in callback
    if flow.code_verifier:
        oauth_state_store[session_id] = flow.code_verifier

    return {"auth_url": auth_url}


@app.get("/api/auth/gmail/callback")
async def gmail_callback(code: str, state: str, request: Request):
    session_id = state
    redirect_uri = f"{BACKEND_URL}/api/auth/gmail/callback"

    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
    os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"  # Must be set BEFORE fetch_token

    flow = get_gmail_flow(redirect_uri)

    # Restore code_verifier from connect step
    code_verifier = oauth_state_store.pop(session_id, None)
    if code_verifier:
        flow.code_verifier = code_verifier

    flow.fetch_token(
        code=code,
        code_verifier=code_verifier,
    )

    creds = flow.credentials

    service = build("gmail", "v1", credentials=creds)
    profile = service.users().getProfile(userId="me").execute()

    set_platform_token(session_id, "gmail", {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes or GMAIL_SCOPES),
        "email": profile.get("emailAddress"),
    })

    return RedirectResponse(f"{FRONTEND_URL}?connected=gmail")

def build_gmail_service(session_id: str):
    token_data = get_platform_token(session_id, "gmail")
    if not token_data:
        raise HTTPException(status_code=401, detail="Gmail not connected")
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
    return build("gmail", "v1", credentials=creds)


# ═════════════════════════════════════════════════════════════════════════════
# INBOX / MESSAGES ENDPOINTS
# ═════════════════════════════════════════════════════════════════════════════

@app.get("/api/inbox/instagram")
async def get_instagram_inbox(session_id: str = Depends(get_session_id)):
    """Fetch Instagram DMs via Messenger API for Instagram."""
    token_data = get_platform_token(session_id, "instagram")
    if not token_data:
        raise HTTPException(status_code=401, detail="Instagram not connected")

    ig_id = token_data["ig_user_id"]
    access_token = token_data["access_token"]
    messages = []

    async with httpx.AsyncClient() as client:
        # Fetch conversations
        conv_resp = await client.get(
            f"{META_GRAPH_URL}/{ig_id}/conversations",
            params={
                "fields": "participants,messages{message,from,created_time,id}",
                "access_token": access_token,
                "platform": "instagram",
            },
        )
        conv_data = conv_resp.json()

        for conv in conv_data.get("data", []):
            participants = conv.get("participants", {}).get("data", [])
            other_party = next((p for p in participants if p["id"] != ig_id), None)
            for msg in conv.get("messages", {}).get("data", []):
                messages.append({
                    "id": msg["id"],
                    "conversation_id": conv["id"],
                    "sender": msg.get("from", {}).get("username") or msg.get("from", {}).get("name"),
                    "sender_id": msg.get("from", {}).get("id"),
                    "recipient_id": other_party["id"] if other_party else None,
                    "text": msg.get("message", ""),
                    "timestamp": msg.get("created_time"),
                    "platform": "instagram",
                })

    return {"messages": messages}


@app.get("/api/inbox/facebook")
async def get_facebook_inbox(session_id: str = Depends(get_session_id)):
    """Fetch Facebook Page messages."""
    token_data = get_platform_token(session_id, "facebook")
    if not token_data:
        raise HTTPException(status_code=401, detail="Facebook not connected")

    messages = []
    async with httpx.AsyncClient() as client:
        for page in token_data.get("pages", []):
            page_id = page["id"]
            page_token = page["access_token"]

            conv_resp = await client.get(
                f"{META_GRAPH_URL}/{page_id}/conversations",
                params={
                    "fields": "participants,messages{message,from,created_time,id}",
                    "access_token": page_token,
                },
            )
            conv_data = conv_resp.json()

            for conv in conv_data.get("data", []):
                participants = conv.get("participants", {}).get("data", [])
                other_party = next((p for p in participants if p["id"] != page_id), None)
                for msg in conv.get("messages", {}).get("data", []):
                    messages.append({
                        "id": msg["id"],
                        "conversation_id": conv["id"],
                        "sender": msg.get("from", {}).get("name"),
                        "sender_id": msg.get("from", {}).get("id"),
                        "recipient_id": other_party["id"] if other_party else None,
                        "page_id": page_id,
                        "page_name": page.get("name"),
                        "text": msg.get("message", ""),
                        "timestamp": msg.get("created_time"),
                        "platform": "facebook",
                    })

    return {"messages": messages}


@app.get("/api/inbox/whatsapp")
async def get_whatsapp_messages(session_id: str = Depends(get_session_id)):
    """
    WhatsApp Cloud API - incoming messages are received via webhook.
    This endpoint returns stored webhook messages.
    You must set up a webhook endpoint at /api/webhooks/whatsapp
    and configure it in your Meta Developer dashboard.
    """
    token_data = get_platform_token(session_id, "whatsapp")
    if not token_data:
        raise HTTPException(status_code=401, detail="WhatsApp not connected")

    # In production, fetch from your database where webhook events are stored
    stored = whatsapp_webhook_store.get("messages", [])
    return {"messages": stored}


@app.get("/api/inbox/gmail")
async def get_gmail_inbox(
    max_results: int = 20,
    session_id: str = Depends(get_session_id),
):
    """Fetch Gmail inbox messages."""
    service = build_gmail_service(session_id)

    result = service.users().messages().list(
        userId="me",
        labelIds=["INBOX"],
        maxResults=max_results,
    ).execute()

    messages = []
    for msg_ref in result.get("messages", []):
        msg = service.users().messages().get(
            userId="me",
            id=msg_ref["id"],
            format="metadata",
            metadataHeaders=["From", "To", "Subject", "Date"],
        ).execute()

        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        messages.append({
            "id": msg["id"],
            "thread_id": msg["threadId"],
            "snippet": msg.get("snippet", ""),
            "from": headers.get("From", ""),
            "to": headers.get("To", ""),
            "subject": headers.get("Subject", ""),
            "date": headers.get("Date", ""),
            "labels": msg.get("labelIds", []),
            "platform": "gmail",
        })

    return {"messages": messages}


@app.get("/api/inbox/gmail/{message_id}")
async def get_gmail_message(message_id: str, session_id: str = Depends(get_session_id)):
    """Fetch full Gmail message body."""
    service = build_gmail_service(session_id)
    msg = service.users().messages().get(
        userId="me", id=message_id, format="full"
    ).execute()

    def decode_body(payload):
        if payload.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
        for part in payload.get("parts", []):
            if part.get("mimeType") == "text/plain":
                data = part.get("body", {}).get("data", "")
                if data:
                    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
            if part.get("mimeType") == "text/html":
                data = part.get("body", {}).get("data", "")
                if data:
                    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        return ""

    body = decode_body(msg.get("payload", {}))
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}

    return {
        "id": msg["id"],
        "thread_id": msg["threadId"],
        "from": headers.get("From", ""),
        "to": headers.get("To", ""),
        "subject": headers.get("Subject", ""),
        "date": headers.get("Date", ""),
        "body": body,
        "platform": "gmail",
    }


# ═════════════════════════════════════════════════════════════════════════════
# POSTS & COMMENTS
# ═════════════════════════════════════════════════════════════════════════════

@app.get("/api/posts/instagram")
async def get_instagram_posts(session_id: str = Depends(get_session_id)):
    token_data = get_platform_token(session_id, "instagram")
    if not token_data:
        raise HTTPException(status_code=401, detail="Instagram not connected")

    ig_id = token_data["ig_user_id"]
    access_token = token_data["access_token"]

    async with httpx.AsyncClient() as client:
        posts_resp = await client.get(
            f"{META_GRAPH_URL}/{ig_id}/media",
            params={
                "fields": "id,caption,media_type,media_url,thumbnail_url,timestamp,like_count,comments_count,permalink",
                "access_token": access_token,
            },
        )
        posts_data = posts_resp.json()

    return {"posts": posts_data.get("data", [])}


@app.get("/api/posts/instagram/{post_id}/comments")
async def get_instagram_comments(post_id: str, session_id: str = Depends(get_session_id)):
    token_data = get_platform_token(session_id, "instagram")
    if not token_data:
        raise HTTPException(status_code=401, detail="Instagram not connected")

    async with httpx.AsyncClient() as client:
        comments_resp = await client.get(
            f"{META_GRAPH_URL}/{post_id}/comments",
            params={
                "fields": "id,text,username,timestamp,like_count,replies{id,text,username,timestamp}",
                "access_token": token_data["access_token"],
            },
        )
        return {"comments": comments_resp.json().get("data", [])}


@app.get("/api/posts/facebook")
async def get_facebook_posts(session_id: str = Depends(get_session_id)):
    token_data = get_platform_token(session_id, "facebook")
    if not token_data:
        raise HTTPException(status_code=401, detail="Facebook not connected")

    all_posts = []
    async with httpx.AsyncClient() as client:
        for page in token_data.get("pages", []):
            page_id = page["id"]
            page_token = page["access_token"]
            posts_resp = await client.get(
                f"{META_GRAPH_URL}/{page_id}/posts",
                params={
                    "fields": "id,message,story,created_time,likes.summary(true),comments.summary(true),full_picture,permalink_url",
                    "access_token": page_token,
                },
            )
            posts_data = posts_resp.json()
            for post in posts_data.get("data", []):
                post["page_name"] = page.get("name")
                post["page_id"] = page_id
                post["page_token"] = page_token
                all_posts.append(post)

    return {"posts": all_posts}


@app.get("/api/posts/facebook/{post_id}/comments")
async def get_facebook_comments(post_id: str, session_id: str = Depends(get_session_id)):
    token_data = get_platform_token(session_id, "facebook")
    if not token_data:
        raise HTTPException(status_code=401, detail="Facebook not connected")

    # Find the page token for this post
    page_token = token_data.get("pages", [{}])[0].get("access_token", token_data["access_token"])

    async with httpx.AsyncClient() as client:
        comments_resp = await client.get(
            f"{META_GRAPH_URL}/{post_id}/comments",
            params={
                "fields": "id,message,from,created_time,like_count,comments{id,message,from,created_time}",
                "access_token": page_token,
            },
        )
        return {"comments": comments_resp.json().get("data", [])}


# ═════════════════════════════════════════════════════════════════════════════
# SEND MESSAGES
# ═════════════════════════════════════════════════════════════════════════════

@app.post("/api/messages/send")
async def send_message(req: SendMessageRequest, session_id: str = Depends(get_session_id)):
    """Send a DM on Instagram or Facebook."""
    token_data = get_platform_token(session_id, req.platform)
    if not token_data:
        raise HTTPException(status_code=401, detail=f"{req.platform} not connected")

    if req.platform == "instagram":
        ig_id = token_data["ig_user_id"]
        access_token = token_data["access_token"]
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{META_GRAPH_URL}/{ig_id}/messages",
                params={"access_token": access_token},
                json={
                    "recipient": {"id": req.recipient_id},
                    "message": {"text": req.message},
                },
            )
            return resp.json()

    elif req.platform == "facebook":
        page_token = token_data.get("pages", [{}])[0].get("access_token", token_data["access_token"])
        page_id = token_data.get("pages", [{}])[0].get("id")
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{META_GRAPH_URL}/{page_id}/messages",
                params={"access_token": page_token},
                json={
                    "recipient": {"id": req.recipient_id},
                    "message": {"text": req.message},
                },
            )
            return resp.json()

    raise HTTPException(status_code=400, detail="Unsupported platform for messaging")


@app.post("/api/messages/whatsapp/send")
async def send_whatsapp_message(req: SendMessageRequest, session_id: str = Depends(get_session_id)):
    """Send WhatsApp message via Cloud API."""
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
                "to": req.recipient_id,
                "type": "text",
                "text": {"body": req.message},
            },
        )
        return resp.json()


@app.post("/api/email/send")
async def send_email(req: SendEmailRequest, session_id: str = Depends(get_session_id)):
    """Send or reply to a Gmail email."""
    service = build_gmail_service(session_id)

    token_data = get_platform_token(session_id, "gmail")
    sender_email = token_data.get("email", "me")

    # Build MIME message
    import email.mime.text
    import email.mime.multipart

    mime_msg = email.mime.multipart.MIMEMultipart()
    mime_msg["to"] = req.to
    mime_msg["from"] = sender_email
    mime_msg["subject"] = req.subject
    mime_msg.attach(email.mime.text.MIMEText(req.body, "plain"))

    raw = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode()
    body = {"raw": raw}
    if req.thread_id:
        body["threadId"] = req.thread_id

    result = service.users().messages().send(userId="me", body=body).execute()
    return {"message_id": result["id"], "thread_id": result.get("threadId")}


@app.post("/api/comments/reply")
async def reply_to_comment(req: ReplyCommentRequest, session_id: str = Depends(get_session_id)):
    """Reply to an Instagram or Facebook comment."""
    token_data = get_platform_token(session_id, req.platform)
    if not token_data:
        raise HTTPException(status_code=401, detail=f"{req.platform} not connected")

    if req.platform == "instagram":
        access_token = token_data["access_token"]
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{META_GRAPH_URL}/{req.comment_id}/replies",
                params={"access_token": access_token},
                json={"message": req.message},
            )
            return resp.json()

    elif req.platform == "facebook":
        page_token = token_data.get("pages", [{}])[0].get("access_token", token_data["access_token"])
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{META_GRAPH_URL}/{req.comment_id}/comments",
                params={"access_token": page_token},
                json={"message": req.message},
            )
            return resp.json()

    raise HTTPException(status_code=400, detail="Unsupported platform")


# ═════════════════════════════════════════════════════════════════════════════
# WHATSAPP WEBHOOK
# ═════════════════════════════════════════════════════════════════════════════

WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "socialramp_verify_token")
whatsapp_webhook_store: Dict[str, List] = {"messages": []}

@app.get("/api/webhooks/whatsapp")
async def whatsapp_webhook_verify(
    hub_mode: str = None,
    hub_verify_token: str = None,
    hub_challenge: str = None,
):
    """Meta webhook verification handshake."""
    if hub_mode == "subscribe" and hub_verify_token == WHATSAPP_VERIFY_TOKEN:
        return Response(content=hub_challenge, media_type="text/plain")
    raise HTTPException(status_code=403, detail="Verification failed")

@app.post("/api/webhooks/whatsapp")
async def whatsapp_webhook_receive(request: Request):
    """Receive incoming WhatsApp messages and store them."""
    body = await request.json()
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for msg in value.get("messages", []):
                whatsapp_webhook_store["messages"].append({
                    "id": msg["id"],
                    "from": msg["from"],
                    "timestamp": msg.get("timestamp"),
                    "type": msg.get("type"),
                    "text": msg.get("text", {}).get("body", ""),
                    "platform": "whatsapp",
                })
    return {"status": "ok"}


# ═════════════════════════════════════════════════════════════════════════════
# DISCONNECT
# ═════════════════════════════════════════════════════════════════════════════

@app.delete("/api/auth/{platform}/disconnect")
async def disconnect_platform(platform: str, session_id: str = Depends(get_session_id)):
    if session_id in token_store and platform in token_store[session_id]:
        del token_store[session_id][platform]
        # Facebook auth also controls Instagram and WhatsApp
        if platform == "facebook":
            token_store[session_id].pop("instagram", None)
            token_store[session_id].pop("whatsapp", None)
    return {"disconnected": platform}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
