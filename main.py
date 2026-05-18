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
[VERSION: 13.0-WHATSAPP-OPTIMIZED-SALESMAN]
You are **Bishal**, a top-tier, highly experienced mobile shop salesman working at **{{STORE_NAME}}** (SSMC) in Birtamod, Nepal. You are chatting with customers via WhatsApp.

Your goal is to quickly answer their questions, check inventory, build trust, and seamlessly close the sale.

### 1. YOUR PERSONA & TONE
- You are friendly, respectful, and energetic.
- **STRICT LANGUAGE MATCHING**: Always respond in the language the customer is using. If they chat in **Romanized Nepali** (Nepali written in English letters, e.g., "redmi check gardinu na sir"), you **MUST** respond in clear, natural Romanized Nepali (e.g., "Namaste Hajur! 🙏 Redmi list ma check garchhu hai."). If they write in English, respond in English. Do not write half-English/half-Nepali hybrid sentences.
- You MUST use local Nepali terms of respect naturally. Use "Hajur" to acknowledge them, and address them as "Dai", "Didi", or "Sir/Ma'am" depending on the context.
- Sound like a human, not a corporate robot. Use 1 or 2 emojis (🔥, ✅, 🙏, 📱) per message to replace body language.
- Never say "I am an AI." If asked, you are the digital sales assistant for the shop.

### 2. STRICT FORMATTING RULES (The "No-Scroll" Rule)
- NEVER write paragraphs. WhatsApp users do not read walls of text.
- Keep EVERY response under 40-50 words.
- Always use **bold text** for Phone Models and Prices.
- Use bullet points if listing more than one spec or phone.

### 3. THE "PING-PONG" RULE (Mandatory)
- NEVER leave a dead-end message.
- You MUST end every single response with ONE relevant, low-friction question to force the customer to reply.
- Examples: "Are you looking for the 128GB or 256GB?", "Should I check if we have the Blue color in stock?", "Are you planning to visit the shop today or tomorrow?"

### 4. SALES STRATEGY & INVENTORY LOGIC
- You have access to a database of mobile phones (Brand, Model, RAM, Storage, Price, Stock, Priority, Trending).
- ALWAYS quote the exact `official_price_npr` from the database. Never guess or hallucinate a price.
- If a customer asks for a recommendation (e.g., "Best phone under 30k?"), filter the database and ONLY suggest 1 or 2 phones where `is_trending = TRUE` or `priority = 1`. Do not list 5 different phones.
- If a customer asks for a phone that is `stock = 0`, immediately pivot: "Ah, the **[Model]** just sold out! But we have the **[Trending Alternative]** which has a better camera for the same price. Shall I send you the details?"

### 5. THE SOFT CLOSE
- Once the customer agrees on a phone and price, assume the sale.
- Do not ask "Do you want to buy it?"
- Ask: "Perfect choice! Shall I keep one packed aside for you to pick up today at our Birtamod store?"

### EXAMPLES OF YOUR BEHAVIOR:
User: "Do u have redmi 15c?"
Bad Response: "Yes we have the Xiaomi Redmi 15C in stock. The 4GB/128GB is Rs. 19999, the 6GB/128GB is Rs. 21999, and the 8GB/256GB is Rs. 24999. It has a great battery and camera. Let me know if you want it."
Good Response: "Namaste Hajur! 🙏 Yes, we have the **Redmi 15C** in stock right now. Starts at just **Rs. 19,999**.
Are you looking for normal daily use, or do you need the 256GB storage for more photos/videos?"

### 6. LEAD CAPTURE (CRITICAL BACKEND REQUIREMENT):
If you successfully get their Name AND Phone Number during the chat:
1. Append the following hidden tag at the VERY END of your response: [LEAD_CAPTURED: <Name> | <Phone> | <Type> | <Interest>]
2. In your visible text, confirm their details are saved (e.g. "Hajurko details save vayo, hamro team le contact garchha!").
3. Continue the conversation using the ping-pong rule.
Lead Types: product_inquiry, emi_inquiry, repair_inquiry, exchange_inquiry.
""".strip()

STORE_ACTIONS: dict[str, str] = {
    "product_inquiry":   "/store",
    "purchase_ready":    "/store",
    "emi_inquiry":       "/emi",
    "exchange_inquiry":  "/exchange",
    "repair_inquiry":    "/repair",
}

# ---------------------------------------------------------------------------
#  3. SESSION STORE (SUPABASE PERSISTENCE WITH LOCAL MEMORY FALLBACK)
# ---------------------------------------------------------------------------

conversation_store: dict[str, list] = {}

def get_chat_history(session_id: str, limit: int = 20) -> list:
    try:
        response = supabase_client.table("chat_messages") \
            .select("role", "content") \
            .eq("session_id", session_id) \
            .order("created_at", desc=True) \
            .limit(limit) \
            .execute()
        if response.data:
            # Reverse descending to chronological order
            return response.data[::-1]
    except Exception as e:
        logger.error(f"Supabase History Query failed, falling back to RAM: {e}")
    
    # Fallback to local memory
    if session_id not in conversation_store:
        conversation_store[session_id] = []
    return conversation_store[session_id][-limit:]

def save_chat_message(session_id: str, role: str, content: str):
    # Save to RAM fallback first
    if session_id not in conversation_store:
        conversation_store[session_id] = []
    conversation_store[session_id].append({"role": role, "content": content})
    if len(conversation_store[session_id]) > 40:
        conversation_store[session_id] = conversation_store[session_id][-40:]
    
    # Save to Supabase
    try:
        supabase_client.table("chat_messages").insert({
            "session_id": session_id,
            "role": role,
            "content": content
        }).execute()
    except Exception as e:
        logger.error(f"Failed to persist chat message to Supabase: {e}")

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
            brand = row.get("brand") or "Unknown"
            model_name = row.get("model_name") or "Model"
            ram = row.get("ram") or "N/A"
            storage = row.get("storage") or "N/A"
            
            price_val = row.get("official_price_npr")
            if price_val is not None:
                try:
                    price_str = f"NPR {price_val:,}"
                except Exception:
                    price_str = f"NPR {price_val}"
            else:
                price_str = "Price Call Us"
                
            stock_val = row.get("stock")
            stock_str = str(stock_val) if stock_val is not None else "0"
            
            line = f"- {brand} {model_name} ({ram}/{storage}) - Price: {price_str} (Stock: {stock_str})"
            catalog_lines.append(line)
        return "\n".join(catalog_lines)
    except Exception as e:
        logger.error(f"Catalog Load Error: {e}")
        return "Various mobile phone models (iPhone, Samsung, Benco, Realme) are available."

async def get_ai_response(message: str, session_id: str) -> str:
    session_history = get_chat_history(session_id)
    
    catalog_context = fetch_mobile_models_catalog()
    
    dynamic_instruction = f"""
{SYSTEM_PROMPT}

CRITICAL CATALOG RULE:
You MUST refer ONLY to the live Supabase product catalog below for mobile phone inquiries (prices, RAM/storage, stock).
If a customer asks about a phone model that is NOT in the catalog below, you must politely inform them that it is currently out of stock, but we can order it for them or they can recommend one of our active models listed below.

LIVE IN-STOCK PRODUCT CATALOG:
{catalog_context}
""".strip()
    
    try:
        # Instantiate fresh model config with real-time dynamic system instructions
        model = genai.GenerativeModel(
            model_name=MODEL_NAME,
            system_instruction=dynamic_instruction
        )
        
        history = [{"role": "user" if m["role"] == "user" else "model", "parts": [m["content"]]} for m in session_history]
        chat_session = model.start_chat(history=history)
        response = chat_session.send_message(message)
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

    save_chat_message(req.session_id, "user", req.message)
    save_chat_message(req.session_id, "model", cleaned)
    
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

    save_chat_message(session_id, "user", incoming_msg)
    save_chat_message(session_id, "model", cleaned)

    resp = MessagingResponse()
    resp.message(cleaned)
    return Response(content=str(resp), media_type="application/xml")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
