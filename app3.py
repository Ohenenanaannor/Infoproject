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
# Page config (must be first st call)
# -----------------------------
st.set_page_config(page_title="WhatsApp Chat Dashboard", page_icon="💬", layout="centered")

# -----------------------------
# Sidebar reset button
# -----------------------------
if st.sidebar.button("♻️ Reset App / Reconnect DB"):
    st.cache_resource.clear()
    st.cache_data.clear()
    st.session_state.clear()
    st.rerun()

# -----------------------------
# Authentication
# -----------------------------
try:
    APP_PASSWORD = st.secrets["APP_PASSWORD"]
    APP_USERNAME = st.secrets.get("APP_USERNAME", "admin")
except Exception:
    APP_PASSWORD = os.getenv("APP_PASSWORD")
    APP_USERNAME = os.getenv("APP_USERNAME", "admin")

st.title("🔐 WhatsApp Conversation")

if "logged_in" not in st.session_state:
    st.session_state.logged_in = False

if not st.session_state.logged_in:
    with st.form("login_form"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        login_btn = st.form_submit_button("🔓 Login")

    if login_btn:
        if username == APP_USERNAME and password == APP_PASSWORD:
            st.session_state.logged_in = True
            st.rerun()
        else:
            st.error("❌ Incorrect username or password")
    st.stop()

st.success("🎉 Authenticated! Loading dashboard…")

# -----------------------------
# Config
# -----------------------------
try:
    API_KEY            = st.secrets["API_KEY"]
    SANDBOX_NUMBER     = st.secrets["SANDBOX_NUMBER"]
    TEXT_API_URL       = st.secrets["TEXT_API_URL"]
    IMAGE_API_URL      = st.secrets["IMAGE_API_URL"]
    VIDEO_API_URL      = st.secrets["VIDEO_API_URL"]
    DOCUMENT_API_URL   = st.secrets["DOCUMENT_API_URL"]
    NGROK_URL          = st.secrets["NGROK_URL"]
    DATABASE_URL       = st.secrets["DATABASE_URL"]
    FASTAPI_PROXY_BASE = st.secrets["FASTAPI_PROXY_BASE"].rstrip("/")
    STREAMLIT_PUBLIC_URL = st.secrets.get("STREAMLIT_URL") or ""
except Exception:
    API_KEY            = os.getenv("API_KEY")
    SANDBOX_NUMBER     = os.getenv("SANDBOX_NUMBER")
    TEXT_API_URL       = os.getenv("TEXT_API_URL")
    IMAGE_API_URL      = os.getenv("IMAGE_API_URL")
    VIDEO_API_URL      = os.getenv("VIDEO_API_URL")
    DOCUMENT_API_URL   = os.getenv("DOCUMENT_API_URL")
    NGROK_URL          = os.getenv("NGROK_URL")
    DATABASE_URL       = os.getenv("DATABASE_URL")
    FASTAPI_PROXY_BASE = os.getenv("FASTAPI_PROXY_BASE", "").rstrip("/")
    STREAMLIT_PUBLIC_URL = os.getenv("STREAMLIT_URL", "")

API_ENABLED = True

# -----------------------------
# Keep FastAPI and Streamlit warm
# Pings every 5 min — does NOT touch Neon DB
# -----------------------------
def ping_url(url):
    try:
        requests.get(url, timeout=6)
    except Exception:
        pass

def pinger_loop():
    fastapi_health = f"{FASTAPI_PROXY_BASE}/health"
    streamlit_url  = STREAMLIT_PUBLIC_URL.rstrip("/")
    while True:
        ping_url(fastapi_health)
        ping_url(streamlit_url)
        time.sleep(300)

threading.Thread(target=pinger_loop, daemon=True).start()

# -----------------------------
# Database connection (cached for life of session)
# -----------------------------
@st.cache_resource
def get_db_connection():
    conn = psycopg2.connect(DATABASE_URL)
    with conn.cursor() as cur:
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
    return conn

def ensure_connection(conn):
    """Reconnect only if the connection is genuinely broken."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        return conn
    except Exception:
        st.cache_resource.clear()
        return get_db_connection()

conn = get_db_connection()

# -----------------------------
# ✅ FIX 1 — Lightweight sidebar query
# Fetches only distinct phone numbers — NOT all message content.
# This was the single biggest source of unnecessary data transfer.
# -----------------------------
def fetch_distinct_phones(conn):
    conn = ensure_connection(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT phone FROM messages ORDER BY phone")
        return [row[0] for row in cur.fetchall()]

# -----------------------------
# ✅ FIX 2 — Contacts cached for 5 minutes
# Contacts rarely change, no need to re-fetch every 60 seconds.
# _conn is prefixed with _ so Streamlit doesn't try to hash it.
# -----------------------------
@st.cache_data(ttl=300)
def fetch_contacts_cached(_conn):
    _conn = ensure_connection(_conn)
    with _conn.cursor() as cur:
        cur.execute("SELECT phone, name FROM contacts")
        return {phone: name for phone, name in cur.fetchall()}

# -----------------------------
# ✅ FIX 3 — Message fetch scoped to selected conversation only
# We no longer fetch ALL messages on every refresh.
# Only the selected contact's messages are pulled each cycle.
# -----------------------------
def fetch_messages(conn, phone: str):
    conn = ensure_connection(conn)
    with conn.cursor() as cur:
        if phone == "All":
            # "All" view: fetch only recent 200 messages to cap payload
            cur.execute(
                "SELECT * FROM messages ORDER BY timestamp DESC LIMIT 200"
            )
            rows = cur.fetchall()
            return list(reversed(rows))
        else:
            cur.execute(
                "SELECT * FROM messages WHERE phone=%s ORDER BY timestamp ASC",
                (phone,)
            )
            return cur.fetchall()

def insert_message(conn, phone, message_text, direction, msg_type, media_link="", caption=""):
    conn = ensure_connection(conn)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO messages (phone, message, direction, timestamp, message_type, media_link, caption)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (phone, message_text, direction, datetime.utcnow(), msg_type, media_link, caption))
    conn.commit()

def upsert_contact(conn, phone, name):
    conn = ensure_connection(conn)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO contacts (phone, name)
            VALUES (%s, %s)
            ON CONFLICT(phone) DO UPDATE SET name=EXCLUDED.name
        """, (phone, name))
    conn.commit()

# -----------------------------
# ✅ FIX 4 — Message count monitor
# Lets you see DB growth without running a full SELECT *
# -----------------------------
@st.cache_data(ttl=300)
def fetch_message_count(_conn):
    _conn = ensure_connection(_conn)
    with _conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM messages")
        return cur.fetchone()[0]

# -----------------------------
# ✅ FIX 5 — Autorefresh slowed from 15s → 60s
# 4× fewer DB round-trips per hour at no cost to usability.
# -----------------------------
st_autorefresh(interval=60000, key="messages_refresh")

# -----------------------------
# Sidebar
# -----------------------------
conn = ensure_connection(conn)

# Cheap: only phone numbers
conversation_keys = fetch_distinct_phones(conn)

# Cheap: cached for 5 min
contacts = fetch_contacts_cached(conn)

contact_display_names = ["All"] + [
    f"{contacts.get(p, p)} ({p})" for p in conversation_keys
]

st.sidebar.title("📱 Contacts")
selected_display = st.sidebar.selectbox("Select a conversation", contact_display_names)
st.sidebar.write("---")
st.sidebar.write("Total contacts:", len(conversation_keys))

# DB size monitor — cached, so costs nothing extra mid-cycle
msg_count = fetch_message_count(conn)
st.sidebar.caption(f"📊 Total messages in DB: {msg_count:,}")

if st.sidebar.button("🔄 Refresh Now"):
    st.cache_data.clear()
    st.rerun()

selected_phone = (
    "All" if selected_display == "All"
    else selected_display.split("(")[-1].replace(")", "")
)

# Only fetch messages for the selected conversation
chat_messages = fetch_messages(conn, selected_phone)

# -----------------------------
# Helpers
# -----------------------------
def build_proxy_url(media_identifier: str, direction: str = "inbound") -> str:
    if not media_identifier:
        return ""
    if direction == "outbound" or media_identifier.startswith("http"):
        return media_identifier
    encoded = urllib.parse.quote_plus(media_identifier)
    return f"{FASTAPI_PROXY_BASE}/media-proxy/{encoded}"

def render_bubble(msg_row):
    _, phone, message_text, direction, timestamp, msg_type, media_link, caption = msg_row
    display_name = f"{contacts.get(phone, phone)} ({phone})"
    is_inbound   = direction == "inbound"
    align        = "flex-start" if is_inbound else "flex-end"
    bg           = "#ffffff" if is_inbound else "#dcf8c6"

    content_html = "<i>No content</i>"
    if msg_type == "text" or not msg_type:
        content_html = message_text or "<i>No text content</i>"
    elif msg_type in ("image", "video", "document", "voice", "audio"):
        if media_link:
            proxy = build_proxy_url(media_link, direction)
            if msg_type == "image":
                content_html = (
                    f"<a href='{proxy}' target='_blank'>"
                    f"<img src='{proxy}' style='max-width:220px; border-radius:8px; border:1px solid #ddd;'>"
                    f"</a>"
                )
            elif msg_type == "video":
                content_html = (
                    f"<a href='{proxy}' target='_blank'>View Video</a><br>"
                    f"<video width='260' controls><source src='{proxy}' type='video/mp4'></video>"
                )
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

# -----------------------------
# Chat view
# -----------------------------
st.title("💬 WhatsApp Chat Dashboard (Live)")

if selected_phone == "All":
    st.subheader("💬 All Conversations (last 200 messages)")
else:
    label = f"{contacts.get(selected_phone, selected_phone)} ({selected_phone})"
    st.subheader(f"💬 Chat with: {label}")

if not chat_messages:
    st.info("No messages yet for this contact.")
else:
    for m in chat_messages:
        render_bubble(m)

# -----------------------------
# Send new message
# -----------------------------
st.subheader("Send a New WhatsApp Message")
recipient    = st.text_input("Recipient number (include country code)")
message_text = st.text_area("Message (text only)")
media_url    = st.text_input("Image/Video/Document URL (optional, must start with https://)")
media_caption = st.text_input("Caption (optional)")

if st.button("Send"):
    if recipient.strip() and (message_text.strip() or media_url.strip()):
        conn = ensure_connection(conn)

        if media_url.strip():
            url_lower = media_url.lower()
            if url_lower.endswith((".jpg", ".jpeg", ".png", ".gif")):
                msg_type = "image"
                api_url  = IMAGE_API_URL
            elif url_lower.endswith((".mp4", ".mov", ".webm")):
                msg_type = "video"
                api_url  = VIDEO_API_URL
            else:
                msg_type = "document"
                api_url  = DOCUMENT_API_URL

            media_link   = media_url.strip()
            message_body = ""
            caption      = media_caption.strip()
        else:
            msg_type     = "text"
            api_url      = TEXT_API_URL
            media_link   = ""
            message_body = message_text.strip()
            caption      = ""

        insert_message(conn, recipient, message_body, "outbound", msg_type, media_link, caption)
        # Invalidate the contacts cache so new recipients appear immediately
        st.cache_data.clear()
        st.success("✅ Message saved locally!")

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
                "content": (
                    {"text": message_body} if msg_type == "text"
                    else {"mediaUrl": media_link, "caption": caption}
                ),
                "callbackData": "Callback data",
                "notifyUrl": f"{FASTAPI_PROXY_BASE}/whatsapp/inbound",
                "urlOptions": {
                    "shortenUrl": True,
                    "trackClicks": False,
                    "removeProtocol": True
                }
            }
            try:
                response = requests.post(api_url, headers=headers, json=payload, timeout=15)
                if response.status_code in (200, 201):
                    st.success(f"✅ Message sent successfully to {recipient}!")
                else:
                    st.error(f"❌ API failed: {response.status_code} {response.text}")
            except Exception as e:
                st.error(f"⚠️ Connection error: {e}")
    else:
        st.warning("Please fill recipient and message or media URL.")