from fastapi import FastAPI, Request

app = FastAPI()

@app.post("/webhook")
async def webhook(request: Request):
    try:
        body = await request.body()
        body_text = body.decode("utf-8") if body else "EMPTY"

        print("📩 Webhook received")
        print("Raw body:", body_text)

        # domyślna akcja (np. BUY)
        action = "buy"

        return {
            "status": "ok",
            "received": body_text,
            "action": action
        }

    except Exception as e:
        print("❌ ERROR:", e)
        return {"status": "error"}
