from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Query, Request, Security
from fastapi.responses import JSONResponse, Response
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field
from urllib.parse import urljoin, urlparse
from curl_cffi.requests import AsyncSession
import ipaddress
import logging
import os
import secrets
import socket
import time
from typing import Any, Dict, Optional

logger = logging.getLogger("proxy-service")
logging.basicConfig(level=logging.INFO)

# Environment variables configured in Azure App Service -> Environment variables
ENV_PROXY_URL = os.getenv("WEBSHARE_PROXY_URL", "")
PROXY_API_KEY = os.getenv("PROXY_API_KEY", "")  # Set a strong secret in Azure to lock down access
MAX_LOG_BODY_BYTES = 10_000
WARMUP_TTL_SECONDS = 900  # Re-warm an origin at most once every 15 minutes
MAX_REDIRECTS = 5

# Track when each origin (e.g. "https://httpbin.org") was last warmed up
_warmed_origins: Dict[str, float] = {}

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def verify_api_key(api_key: Optional[str] = Security(api_key_header)):
    """Enforces API key auth if PROXY_API_KEY is configured in Azure Environment Variables."""
    if not PROXY_API_KEY:
        return  # Auth disabled if env var is empty (useful for quick local testing)
    if not api_key or not secrets.compare_digest(api_key, PROXY_API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header.")


def validate_public_url(url: str) -> urlparse:
    """Allows any public URL while blocking private, loopback, and cloud metadata IPs (SSRF)."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(status_code=400, detail="URL must include http:// or https:// and a valid hostname.")

    try:
        addr_info = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror:
        raise HTTPException(status_code=400, detail=f"Could not resolve hostname: {parsed.hostname}")

    for family, _, _, _, sockaddr in addr_info:
        ip_obj = ipaddress.ip_address(sockaddr[0])
        if not ip_obj.is_global:
            raise HTTPException(status_code=403, detail="Target resolves to a non-public IP address.")

    return parsed


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Maintains persistent HTTP/2 & cookie pools across requests to save CPU on Azure F1."""
    proxies = {"http": ENV_PROXY_URL, "https": ENV_PROXY_URL} if ENV_PROXY_URL else None
    app.state.direct_session = AsyncSession(impersonate="safari", timeout=30)
    app.state.proxy_session = AsyncSession(impersonate="safari", proxies=proxies, timeout=30)
    try:
        yield
    finally:
        await app.state.direct_session.close()
        await app.state.proxy_session.close()


app = FastAPI(lifespan=lifespan)


async def response_body_iterator(body: bytes):
    yield body


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.perf_counter()
    logger.info("Request received: %s %s", request.method, request.url.path)

    response = await call_next(request)
    body_chunks = []
    async for chunk in response.body_iterator:
        body_chunks.append(chunk)
    body = b"".join(body_chunks)
    response.body_iterator = response_body_iterator(body)

    duration_ms = (time.perf_counter() - start_time) * 1000
    body_for_log = body[:MAX_LOG_BODY_BYTES].decode("utf-8", errors="replace")
    if len(body) > MAX_LOG_BODY_BYTES:
        body_for_log += f"... [truncated; total_bytes={len(body)}]"
    logger.info(
        "Response sent: %s %s status=%s duration_ms=%.2f body=%s",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
        body_for_log,
    )
    return response


async def safe_request(
    session: AsyncSession,
    method: str,
    url: str,
    headers: Dict[str, str],
    impersonate: str,
    json_body: Optional[Any] = None,
):
    """Executes the request and validates every redirect hop against SSRF."""
    current_url = url
    for _ in range(MAX_REDIRECTS + 1):
        validate_public_url(current_url)
        resp = await session.request(
            method=method.upper(),
            url=current_url,
            headers=headers,
            json=json_body,
            impersonate=impersonate,
            allow_redirects=False,
        )
        if resp.status_code in {301, 302, 303, 307, 308}:
            location = resp.headers.get("location")
            if not location:
                return resp
            current_url = urljoin(current_url, location)
            if resp.status_code == 303:
                method = "GET"
                json_body = None
            continue
        return resp

    raise HTTPException(status_code=508, detail="Exceeded maximum redirects.")


async def execute_proxied_call(
    request: Request,
    url: str,
    method: str = "GET",
    use_proxy: bool = True,
    warmup_origin: bool = True,
    impersonate: str = "safari",
    custom_headers: Optional[Dict[str, str]] = None,
    json_body: Optional[Any] = None,
):
    parsed = validate_public_url(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    session: AsyncSession = (
        request.app.state.proxy_session
        if (use_proxy and ENV_PROXY_URL)
        else request.app.state.direct_session
    )

    # Dynamically build headers matching the target origin
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Referer": f"{origin}/",
    }
    if method.upper() != "GET":
        headers["Origin"] = origin
    if custom_headers:
        headers.update(custom_headers)

    session_key = f"{'proxy' if (use_proxy and ENV_PROXY_URL) else 'direct'}:{origin}"
    now = time.time()

    # Step 1: Warm up the target origin once per TTL window to establish Cloudflare cookies
    if warmup_origin and (now - _warmed_origins.get(session_key, 0) > WARMUP_TTL_SECONDS):
        try:
            await safe_request(session, "GET", f"{origin}/", headers={}, impersonate=impersonate)
            _warmed_origins[session_key] = now
        except Exception as exc:
            logger.warning("Origin warm-up failed for %s: %s", origin, exc)

    # Step 2: Execute the actual API request
    response = await safe_request(session, method, url, headers, impersonate, json_body)

    # Step 3: Try the API directly. If Cloudflare blocks it, automatically 
    # override and do a warm-up, regardless of the warmup_origin setting.
    is_cf_challenge = "Just a moment..." in response.text or "_cf_chl_opt" in response.text
    
    if response.status_code == 403 or is_cf_challenge:
        logger.info("Cloudflare challenge detected on %s; forcing origin warm-up...", origin)
        await safe_request(session, "GET", f"{origin}/", headers={}, impersonate=impersonate)
        _warmed_origins[session_key] = time.time()
        
        # Retry the actual API call now that we have the cookies
        response = await safe_request(session, method, url, headers, impersonate, json_body)
        is_cf_challenge = "Just a moment..." in response.text or "_cf_chl_opt" in response.text

    try:
        payload = response.json()
    except Exception:
        payload = {
            "cloudflare_challenged": is_cf_challenge,
            "upstream_status": response.status_code,
            "content": response.text,
        }

    return JSONResponse(content=payload, status_code=response.status_code)


@app.get("/fetch", dependencies=[Security(verify_api_key)])
async def fetch_get(
    request: Request,
    url: str = Query(..., description="Any public API URL to proxy"),
    use_proxy: bool = True,
    warmup_origin: bool = True,
    impersonate: str = "safari",
):
    """Backward-compatible GET endpoint for simple URL proxying."""
    return await execute_proxied_call(
        request=request,
        url=url,
        method="GET",
        use_proxy=use_proxy,
        warmup_origin=warmup_origin,
        impersonate=impersonate,
    )


class ProxyRequestPayload(BaseModel):
    url: str
    method: str = Field(default="GET", description="HTTP method: GET, POST, PUT, DELETE, etc.")
    use_proxy: bool = True
    warmup_origin: bool = True
    impersonate: str = "safari"
    headers: Optional[Dict[str, str]] = None
    body: Optional[Any] = None


@app.post("/fetch", dependencies=[Security(verify_api_key)])
async def fetch_post(request: Request, payload: ProxyRequestPayload):
    """Generic POST endpoint allowing other microservices to pass custom methods, headers, and JSON bodies."""
    return await execute_proxied_call(
        request=request,
        url=payload.url,
        method=payload.method,
        use_proxy=payload.use_proxy,
        warmup_origin=payload.warmup_origin,
        impersonate=payload.impersonate,
        custom_headers=payload.headers,
        json_body=payload.body,
    )