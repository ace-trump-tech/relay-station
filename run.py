"""Start one local web server. Environment files are not loaded implicitly."""
import uvicorn

from app.main import create_app

if __name__ == "__main__":
    print("Relay Station: http://localhost:8765")
    uvicorn.run(create_app(), host="127.0.0.1", port=8765, access_log=False)
