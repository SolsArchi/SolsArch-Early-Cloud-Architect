from fastapi.responses import StreamingResponse

def chat():
    return StreamingResponse(iter([]), media_type="text/event-stream")
