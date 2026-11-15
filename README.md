# tinysecret

A tiny self-hosted drop box for secrets.

- **You** paste secret values from your phone.
- **Other containers** read them as plain files: `/secrets/NAME`.
- **Your AI assistant** (Claude Code, Hermes, any MCP client) can *ask* for a secret and check it's set, but can never read or write a value.

> LAN only. Do not port-forward it or put it behind a public reverse proxy.

## How it works

```
assistant ──MCP──▶ request_secret("NORDVPN_USER", note, requested_by)
                    │
you (phone) ◀──────┘  "waiting" shows at the top of the page → paste value → Save
                    │
/mnt/.../secrets/NORDVPN_USER  ──read-only mount──▶  gluetun, etc.
```

- One file per secret, written atomically (temp file + rename), mode `0640`, no trailing newline, owned by `PUID:PGID`.
- Notes, who asked, and timestamps live in `/secrets/.meta.json`, never inside the secret files.
- Names: `A-Z`, `0-9`, `_` only. Anything else is rejected.
- Values are never shown again in the UI (not even masked), never returned by MCP, never logged.

## 1. Install

The image is public: **`ghcr.io/tduarte/tinysecret:latest`** (amd64 and arm64).
Tags: `latest` (main branch), `X.Y.Z` / `X.Y` (releases), `sha-<commit>`.

### Docker Compose

```yaml
services:
  tinysecret:
    image: ghcr.io/tduarte/tinysecret:latest
    container_name: tinysecret
    restart: unless-stopped
    user: "568:568"            # keep equal to PUID:PGID
    ports:
      - "8790:8790"
    environment:
      UI_PASSWORD: "change-me-to-a-long-passphrase"
      MCP_TOKEN: "paste-output-of-openssl-rand-hex-32"
      PUID: "568"
      PGID: "568"
      PUBLIC_URL: "http://truenas.local:8790"   # optional
    volumes:
      - /mnt/SSDs/Applications/secrets:/secrets
    read_only: true
    cap_drop: [ALL]
    security_opt:
      - no-new-privileges:true
```

Same file: [`docker-compose.yml`](docker-compose.yml).

### TrueNAS 25.10

1. Create the folder and give it to the apps user (568). In **System → Shell**:
   ```sh
   mkdir -p /mnt/SSDs/Applications/secrets
   chown 568:568 /mnt/SSDs/Applications/secrets
   chmod 750 /mnt/SSDs/Applications/secrets
   ```
2. Make a token for the MCP server: `openssl rand -hex 32`
3. **Apps → Discover Apps → ⋮ (top right) → Install via YAML**. Name it `tinysecret`, paste the compose file above, and fill in `UI_PASSWORD`, `MCP_TOKEN` and `PUBLIC_URL`.
4. Wait until the app shows **Running**, then open `http://<truenas-ip>:8790` and log in.

**Updating:** Apps → tinysecret → **Update** (or `docker compose pull && docker compose up -d` elsewhere). Secrets live in the folder, so updates don't touch them.

### Plain Docker

```sh
docker run -d --name tinysecret --restart unless-stopped \
  --user 568:568 --read-only --cap-drop ALL --security-opt no-new-privileges:true \
  -p 8790:8790 \
  -e UI_PASSWORD='change-me' -e MCP_TOKEN="$(openssl rand -hex 32)" \
  -e PUID=568 -e PGID=568 \
  -v /path/to/secrets:/secrets \
  ghcr.io/tduarte/tinysecret:latest
```

If you generate the token inline like this, read it back with `docker inspect tinysecret` to connect your assistant.

### Settings

| Env var | Required | What it does |
|---|---|---|
| `UI_PASSWORD` | yes | Password for the web page. |
| `MCP_TOKEN` | yes (16+ chars) | Bearer token for `/mcp`. |
| `PUID` / `PGID` | no | Owner of secret files. Keep equal to the compose `user:`. |
| `PUBLIC_URL` | no | The address of this page. The assistant passes it on when it asks you for a secret. |
| `COOKIE_SECURE` | no | Set `true` only if you serve it over HTTPS. |
| `PORT` | no | Default `8790`. |

Failed logins: 5 wrong passwords from one device locks it out for 15 minutes.

## 2. Add a secret from your phone

1. Open `http://<truenas-ip>:8790` in Safari → Share → **Add to Home Screen**.
2. Anything your assistant asked for is at the top under **Waiting**. Tap it, paste, **Save**.
3. Or tap **+ Add secret**, type a name like `NORDVPN_USER`, paste the value. Leave the value empty to park it as "waiting".
4. To replace a value, tap the secret and save a new one. To remove it, **Delete secret** → confirm.

## 3. Connect your assistant (MCP)

Endpoint: `http://<truenas-ip>:8790/mcp` (streamable HTTP), header `Authorization: Bearer <MCP_TOKEN>`.

**Claude Code**
```sh
claude mcp add --transport http tinysecret http://<truenas-ip>:8790/mcp \
  --header "Authorization: Bearer <MCP_TOKEN>"
```

**Any other MCP client** (Hermes, etc.): add a remote / streamable-HTTP server with the URL and header above.

Tools:

| Tool | What it does |
|---|---|
| `list_secrets` | Names, `set`/`waiting`, note, requested_by. No values. |
| `check_secret(name)` | `set`, `waiting` or `missing` for one name. |
| `request_secret(name, note, requested_by)` | Adds a "waiting" entry for you to fill. Never touches a value that is already set. |

The server tells the assistant never to ask you to paste a secret into chat, and to use `request_secret` instead.

Your assistant must be able to reach the NAS on your network (same LAN, or a private overlay such as Tailscale). Cloud-hosted connectors that need a public HTTPS URL won't work, by design.

## 4. Use a secret in another app (Gluetun example)

Mount the same folder **read-only** and point the app at the file. See [`examples/gluetun-compose.yml`](examples/gluetun-compose.yml):

```yaml
services:
  gluetun:
    image: qmcgaw/gluetun:latest
    cap_add: [NET_ADMIN]
    devices: [/dev/net/tun:/dev/net/tun]
    environment:
      VPN_SERVICE_PROVIDER: nordvpn
      OPENVPN_USER_SECRETFILE: /secrets/NORDVPN_USER
      OPENVPN_PASSWORD_SECRETFILE: /secrets/NORDVPN_PASSWORD
    volumes:
      - /mnt/SSDs/Applications/secrets:/secrets:ro
```

Any app that supports `*_FILE` / `*_SECRETFILE` variables works the same way. Restart the app after changing a value.
An app that runs as a non-root user needs to be in group 568 (e.g. `group_add: ["568"]`) to read the files.

## Development

```sh
pip install -r requirements-dev.txt
python -m pytest -q
UI_PASSWORD=dev MCP_TOKEN=devdevdevdevdevdev SECRETS_DIR=./secrets python -m tinysecret
```

Images are built for amd64 and arm64 and published to `ghcr.io/tduarte/tinysecret` by GitHub Actions on every push to `main` and on `v*` tags (push a tag such as `v0.1.0` to cut a release).
