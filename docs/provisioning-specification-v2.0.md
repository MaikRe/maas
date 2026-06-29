| Index                                                                                                        | MA302                                           |                                                                                                                |             |
| :----------------------------------------------------------------------------------------------------------- | :---------------------------------------------- | :------------------------------------------------------------------------------------------------------------- | :---------- |
| Title                                                                                                        | MAAS Network Switch Provisioning                |                                                                                                                |             |
| **[Type](https://docs.google.com/document/d/1lStJjBGW7lyojgBhxGLUNnliUocYWjAZ1VEbbVduX54/edit?usp=sharing)** | **Author(s)**                                   | **[Status](https://docs.google.com/document/d/1lStJjBGW7lyojgBhxGLUNnliUocYWjAZ1VEbbVduX54/edit?usp=sharing)** | **Created** |
| Implementation                                                                                               | [Maik Rebaum](mailto:maik.rebaum@canonical.com) | Drafting                                                                                                       | 29 Jun 2026 |
|                                                                                                              | **Reviewer(s)**                                 | **Status**                                                                                                     | **Date**    |
|                                                                                                              | Person                                          | Pending Review                                                                                                 | Date        |

# Abstract

MAAS needs a way to automatically provision network switches using ONIE (Open Network Install Environment) and execute provisioning steps with comprehensive logging. A MAAS operator should be able to register a switch, receive a templated provisioning script via ONIE boot, and query the provisioning status and logs via the v3 API during and after execution.

This specification describes how the provisioning workflow is orchestrated via a thin server-templated POSIX shell wrapper script that bootstraps a compiled Go binary (`maas-switch-provisioner`), how provisioning state is tracked, how security is enforced through UUID-based switch identification and TLS, and how the provisioning scripts are defined and executed with full observability through unified logging.

---

# Rationale

As of now, MAAS has a basic provisioning mechanism that treats the entire switch provisioning as a single opaque unit. Operators lack:

- **Visibility into provisioning progress**: No way to know if provisioning is in progress or complete
- **Structured logging**: No ability to download provisioning script output for debugging
- **Simple provisioning model**: Complex token handshakes and ephemeral state tracking add unnecessary complexity
- **Observability through v3 API**: No standard way to query provisioning status and logs programmatically

In the v3 API, we want to introduce a streamlined provisioning framework to provide:

- **Clear provisioning status**: Operators can query whether provisioning succeeded or failed
- **UUID-based switch identity**: Non-sequential, identifiers prevent ID enumeration and serve as deployment endpoints
- **Unified log storage**: All provisioning output (NOS installation, provisioning scripts) stored in a single log table with categorical metadata
- **Go-binary-driven execution**: A compiled, statically-linked Go binary handles all API interactions, removing the need for multiple custom HTTP headers in the ONIE busybox environment
- **Full audit trail**: All provisioning actions are logged and queryable

---

# Specification

## Provisioning Workflow Overview

### **High-Level Architecture**

The provisioning system consists of three components:

1. **v3 API (region)** — Generates and serves the thin wrapper script, manages the provisioning script catalog, and exposes endpoints for status updates, log uploads, and result queries
2. **Rack controller HTTP service** — Serves the architecture-specific `maas-switch-provisioner` binary and the NOS installer image over HTTP on port 5248
3. **DHCP service** — Advertises the per-MAC provisioning wrapper URL (gated by MAC address)

### **Provisioning Workflow Lifecycle**

```
1. Operator registers switch with MAAS v3 API and optionally assigns a provisioning script and NOS image
   ↓
2. DHCP option advertises provisioning wrapper URL: http://<region>/MAAS/a/v3/switch-installer
   ↓
3. Switch boots in ONIE, receives wrapper URL via DHCP
   ↓
4. ONIE requests: GET /MAAS/a/v3/switch-installer
     - Region validates Onie-Eth-Addr and Onie-Arch headers
     - Region generates a thin wrapper script with SWITCH_UUID, MAAS_URL, NOS_URL, and
       PROVISIONER_URL pre-filled based on the switch's registered MAC and architecture
     - Returns the wrapper script with 200 OK
   ↓
5. ONIE executes the wrapper script on the switch:
     - Exports environment variables (MAAS_URL, SWITCH_UUID, SWITCH_MAC, NOS_URL)
     - Downloads maas-switch-provisioner from rack:
       GET http://<rack>:5248/switch-provisioner/<arch>/maas-switch-provisioner
     - Executes the binary
   ↓
6. maas-switch-provisioner orchestrates the full provisioning lifecycle:
     - POST /switches/{SWITCH_UUID}/status → DEPLOYING
     - Downloads and executes NOS installer (from NOS_URL via rack cache proxy)
     - Uploads NOS output: POST /switches/{SWITCH_UUID}/logs (X-Log-Category: NOS_INSTALLATION)
     - Fetches provisioning script: GET /switches/{SWITCH_UUID}/provisioning-script
     - Executes provisioning script
     - Uploads script output: POST /switches/{SWITCH_UUID}/logs (X-Log-Category: PROVISIONING_SCRIPT)
     - POST /switches/{SWITCH_UUID}/status → READY (or FAILED on any error)
   ↓
7. Operator queries provisioning status and logs via v3 API
```

---

## Core Provisioning Workflow

### **Step 1: Wrapper Script Generation**

**Purpose**: Validate the switch MAC address and generate the thin bootstrapping wrapper script.

**Trigger**: ONIE requests `GET /MAAS/a/v3/switch-installer`

**ONIE Headers Used**:

- `Onie-Eth-Addr`: Management MAC address of the switch (required)
- `Onie-Arch`: CPU architecture reported by ONIE (optional, defaults to `amd64`)

**ONIE Arch to Binary Directory Mapping**:

| Onie-Arch header value | Binary directory |
| :--------------------- | :--------------- |
| `x86_64`               | `amd64`          |
| `amd64`                | `amd64`          |
| `aarch64`              | `arm64`          |
| `arm64`                | `arm64`          |
| `ppc64el` / `ppc64le`  | `ppc64el`        |

**Server Actions**:

- Validate MAC address format and look up switch in database
- Check switch status:
  - `NOT_PROVISIONED`: Proceed normally
  - `DEPLOYING` or `READY`: Transition status to `FAILED`, return 404 Not Found
  - `FAILED`: Return 404 Not Found (no state change)
- Resolve the NOS installer URL (empty string if no NOS image is assigned or not yet available on the rack)
- Resolve the architecture-specific `maas-switch-provisioner` URL from the `Onie-Arch` header
- Return the rendered wrapper script with 200 OK

**State Transition on Re-request**:

If a switch in `DEPLOYING` or `READY` state re-requests the wrapper script, the server transitions the status to `FAILED` before returning 404. This indicates the prior provisioning attempt did not complete successfully. A switch in `FAILED` state must be reset by an operator before it can be reprovisioned.

**Wrapper Script Format**:

The wrapper script sets environment variables and downloads the Go binary from the rack. It makes exactly one `wget` call with no custom headers.

```shell
#!/bin/sh
set -eu

export MAAS_URL="http://maas.local/MAAS/a/v3"
export SWITCH_UUID="8f3b9c2a-4d1e-4b6a-9f8c-7e6d5c4b3a2a"
export SWITCH_MAC="00:11:22:33:44:55"
export NOS_URL="http://10.20.0.2:5248/images/abc123def456"

wget -q "http://10.20.0.2:5248/switch-provisioner/amd64/maas-switch-provisioner" -O /tmp/maas-switch-provisioner
chmod +x /tmp/maas-switch-provisioner
exec /tmp/maas-switch-provisioner
```

`NOS_URL` is empty if no NOS image is assigned. The Go binary skips NOS installation in that case.

---

### **Step 2: Go Binary Execution (`maas-switch-provisioner`)**

**Purpose**: Orchestrate the full provisioning lifecycle.

**Trigger**: Wrapper script downloads and executes the binary.

**Environment Variables Consumed**:

| Variable      | Description                                              |
| :------------ | :------------------------------------------------------- |
| `MAAS_URL`    | Region v3 API base URL                                   |
| `SWITCH_UUID` | UUID identifying this switch                             |
| `SWITCH_MAC`  | Management MAC address                                   |
| `NOS_URL`     | Full URL to NOS installer on the rack (empty = skip NOS) |

**Execution Sequence**:

1. Derive `STATUS_URL`, `LOG_URL`, and `PROV_SCRIPT_URL` from `MAAS_URL` + `SWITCH_UUID`
2. `POST STATUS_URL` → `DEPLOYING`
3. If `NOS_URL` is non-empty:
   - Download NOS installer via HTTP GET, execute it, capture combined stdout/stderr
   - `POST LOG_URL` with `X-Log-Category: NOS_INSTALLATION` and `X-Exit-Code: <code>`
   - On non-zero exit: `POST STATUS_URL` → `FAILED` and exit
4. `GET PROV_SCRIPT_URL` to fetch operator provisioning script
   - If 404 (no script assigned): skip silently
   - If assigned: execute, capture output
   - `POST LOG_URL` with `X-Log-Category: PROVISIONING_SCRIPT` and `X-Exit-Code: <code>`
   - On non-zero exit: `POST STATUS_URL` → `FAILED` and exit
5. `POST STATUS_URL` → `READY`

All HTTP calls use retry logic (3 attempts, 2s delay).

---

## `maas-switch-provisioner` Binary

### **Build**

The binary is statically linked (`CGO_ENABLED=0`) and cross-compiled for each supported architecture via `src/maasagent/Makefile`:

| Target arch | Go `GOARCH` |
| :---------- | :---------- |
| `amd64`     | `amd64`     |
| `arm64`     | `arm64`     |
| `ppc64el`   | `ppc64le`   |

### **Installation**

Each architecture binary is installed to:

```
<prefix>/usr/sbin/switch-provisioner/<arch>/maas-switch-provisioner
```

In the snap, the `maas-agent` part primes `usr/sbin/`, which covers the `switch-provisioner/` subdirectory. In the deb package (`maas-switch-provisioner`), all three arch binaries are installed under `usr/sbin/switch-provisioner/`.

### **Serving**

The rack nginx configuration serves the binaries using an `alias` directive:

```nginx
location /switch-provisioner/ {
    alias <snap_root>/usr/sbin/switch-provisioner/;
    autoindex off;
}
```

A request for `GET http://<rack>:5248/switch-provisioner/amd64/maas-switch-provisioner` resolves to `<snap_root>/usr/sbin/switch-provisioner/amd64/maas-switch-provisioner`.

---

## Script & Asset Management

### **Provisioning Script Lifecycle**

Provisioning scripts are operator-authored artifacts that are stored in the MAAS database and served to switches during the provisioning workflow. Each provisioning script has an associated metadata record that includes:

- **Script name**: Human-readable identifier
- **Description**: Purpose and scope of the script

Script delivery is authenticated via the switch UUID, ensuring only registered switches can retrieve provisioning scripts. The server tracks script execution history and provides queryable logs through the v3 API for post-provisioning audit and debugging.

### **NOS Installer Asset Delivery**

The NOS installer image is a binary uploaded by the operator and stored as a boot resource on the region controller.

**Image URL Construction**:

When the region generates the wrapper script, it resolves the NOS installer URL as:

```
http://<rack>:5248/images/<filename_on_disk>
```

where `filename_on_disk` is the SHA256-based filename used by MAAS boot resource storage, and `<rack>` is the rack controller hostname derived from the requesting ONIE connection.

**Rack-Side Image Caching**:

The rack nginx configuration serves `/images/` from local storage and falls back to the maas-agent caching HTTP proxy if the file is not present locally:

```nginx
location ~ ^/images/([^/]+) {
    root <image-storage>;
    try_files /$1 @agent;
}
```

When the Go binary requests the NOS installer, the rack automatically fetches and caches the image from the region if it has not been cached yet. No explicit sync step is required before provisioning.

If NOS installation fails (non-zero exit code), provisioning is halted, the switch status is set to `FAILED`, and the failure is logged with exit code and combined stdout/stderr output. Operators can retrieve the installer output logs via the v3 API.

---

## Database Changes

### **maasserver_switch (Extensions)**

The existing switch table is extended with provisioning-related fields.

**New Columns**:

| Column      | Type    | Description                                                                |
| :---------- | :------ | :------------------------------------------------------------------------- |
| status      | varchar | Switch provisioning status (NOT_PROVISIONED, DEPLOYING, READY, FAILED)     |
| switch_uuid | uuid    | Unique, non-sequential switch identifier for deployment endpoints (UNIQUE) |

**Notes**:

- `status` tracks the lifecycle of provisioning attempts:
  - **NOT_PROVISIONED**: Switch registered but provisioning never started
  - **DEPLOYING**: Provisioning script currently executing
  - **READY**: Provisioning completed successfully; switch is rebooting or rebooted
  - **FAILED**: Most recent provisioning attempt failed or was interrupted

- `switch_uuid` is generated randomly on switch creation, cryptographically secure, and serves as the path identifier for all deployment API endpoints

### **switch_scripts**

Persistent catalog of provisioning scripts (admin-managed, user-provided).

| Column      | Type         | Description                   |
| :---------- | :----------- | :---------------------------- |
| id          | bigserial    | Primary key                   |
| name        | varchar(255) | Unique script identifier      |
| description | text         | Operator-provided description |
| content     | text         | Raw script content            |
| created_at  | timestamptz  | Creation timestamp            |
| updated_at  | timestamptz  | Last update timestamp         |

### **switch_script_assignment**

Mapping table linking switches to provisioning scripts.

| Column     | Type        | Description                                 |
| :--------- | :---------- | :------------------------------------------ |
| id         | bigserial   | Primary key                                 |
| switch_id  | bigint      | FK to maasserver_switch (on delete CASCADE) |
| script_id  | bigint      | FK to switch_scripts (on delete CASCADE)    |
| created_at | timestamptz | Creation timestamp                          |
| updated_at | timestamptz | Last update timestamp                       |

### **switch_logs**

Unified log storage for all provisioning execution output.

| Column       | Type        | Description                                                           |
| :----------- | :---------- | :-------------------------------------------------------------------- |
| id           | bigserial   | Primary key                                                           |
| switch_id    | bigint      | FK to maasserver_switch (on delete CASCADE)                           |
| log_category | varchar(32) | Log categorization: WRAPPER, NOS_INSTALLATION, or PROVISIONING_SCRIPT |
| exit_code    | integer     | Process exit code (0=success, non-zero=failure)                       |
| output       | text        | Combined stdout and stderr output stream                              |
| created_at   | timestamptz | Log creation timestamp (when uploaded)                                |
| updated_at   | timestamptz | Last update timestamp                                                 |

## API Changes

### **Removed Endpoints**

| Endpoint             | Reason                                                                                              |
| :------------------- | :-------------------------------------------------------------------------------------------------- |
| `GET /nos-installer` | Replaced by rack-side image caching. The Go binary downloads the NOS image directly from `NOS_URL`. |

The `NOSInstallerHandler` class and its `nos.py` handler file are removed from the codebase.

### **Wrapper Endpoint (`GET /MAAS/a/v3/switch-installer`)**

- Reads `Onie-Eth-Addr` header (required) and `Onie-Arch` header (optional, defaults to `amd64`)
- Validates MAC address format; returns 400 if invalid
- Looks up switch by MAC; returns 404 if unknown
- Applies status gate:
  - `NOT_PROVISIONED` → serve wrapper script (200 OK)
  - `DEPLOYING` or `READY` → transition to `FAILED`, return 404
  - `FAILED` → return 404 (no state change)
- Returns a `text/plain` wrapper shell script

### **Switch Deployment Endpoints (used by the Go binary)**

These endpoints require only knowledge of the switch UUID. UUID complexity (128-bit random) prevents enumeration.

- **`POST /switches/{switch_uuid}/status`**: Update switch status
  - Body: plain text, one of `DEPLOYING`, `READY`, or `FAILED`
  - Returns 204 No Content

- **`GET /switches/{switch_uuid}/provisioning-script`**: Fetch assigned provisioning script
  - Returns raw script content as `text/plain`
  - Returns 404 if no script is assigned

- **`POST /switches/{switch_uuid}/logs`**: Upload execution logs
  - Headers: `X-Log-Category` (NOS_INSTALLATION or PROVISIONING_SCRIPT), `X-Exit-Code` (integer)
  - Body: combined stdout + stderr (max 50 MB; exceeding returns 413)
  - Returns 201 Created

### **Provisioning Script Management Endpoints (`/MAAS/a/v3/switch-scripts`, OAuth2 + OpenFGA)**

These endpoints allow operators to create, update, and delete the provisioning scripts that are served to switches during the Go binary execution phase. All endpoints require OAuth2 authentication and `CAN_VIEW/EDIT_GLOBAL_ENTITIES` OpenFGA permissions.

- **`POST /switch-scripts`**: Upload a new provisioning script
  - Body (JSON): `name` (string, unique), `description` (string, optional), `content` (string, raw script text)
  - Returns 201 Created with the created `SwitchScriptResponse`
  - Returns 409 Conflict if a script with the same name already exists

- **`GET /switch-scripts`**: List all provisioning scripts (paginated)
  - Returns a list of `SwitchScriptResponse` objects (does not include `content` to keep list responses small)

- **`GET /switch-scripts/{script_id}`**: Fetch a single provisioning script including its content
  - Returns 404 if not found

- **`PUT /switch-scripts/{script_id}`**: Replace script content and/or metadata
  - Body (JSON): `name`, `description`, `content` (all fields)
  - Returns 200 OK with the updated `SwitchScriptResponse`

- **`DELETE /switch-scripts/{script_id}`**: Delete a provisioning script
  - Cascades: any `switch_script_assignment` rows referencing this script are deleted
  - Returns 204 No Content

#### Script Assignment

Scripts are assigned to switches when creating or updating the switch record. The field `script_id` on `POST /switches` and `PATCH /switches/{switch_id}` sets or replaces the script assignment. Set `script_id` to `null` to remove the assignment. Only one script may be assigned to a switch at a time.

#### `SwitchScriptResponse`

```json
{
  "id": 1,
  "name": "configure-vlan-trunk",
  "description": "Configures all ports as VLAN trunk with PVID 100",
  "content": "#!/bin/sh\n...",
  "created_at": "2026-06-29T12:00:00Z",
  "updated_at": "2026-06-29T12:00:00Z"
}
```

`content` is omitted from list responses (`GET /switch-scripts`).

---

### **Admin Endpoints (`/MAAS/a/v3/switches`, OAuth2 + OpenFGA)**

- **`POST /switches`**: Create switch (with optional `target_image_id` and `script_id` assignment)
- **`PATCH /switches/{switch_id}`**: Update switch (including `script_id` to assign/unassign a script)
- **`GET /switches/{switch_uuid}/logs`**: List provisioning logs (paginated)
  - Query parameter: `category=NOS_INSTALLATION|PROVISIONING_SCRIPT` (optional)

### **Response Objects**

#### Wrapper Script Response (`GET /switch-installer`)

Returns a `text/plain` wrapper script:

```shell
#!/bin/sh
set -eu

export MAAS_URL="http://maas.local/MAAS/a/v3"
export SWITCH_UUID="8f3b9c2a-4d1e-4b6a-9f8c-7e6d5c4b3a2a"
export SWITCH_MAC="00:11:22:33:44:55"
export NOS_URL="http://10.20.0.2:5248/images/abc123def456"

wget -q "http://10.20.0.2:5248/switch-provisioner/amd64/maas-switch-provisioner" -O /tmp/maas-switch-provisioner
chmod +x /tmp/maas-switch-provisioner
exec /tmp/maas-switch-provisioner
```

#### SwitchStatusResponse

```json
{
  "switch_uuid": "8f3b9c2a-4d1e-4b6a-9f8c-7e6d5c4b3a2a",
  "status": "DEPLOYING",
  "mac": "00:11:22:33:44:55",
  "created_at": "2026-06-29T12:00:00Z",
  "updated_at": "2026-06-29T12:01:00Z"
}
```

#### ProvisioningLogResponse

```json
{
  "id": 1,
  "switch_uuid": "8f3b9c2a-4d1e-4b6a-9f8c-7e6d5c4b3a2a",
  "log_category": "NOS_INSTALLATION",
  "exit_code": 0,
  "output": "Downloading NOS image...\nInstalling NOS...\nInstallation complete\n",
  "created_at": "2026-06-29T12:03:00Z"
}
```

### **HTTP Status Codes**

| Code | Meaning                                                                          |
| :--- | :------------------------------------------------------------------------------- |
| 200  | Successfully retrieved data                                                      |
| 201  | Log uploaded or resource created                                                 |
| 204  | Status updated successfully                                                      |
| 400  | Invalid MAC address format or malformed request                                  |
| 404  | Switch not found, switch state prevents provisioning, or resource does not exist |
| 413  | Log upload body exceeds 50 MB                                                    |
| 500  | Server error                                                                     |

---

## Security Framework

### **UUID-Based Switch Identity**

**Generation**:

- Each switch receives a unique, non-sequential UUID on registration
- Generated using `uuid.uuid4()` or equivalent cryptographic random UUID
- Stored in `switch.switch_uuid` column and immutable for the lifetime of the switch

**Validation**:

- All deployment endpoints verify switch_uuid exists and matches the MAC address from DHCP context (if available)
- UUID complexity (128-bit random) prevents enumeration attacks
- No incremental ID mapping is possible

**Advantages**:

- Cannot enumerate switches by ID
- Server-side identification without requiring persistent client-side tokens
- Clean, RESTful endpoint structure
- Each switch has a unique, permanent deployment identifier

**Note: This does not create fully encrypted traffic between MAAS and the switch. This is impossible without certificates on the switch, as will be the case for Ubuntu NOS. The UUID-based identifier provides consistent switch identification and prevents ID enumeration, not confidentiality.**

### **Secrets Management**

Operators manage sensitive credentials independently of MAAS. **Since scripts are served without templating**, operators are responsible for:

1. Embedding credentials directly in provisioning scripts
2. Using external secret retrieval mechanisms (e.g., wget from a secrets server)
3. Protecting script content via access controls and TLS
4. Sanitizing provisioning script output before uploading (removing sensitive data from logs if needed)

MAAS serves the provisioning scripts **as is** and does not guarantee anything about their format, execution flow, or what configurations they actually apply to the switch.

**Important**: Because the wrapper script captures combined stdout/stderr streams and MAAS records and serves these logs "as is," operators are solely responsible for ensuring their custom provisioning scripts do not accidentally echo raw credentials, API tokens, or other sensitive data to the console. Any credentials that appear in the captured logs will be permanently stored and accessible to operators with OpenFGA log view permissions. Implement credential masking and careful output handling in all custom provisioning scripts.

### **Log Upload & Storage**

**When**:

- Wrapper logs: Captured throughout script execution and uploaded at exit (success or failure)
- NOS logs: After NOS image execution completes
- Provisioning logs: After provisioning script execution completes

**How**:

- Logs sent as binary data to `/switches/{switch_uuid}/logs` endpoint
- Categorical metadata passed via `X-Log-Category` header (WRAPPER, NOS_INSTALLATION, or PROVISIONING_SCRIPT)
- Exit code passed via `X-Exit-Code` header
- Combined stdout and stderr in request body
- Server enforces a maximum log upload size limit of 50MB per category request
- Payloads exceeding the 50MB limit return HTTP `413 Payload Too Large` response

**Storage**:

- Stored in `switch_logs` table with log_category field
- Linked to `switch_id` via FK
- Output stored as text field (combined stdout/stderr)
- Immutable once written; operator access controlled via OpenFGA

**Retrieval**:

- Query logs: `GET /switches/{switch_uuid}/logs` (JSON array of log objects)
- Filter by category: `GET /switches/{switch_uuid}/logs?category=NOS_INSTALLATION` (optional)
- Logs queryable from UI for operator troubleshooting

### **Authentication & Authorization**

**Two endpoint classes**:

| Class  | Auth             | OpenFGA                       |
| :----- | :--------------- | :---------------------------- |
| Public | None (MAC-gated) | None                          |
| Admin  | OAuth2           | CAN_VIEW/EDIT_GLOBAL_ENTITIES |

- Public endpoints validate MAC address via DHCP context or request parameter
- Admin endpoints require OAuth2 tokens and OpenFGA authorization
- UUID-based client endpoints are public but require knowledge of UUID (128-bit random value prevents enumeration)

### **Payload Validation**

**Wrapper Request**:

- MAC address format validation: `XX:XX:XX:XX:XX:XX`
- 400 Bad Request if format invalid
- 404 Not Found if MAC unknown or switch state prevents provisioning (DEPLOYING, READY, or FAILED)

**Log Upload Request**:

- `X-Log-Category` header validation: Must be `WRAPPER`, `NOS_INSTALLATION`, or `PROVISIONING_SCRIPT`
- `X-Exit-Code` header validation: Must be integer
- Body must be present (but size limited to 50MB per upload)
- 400 Bad Request if headers missing or invalid
- 413 Payload Too Large if body exceeds 50MB size limit

### **Error Scenarios**

| Scenario                          | Status Before   | Server Response | Status After                            |
| :-------------------------------- | :-------------- | :-------------- | :-------------------------------------- |
| Fresh provisioning                | NOT_PROVISIONED | 200 OK, script  | NOT_PROVISIONED (binary sets DEPLOYING) |
| Re-request while deploying        | DEPLOYING       | 404 Not Found   | FAILED                                  |
| Re-request after READY            | READY           | 404 Not Found   | FAILED                                  |
| Re-request after previous failure | FAILED          | 404 Not Found   | FAILED (unchanged)                      |
| NOS download fails                | DEPLOYING       | —               | FAILED (set by binary)                  |
| NOS execution fails               | DEPLOYING       | —               | FAILED (set by binary)                  |
| Provisioning script fails         | DEPLOYING       | —               | FAILED (set by binary)                  |
| All steps succeed                 | DEPLOYING       | —               | READY (set by binary)                   |

### **Log Output Security**

**Storage**: Combined stdout and stderr stored as plain text.

**Sanitization**:

- **Script authors**: Responsible for masking secrets in output before execution completes
- **Server-side**: Logs stored as-is; access controlled via OpenFGA
- **Operator**: Can review logs before sharing externally

### **Logging & Monitoring**

**Client-Side Logging**:

- Human-readable logs written to stderr (visible in ONIE console)
- Log format: Clear, line-based text suitable for capture via curl/wget

---

# Further Information

## References

- [MA289 \- Spike: Design authenticated ONIE deployment scripts with per-step MAAS reporting](https://docs.google.com/document/u/0/d/1AzoDHmaCvEPS8MZ2YbfO1T2We1GnOIHK3_j87PeQ8II/edit)
  - Complete OpenAPI 3.0.3 schema
  - Detailed Go client specifications
  - Switch deployment data model

- [MA285 \- Tracking Switch Provisioning](https://docs.google.com/document/u/0/d/1yflc_woM2FhPIYzAn49P14powsPMW6z1mzzOaNWPczQ/edit)

# Spec History and Changelog

Please be thorough when recording changes and progress with the spec itself and the work resulting from it. Record every meeting, attendees and conclusions from the meeting.

| Author(s)                                       | Status    | Date        | Comment                                                                                                                                                                                                       |
| :---------------------------------------------- | :-------- | :---------- | :------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| [Maik Rebaum](mailto:maik.rebaum@canonical.com) | Braindump | 29 Jun 2026 | Brain dump                                                                                                                                                                                                    |
| [Maik Rebaum](mailto:maik.rebaum@canonical.com) | Drafting  | 30 Jun 2026 | Drafting                                                                                                                                                                                                      |
| [Maik Rebaum](mailto:maik.rebaum@canonical.com) | Drafting  | 01 Jul 2026 | Replaced full shell script with thin wrapper + Go binary; added arch-specific binary build/serve; removed `/nos-installer`; updated state transition semantics for re-requests; documented rack image caching |
| Person                                          | Approved  | Date        |                                                                                                                                                                                                               |
