import os
from fastapi import FastAPI, Request, HTTPException

app = FastAPI()

# Sekret pobierany z Railway Variables
SECRET = os.getenv("WEBHOOK_TOKEN")

@app.post("/webhook")
async def webhook(request: Request):
    token = request.query_params.get("token")

    # 1️⃣ Zabezpieczenie
    if token != SECRET:
        raise HTTPException(status_code=403, detail="Invalid token")

    # 2️⃣ Odczyt body (działa też gdy EMPTY)
    raw_body = await request.body()
    text = raw_body.decode("utf-8") if raw_body else "EMPTY"

    # 3️⃣ Log (widzisz w Railway)
    print("Webhook received:")
    print(text)

    # 4️⃣ Tymczasowa logika (placeholder)
    action = "buy" if "buy" in text.lower() else "sell" if "sell" in text.lower() else "none"

    return {
        "status": "ok",
        "received": text,
        "action": action
    }
