# MAAS_URL Injection for Go Provisioning Client

## Problem

The Go provisioning client is statically compiled (~6MB per architecture). Unlike shell scripts that can use environment variables set at invocation time, a compiled binary needs runtime parameters injected somehow. The primary parameter is `MAAS_URL`, which tells the client where the MAAS API is located.

## Chicken-and-Egg Scenario

1. ONIE is booted and needs to fetch the provisioning client
2. DHCP option advertises: `https://maas.local/MAAS/a/v3/switch-deploy/provisioning-script?mac=00:11:22:33:44:55`
3. ONIE downloads and executes the Go binary
4. **But the binary doesn't know where MAAS is** — it only knows the URL it was downloaded from

The binary needs `MAAS_URL` to:
- Bootstrap and register the switch (`POST /bootstrap`)
- Download NOS image (`GET /nos-image-url`)
- Download provisioning script (`GET /runs/{run_id}/provisioning-script`)
- Upload results (`POST /switch/{mac}/nos-results`)

## Solution Options

### Option 1: Wrapper Script (Minimal)
**Pros:**
- Clean separation: ONIE runs a shell wrapper; wrapper injects parameters and execs binary
- Server-side substitution: MAAS substitutes `__MAAS_URL__` and `__SWITCH_MAC__` at fetch time
- ONIE doesn't need custom logic
- Testable: shell wrapper is transparent and easy to audit
- **No overhead**: One-line shell stub; negligible size/performance

**Implementation:**
```bash
#!/bin/sh
set -eu
MAAS_URL="__MAAS_URL__"
SWITCH_MAC="__SWITCH_MAC__"
exec /tmp/binary --maas-url="$MAAS_URL" --mac="$SWITCH_MAC"
```

**Cons:**
- One extra HTTP request (GET /wrapper first, then GET /client)
- Adds a "layer" even though it's minimal

### Option 2: DHCP Option Parsing
**Pros:**
- No wrapper needed
- Binary parses the provisioning URL it was fetched from: `https://maas.local/...` → extracts `https://maas.local`

**Implementation:**
```go
// Binary reads its own argv[0] or /proc/self/exe to find the download URL
// Or reads DHCP lease file to find provisioning-script URL
url := parseMAASFromDHCPOption()
maasURL := url.Scheme + "://" + url.Host
```

**Cons:**
- DHCP parsing is OS/ONIE-specific and fragile
- Assumes DHCP lease file is available and parseable
- Adds complexity to Go client startup
- Requires binary to know where provisioning URL came from (extra bookkeeping)

### Option 3: ONIE Environment Variable
**Pros:**
- No wrapper
- ONIE just needs to set `MAAS_URL` before executing binary
- Clean and portable

**Implementation:**
```go
maasURL := os.Getenv("MAAS_URL")
if maasURL == "" {
  log.Fatal("MAAS_URL environment variable not set")
}
```

**Cons:**
- Requires ONIE to know about and set this variable
- Extra configuration on ONIE side
- ONIE hook needs to extract MAAS_URL from somewhere (same chicken-and-egg problem)

### Option 4: Well-Known URL Discovery
**Pros:**
- Zero configuration
- Binary tries to find MAAS by checking common URLs or using mDNS

**Implementation:**
```go
for _, url := range []string{"https://maas.local", "http://10.0.0.1:5240", ...} {
  resp, err := http.Get(url + "/MAAS/api")
  if err == nil { maasURL = url; break }
}
```

**Cons:**
- Slow (multiple requests, timeouts)
- Unreliable (may find wrong MAAS in shared networks)
- Requires predictable MAAS hostname/IP

## Recommendation

**Use Option 1 (Minimal Wrapper)** because:

1. **Zero overhead**: One-line shell stub substituted server-side
2. **No ONIE modification**: ONIE just executes what it downloads
3. **Clean handoff**: Wrapper → Go binary is transparent and auditable
4. **Reliable**: Server knows exactly what parameters to inject
5. **Extensible**: Easy to add more parameters (e.g., `--token-ttl`, `--retry-policy`) without ONIE knowledge

## Updated Specification

The Go Provisioning Client Binary Delivery section should include:

```
**Wrapper Delivery (Optional)**:
- ONIE can optionally fetch `GET /wrapper?mac=...` instead of `/provisioning-script?mac=...`
- Server substitutes `__MAAS_URL__` and `__SWITCH_MAC__` into a one-line shell script
- Wrapper is responsible for detecting architecture and injecting MAAS_URL
- Wrapper then fetches and executes the Go binary with proper arguments

**Direct Binary Delivery (Simplified)**:
- ONIE directly fetches `GET /provisioning-script?mac=...` with `X-System-Architecture` header
- Server returns statically-compiled Go binary with MAAS_URL baked in from response headers
  OR Go binary auto-discovers MAAS_URL from DHCP/environment
```

The spec should recommend the wrapper approach because it's the cleanest separation of concerns and requires zero changes to ONIE beyond fetching and executing.
