from fastapi import FastAPI, HTTPException
from curl_cffi import requests
import logging
import os
import time

app = FastAPI()
logger = logging.getLogger("proxy-service")

# The proxy details will be loaded from Azure Environment Variables later.
# Expected format: http://USERNAME:PASSWORD@IP_ADDRESS:PORT
ENV_PROXY_URL = os.getenv("WEBSHARE_PROXY_URL", "")


@app.middleware("http")
async def log_requests(request, call_next):
    start_time = time.perf_counter()
    logger.info("Request received: %s %s", request.method, request.url.path)

    response = await call_next(request)

    duration_ms = (time.perf_counter() - start_time) * 1000
    logger.info(
        "Response sent: %s %s - status=%s duration_ms=%.2f",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    return response


@app.get("/fetch")
def fetch_url(url: str, use_proxy: bool = True):
    # Enable or disable the proxy based on the 'use_proxy' parameter in the API request
    proxies = None
    if use_proxy and ENV_PROXY_URL:
        proxies = {"http": ENV_PROXY_URL, "https": ENV_PROXY_URL}
    
    try:
        # impersonate="chrome" applies the TLS fingerprint spoofing to bypass Cloudflare
        response = requests.get(
            url, 
            impersonate="chrome", 
            proxies=proxies,
            timeout=30
        )
        
        # If the response is JSON (like the BookMyShow API), return it as JSON.
        # If it is standard HTML, return the raw text inside a dictionary.
        try:
            return response.json()
        except:
            return {"content": response.text}
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))