# Bastion-relay plan — moving CF Tunnel anchor off josh-box

> Drafted 2026-04-29. Reviewed by Codex; corrections applied (see "Revisions").
> Status: ready to execute. josh-box-side changes not yet applied.

## 0. Why

The current public ingress for `aztec.adjacentpossible.dev` is a Cloudflare
Tunnel anchored on josh-box (LHR area). The CF backbone has had repeated
trouble routing from non-LHR edges to that anchor:

- 2026-04-28 morning: ORD-edge users 502'd. Diagnosed as CF backbone
  failing ORD → LHR.
- 2026-04-28 afternoon: tactical fix added a same-host `cloudflared-us`
  replica. Helped US-East users, didn't help anyone else.
- 2026-04-28 evening: VPN exits in Afghanistan / Armenia 502'ing for the
  same class of reason — CF edge → tunnel anchor backhaul failure.

The structural problem is single-region anchoring. The tactical fix
(more replicas on the same host) doesn't add geographic resilience;
canonical CF Tunnel HA wants different hosts in different regions.

**This plan moves the tunnel anchor onto `ci-bastion.aztecprotocol.com`
(AWS us-east-2, Ohio).** Bastion sits in AWS's well-connected backbone,
which CF reaches reliably from edges worldwide. josh-box stays exactly
as private as today (no public IP, no inbound port). A long-lived
SSH reverse tunnel from josh-box → bastion exposes the docker stack to
bastion-local `127.0.0.1:5080`, where cloudflared on bastion picks it
up and serves it to CF.

Cost: $0 incremental. Bastion already exists.

---

## 1. Architecture

```
[Internet]
   │
   ▼
[Cloudflare anycast edge]            ← public ingress, TLS terminated
   │
   ▼
[CF Tunnel] ── healthy backbone ──▶ [cloudflared on bastion (us-east-2)]
                                         │
                                         ▼
                                  [127.0.0.1:5080 on bastion]
                                         │  (SSH reverse tunnel,
                                         │   initiated outbound by josh-box,
                                         │   systemd-supervised)
                                         ▼
                                  [127.0.0.1:5080 on josh-box]
                                         │
                                         ▼
                                  [Caddy:80 in docker-compose-hub]
                                         │
                                         ▼
                                  [backend, frontend, postgres, …]
```

Compared to today: only the location of `cloudflared` changes. Caddy,
backend, frontend, postgres, redis, worker, discord-bot all stay on
josh-box, untouched.

### Trust model after the change

- **Public internet:** unchanged. CF Tunnel is the only way in.
- **CF edges → cloudflared:** unchanged transport, healthier backhaul
  (us-east-2 vs LHR).
- **Bastion local users:** **new trust expansion.** Anyone with a shell
  on `ci-bastion.aztecprotocol.com` can reach the docs widget origin
  via `curl localhost:5080/`, bypassing Cloudflare Access. This is
  **accepted** because bastion is an Aztec-employees-only host and the
  widget itself serves public anonymous traffic anyway. **Caveat:**
  this also means anyone with bastion shell can reach
  `/api/internal/*` without Cloudflare Access — the previous CF Access
  gate on those admin endpoints is effectively bypassable from
  bastion. Mitigation: those endpoints still require
  `MCP_PROVISIONING_KEY` (`/api/internal/create_mcp_key`) or
  `INTERNAL_KEY`, so unauthenticated bastion access can't directly
  exploit them.
- **josh-box local users:** unchanged. Same loopback-only origin
  exposure as today.

### What this is NOT

- Not multi-region anchored. Bastion is a single anchor in
  us-east-2. If bastion is down, the widget is down.
- Not a permanent end-state if traffic grows materially. See §8.

---

## 2. Decisions locked (revisit if any change before execute)

| Decision | Choice | Rationale |
| --- | --- | --- |
| Port on bastion + josh-box | **5080** | Coworker's suggestion; their cloxy uses 3100 so we don't collide. |
| Hostname | **`aztec.adjacentpossible.dev` (unchanged)** | Move the existing `CLOUDFLARE_TUNNEL_TOKEN` to bastion. CF supports multiple connectors per tunnel; cleaner than minting a new tunnel + DNS. |
| SSH identity | **dedicated key `~/.ssh/aztec_docs_relay`** | Don't reuse `build_instance_key` — that key is for CI worker provisioning and shouldn't be doing double duty as ingress. |
| Bastion-local trust | **accepted** | Aztec-employees-only host. See §1 trust model. |
| Cutover overlap | **brief overlap** (minutes, not hours) | Validate bastion connector is taking traffic, then immediately stop josh-box's cloudflared. Avoids long mixed-routing window. |

---

## 3. Pre-flight (do these BEFORE touching anything)

### 3.1 Bastion-side checks

SSH to bastion and confirm:

```bash
# 1. Port 5080 is free.
ss -ltn '( sport = :5080 )' | tail -n +2
# expect empty output

# 2. sshd allows TCP forwarding (default on most Ubuntu sshd).
sudo grep -E '^(AllowTcpForwarding|GatewayPorts)' /etc/ssh/sshd_config
# expect: AllowTcpForwarding yes (or absent — defaults yes)
#         GatewayPorts no (default; that's what we want for loopback-only -R)

# 3. cloudflared is installable / present.
which cloudflared || echo "needs install"

# 4. No existing tunnel that would collide. Check running services:
systemctl list-units --type=service --no-pager | grep -i cloud || true
ps aux | grep -i cloudflared | grep -v grep || true

# 5. Capture bastion's host key fingerprint for pre-pinning.
ssh-keyscan -t ed25519 ci-bastion.aztecprotocol.com 2>/dev/null
```

Save the host-key output. We'll pin it on josh-box in §4.3.

### 3.2 josh-box-side checks

```bash
# 1. Confirm we have docker compose access and prod compose is healthy.
export PATH=/usr/bin:$PATH; unset DOCKER_HOST
docker compose -f deployment/docker-compose-hub.yaml --env-file .env ps

# 2. Capture the existing tunnel token (we'll need it for bastion).
grep '^CLOUDFLARE_TUNNEL_TOKEN=' .env | head -1

# 3. Confirm no listener on 127.0.0.1:5080 already.
ss -ltn '( sport = :5080 )' | tail -n +2
```

### 3.3 Communications

Coordinate with the bastion admin / coworker before standing up cloudflared on bastion. Items to confirm:

- Bastion-local cleanup/cron policies don't kill long-running systemd services.
- Bastion's outbound connectivity to Cloudflare (`*.cloudflare.com`, the cloudflared QUIC ports) is not firewalled.
- The bastion-local Access bypass acknowledged in §1 is acceptable from a security-posture perspective for whoever owns bastion.

---

## 4. josh-box-side changes (apply when ready)

### 4.1 Bind Caddy locally on the host

Edit `deployment/docker-compose-hub.yaml`:

```diff
   caddy:
     image: caddy:2-alpine
     restart: unless-stopped
-    # No host ports. cloudflared reaches caddy:80 via the compose network.
+    # Loopback-only host port for the SSH reverse tunnel to bastion.
+    # 127.0.0.1 (NOT 0.0.0.0) — never publicly reachable on josh-box.
+    ports:
+      - "127.0.0.1:5080:80"
     volumes:
       - ./Caddyfile:/etc/caddy/Caddyfile:ro
```

Apply: `docker compose -f deployment/docker-compose-hub.yaml --env-file .env up -d --force-recreate caddy`.

Verify locally on josh-box:

```bash
curl -i -H 'Host: aztec.adjacentpossible.dev' http://127.0.0.1:5080/api/health
# expect 200 from backend
```

### 4.2 Create the dedicated SSH key

```bash
ssh-keygen -t ed25519 -f ~/.ssh/aztec_docs_relay -N "" \
  -C "aztec-docs-relay@josh-box $(date -u +%Y-%m-%d)"
```

Add the public key to `ubuntu@ci-bastion`'s `~/.ssh/authorized_keys` with restrictions:

```
restrict,permitlisten="localhost:5080",no-agent-forwarding,no-X11-forwarding,no-pty,from="<josh-box egress IP>" ssh-ed25519 AAAA… aztec-docs-relay@josh-box ...
```

`permitlisten="localhost:5080"` is the key control — this key can ONLY create the loopback `:5080` listen port on bastion, nothing else. `from="..."` further pins the source IP if josh-box has a static egress IP.

### 4.3 Pin bastion host key

```bash
# Use the keyscan output captured in §3.1.
ssh-keyscan -t ed25519 ci-bastion.aztecprotocol.com >> ~/.ssh/known_hosts
# Verify:
ssh-keygen -l -F ci-bastion.aztecprotocol.com -f ~/.ssh/known_hosts
```

Cross-check the fingerprint against a trusted source (coworker, AWS console, etc.) before trusting it.

### 4.4 Systemd user service for the reverse tunnel

`~/.config/systemd/user/aztec-docs-tunnel.service`:

```ini
[Unit]
Description=SSH reverse tunnel josh-box → ci-bastion (port 5080) for aztec docs widget
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/ssh -N -R localhost:5080:localhost:5080 \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -o ExitOnForwardFailure=yes \
  -o StrictHostKeyChecking=yes \
  -o BatchMode=yes \
  -i /mnt/user-data/josh/.ssh/aztec_docs_relay \
  ubuntu@ci-bastion.aztecprotocol.com
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

Notes:
- **`-R localhost:5080:localhost:5080`** — explicit loopback bind on bastion regardless of bastion's `GatewayPorts` setting.
- **`StrictHostKeyChecking=yes`** — relies on §4.3 pinning. No `accept-new`.
- **`-F` flag dropped.** Don't depend on `~/aztec-packages/ci3/aws/build_instance_ssh_config` — that path is for the build worker pipeline and could be moved/changed without warning. Inline the connection params.
- **`ExitOnForwardFailure=yes`** — if the remote bind fails (e.g., port already taken on bastion), the SSH process exits immediately and systemd retries. Without this, the SSH session can stay up while the actual tunnel is dead.

Enable:

```bash
loginctl enable-linger josh   # only needed once; persists service across logout
systemctl --user daemon-reload
systemctl --user enable --now aztec-docs-tunnel.service
journalctl --user -u aztec-docs-tunnel.service -f   # watch for "Connection established"
```

### 4.5 Validate the tunnel from josh-box's side

From josh-box, SSH into bastion and probe:

```bash
ssh -i ~/.ssh/aztec_docs_relay -F /dev/null \
  -o StrictHostKeyChecking=yes \
  ubuntu@ci-bastion.aztecprotocol.com \
  -- "curl -is -H 'Host: aztec.adjacentpossible.dev' http://localhost:5080/api/health"
```

Expect 200. **Note the explicit `Host:` header** — Caddy is host-matched on `${PUBLIC_HOSTNAME}`, so a bare `curl localhost:5080/` will hit Caddy's default response or fail. Earlier draft of this plan had that wrong; verified curl test is the one above.

---

## 5. Bastion-side changes (Josh runs these manually on bastion)

### 5.1 Install cloudflared

```bash
curl -L --output cloudflared.deb \
  https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i cloudflared.deb
cloudflared --version
rm cloudflared.deb
```

### 5.2 Token

Copy `CLOUDFLARE_TUNNEL_TOKEN` from josh-box's `.env` to bastion. Suggested location: `/etc/cloudflared/aztec-docs.env`.

```bash
sudo install -d -m 0700 /etc/cloudflared
sudo tee /etc/cloudflared/aztec-docs.env >/dev/null <<'EOF'
CF_TOKEN=<paste from josh-box .env>
EOF
sudo chmod 600 /etc/cloudflared/aztec-docs.env
```

### 5.3 Systemd service

`/etc/systemd/system/cloudflared-aztec-docs.service`:

```ini
[Unit]
Description=Cloudflare tunnel for aztec docs widget (relay for josh-box)
After=network-online.target
Wants=network-online.target

[Service]
EnvironmentFile=/etc/cloudflared/aztec-docs.env
ExecStart=/usr/bin/cloudflared tunnel --url http://localhost:5080 run --token ${CF_TOKEN}
Restart=always
RestartSec=5
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now cloudflared-aztec-docs.service
sudo journalctl -u cloudflared-aztec-docs.service -f
# Expect: 4 connections registered at us-east edges (iad, atl, mia, etc.)
```

---

## 6. Cutover

Sequence matters — don't disable josh-box's cloudflared until bastion's is taking traffic.

1. **Confirm both anchors are live.** josh-box's existing cloudflared is still running. Bastion's new cloudflared is also registered. CF will route to whichever is healthier — for a few minutes, traffic might land on either.
2. **Test from a previously-broken VPN exit** (Afghanistan / Armenia / ORD).
   Expected: now returns 200. If still 502 → check both cloudflared logs for which connector picked up the request.
3. **Run a curl from a US edge as a positive control:**
   ```bash
   curl -i https://aztec.adjacentpossible.dev/api/health -H 'Origin: https://docs.aztec.network' --max-time 10
   ```
4. **Stop josh-box's cloudflared.** Either comment out the service in `docker-compose-hub.yaml` and `docker compose ... up -d --remove-orphans`, or just `docker stop docsgpt-aztec-cloudflared-1 docsgpt-aztec-cloudflared-us-1`. Don't delete the unit yet — keep it as quick-rollback for ~24 hours.
5. **Watch for tunnel-flap.** `journalctl --user -u aztec-docs-tunnel.service -f` on josh-box, `journalctl -u cloudflared-aztec-docs -f` on bastion. The SSH tunnel will occasionally flap (TCP keepalive declares dead → restart). Confirm `Restart=always` recovers within ~5–10 s.

---

## 7. Rollback (if cutover fails)

Cleanest rollback: re-enable josh-box's cloudflared.

```bash
# On josh-box:
docker compose -f deployment/docker-compose-hub.yaml --env-file .env up -d cloudflared
# On bastion (optional, only if you want to fully reverse):
sudo systemctl stop cloudflared-aztec-docs.service
sudo systemctl disable cloudflared-aztec-docs.service
# On josh-box, stop the SSH tunnel:
systemctl --user stop aztec-docs-tunnel.service
# Remove the loopback port binding from compose if you want to fully revert:
docker compose ... up -d --force-recreate caddy
```

CF will reconverge to whichever connectors are alive within ~10 s.

---

## 8. Failure modes after the change

| Failure | Detection | Mitigation |
| --- | --- | --- |
| Bastion goes down / rebooted | All public traffic 502/503 | Restart josh-box's old cloudflared (rollback §7). Coordinate with bastion admin on uptime expectations. |
| SSH tunnel flap | Brief (~5–10 s) outage; journalctl shows reconnect | `Restart=always` handles it. For a service this small, acceptable. |
| Bastion `5080` taken by another service | systemd-user service fails fast (`ExitOnForwardFailure=yes`) | Pick a different port; update both systemd unit and cloudflared service. |
| SSH key compromised / revoked | Tunnel can't connect; 502s | Rotate via §4.2; bastion admin can drop the key from `authorized_keys`. |
| CF backbone routing fails to us-east-2 (rare) | Same 502 class, different region | Provision a second relay (hetzner FRA, fly.io, or a second bastion) for actual multi-region anchoring. |
| TCP-over-TCP HOL on busy SSH session | All SSE streams stall together briefly | Only matters at higher concurrency than we have today. See §9. |

---

## 9. Future improvements (only if symptoms manifest)

This plan treats the SSH reverse tunnel as the **permanent** transport. Codex flagged a WireGuard / Tailscale upgrade as architecturally cleaner. We're skipping that unless one of these triggers:

- **Traffic grows enough that TCP-over-TCP HOL bites.** All client streams multiplex over one SSH TCP session today; concurrency in the dozens is fine, hundreds would not be. Stress test confirmed effective `/stream` ceiling is ~10–20 concurrent at current OpenRouter quotas, so we're nowhere close.
- **SSH tunnel flaps become frequent enough to be user-visible.** A few per day is fine. A few per hour means switch transports.
- **We add a second relay.** Multi-host overlay networking is much cleaner with WG/Tailscale than with two independent SSH `-R` sessions.

If any of those trip, the upgrade path is:

1. Stand up Tailscale on josh-box + bastion (free tier, ~10 min setup).
2. Switch the bastion cloudflared upstream from `http://localhost:5080` to `http://<josh-box-tailscale-IP>:5080` (requires Caddy on josh-box to also listen on the Tailscale interface).
3. Disable the SSH reverse tunnel.
4. Same outage envelope as the cutover above.

Other future improvements worth queuing as separate work:

- **Healthcheck on bastion** that distinguishes "CF tunnel up, SSH leg down" from "origin unhealthy". Cron'd `curl localhost:5080/api/health` → log to journal or push to a status channel.
- **Second relay for actual multi-region HA** (Hetzner FRA, fly.io anycast). Only if bastion-as-SPOF becomes uncomfortable.
- **Decommission `cloudflared-us` workaround** introduced 2026-04-28. After the bastion cutover, both `cloudflared` and `cloudflared-us` services on josh-box can be removed from the compose.

---

## 10. Open items to confirm before execute

- [ ] Coordinate with bastion admin: confirm the new long-running services (cloudflared + the `:5080` listen port) are acceptable, not at risk of cleanup-cron termination.
- [ ] Verify bastion's outbound to CF tunnel endpoints isn't firewalled.
- [ ] Confirm josh-box's egress IP is static enough to use in `from="..."` on the bastion `authorized_keys` line. If not, drop the `from=` restriction and rely on the `permitlisten=` + key-only auth.
- [ ] Decide whether to keep josh-box's `cloudflared` and `cloudflared-us` units stopped-but-defined (rollback ergonomics) or remove them (cleanup). Recommend stopped-but-defined for ~7 days, then remove.

---

## Revisions

**2026-04-29 (Codex review pass):**
- §1 trust model: explicit acknowledgment of bastion-local Access bypass. Accepted; documented.
- §4.4: changed `-R 5080:localhost:5080` → `-R localhost:5080:localhost:5080`. Removes ambiguity around `GatewayPorts`.
- §4.4: `StrictHostKeyChecking=accept-new` → `yes`, with §4.3 pre-pinning step added.
- §4.4: dropped `-F /mnt/user-data/josh/aztec-packages/ci3/aws/build_instance_ssh_config` reference. That path is for build-worker provisioning, not prod ingress.
- §4.2: added `permitlisten="localhost:5080"` and other restrictions to `authorized_keys` — key can only do this one thing, nothing else.
- §4.5: corrected verification curl to include `-H 'Host: aztec.adjacentpossible.dev'`. Caddy is host-matched; bare `curl localhost:5080/` would hit a default-Caddy response and mislead debugging.
- §9: WG/Tailscale moved from "permanent fix" to "future option, only if specific triggers fire." For current traffic shape and operational tolerance, the SSH reverse tunnel is the permanent transport.
