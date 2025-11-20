import os
import psycopg2
import urllib.parse
import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse
from datetime import datetime
import uvicorn
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

# -----------------------------
# Neon / Postgres
# -----------------------------
DATABASE_URL = os.getenv("DATABASE_URL")

def get_pg_connection():
    return psycopg2.connect(DATABASE_URL)

def ensure_db():
    conn = get_pg_connection()
    c = conn.cursor()
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

ensure_db()

# -----------------------------
# Infobip config
# -----------------------------
API_KEY = os.getenv("API_KEY")
AUTH_HEADER = f"App {API_KEY}"  
SENDER_NUMBER = os.getenv("SENDER_NUMBER")
MEDIA_BASE_URL = "https://api.infobip.com"

# -----------------------------
# Helpers
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

    return text, msg_type, media_identifier, caption, sender, contact_name

# -----------------------------
# Inbound webhook
# -----------------------------
@app.post("/whatsapp/inbound")
async def inbound(request: Request):
    payload = await request.json()
    results = payload.get("results", []) or payload.get("messages", [])
    conn = get_pg_connection()
    c = conn.cursor()
    received = 0
    for msg in results:
        parsed = parse_infobip_message(msg)
        if not parsed:
            continue
        text, msg_type, media_id, caption, sender, name = parsed
        # Upsert contact
        c.execute("""
            INSERT INTO contacts (phone, name)
            VALUES (%s, %s)
            ON CONFLICT(phone) DO UPDATE SET name=EXCLUDED.name
        """, (sender, name))
        # Insert message
        c.execute("""
            INSERT INTO messages (phone, message, direction, timestamp, message_type, media_link, caption)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (sender, text, "inbound", datetime.now(), msg_type, media_id, caption))
        received += 1
    conn.commit()
    conn.close()
    return {"status":"ok","received":received}

# -----------------------------
# Media proxy
# -----------------------------
@app.get("/media-proxy/{media_identifier}")
def media_proxy(media_identifier: str):
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
# Run server
# -----------------------------
if __name__=="__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
