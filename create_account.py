from fastapi import FastAPI, UploadFile, File, HTTPException, Request, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, validator, Field
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from supabase import create_client
import jwt
import smtplib
from passlib.context import CryptContext
import random
from email.mime.text import MIMEText
from dateutil.parser import isoparse
from dotenv import load_dotenv
import os

load_dotenv()
app = FastAPI()


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Adjust this to your needs
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Supabase setup
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')
supabases = create_client(SUPABASE_URL, SUPABASE_KEY)

# JWT setup
SECRET_KEY = os.getenv('SECRET_KEY')
ALGORITHM = "HS256"


class User(BaseModel):
    username: str
    email: str
    password: str


class OTPVerification(BaseModel):
    email: str
    otp: str


class Login(BaseModel):
    email: str
    password: str


class PasswordResetRequest(BaseModel):
    email: str


class PasswordReset(BaseModel):
    email: str
    reset_token: str
    new_password: str


pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


@app.post("/register/")
async def register(user: User):
    otp = str(random.randint(100000, 999999))
    hashed_password = pwd_context.hash(user.password)

    response = supabases.table("users").insert({
        "username": user.username,
        "email": user.email,
        "password": hashed_password,
        "otp": otp,
        "verified": False
    }).execute()

    # if response.status_code != 201:
    #     raise HTTPException(status_code=response.status_code, detail="Error registering user")

    send_otp_email(user.email, otp)
    return {"message": "Registration successful. Please check your email for OTP"}


@app.post("/verifyotp/")
async def verify_otp(otp_verification: OTPVerification):
    response = supabases.table("users").select("*").eq("email", otp_verification.email).execute()
    user_data = response.data
    user = user_data[0] if user_data else None

    if user:
        if user['otp'] == otp_verification.otp:
            # Use upsert to update or insert record
            upsert_response = supabases.table("users").upsert({
                "email": otp_verification.email,
                "otp": None,  # Reset OTP
                "verified": True  # Set verified to True
            }, on_conflict="email").execute()

            # Check for errors in the response
            # if upsert_response.error:
            #     raise HTTPException(status_code=500, detail="Error updating user status")

            token = jwt.encode({"email": user['email']}, SECRET_KEY, algorithm=ALGORITHM)
            return {"token": token}
        # else:
        #     raise HTTPException(status_code=400, detail="Invalid OTP")
    else:
        raise HTTPException(status_code=404, detail="User not found")


@app.post("/login/")
async def login(login: Login):
    response = supabases.table("users").select("*").eq("email", login.email).execute()
    user_data = response.data
    user = user_data[0] if user_data else None

    if user and pwd_context.verify(login.password, user['password']):
        if user['verified']:
            # Include user ID and email in the token
            token = jwt.encode({
                "user_id": user['ID'],
                "email": user['email']
            }, SECRET_KEY, algorithm=ALGORITHM)

            return {
                "token": token,
                "user_id": user['ID'],
                "email": user['email']
            }
        else:
            raise HTTPException(status_code=400, detail="Email not verified. Please check your email for OTP.")
    else:
        raise HTTPException(status_code=401, detail="Invalid email or password")


@app.post("/logout/")
async def logout():
    # For simplicity, the actual token invalidation should be handled client-side.
    return {"message": "Logged out successfully"}


@app.post("/request-reset/")
async def request_reset(request: PasswordResetRequest):
    response = supabases.table("users").select("*").eq("email", request.email).execute()
    user_data = response.data
    user = user_data[0] if user_data else None

    if user:
        reset_token = str(random.randint(100000, 999999))
        reset_token_expires = (datetime.utcnow() + timedelta(hours=1)).isoformat()
        # Save reset token and its expiration time in the database
        upsert_response = supabases.table("users").upsert({
            "email": request.email,  # Ensure the email is used for primary key
            "reset_token": reset_token,
            "reset_token_expires": reset_token_expires
        }, on_conflict="email").execute()

        # Send reset token email
        send_otp_email(request.email, reset_token)
        return {"message": "Password reset token sent to your email"}
    else:
        raise HTTPException(status_code=404, detail="User not found")


@app.post("/reset-password/")
async def reset_password(data: PasswordReset):
    response = supabases.table("users").select("*").eq("email", data.email).execute()
    user_data = response.data
    user = user_data[0] if user_data else None

    if user:
        # Convert the ISO 8601 string to a datetime object
        reset_token_expires = isoparse(user['reset_token_expires'])

        if user['reset_token'] == data.reset_token and datetime.utcnow() < reset_token_expires:
            hashed_password = pwd_context.hash(data.new_password)
            supabases.table("users").update({
                "password": hashed_password,
                "reset_token": None,
                "reset_token_expires": None
            }).eq("email", data.email).execute()
            return {"message": "Password reset successfully"}
        else:
            raise HTTPException(status_code=400, detail="Invalid or expired reset token")
    else:
        raise HTTPException(status_code=404, detail="User not found")


def send_otp_email(to_email: str, otp: str):
    smtp_server = 'mail.privateemail.com'
    smtp_port = 587
    smtp_user = os.getenv('EMAIL_USER')
    smtp_password = os.getenv('EMAIL_PASSWORD')

    msg = MIMEText(f"""
    Dear User,

    Your One-Time Password (OTP) for verification is: {otp}

    Please use this code within the next 10 minutes to complete your authentication. 
    Do not share this code with anyone for security reasons. 

    If you did not request this code, please ignore this message or contact support immediately.

    Thank you,
    Exam Paper Academy
    """.strip())
    msg['Subject'] = 'Your Exam Paper Academy OTP Code for Verification'
    msg['From'] = smtp_user
    msg['To'] = to_email

    with smtplib.SMTP(smtp_server, smtp_port) as server:
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_user, to_email, msg.as_string())
