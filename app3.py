# app.py
import os
import psycopg2
import threading
import time
import urllib.parse
import uuid
from datetime import datetime
import streamlit as st
import requests
from streamlit_autorefresh import st_autorefresh
from dotenv import load_dotenv

load_dotenv()

# -----------------------------
# Config - prefer Streamlit secrets, fallback to env
# -----------------------------
try:
    API_KEY = st.secrets["API_KEY"]
    SANDBOX_NUMBER = st.secrets["SANDBOX_NUMBER"]
    TEXT_API_URL = st.secrets["TEXT_API_URL"]
    IMAGE_API_URL = st.secrets["IMAGE_API_URL"]
    VIDEO_API_URL = st.secrets["VIDEO_API_URL"]
    DOCUMENT_API_URL = st.secrets["DOCUMENT_API_URL"]
    NGROK_URL = st.secrets["NGROK_URL"]
    DATABASE_URL = st.secrets["DATABASE_URL"]
    FASTAPI_PROXY_BASE = st.secrets["FASTAPI_PROXY_BASE"].rstrip("/")
    STREAMLIT_PUBLIC_URL = st.secrets.get("STREAMLIT_URL") or ""
except Exception:
    API_KEY = os.getenv("API_KEY")
    SANDBOX_NUMBER = os.getenv("SANDBOX_NUMBER")
    TEXT_API_URL = os.getenv("TEXT_API_URL")
    IMAGE_API_URL = os.getenv("IMAGE_API_URL")
    VIDEO_API_URL = os.getenv("VIDEO_API_URL")
    DOCUMENT_API_URL = os.getenv("DOCUMENT_API_URL")
    NGROK_URL = os.getenv("NGROK_URL")
    DATABASE_URL = os.getenv("DATABASE_URL")
    FASTAPI_PROXY_BASE = os.getenv("FASTAPI_PROXY_BASE", "").rstrip("/")
    STREAMLIT_PUBLIC_URL = os.getenv("STREAMLIT_URL", "")

# If you didn't set FASTAPI_PROXY_BASE in secrets/env, use the backend URL you shared:
if not FASTAPI_PROXY_BASE:
    FASTAPI_PROXY_BASE = "https://fastapi-whatsapp-backend.onrender.com"

# If you didn't set STREAMLIT_PUBLIC_URL, use the Streamlit URL you shared:
if not STREAMLIT_PUBLIC_URL:
    STREAMLIT_PUBLIC_URL = "https://infoproject-iahbcuym4jenzg2swn8jrb.streamlit.app/"

API_ENABLED = True

# -----------------------------
# Keep FastAPI and Streamlit warm (background pinger)
# -----------------------------
def ping_url(url):
    try:
        requests.get(url, timeout=6)
    except Exception:
        pass

def pinger_loop():
    fastapi_health = f"{FASTAPI_PROXY_BASE}/health"
    streamlit_url = STREAMLIT_PUBLIC_URL.rstrip("/")  # root URL
    while True:
        # ping backend health
        ping_url(fastapi_health)
        # ping streamlit itself to keep it awake
        ping_url(streamlit_url)
        # sleep 5 minutes
        time.sleep(300)

# start background thread (daemon so it won't block shutdown)
thread = threading.Thread(target=pinger_loop, daemon=True)
thread.start()

# -----------------------------
# DB initialization (cached)
# -----------------------------
@st.cache_resource
def init_db():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        id SERIAL PRIMARY KEY,
        phone TEXT,
        message TEXT,
        direction TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        message_type TEXT,
        media_link TEXT,
        caption TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS contacts (
        id SERIAL PRIMARY KEY,
        phone TEXT UNIQUE,
        name TEXT
    )
    """)
    conn.commit()
    return conn, cur

conn, cursor = init_db()

# -----------------------------
# Streamlit UI
# -----------------------------
st.set_page_config(page_title="WhatsApp Chat Dashboard", page_icon="💬", layout="centered")
st.title("💬 WhatsApp Chat Dashboard (Live)")

# auto refresh
st_autorefresh(interval=15000, key="messages_refresh")
if st.button("🔄 Refresh Now"):
    st.rerun()

# load messages
cursor.execute("SELECT * FROM messages ORDER BY timestamp ASC")
messages = cursor.fetchall()
cursor.execute("SELECT phone, name FROM contacts")
contacts = {phone: name for phone, name in cursor.fetchall()}

conversation_keys = sorted({m[1] for m in messages})
contact_display_names = ["All"] + [f"{contacts.get(p,p)} ({p})" for p in conversation_keys]

st.sidebar.title("📱 Contacts")
selected_display = st.sidebar.selectbox("Select a conversation", contact_display_names)
st.sidebar.write("---")
st.sidebar.write("Total contacts:", len(conversation_keys))

selected_phone = "All" if selected_display == "All" else selected_display.split("(")[-1].replace(")", "")
chat_messages = [m for m in messages if selected_phone == "All" or m[1] == selected_phone]

# helpers
def build_proxy_url(media_identifier: str, direction: str="inbound") -> str:
    if not media_identifier:
        return ""
    if direction == "outbound" or media_identifier.startswith("http"):
        return media_identifier
    encoded = urllib.parse.quote_plus(media_identifier)
    return f"{FASTAPI_PROXY_BASE}/media-proxy/{encoded}"

def render_bubble(msg_row):
    _, phone, message_text, direction, timestamp, msg_type, media_link, caption = msg_row
    display_name = f"{contacts.get(phone, phone)} ({phone})"
    is_inbound = direction == "inbound"
    align = "flex-start" if is_inbound else "flex-end"
    bg = "#ffffff" if is_inbound else "#dcf8c6"

    content_html = "<i>No content</i>"
    if msg_type == "text" or not msg_type:
        content_html = message_text or "<i>No text content</i>"
    elif msg_type in ("image", "video", "document", "voice", "audio"):
        if media_link:
            proxy = build_proxy_url(media_link, direction)
            if msg_type == "image":
                content_html = f"<a href='{proxy}' target='_blank'><img src='{proxy}' style='max-width:220px; border-radius:8px; border:1px solid #ddd;'></a>"
            elif msg_type == "video":
                content_html = f"<a href='{proxy}' target='_blank'>View Video</a><br><video width='260' controls><source src='{proxy}' type='video/mp4'></video>"
            elif msg_type in ("voice", "audio"):
                content_html = f"<audio controls><source src='{proxy}' type='audio/mpeg'></audio>"
            elif msg_type == "document":
                content_html = f"<a href='{proxy}' target='_blank'>Open Document</a>"
            if caption:
                content_html += f"<div style='margin-top:6px'>{caption}</div>"

    st.markdown(f"""
    <div style='display:flex; justify-content:{align}; margin:8px 0;'>
      <div style='max-width:72%; background:{bg}; padding:10px; border-radius:10px; border:1px solid #ddd;'>
        <b>{display_name}</b><br><br>
        {content_html}
        <div style='text-align:right; font-size:11px; color:#666; margin-top:6px;'>{timestamp}</div>
      </div>
    </div>
    """, unsafe_allow_html=True)

# header & render
st.subheader("💬 All Conversations" if selected_phone == "All" else f"💬 Chat with: {contacts.get(selected_phone, selected_phone)} ({selected_phone})")
if not chat_messages:
    st.info("No messages yet for this contact.")
else:
    for m in chat_messages:
        render_bubble(m)

# send new message
st.subheader("Send a New WhatsApp Message")
recipient = st.text_input("Recipient number (include country code)")
message_text = st.text_area("Message (text only)")
media_url = st.text_input("Image/Video/Document URL (optional, must start with https://)")
media_caption = st.text_input("Caption (optional)")

if st.button("Send"):
    if recipient.strip() and (message_text.strip() or media_url.strip()):
        if media_url.strip():
            url_lower = media_url.lower()
            if url_lower.endswith((".jpg", ".jpeg", ".png", ".gif")):
                msg_type = "image"
                api_url = IMAGE_API_URL
            elif url_lower.endswith((".mp4", ".mov", ".webm")):
                msg_type = "video"
                api_url = VIDEO_API_URL
            else:
                msg_type = "document"
                api_url = DOCUMENT_API_URL
            media_link = media_url.strip()
            message_body = ""
            caption = media_caption.strip()
        else:
            msg_type = "text"
            api_url = TEXT_API_URL
            media_link = ""
            message_body = message_text.strip()
            caption = ""

        # Save locally
        cursor.execute("""
            INSERT INTO messages (phone, message, direction, timestamp, message_type, media_link, caption)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (recipient, message_body, "outbound", datetime.utcnow(), msg_type, media_link, caption))
        conn.commit()
        st.success("✅ Message saved locally!")

        # send via API
        if API_ENABLED:
            headers = {
                "Authorization": f"App {API_KEY}",
                "Content-Type": "application/json",
                "Accept": "application/json"
            }
            message_id = str(uuid.uuid4())
            payload = {
                "from": SANDBOX_NUMBER,
                "to": recipient,
                "messageId": message_id,
                "content": {"text": message_body} if msg_type=="text" else {"mediaUrl": media_link, "caption": caption},
                "callbackData": "Callback data",
                "notifyUrl": f"{FASTAPI_PROXY_BASE}/whatsapp/inbound",
                "urlOptions": {"shortenUrl": True, "trackClicks": False, "removeProtocol": True}
            }
            try:
                response = requests.post(api_url, headers=headers, json=payload, timeout=15)
                if response.status_code in (200,201):
                    st.success(f"✅ Message sent successfully to {recipient}!")
                else:
                    st.error(f"❌ API failed: {response.status_code} {response.text}")
            except Exception as e:
                st.error(f"⚠️ Connection error: {e}")
    else:
        st.warning("Please fill recipient and message or media URL.")
