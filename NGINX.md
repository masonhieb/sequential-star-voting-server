# Nginx Reverse Proxy — serving at `/voting`

## 1. Start the server with a prefix

```bash
python3 server.py --host 127.0.0.1 --port 8080 --prefix /voting
```

The `--prefix` flag injects a `<base href="/voting/">` tag into every HTML page so the
browser resolves all asset and API requests under `/voting/` instead of the root.

## 2. Add a location block to your existing nginx site

Inside your `server { listen 443 … }` block:

```nginx
location /voting/ {
    proxy_pass         http://127.0.0.1:8080/;
    proxy_http_version 1.1;

    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    # Required for SSE (/voting/api/stream)
    proxy_buffering           off;
    proxy_cache               off;
    proxy_read_timeout        3600s;
    proxy_set_header Connection '';
    chunked_transfer_encoding on;
}

# Redirect bare /voting → /voting/
location = /voting {
    return 301 /voting/;
}
```

The trailing slash on `proxy_pass http://127.0.0.1:8080/` strips the `/voting` prefix
before forwarding to aiohttp, so the app sees normal root-relative paths.

Reload nginx:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

## 3. Optional — run as a systemd service

Create `/etc/systemd/system/voting.service`:

```ini
[Unit]
Description=Sequential STAR Voting Server
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/path/to/sequential-star-voting-server
ExecStart=/usr/bin/python3 server.py --host 127.0.0.1 --port 8080 --prefix /voting
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now voting
```
