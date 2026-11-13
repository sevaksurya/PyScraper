# Python Proxy Service

An asynchronous [FastAPI](https://fastapi.tiangolo.com/) service for sending
HTTP requests to public URLs on behalf of other applications. Requests can be
sent directly or through an optional Webshare proxy. The service uses
`curl-cffi` browser impersonation, persistent sessions, and origin warm-up
requests to improve compatibility with sites protected by Cloudflare.

## Features

- `GET /fetch` for simple GET requests.
- `POST /fetch` for custom HTTP methods, headers, and JSON bodies.
- Optional API-key authentication with the `X-API-Key` header.
- SSRF protection for URLs and every redirect destination.
- Optional upstream proxy support through `WEBSHARE_PROXY_URL`.
- Persistent sessions for connection and cookie reuse.
- Automatic origin warm-up and retry after Cloudflare challenges.
- JSON responses preserved with the upstream status code.
- Text responses returned in a consistent JSON envelope.
- Response logging with large bodies truncated to 10,000 bytes.

## Requirements

- Python 3.11 or newer (for running locally without Docker)
- Docker and Docker Compose (optional, for running in containers)
- The dependencies listed in [`requirements.txt`](requirements.txt)

## Configuration

The service is configured with environment variables:

| Variable | Default | Description |
|---|---|---|
| `WEBSHARE_PROXY_URL` | Empty | Optional upstream proxy URL. |
| `PROXY_API_KEY` | Empty | API key required in `X-API-Key`. Authentication is disabled when empty. |
| `PORT` | `8080` | Host port used by Docker Compose mapping (`${PORT:-8080}:80`). |

Example:

```bash
export WEBSHARE_PROXY_URL="http://username:password@proxy-host:proxy-port"
export PROXY_API_KEY="replace-with-a-long-random-secret"
```

On Windows PowerShell:

```powershell
$env:WEBSHARE_PROXY_URL = "http://username:password@proxy-host:proxy-port"
$env:PROXY_API_KEY = "replace-with-a-long-random-secret"
```

Keep proxy credentials and API keys in environment variables or a secret
manager. Do not commit them to source control.

## Running the service

### Option 1: Running Without Docker (Local Python)

1. **Create and activate a virtual environment (recommended):**

   On Windows PowerShell:
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```
   *(Or with Command Prompt: `.\.venv\Scripts\activate.bat`)*

   On macOS / Linux:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

2. **Install dependencies:**
   ```bash
   python -m pip install -r requirements.txt
   ```

3. **Start the application with Uvicorn:**
   ```bash
   python -m uvicorn main:app --host 127.0.0.1 --port 8080 --reload
   ```

---

### Option 2: Running With Docker

#### Using Docker Compose (Recommended)

1. **Build and start the container:**
   ```bash
   docker-compose up --build
   ```
   To run in the background (detached mode), add `-d`:
   ```bash
   docker-compose up -d --build
   ```

2. **(Optional) Configure environment variables with a `.env` file:**
   Create a `.env` file in the project root:
   ```env
   PORT=8080
   WEBSHARE_PROXY_URL=http://username:password@proxy-host:proxy-port
   PROXY_API_KEY=replace-with-a-long-random-secret
   ```

3. **Stop the container:**
   ```bash
   docker-compose down
   ```

#### Using Docker CLI Directly

1. **Build the image:**
   ```bash
   docker build -t pyscraper .
   ```

2. **Run the container:**
   ```bash
   docker run -d --name pyscraper -p 8080:80 pyscraper
   ```

   With environment variables:
   ```bash
   docker run -d --name pyscraper -p 8080:80 \
     -e WEBSHARE_PROXY_URL="http://username:password@proxy-host:proxy-port" \
     -e PROXY_API_KEY="replace-with-a-long-random-secret" \
     pyscraper
   ```

3. **Stop and remove the container:**
   ```bash
   docker stop pyscraper
   docker rm pyscraper
   ```

---

### Service Verification and Documentation

Once running, the service is available at `http://127.0.0.1:8080`.

FastAPI provides interactive documentation at:

- `http://127.0.0.1:8080/docs`
- `http://127.0.0.1:8080/redoc`
- `http://127.0.0.1:8080/openapi.json`

## API

### `GET /fetch`

Proxies a GET request to a public HTTP(S) URL.

Query parameters:

| Parameter | Required | Default | Description |
|---|---:|---|---|
| `url` | Yes | — | Public HTTP(S) URL to fetch. |
| `use_proxy` | No | `true` | Use the configured proxy when available. |
| `warmup_origin` | No | `true` | Warm the target origin when its warm-up period has expired. |
| `impersonate` | No | `safari` | Browser profile used by `curl-cffi`. |

Example:

```bash
curl "http://127.0.0.1:8080/fetch?url=https%3A%2F%2Fhttpbin.org%2Fjson"
```

When authentication is enabled:

```bash
curl -H "X-API-Key: replace-with-a-long-random-secret" \
  "http://127.0.0.1:8080/fetch?url=https%3A%2F%2Fhttpbin.org%2Fjson"
```

### `POST /fetch`

Accepts a JSON request describing the upstream call:

```json
{
  "url": "https://httpbin.org/anything",
  "method": "POST",
  "use_proxy": true,
  "warmup_origin": true,
  "impersonate": "safari",
  "headers": {
    "X-Request-Source": "example-client"
  },
  "body": {
    "message": "hello"
  }
}
```

Request fields:

| Field | Required | Default | Description |
|---|---:|---|---|
| `url` | Yes | — | Public HTTP(S) URL to fetch. |
| `method` | No | `GET` | HTTP method sent upstream. |
| `use_proxy` | No | `true` | Use the configured proxy when available. |
| `warmup_origin` | No | `true` | Enable the normal origin warm-up step. |
| `impersonate` | No | `safari` | Browser profile used by `curl-cffi`. |
| `headers` | No | `null` | Additional upstream headers. |
| `body` | No | `null` | JSON body sent upstream. |

Example:

```bash
curl -X POST "http://127.0.0.1:8080/fetch" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://httpbin.org/anything",
    "method": "POST",
    "body": {"message": "hello"}
  }'
```

## Response behavior

Valid JSON returned by the upstream is passed through with the upstream status
code. Non-JSON responses use this structure:

```json
{
  "cloudflare_challenged": false,
  "upstream_status": 200,
  "content": "upstream response text"
}
```

The service detects common Cloudflare challenge responses, warms the origin, and
retries the request. If the final response is non-JSON,
`cloudflare_challenged` indicates whether a challenge was detected.

Common error responses:

- `400` — invalid URL or hostname resolution failure.
- `401` — missing or invalid API key.
- `403` — target resolves to a non-public IP address.
- `508` — more than five redirects were encountered.

## Security

The service accepts arbitrary public URLs, so protect it before exposing it to
untrusted clients:

1. Set a strong `PROXY_API_KEY`.
2. Use HTTPS and an authenticated gateway where appropriate.
3. Keep proxy credentials outside the source code.
4. Monitor outbound requests and service logs.
5. Add an application-level destination allowlist if arbitrary public URLs are
   not required.

URL validation rejects non-HTTP(S) URLs and hostnames resolving to private,
loopback, link-local, or other non-global IP addresses. Redirect destinations
are validated individually.

## Application behavior

- Direct and proxied HTTP sessions are created when the application starts and
  closed during shutdown.
- Upstream requests use a 30-second timeout.
- Origin warm-up state is kept in memory and expires after 15 minutes.
- Warm-up state is not persisted across application restarts.
- Response bodies are buffered by the logging middleware, so very large
  responses can increase memory usage.

## License

This project is distributed under the license in [`LICENSE`](LICENSE).
