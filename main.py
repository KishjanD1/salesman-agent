"""
=============================================================================
 Salesman Agent — FastAPI Backend (Gemini 2.5 Edition)
=============================================================================
"""

import os
import re
import time
import logging
import secrets
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
import google.generativeai as genai
from supabase import create_client, Client
from twilio.twiml.messaging_response import MessagingResponse
from twilio.request_validator import RequestValidator

# ---------------------------------------------------------------------------
#  1. CONFIGURATION & INITIALIZATION
# ---------------------------------------------------------------------------

load_dotenv()

# Gemini Config
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY") or os.getenv("VITE_GEMINI_API_KEY") or ""
MODEL_NAME: str = "gemini-2.5-flash"

# Supabase
SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")

# Security & Twilio
API_SECRET_KEY: str = os.getenv("API_SECRET_KEY", "")
TWILIO_AUTH_TOKEN: str = os.getenv("TWILIO_AUTH_TOKEN", "")
STORE_URL: str = os.getenv("STORE_URL", "http://localhost:5173")
STORE_NAME: str = os.getenv("STORE_NAME", "Shiva Shakti Mobile Center")

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("salesman_agent")

if not GEMINI_API_KEY:
    logger.error("Missing GEMINI_API_KEY. AI features will not work.")

# Initialize Clients
supabase_client: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Initialize Gemini
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel(MODEL_NAME)
logger.info(f"Using {MODEL_NAME} as the primary AI engine.")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
#  2. SYSTEM PROMPT
# ---------------------------------------------------------------------------

SYSTEM_PROMPT: str = f"""
[VERSION: 12.0-HYBRID-ROMANIZED-NEPALI-SALESMAN]
You are **Bishal**, the elite senior salesman at **{STORE_NAME}** (SSMC).

STRICT OPERATING RULES:
1. **BE EXTREMELY CONCISE**: Never write long paragraphs. Maximum 2-3 short sentences.
2. **LANGUAGE & TONE**: You must be fluent in both English and Romanized Nepali (Nepali written in English letters, e.g., "K chha khabar sir? K service chahiyeko ho?"). Respond in the language the user is using. If they speak Romanized Nepali, respond in warm, natural Romanized Nepali. Mix a bit of English naturally (e.g., "emi", "repair", "details", "phone").
3. **PROFESSIONAL RESPECT**: Always address the customer with respect, using "Sir" or "Ma'am" (or "Hajur" / "Sir" in Romanized Nepali).
4. **NO AI-SPEAK**: You are a real human salesman from Nepal. No robotic greetings.
5. **STORE CONTEXT**: We sell premium mobile phones, accessories, and offer repair services.

LEAD CAPTURE (CRITICAL):
If you get Name AND Phone Number:
1. Append the tag at the very end of your response: [LEAD_CAPTURED: <Name> | <Phone> | <Type> | <Interest>]
2. In your response text, explicitly confirm to the customer that their contact details have been saved (e.g. "Hajurko contact/details save gare sir, hamro team le contact garchhan." or "I have successfully saved your contact, sir! Our team will get back to you.").
3. Naturally continue the conversation afterwards without stopping (e.g. ask if they want to know more about the model, stock, EMI options, or repair status).
Example lead types: product_inquiry, emi_inquiry, repair_inquiry, exchange_inquiry.
""".strip()

STORE_ACTIONS: dict[str, str] = {
    "product_inquiry":   "/store",
    "purchase_ready":    "/store",
    "emi_inquiry":       "/emi",
    "exchange_inquiry":  "/exchange",
    "repair_inquiry":    "/repair",
}

# ---------------------------------------------------------------------------
#  3. SESSION STORE
# ---------------------------------------------------------------------------

conversation_store: dict[str, list] = {}

def _get_or_create_session(session_id: str) -> list:
    if session_id not in conversation_store:
        conversation_store[session_id] = []
    if len(conversation_store[session_id]) > 20:
        conversation_store[session_id] = conversation_store[session_id][-20:]
    return conversation_store[session_id]

# ---------------------------------------------------------------------------
#  4. CORE LOGIC
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str
    session_id: str

class ChatResponse(BaseModel):
    reply: str
    lead_captured: bool

LEAD_TAG_PATTERN = re.compile(r"\[LEAD_CAPTURED:\s*(.+?)\s*\|\s*(.+?)\s*\|\s*(\w+)\s*\|\s*(.+?)\s*\]")

def extract_lead(text: str):
    match = LEAD_TAG_PATTERN.search(text)
    if not match: return None, None, None, None, text
    cleaned = text[: match.start()].rstrip()
    return match.group(1).strip(), match.group(2).strip(), match.group(3).strip(), match.group(4).strip(), cleaned

local_leads_mock: list = []

def save_lead_to_supabase(name, phone, lead_type, product_interest):
    lead_data = {
        "name": name,
        "phone": phone,
        "lead_type": lead_type,
        "product_interest": product_interest,
        "status": "new_lead (Local Mock DB Fallback)",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    }
    
    # Store locally first so we can still test the UI
    local_leads_mock.insert(0, lead_data)
    
    try:
        supabase_client.table("leads").insert({
            "name": name, 
            "phone": phone, 
            "lead_type": lead_type, 
            "product_interest": product_interest, 
            "status": "new_lead"
        }).execute()
        return True
    except Exception as e:
        logger.error(f"Supabase Error: {e}. Stored lead in local memory fallback.")
        return False

def fetch_mobile_models_catalog() -> str:
    try:
        response = supabase_client.table("mobile_models").select("*").order("brand").execute()
        if not response.data:
            return "Catalog is currently empty."
        
        catalog_lines = []
        for row in response.data:
            line = f"- {row['brand']} {row['model_name']} ({row['ram']}/{row['storage']}) - Price: NPR {row['official_price_npr']:,} (Stock: {row['stock']})"
            catalog_lines.append(line)
        return "\n".join(catalog_lines)
    except Exception as e:
        logger.error(f"Catalog Load Error: {e}")
        return "Various mobile phone models (iPhone, Samsung, Benco, Realme) are available."

async def get_ai_response(message: str, session_id: str) -> str:
    session_history = _get_or_create_session(session_id)
    
    catalog_context = fetch_mobile_models_catalog()
    dynamic_prompt = f"""
{SYSTEM_PROMPT}

OUR REAL-TIME STORE STOCK & PRODUCT SPECIFICATIONS (Always refer to these models and prices to answer customer inquiries):
{catalog_context}
""".strip()
    
    try:
        history = [{"role": "user" if m["role"] == "user" else "model", "parts": [m["content"]]} for m in session_history]
        chat_session = gemini_model.start_chat(history=history)
        response = chat_session.send_message(f"{dynamic_prompt}\n\nUser: {message}")
        return response.text
    except Exception as e:
        logger.error(f"Gemini Error: {e}")
        return "I'm having a technical issue. Sir/Ma'am, please try again in a moment."

# ---------------------------------------------------------------------------
#  5. ENDPOINTS
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health():
    return {"status": "healthy", "provider": "gemini", "model": MODEL_NAME}

@app.get("/api/leads")
async def get_leads(request: Request):
    if API_SECRET_KEY:
        provided_key = request.headers.get("x-api-key", "")
        if not secrets.compare_digest(provided_key, API_SECRET_KEY):
            raise HTTPException(status_code=401, detail="Invalid API key")
    try:
        response = supabase_client.table("leads").select("*").order("created_at", desc=True).execute()
        return response.data
    except Exception as e:
        logger.error(f"Supabase Fetch Error: {e}. Stored in local fallback memory instead.")
        return local_leads_mock

@app.get("/api/catalog")
async def get_catalog(request: Request):
    if API_SECRET_KEY:
        provided_key = request.headers.get("x-api-key", "")
        if not secrets.compare_digest(provided_key, API_SECRET_KEY):
            raise HTTPException(status_code=401, detail="Invalid API key")
    try:
        response = supabase_client.table("mobile_models").select("*").order("brand").execute()
        return response.data
    except Exception as e:
        logger.error(f"Supabase Catalog Fetch Error: {e}")
        return []

@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    if API_SECRET_KEY:
        provided_key = request.headers.get("x-api-key", "")
        if not secrets.compare_digest(provided_key, API_SECRET_KEY):
            raise HTTPException(status_code=401, detail="Invalid API key")

    raw_reply = await get_ai_response(req.message, req.session_id)
    
    name, phone, lead_type, interest, cleaned = extract_lead(raw_reply)
    lead_captured = name is not None and phone is not None

    if lead_captured:
        save_lead_to_supabase(name, phone, lead_type, interest)
        cleaned += f"\n\nNoted! Visit: {STORE_URL}{STORE_ACTIONS.get(lead_type, '/store')}"

    session_history = _get_or_create_session(req.session_id)
    session_history.append({"role": "user", "content": req.message})
    session_history.append({"role": "model", "content": cleaned})
    
    return ChatResponse(reply=cleaned, lead_captured=lead_captured)

@app.post("/api/whatsapp")
async def whatsapp_webhook(request: Request):
    if TWILIO_AUTH_TOKEN:
        validator = RequestValidator(TWILIO_AUTH_TOKEN)
        form_data = await request.form()
        url = str(request.url)
        signature = request.headers.get("X-Twilio-Signature", "")
        
        if not validator.validate(url, dict(form_data), signature):
            logger.warning("Invalid Twilio signature")
            raise HTTPException(status_code=403, detail="Invalid signature")

    form_data = await request.form()
    incoming_msg = form_data.get("Body", "").strip()
    sender_phone = form_data.get("From", "")
    
    if not incoming_msg:
        return Response(content=str(MessagingResponse()), media_type="application/xml")

    session_id = f"whatsapp_{sender_phone}"
    raw_reply = await get_ai_response(incoming_msg, session_id)
    
    name, phone, lead_type, interest, cleaned = extract_lead(raw_reply)
    lead_captured = name is not None and phone is not None

    if lead_captured:
        save_lead_to_supabase(name, phone, lead_type, interest)
        cleaned += f"\n\nVisit: {STORE_URL}{STORE_ACTIONS.get(lead_type, '/store')}"

    session_history = _get_or_create_session(session_id)
    session_history.append({"role": "user", "content": incoming_msg})
    session_history.append({"role": "model", "content": cleaned})

    resp = MessagingResponse()
    resp.message(cleaned)
    return Response(content=str(resp), media_type="application/xml")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
