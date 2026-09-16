import os
import psycopg2
import threading
import time
import urllib.parse
import uuid
import hashlib
import io
from datetime import datetime, date
import streamlit as st
import streamlit.components.v1 as components
import requests
from streamlit_autorefresh import st_autorefresh
from dotenv import load_dotenv
from pydub import AudioSegment

load_dotenv()

# -----------------------------
# Page config (must be first st call)
# -----------------------------
st.set_page_config(page_title="WhatsApp Chat Dashboard", page_icon="💬", layout="centered")

# -----------------------------
# ✅ WhatsApp-style theme (CSS injection)
# -----------------------------
def inject_whatsapp_theme():
    st.markdown("""
    <style>
        [data-testid="stAppViewContainer"] {
            background-color: #ECE5DD;
        }
        [data-testid="stHeader"] {
            background-color: rgba(0,0,0,0);
        }
        [data-testid="stSidebar"] {
            background-color: #075E54;
        }
        [data-testid="stSidebar"] * {
            color: #F0F2F1 !important;
        }
        h1, h2, h3 {
            color: #075E54 !important;
        }

        .stButton > button {
            background-color: #00A884;
            color: white;
            border-radius: 20px;
            border: none;
            padding: 0.5em 1.2em;
            font-weight: 600;
        }
        .stButton > button:hover {
            background-color: #06997A;
            color: white;
        }

        [data-testid="stSidebar"] [data-baseweb="select"] > div {
            background-color: #128C7E;
            border-radius: 8px;
            border: none;
            color: white;
        }

        div[data-baseweb="input"] > div,
        div[data-baseweb="textarea"] > div {
            border-radius: 20px !important;
            border: 1px solid #D1D7D3 !important;
            background-color: #FFFFFF !important;
        }
        div[data-baseweb="input"] > div:focus-within,
        div[data-baseweb="textarea"] > div:focus-within {
            border: 1px solid #00A884 !important;
            box-shadow: 0 0 0 1px #00A884 !important;
        }

        div[data-testid="stAlert"] {
            border-radius: 10px;
        }

        ::-webkit-scrollbar {
            width: 8px;
        }
        ::-webkit-scrollbar-thumb {
            background: #B7C2BE;
            border-radius: 10px;
        }

        /* Tighten spacing for the bubble/menu column rows */
        div[data-testid="column"] {
            padding-top: 0px;
            padding-bottom: 0px;
        }
    </style>
    """, unsafe_allow_html=True)

inject_whatsapp_theme()

# -----------------------------
# Sidebar reset button
# -----------------------------
if st.sidebar.button("♻️ Reset App / Reconnect DB", key="reset_app_btn"):
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
    AUDIO_API_URL      = st.secrets["AUDIO_API_URL"]
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
    AUDIO_API_URL      = os.getenv("AUDIO_API_URL")
    NGROK_URL          = os.getenv("NGROK_URL")
    DATABASE_URL       = os.getenv("DATABASE_URL")
    FASTAPI_PROXY_BASE = os.getenv("FASTAPI_PROXY_BASE", "").rstrip("/")
    STREAMLIT_PUBLIC_URL = os.getenv("STREAMLIT_URL", "")

API_ENABLED = True

# -----------------------------
# Keep FastAPI and Streamlit warm
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
        cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS starred BOOLEAN DEFAULT FALSE")
        cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS wamid TEXT")
        conn.commit()
    return conn

def ensure_connection(conn):
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        return conn
    except Exception:
        st.cache_resource.clear()
        return get_db_connection()

conn = get_db_connection()

# -----------------------------
# Query helpers
# -----------------------------
def fetch_distinct_phones(conn):
    conn = ensure_connection(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT phone FROM messages ORDER BY phone")
        return [row[0] for row in cur.fetchall()]

@st.cache_data(ttl=300)
def fetch_contacts_cached(_conn):
    _conn = ensure_connection(_conn)
    with _conn.cursor() as cur:
        cur.execute("SELECT phone, name FROM contacts")
        return {phone: name for phone, name in cur.fetchall()}

def fetch_messages(conn, phone: str):
    conn = ensure_connection(conn)
    with conn.cursor() as cur:
        if phone == "All":
            cur.execute(
                "SELECT id, phone, message, direction, timestamp, message_type, media_link, caption, starred, wamid "
                "FROM messages ORDER BY timestamp DESC LIMIT 200"
            )
            rows = cur.fetchall()
            return list(reversed(rows))
        else:
            cur.execute(
                "SELECT id, phone, message, direction, timestamp, message_type, media_link, caption, starred, wamid "
                "FROM messages WHERE phone=%s ORDER BY timestamp ASC",
                (phone,)
            )
            return cur.fetchall()

def insert_message(conn, phone, message_text, direction, msg_type, media_link="", caption="", wamid=""):
    conn = ensure_connection(conn)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO messages (phone, message, direction, timestamp, message_type, media_link, caption, wamid)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (phone, message_text, direction, datetime.utcnow(), msg_type, media_link, caption, wamid))
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

@st.cache_data(ttl=300)
def fetch_message_count(_conn):
    _conn = ensure_connection(_conn)
    with _conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM messages")
        return cur.fetchone()[0]

def toggle_star(conn, message_id):
    conn = ensure_connection(conn)
    with conn.cursor() as cur:
        cur.execute("UPDATE messages SET starred = NOT starred WHERE id = %s", (message_id,))
    conn.commit()

def delete_message_by_id(conn, message_id):
    conn = ensure_connection(conn)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM messages WHERE id = %s", (message_id,))
    conn.commit()

st_autorefresh(interval=60000, key="messages_refresh")

conn = ensure_connection(conn)

conversation_keys = fetch_distinct_phones(conn)
contacts = fetch_contacts_cached(conn)

# -----------------------------
# Avatar helpers
# -----------------------------
AVATAR_COLORS = [
    "#25D366", "#128C7E", "#075E54", "#34B7F1",
    "#FF6B6B", "#F7B733", "#A66DD4", "#EE5A6F",
    "#4ECDC4", "#5C7AEA",
]
CIRCLE_EMOJIS = ["🟢", "🔵", "🟣", "🟠", "🔴", "🟡", "🟤", "⚫"]

def get_initials(name: str) -> str:
    name = (name or "?").strip()
    parts = [p for p in name.split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()

def get_avatar_color(key: str) -> str:
    h = int(hashlib.md5((key or "?").encode()).hexdigest(), 16)
    return AVATAR_COLORS[h % len(AVATAR_COLORS)]

def get_avatar_emoji(key: str) -> str:
    h = int(hashlib.md5((key or "?").encode()).hexdigest(), 16)
    return CIRCLE_EMOJIS[h % len(CIRCLE_EMOJIS)]

def render_avatar(name: str, key: str) -> str:
    initials = get_initials(name)
    color = get_avatar_color(key)
    return (
        f"<div style='width:36px; height:36px; border-radius:50%; background:{color}; "
        f"color:white; display:flex; align-items:center; justify-content:center; "
        f"font-size:13px; font-weight:600; flex-shrink:0;'>{initials}</div>"
    )

# -----------------------------
# ✅ Sidebar — dedicated All button + searchable dropdown
# -----------------------------
if "selected_phone" not in st.session_state:
    st.session_state.selected_phone = "All"

st.sidebar.title("📱 Contacts")

if st.sidebar.button("🌍 All conversations", key="all_conversations_btn", use_container_width=True):
    st.session_state.selected_phone = "All"
    st.rerun()

contact_options = ["All"] + conversation_keys

def format_contact_option(p):
    if p == "All":
        return "💬  All conversations"
    name = contacts.get(p, p)
    emoji = get_avatar_emoji(p)
    return f"{emoji}  {name}  ({p})"

selected_phone = st.sidebar.selectbox(
    "Select a conversation",
    contact_options,
    format_func=format_contact_option,
    index=contact_options.index(st.session_state.selected_phone)
        if st.session_state.selected_phone in contact_options else 0,
    key="contact_selectbox",
)
st.session_state.selected_phone = selected_phone

st.sidebar.write("---")
st.sidebar.write("Total contacts:", len(conversation_keys))

msg_count = fetch_message_count(conn)
st.sidebar.caption(f"📊 Total messages in DB: {msg_count:,}")

if st.sidebar.button("🔄 Refresh Now", key="refresh_now_btn"):
    st.cache_data.clear()
    st.rerun()

chat_messages = fetch_messages(conn, selected_phone)

# -----------------------------
# Timestamp helpers
# -----------------------------
def format_time_only(ts) -> str:
    try:
        t = ts.strftime("%I:%M %p").lstrip("0")
        return t
    except Exception:
        return str(ts)

def format_date_label(ts) -> str:
    try:
        msg_date = ts.date() if hasattr(ts, "date") else ts
        today = date.today()
        diff = (today - msg_date).days
        if diff == 0:
            return "Today"
        elif diff == 1:
            return "Yesterday"
        else:
            return msg_date.strftime("%B %d, %Y")
    except Exception:
        return str(ts)

# -----------------------------
# Media proxy helper
# -----------------------------
def build_proxy_url(media_identifier: str, direction: str = "inbound") -> str:
    if not media_identifier:
        return ""
    if direction == "outbound" or media_identifier.startswith("http"):
        return media_identifier
    encoded = urllib.parse.quote_plus(media_identifier)
    return f"{FASTAPI_PROXY_BASE}/media-proxy/{encoded}"

# -----------------------------
# ✅ Voice note helpers
# -----------------------------
def convert_to_ogg_opus(audio_bytes: bytes) -> bytes:
    audio = AudioSegment.from_file(io.BytesIO(audio_bytes))
    out_buffer = io.BytesIO()
    audio.export(out_buffer, format="ogg", codec="libopus")
    return out_buffer.getvalue()

def upload_audio_and_get_url(ogg_bytes: bytes) -> str:
    files = {"file": ("voice.ogg", ogg_bytes, "audio/ogg")}
    response = requests.post(
        f"{FASTAPI_PROXY_BASE}/upload-audio",
        files=files,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["url"]

# -----------------------------
# ✅ Send-message helper (reused by composer + forward)
# -----------------------------
def send_text_or_media(recipient_value, message_body, media_link, caption, msg_type, api_url, reply_wamid=None):
    headers = {
        "Authorization": f"App {API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    message_id = str(uuid.uuid4())
    content = {"text": message_body} if msg_type == "text" else {"mediaUrl": media_link, "caption": caption}
    payload = {
        "from": SANDBOX_NUMBER,
        "to": recipient_value,
        "messageId": message_id,
        "content": content,
        "callbackData": "Callback data",
        "notifyUrl": f"{FASTAPI_PROXY_BASE}/whatsapp/inbound",
        "urlOptions": {
            "shortenUrl": True,
            "trackClicks": False,
            "removeProtocol": True
        }
    }
    if reply_wamid:
        payload["context"] = {"messageId": reply_wamid}

    return requests.post(api_url, headers=headers, json=payload, timeout=15)

# -----------------------------
# ✅ Per-message "⋮" menu (Reply / Star / Forward / Delete)
# -----------------------------
def render_menu_popover(msg_id, message_text, msg_type, media_link, caption, wamid, starred):
    with st.popover("⋮"):
        if st.button("↩️ Reply", key=f"reply_{msg_id}", use_container_width=True):
            preview = (message_text or f"[{msg_type}]")[:80]
            st.session_state["reply_to"] = {"id": msg_id, "wamid": wamid, "preview": preview}
            st.rerun()

        star_label = "⭐ Unstar" if starred else "☆ Star"
        if st.button(star_label, key=f"star_{msg_id}", use_container_width=True):
            toggle_star(conn, msg_id)
            st.cache_data.clear()
            st.rerun()

        st.markdown("**➡️ Forward**")
        fwd_number = st.text_input("Number", key=f"fwd_number_{msg_id}", label_visibility="collapsed", placeholder="Forward to number")
        if st.button("Send forward", key=f"fwd_send_{msg_id}", use_container_width=True):
            fwd_number_clean = (fwd_number or "").strip()
            if fwd_number_clean:
                if msg_type in ("text", "contact") or not msg_type:
                    resp = send_text_or_media(fwd_number_clean, message_text or "", "", "", "text", TEXT_API_URL)
                else:
                    api_map = {
                        "image": IMAGE_API_URL, "video": VIDEO_API_URL,
                        "document": DOCUMENT_API_URL, "voice": AUDIO_API_URL, "audio": AUDIO_API_URL,
                    }
                    resp = send_text_or_media(fwd_number_clean, "", media_link, caption or "", msg_type, api_map.get(msg_type, DOCUMENT_API_URL))
                if resp.status_code in (200, 201):
                    insert_message(conn, fwd_number_clean, message_text or "", "outbound", msg_type, media_link or "", caption or "")
                    st.cache_data.clear()
                    st.success("Forwarded!")
                    st.rerun()
                else:
                    st.error(f"Forward failed: {resp.status_code} {resp.text}")
            else:
                st.warning("Enter a number to forward to.")

        st.markdown("---")

        if st.session_state.get(f"confirm_delete_{msg_id}"):
            st.warning("Delete this message from your dashboard? (Not from the customer's WhatsApp)")
            if st.button("✅ Confirm delete", key=f"del_confirm_{msg_id}", use_container_width=True):
                delete_message_by_id(conn, msg_id)
                st.cache_data.clear()
                st.session_state.pop(f"confirm_delete_{msg_id}", None)
                st.rerun()
        else:
            if st.button("🗑️ Delete", key=f"del_start_{msg_id}", use_container_width=True):
                st.session_state[f"confirm_delete_{msg_id}"] = True
                st.rerun()

# -----------------------------
# Bubble rendering
# -----------------------------
def render_bubble(msg_row, show_header: bool):
    msg_id, phone, message_text, direction, timestamp, msg_type, media_link, caption, starred, wamid = msg_row
    display_name = contacts.get(phone, phone)
    is_inbound   = direction == "inbound"
    bg           = "#FFFFFF" if is_inbound else "#DCF8C6"
    time_str     = format_time_only(timestamp)

    content_html = "<i>No content</i>"
    if msg_type in ("text", "contact") or not msg_type:
        content_html = message_text or "<i>No text content</i>"
    elif msg_type in ("image", "video", "document", "voice", "audio"):
        if media_link:
            proxy = build_proxy_url(media_link, direction)
            if msg_type == "image":
                content_html = f"<a href='{proxy}' target='_blank'><img src='{proxy}' style='max-width:220px; border-radius:8px; border:1px solid #ddd;'></a>"
            elif msg_type == "video":
                content_html = f"<a href='{proxy}' target='_blank'>View Video</a><br><video width='260' controls><source src='{proxy}' type='video/mp4'></video>"
            elif msg_type in ("voice", "audio"):
                content_html = f"<audio controls><source src='{proxy}' type='audio/ogg'></audio>"
            elif msg_type == "document":
                content_html = f"<a href='{proxy}' target='_blank'>Open Document</a>"
            if caption:
                content_html += f"<div style='margin-top:6px'>{caption}</div>"

    ticks_html = " <span style='color:#34B7F1;'>&#10003;&#10003;</span>" if not is_inbound else ""
    star_html = " ⭐" if starred else ""
    avatar_html = render_avatar(display_name, phone) if (show_header and is_inbound) else "<div style='width:36px; flex-shrink:0;'></div>"
    header_html = f"<b>{display_name} ({phone})</b><br>" if show_header else ""

    justify = "flex-start" if is_inbound else "flex-end"
    bubble_inner = (
        f"<div style='display:flex; justify-content:{justify};'>"
        f"<div style='max-width:100%; background:{bg}; padding:8px 10px; border-radius:10px; box-shadow:0 1px 2px rgba(0,0,0,0.15);'>"
        f"{header_html}"
        f"{content_html}"
        f"<div style='text-align:right; font-size:11px; color:#667781; margin-top:4px;'>{time_str}{ticks_html}{star_html}</div>"
        f"</div>"
        f"</div>"
    )

    if is_inbound:
        col_avatar, col_bubble, col_menu = st.columns([1, 7, 1])
        with col_avatar:
            st.markdown(avatar_html, unsafe_allow_html=True)
        with col_bubble:
            st.markdown(bubble_inner, unsafe_allow_html=True)
        with col_menu:
            render_menu_popover(msg_id, message_text, msg_type, media_link, caption, wamid, starred)
    else:
        col_menu, col_bubble, col_avatar = st.columns([1, 7, 1])
        with col_menu:
            render_menu_popover(msg_id, message_text, msg_type, media_link, caption, wamid, starred)
        with col_bubble:
            st.markdown(bubble_inner, unsafe_allow_html=True)
        with col_avatar:
            st.markdown(avatar_html, unsafe_allow_html=True)

def render_date_divider(label: str):
    divider = (
        f"<div style='text-align:center; margin:14px 0;'>"
        f"<span style='background:#E1F2FA; color:#54656F; font-size:12px; padding:4px 12px; border-radius:8px;'>{label}</span>"
        f"</div>"
    )
    st.markdown(divider, unsafe_allow_html=True)

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
    prev_phone = None
    prev_date = None
    for m in chat_messages:
        msg_id, phone, message_text, direction, timestamp, msg_type, media_link, caption, starred, wamid = m

        msg_date = timestamp.date() if hasattr(timestamp, "date") else timestamp
        if msg_date != prev_date:
            render_date_divider(format_date_label(timestamp))
            prev_date = msg_date
            prev_phone = None

        show_header = (phone != prev_phone)
        render_bubble(m, show_header)
        prev_phone = phone

# -----------------------------
# ✅ Auto-scroll to the latest message
# -----------------------------
st.markdown('<div id="bottom-anchor"></div>', unsafe_allow_html=True)

components.html(
    """
    <script>
        setTimeout(function() {
            var anchor = window.parent.document.getElementById('bottom-anchor');
            if (anchor) {
                anchor.scrollIntoView({behavior: "instant", block: "end"});
            }
        }, 300);
    </script>
    """,
    height=0,
)

# -----------------------------
# ✅ Reply preview (if replying to a message)
# -----------------------------
if st.session_state.get("reply_to"):
    reply_info = st.session_state["reply_to"]
    col_preview, col_cancel = st.columns([6, 1])
    with col_preview:
        st.info(f"↩️ Replying to: {reply_info['preview']}")
    with col_cancel:
        if st.button("✖", key="cancel_reply_btn"):
            st.session_state.pop("reply_to", None)
            st.rerun()

# -----------------------------
# ✅ Message composer — "+" attach menu, voice, text box, send button
# -----------------------------
st.write("")

if selected_phone == "All":
    recipient = st.text_input(
        "Recipient number (include country code)",
        key="recipient_input_all",
    )
else:
    recipient = selected_phone
    st.caption(f"Sending to: {contacts.get(selected_phone, selected_phone)} ({selected_phone})")

col_attach, col_form = st.columns([1, 8])

with col_attach:
    with st.popover("➕"):
        st.markdown("**Attach**")
        media_url = st.text_input(
            "📄 Document / 🖼 Photo / 🎬 Video (URL, https://)",
            key="media_url_input",
        )
        media_caption = st.text_input("Caption (optional)", key="media_caption_input")
        st.markdown("---")
        st.markdown("**🎙️ Voice note**")
        recorded_audio_widget = st.audio_input("Record a voice note", key="voice_recorder")
        if recorded_audio_widget is not None:
            st.session_state["captured_voice_bytes"] = recorded_audio_widget.getvalue()
            st.caption("✅ Recording captured — ready to send")
        if st.session_state.get("captured_voice_bytes") and st.button("🗑️ Discard recording", key="discard_voice_btn"):
            st.session_state.pop("captured_voice_bytes", None)
            st.rerun()

with col_form:
    with st.form(key="send_message_form", clear_on_submit=True):
        col_input, col_send = st.columns([6, 1])
        with col_input:
            message_text = st.text_input(
                "Message",
                placeholder="Type a message",
                label_visibility="collapsed",
            )
        with col_send:
            send_clicked = st.form_submit_button("➤")

if send_clicked:
    recipient_value = (recipient or "").strip()
    message_value = (message_text or "").strip()
    media_url_value = (media_url or "").strip()
    media_caption_value = (media_caption or "").strip()
    captured_voice_bytes = st.session_state.get("captured_voice_bytes")
    reply_info = st.session_state.get("reply_to")
    reply_wamid = reply_info["wamid"] if reply_info else None

    if not recipient_value:
        st.warning("Please select or enter a recipient.")
    elif captured_voice_bytes:
        try:
            with st.spinner("Converting and uploading voice note..."):
                ogg_bytes = convert_to_ogg_opus(captured_voice_bytes)
                public_url = upload_audio_and_get_url(ogg_bytes)

            conn = ensure_connection(conn)
            insert_message(conn, recipient_value, "", "outbound", "voice", public_url, "")
            st.cache_data.clear()
            st.success("✅ Voice note saved locally!")

            if API_ENABLED:
                resp = send_text_or_media(recipient_value, "", public_url, "", "voice", AUDIO_API_URL)
                if resp.status_code in (200, 201):
                    st.success(f"✅ Voice note sent to {recipient_value}!")
                else:
                    st.error(f"❌ API failed: {resp.status_code} {resp.text}")

            st.session_state.pop("captured_voice_bytes", None)
            st.session_state.pop("reply_to", None)
            st.rerun()
        except Exception as e:
            st.error(f"⚠️ Voice note error: {e}")

    elif recipient_value and (message_value or media_url_value):
        conn = ensure_connection(conn)

        if media_url_value:
            url_lower = media_url_value.lower()
            if url_lower.endswith((".jpg", ".jpeg", ".png", ".gif")):
                msg_type = "image"
                api_url  = IMAGE_API_URL
            elif url_lower.endswith((".mp4", ".mov", ".webm")):
                msg_type = "video"
                api_url  = VIDEO_API_URL
            else:
                msg_type = "document"
                api_url  = DOCUMENT_API_URL

            media_link   = media_url_value
            message_body = ""
            caption      = media_caption_value
        else:
            msg_type     = "text"
            api_url      = TEXT_API_URL
            media_link   = ""
            message_body = message_value
            caption      = ""

        insert_message(conn, recipient_value, message_body, "outbound", msg_type, media_link, caption)
        st.cache_data.clear()
        st.success("✅ Message saved locally!")

        if API_ENABLED:
            try:
                resp = send_text_or_media(recipient_value, message_body, media_link, caption, msg_type, api_url, reply_wamid=reply_wamid)
                if resp.status_code in (200, 201):
                    st.success(f"✅ Message sent successfully to {recipient_value}!")
                else:
                    st.error(f"❌ API failed: {resp.status_code} {resp.text}")
            except Exception as e:
                st.error(f"⚠️ Connection error: {e}")

        st.session_state.pop("reply_to", None)
        st.rerun()
    else:
        st.warning("Please fill recipient and message or media URL.")