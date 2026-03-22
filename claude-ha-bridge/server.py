"""
Claude <-> Home Assistant REST API Bridge
z pełną integracją HA API (odczyt stanów, sterowanie)
"""

from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import anthropic
import os, urllib.request, urllib.parse, json
from dotenv import load_dotenv
import logging

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ha-claude-bridge")

app = FastAPI(title="Claude-HA Bridge", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
HA_BRIDGE_TOKEN   = os.getenv("HA_BRIDGE_TOKEN", "")
HA_URL            = os.getenv("HA_URL", "").rstrip("/")
HA_TOKEN          = os.getenv("HA_TOKEN", "")
MODEL             = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")
MAX_TOKENS        = int(os.getenv("MAX_TOKENS", "1024"))

SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT",
    "You are a smart home assistant integrated with Home Assistant. "
    "Be concise. Answer in the same language as the user. "
    "When you receive sensor data or device states, analyze them and respond helpfully."
)

# ── HA API helpers ────────────────────────────────────────────────────────────

def ha_headers():
    return {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}

def ha_get(path: str):
    req = urllib.request.Request(f"{HA_URL}/api{path}", headers=ha_headers())
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def ha_post(path: str, payload: dict):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(f"{HA_URL}/api{path}", data=data,
                                  headers=ha_headers(), method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def get_ha_context(domains: list[str] | None = None) -> str:
    """Pobiera stany encji z HA i zwraca jako tekst dla Claude."""
    try:
        states = ha_get("/states")
        lines = []
        for e in states:
            eid = e["entity_id"]
            domain = eid.split(".")[0]
            if domains and domain not in domains:
                continue
            attrs = e.get("attributes", {})
            name = attrs.get("friendly_name", eid)
            state = e["state"]
            extra = ""
            if domain == "sensor" and "unit_of_measurement" in attrs:
                extra = f" {attrs['unit_of_measurement']}"
            elif domain == "climate":
                extra = f" (cel: {attrs.get('temperature','?')}°C, tryb: {attrs.get('hvac_action','?')})"
            lines.append(f"{name} [{eid}]: {state}{extra}")
        return "\n".join(lines)
    except Exception as ex:
        return f"Błąd pobierania stanu HA: {ex}"

# ── Auth ──────────────────────────────────────────────────────────────────────

def verify_token(x_ha_token: Optional[str] = Header(None)):
    if HA_BRIDGE_TOKEN and x_ha_token != HA_BRIDGE_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")
    return True

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
sessions: dict[str, list] = {}

# ── Models ────────────────────────────────────────────────────────────────────

class QueryRequest(BaseModel):
    message: str
    session_id: Optional[str] = "default"
    context: Optional[dict] = None
    system_override: Optional[str] = None
    include_ha_domains: Optional[list[str]] = None  # np. ["light","climate"]

class QueryResponse(BaseModel):
    response: str
    session_id: str
    tokens_used: int

class ServiceCallRequest(BaseModel):
    domain: str
    service: str
    entity_id: Optional[str] = None
    extra: Optional[dict] = None

# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    ha_ok = False
    try:
        ha_get("/")
        ha_ok = True
    except:
        pass
    return {"status": "ok", "model": MODEL, "ha_connected": ha_ok}

@app.post("/query", response_model=QueryResponse)
def query(req: QueryRequest, _=Depends(verify_token)):
    session_id = req.session_id or "default"
    if session_id not in sessions:
        sessions[session_id] = []

    user_content = req.message

    # Dołącz kontekst HA jeśli podano domeny
    if req.include_ha_domains:
        ha_ctx = get_ha_context(req.include_ha_domains)
        user_content += f"\n\n[Aktualny stan urządzeń]\n{ha_ctx}"
    elif req.context:
        ctx_lines = "\n".join(f"  {k}: {v}" for k, v in req.context.items())
        user_content += f"\n\n[Kontekst]\n{ctx_lines}"

    sessions[session_id].append({"role": "user", "content": user_content})
    if len(sessions[session_id]) > 40:
        sessions[session_id] = sessions[session_id][-40:]

    try:
        result = client.messages.create(
            model=MODEL, max_tokens=MAX_TOKENS,
            system=req.system_override or SYSTEM_PROMPT,
            messages=sessions[session_id],
        )
    except anthropic.APIError as e:
        raise HTTPException(status_code=502, detail=str(e))

    reply = result.content[0].text
    sessions[session_id].append({"role": "assistant", "content": reply})
    log.info("[%s] tokens=%d", session_id, result.usage.input_tokens + result.usage.output_tokens)

    return QueryResponse(response=reply, session_id=session_id,
                         tokens_used=result.usage.input_tokens + result.usage.output_tokens)


@app.post("/query/simple")
def query_simple(req: QueryRequest, _=Depends(verify_token)):
    user_content = req.message
    if req.include_ha_domains:
        user_content += f"\n\n[Stan urządzeń]\n{get_ha_context(req.include_ha_domains)}"
    try:
        result = client.messages.create(
            model=MODEL, max_tokens=MAX_TOKENS,
            system=req.system_override or SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
    except anthropic.APIError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"response": result.content[0].text}

@app.get("/ha/states")
def ha_states(domain: Optional[str] = None, _=Depends(verify_token)):
    """Zwraca stany encji z HA."""
    try:
        states = ha_get("/states")
        if domain:
            states = [s for s in states if s["entity_id"].startswith(domain+".")]
        return {"count": len(states), "states": states}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/ha/state/{entity_id:path}")
def ha_state(entity_id: str, _=Depends(verify_token)):
    """Stan pojedynczej encji."""
    try:
        return ha_get(f"/states/{entity_id}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/ha/service")
def ha_service(req: ServiceCallRequest, _=Depends(verify_token)):
    """Wywołaj usługę HA (np. light.turn_on, switch.toggle)."""
    payload = req.extra or {}
    if req.entity_id:
        payload["entity_id"] = req.entity_id
    try:
        result = ha_post(f"/services/{req.domain}/{req.service}", payload)
        return {"ok": True, "result": result}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.delete("/session/{session_id}")
def clear_session(session_id: str, _=Depends(verify_token)):
    sessions.pop(session_id, None)
    return {"cleared": session_id}


@app.get("/sessions")
def list_sessions(_=Depends(verify_token)):
    return {"sessions": list(sessions.keys()), "count": len(sessions)}


# ── Tuya reconnect ────────────────────────────────────────────────────────────

TUYA_LOCAL_ENTRIES = [
    "01KK7TZCHM927ARFJH5ENP4K90","01KK7V2D7MSVZFWNXK1MF2X1WE",
    "01KK7V4RB26W1YEEPWKS723GFH","01KK7V8CKDQNA7P9JJMPNV6D13",
    "01KK7W84HD9XKJP8063Q9RSA5R","01KK7WB6WW6D8S3B0BEA7WQ0FH",
    "01KK7XWNKV0QRGTE698VSKA9BD","01KK7ZRFXPKSZ53RX5M6RBDENN",
    "01KK7ZV97T0E46CQPDKAJZBHJ3","01KK7ZY4A8EDDTFT5NKH8YSM4Y",
    "01KK8005AYSTK6Q86G9BK4YS81","01KK8029ZZVQ4QC8DY90YRYS68",
    "01KK803NK0EZ2SAX9J4WMAQ4VB","01KK805SFAKGVT8JRKR26MPK9R",
    "01KK807XXFAT838HWDHMS22Y0W","01KK809R31PBAJCEKD6Q4CH1CZ",
    "01KK80BQJ2DJ7NGYS2256RGMQY","01KK80DNQCB7ND81KCMXFSV5Z8",
]

def _reload_entry(entry_id: str):
    req = urllib.request.Request(
        f"{HA_URL}/api/config/config_entries/entry/{entry_id}/reload",
        data=b"{}",
        headers={**ha_headers()},
        method="POST"
    )
    try:
        return json.loads(urllib.request.urlopen(req, timeout=10).read())
    except Exception as e:
        return {"error": str(e)}


@app.post("/reload-lights")
def reload_lights(_=Depends(verify_token)):
    """Przeladuj wszystkie tuya_local jesli swiatla sa unavailable."""
    try:
        states = ha_get("/states")
        unavail = [s["entity_id"] for s in states
                   if s["entity_id"].startswith("light.") and s["state"] == "unavailable"]
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    if not unavail:
        return {"reloaded": False, "reason": "all lights OK"}

    log.info("Reloading %d tuya_local entries (%d lights unavailable)", len(TUYA_LOCAL_ENTRIES), len(unavail))
    results = {eid: _reload_entry(eid) for eid in TUYA_LOCAL_ENTRIES}
    return {"reloaded": True, "unavailable_count": len(unavail), "results": results}
