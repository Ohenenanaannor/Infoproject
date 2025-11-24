# webhook.py
import os
import logging
import urllib.parse
import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from datetime import datetime
from dotenv import load_dotenv
import psycopg2

load_dotenv()
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="WhatsApp Webhook")

DATABASE_URL = os.getenv("DATABASE_URL")
API_KEY = os.getenv("API_KEY")
SENDER_NUMBER = os.getenv("SENDER_NUMBER")
MEDIA_BASE_URL = "https://api.infobip.com"
AUTH_HEADER = f"App {API_KEY}" if API_KEY else None

if not DATABASE_URL:
    logging.error("DATABASE_URL not set. Exiting.")
    raise RuntimeError("DATABASE_URL is required")

# -----------------------------
# Database helpers
# -----------------------------
def get_pg_connection():
    return psycopg2.connect(DATABASE_URL)

def ensure_db():
    conn = get_pg_connection()
    with conn.cursor() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS contacts (
            id SERIAL PRIMARY KEY,
            phone TEXT UNIQUE,
            name TEXT
        )
        """)
        c.execute("""
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
        conn.commit()
    conn.close()

def insert_message(conn, phone, message_text, direction, msg_type, media_link="", caption=""):
    with conn.cursor() as cursor:
        cursor.execute("""
            INSERT INTO messages (phone, message, direction, timestamp, message_type, media_link, caption)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (phone, message_text, direction, datetime.utcnow(), msg_type, media_link, caption))
    conn.commit()

def upsert_contact(conn, phone, name):
    with conn.cursor() as cursor:
        cursor.execute("""
            INSERT INTO contacts (phone, name)
            VALUES (%s, %s)
            ON CONFLICT(phone) DO UPDATE SET name=EXCLUDED.name
        """, (phone, name))
    conn.commit()

ensure_db()

# -----------------------------
# Health endpoint
# -----------------------------
@app.get("/health")
def health():
    return {"status": "ok"}

# -----------------------------
# Helpers for parsing messages
# -----------------------------
def extract_media_id_from_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlparse(url)
        return parsed.path.rstrip("/").split("/")[-1]
    except Exception:
        return url

def parse_infobip_message(msg):
    sender = msg.get("from")
    if not sender:
        return None
    contact_name = msg.get("contact", {}).get("name", "").strip() or sender
    content = msg.get("message", {}) or {}
    msg_type_raw = str(content.get("type", "TEXT")).upper()

    text = ""
    media_identifier = ""
    caption = ""
    msg_type = "text"

    if msg_type_raw == "TEXT":
        t = content.get("text", "")
        text = t.get("body","") if isinstance(t, dict) else str(t or "")
        msg_type = "text"
    elif msg_type_raw in ("IMAGE","VIDEO","DOCUMENT","VOICE","AUDIO"):
        msg_type = msg_type_raw.lower()
        caption = content.get("caption","") or ""
        media_id = content.get("mediaId") or content.get("id")
        media_url = content.get("url") or content.get("mediaUrl")
        if media_id:
            media_identifier = str(media_id)
        elif media_url:
            media_identifier = extract_media_id_from_url(media_url)
    else:
        t = content.get("text", "")
        text = t.get("body","") if isinstance(t, dict) else str(t or "")
        msg_type = "text"

    return text, msg_type, media_identifier, caption, sender, contact_name

# -----------------------------
# Inbound webhook
# -----------------------------
@app.post("/whatsapp/inbound")
async def inbound(request: Request):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    results = payload.get("results", []) or payload.get("messages", [])
    if not results:
        return JSONResponse({"status":"ok","received":0})

    conn = get_pg_connection()
    received = 0
    try:
        for msg in results:
            parsed = parse_infobip_message(msg)
            if not parsed:
                continue
            text, msg_type, media_id, caption, sender, name = parsed
            upsert_contact(conn, sender, name)
            insert_message(conn, sender, text, "inbound", msg_type, media_id, caption)
            received += 1
        return {"status":"ok","received":received}
    finally:
        conn.close()

# -----------------------------
# Media proxy endpoint
# -----------------------------
@app.get("/media-proxy/{media_identifier}")
def media_proxy(media_identifier: str):
    if not AUTH_HEADER or not SENDER_NUMBER:
        raise HTTPException(status_code=500, detail="Media proxy misconfigured")

    media_id = urllib.parse.unquote_plus(media_identifier)
    url = f"{MEDIA_BASE_URL}/whatsapp/1/senders/{SENDER_NUMBER}/media/{media_id}"
    headers = {"Authorization": AUTH_HEADER, "Accept": "*/*"}

    try:
        resp = requests.get(url, headers=headers, stream=True, timeout=30)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Error fetching media: {e}")

    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=f"Infobip error: {resp.status_code} {resp.text[:500]}")

    content_type = resp.headers.get("Content-Type","application/octet-stream")

    def iter_stream():
        try:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk
        finally:
            resp.close()

    return StreamingResponse(iter_stream(), media_type=content_type)

# -----------------------------
# Run server locally (for dev)
# -----------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
