# IP-003 Phase 6 — License Server network isolation

The LCP **License Server** (`:8989`) MUST NOT be reachable from the public
internet. Only the Catalog (Django) process on the same host and the
Encryption Tool need to talk to it.

The LCP **Status Server** (`:8990`) IS reachable, but only via the Nginx
reverse proxy on `:443` — never directly from clients on its native port.

## iptables example (Debian/Ubuntu)

```bash
# License Server: accept loopback, drop everything else.
iptables -A INPUT -p tcp --dport 8989 -s 127.0.0.1 -j ACCEPT
iptables -A INPUT -p tcp --dport 8989 -j DROP

# Status Server: same.
iptables -A INPUT -p tcp --dport 8990 -s 127.0.0.1 -j ACCEPT
iptables -A INPUT -p tcp --dport 8990 -j DROP
```

## Or systemd-bound localhost (preferred)

Configure the LCP server YAML to bind to 127.0.0.1 directly:

```yaml
lsd:
  public_base_url: "http://127.0.0.1:8990"
lcp:
  public_base_url: "http://127.0.0.1:8989"
```

Then no firewall rules are needed — the process never accepts non-loopback connections.

## Docker Compose

If the LCP servers run in a Docker network, do NOT publish their ports to the
host. Use an internal network and let only the Catalog container resolve them:

```yaml
services:
  lcpserver:
    image: edrlab/lcp-server:...
    networks: [lcp-internal]
    # No `ports:` — only reachable from lcp-internal
  catalog:
    networks: [lcp-internal, lcp-public]
    environment:
      EVILFLOWERS_READIUM_LCPSV_URL: "http://lcpserver:8989"

networks:
  lcp-internal: { internal: true }
  lcp-public:
```

## Verification

From outside the host:
```bash
curl -m 2 http://catalog.example.org:8989/ && echo BAD || echo GOOD
curl -m 2 http://catalog.example.org:8990/ && echo BAD || echo GOOD
```
Both should fail (timeout / connection refused) and print `GOOD`.

From the Catalog process on the host:
```bash
curl -fsS http://127.0.0.1:8989/ -u $LCPSV_USER:$LCPSV_PASS
```
Should return License Server status info.
