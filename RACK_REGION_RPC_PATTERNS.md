# Rack-Region Communication Patterns in MAAS

## Overview

MAAS uses **AMP (Asynchronous Messaging Protocol)** from Twisted for bidirectional RPC communication between rack and region controllers. This is a binary protocol that supports request-response patterns with error handling.

## Architecture

```
┌─────────────────┐                    ┌─────────────────┐
│  Rack Services  │                    │ Region Services │
│  (provisioning) │ ← RPC Connection → │   (maasserver)  │
└─────────────────┘                    └─────────────────┘
        │                                      │
        │ Uses: ClusterClient                  │ Implements: Region responders
        │ Entry: getRegionClient()             │ File: regionservice.py
        │ Calls: region.py commands            │ Handles: @region.X.responder
        │                                      │
        └─ RPC Commands defined in region.py ─┘
```

## Key Files

| File | Purpose |
|------|---------|
| `src/provisioningserver/rpc/region.py` | RPC command definitions rack calls region |
| `src/provisioningserver/rpc/common.py` | AMP protocol implementation, Client wrapper |
| `src/provisioningserver/rpc/clusterservice.py` | ClusterClient for rack→region calls |
| `src/provisioningserver/rpc/__init__.py` | `getRegionClient()` function |
| `src/maasserver/rpc/regionservice.py` | Region RPC protocol & responders |
| `src/provisioningserver/rackdservices/*.py` | Services using RPC (power monitor, networks, etc) |

## 1. RPC Command Definition

Commands are defined in [src/provisioningserver/rpc/region.py](src/provisioningserver/rpc/region.py).

### Pattern: Define Command with Arguments and Response

```python
from twisted.protocols import amp
from provisioningserver.rpc.arguments import StructureAsJSON, ParsedURL
from provisioningserver.rpc.exceptions import BootConfigNoResponse

class GetBootConfig(amp.Command):
    """Get the boot configuration for booting a machine.
    
    This is called by PXE boot handlers when a machine requests its
    boot configuration.
    """

    arguments = [
        # Caller (rack controller system_id)
        (b"system_id", amp.Unicode()),
        # Machine's network information
        (b"local_ip", amp.Unicode()),
        (b"remote_ip", amp.Unicode()),
        # Optional hardware details
        (b"arch", amp.Unicode(optional=True)),
        (b"subarch", amp.Unicode(optional=True)),
        (b"mac", amp.Unicode(optional=True)),
        (b"hardware_uuid", amp.Unicode(optional=True)),
        (b"bios_boot_method", amp.Unicode(optional=True)),
    ]
    
    response = [
        # Boot image details
        (b"arch", amp.Unicode()),
        (b"subarch", amp.Unicode()),
        (b"osystem", amp.Unicode()),
        (b"release", amp.Unicode()),
        (b"kernel_osystem", amp.Unicode()),
        (b"kernel_release", amp.Unicode()),
        (b"kernel", amp.Unicode(optional=True)),
        (b"initrd", amp.Unicode(optional=True)),
        # Boot URLs and configuration
        (b"preseed_url", amp.Unicode()),
        (b"fs_host", amp.Unicode()),
        (b"log_host", amp.Unicode()),
        (b"log_port", amp.Integer(optional=True)),
        (b"extra_opts", amp.Unicode()),
        (b"purpose", amp.Unicode()),
        # Other metadata
        (b"hostname", amp.Unicode()),
        (b"domain", amp.Unicode()),
        (b"system_id", amp.Unicode(optional=True)),
        (b"http_boot", amp.Boolean(optional=True)),
    ]
    
    # Define error types that can be returned
    errors = {BootConfigNoResponse: b"BootConfigNoResponse"}
```

### Data Types

```python
# Basic types
(b"name", amp.Unicode())              # String
(b"count", amp.Integer())             # Number  
(b"enabled", amp.Boolean())           # True/False

# Complex types
(b"url", ParsedURL())                 # URL object (urlparse result)
(b"data", StructureAsJSON())          # Complex object as JSON string
(b"items", AmpList(StructureAsJSON())) # List of objects

# Optional arguments/response
(b"optional_field", amp.Unicode(optional=True))
```

## 2. Region-Side: Implementing Responders

Responders are implemented in [src/maasserver/rpc/regionservice.py](src/maasserver/rpc/regionservice.py) in the `Region` class.

### Pattern: Responder Handler

```python
from twisted.internet.defer import inlineCallbacks
from maasserver.utils.orm import transactional
from maasserver.rpc import region, nodes

class Region(SecuredRPCProtocol):
    
    @region.GetBootConfig.responder
    def get_boot_config(
        self,
        system_id,
        local_ip,
        remote_ip,
        arch=None,
        subarch=None,
        mac=None,
        hardware_uuid=None,
        bios_boot_method=None,
    ):
        """Handle GetBootConfig RPC request from rack.
        
        The rack calls this when a machine requests its boot configuration.
        We query the region database for the machine's boot parameters.
        """
        # deferToDatabase wraps sync DB call in async operation
        return deferToDatabase(
            boot.get_config,
            system_id,
            local_ip,
            remote_ip,
            arch=arch,
            subarch=subarch,
            mac=mac,
            hardware_uuid=hardware_uuid,
            bios_boot_method=bios_boot_method,
        )
```

### Pattern: Responder Returning Data

```python
@region.ListNodePowerParameters.responder
def list_node_power_parameters(self, uuid):
    """Get list of nodes whose power state needs to be queried.
    
    The power monitor service on the rack calls this periodically
    to get the list of nodes it should query.
    """
    d = deferToDatabase(nodes.list_cluster_nodes_power_parameters, uuid)
    # Transform result to match response spec
    d.addCallback(lambda nodes: {"nodes": nodes})
    return d
```

### Pattern: Responder with No Response Data

```python
@region.MarkNodeFailed.responder
def mark_node_failed(self, system_id, error_description):
    """Mark a node as failed.
    
    Called by rack when provisioning/commissioning fails.
    """
    d = deferToDatabase(
        nodes.mark_node_failed, system_id, error_description
    )
    # Return empty dict even though response spec is empty
    d.addCallback(lambda args: {})
    return d
```

### Pattern: Fire-and-Forget Events

```python
@region.SendEvent.responder
def send_event(self, system_id, type_name, description):
    """Log an event reported by the rack.
    
    Don't wait for the database write; return immediately.
    """
    timestamp = timezone.now()
    dbtasks = eventloop.services.getServiceNamed("database-tasks")
    dbtasks.addTask(
        events.send_event, 
        system_id, type_name, description, timestamp
    )
    # Return immediately without waiting
    return succeed({})
```

### Pattern: Complex Data Processing

```python
@region.RequestNodeInfoByMACAddress.responder
def request_node_info_by_mac_address(self, mac_address):
    """Look up node information by MAC address.
    
    This is called during network discovery on the rack.
    """
    d = deferToDatabase(request_node_info_by_mac_address, mac_address)

    def format_response(data):
        """Transform database results into response format."""
        node, purpose = data
        return {
            "system_id": node.system_id,
            "hostname": node.hostname,
            "status": node.status,
            "boot_type": "fastpath",
            "osystem": node.osystem,
            "distro_series": node.distro_series,
            "architecture": node.architecture,
            "purpose": purpose,
        }

    d.addCallback(format_response)
    return d
```

## 3. Rack-Side: Calling the Region

Rack services use [src/provisioningserver/rpc/__init__.py](src/provisioningserver/rpc/__init__.py)'s `getRegionClient()` to get a client.

### Pattern: Getting the Client

```python
from provisioningserver.rpc import getRegionClient
from provisioningserver.rpc.exceptions import NoConnectionsAvailable

def try_query_nodes(self):
    """Attempt to query nodes from the region.
    
    This is called periodically. Log errors but don't stop the timer.
    """
    try:
        client = getRegionClient()
    except NoConnectionsAvailable:
        log.debug(
            "Cannot monitor nodes; region controller not available."
        )
        return None
    else:
        # Make the actual RPC call
        d = self.query_nodes(client)
        d.addErrback(self.query_nodes_failed, client.localIdent)
        return d
```

### Pattern: Making RPC Calls with Async/Await

```python
from twisted.internet.defer import inlineCallbacks
from provisioningserver.rpc.region import ListNodePowerParameters

@inlineCallbacks
def query_nodes(self, client):
    """Query all nodes that need power status checked.
    
    Keep fetching from region until we get an empty list.
    """
    while True:
        # Call the RPC: yield client(CommandClass, **args)
        response = yield client(
            ListNodePowerParameters, 
            uuid=client.localIdent
        )
        
        # Extract response data
        power_parameters = response["nodes"]
        
        if len(power_parameters) > 0:
            # Process the parameters (in parallel)
            yield query_all_nodes(
                power_parameters,
                max_concurrency=self.max_nodes_at_once,
                clock=self.clock,
            )
        else:
            # No more nodes to check
            break
```

### Pattern: Handling Errors

```python
from provisioningserver.rpc.exceptions import NoSuchCluster, ConnectionDone

def query_nodes_failed(self, failure, localIdent):
    """Handle RPC call failures."""
    if failure.check(NoSuchCluster):
        # Rack not registered with region
        maaslog.error(
            "Rack controller '%s' is not recognised.", localIdent
        )
    elif failure.check(ConnectionDone):
        # Lost connection to region
        maaslog.error("Lost connection to region controller.")
    else:
        # Other error - log full details
        log.err(failure, "Querying node power states.")
        maaslog.error(
            "Failed to query nodes' power status: %s",
            failure.getErrorMessage(),
        )
```

## 4. Real Examples from MAAS

### Example 1: Power State Monitoring Service

**Service** ([src/provisioningserver/rackdservices/node_power_monitor_service.py](src/provisioningserver/rackdservices/node_power_monitor_service.py)):

```python
from datetime import timedelta
from twisted.application.internet import TimerService
from twisted.internet.defer import inlineCallbacks
from provisioningserver.rpc import getRegionClient
from provisioningserver.rpc.region import ListNodePowerParameters

class NodePowerMonitorService(TimerService):
    """Periodically check power status of nodes in the cluster."""

    check_interval = timedelta(seconds=15).total_seconds()
    max_nodes_at_once = 5

    def __init__(self, clock=None):
        # Call try_query_nodes every 15 seconds
        super().__init__(self.check_interval, self.try_query_nodes)
        self.clock = clock

    def try_query_nodes(self):
        """Attempt to get power parameters from region."""
        try:
            client = getRegionClient()
        except NoConnectionsAvailable:
            log.debug("Region not available for power monitoring.")
        else:
            d = self.query_nodes(client)
            d.addErrback(self.query_nodes_failed, client.localIdent)
            return d

    @inlineCallbacks
    def query_nodes(self, client):
        """Fetch and process power parameters."""
        while True:
            # RPC call to region
            response = yield client(
                ListNodePowerParameters,
                uuid=client.localIdent
            )
            power_parameters = response["nodes"]
            
            if power_parameters:
                # Process all nodes concurrently
                yield query_all_nodes(
                    power_parameters,
                    max_concurrency=self.max_nodes_at_once,
                    clock=self.clock,
                )
            else:
                break

    def query_nodes_failed(self, failure, localIdent):
        """Handle failures gracefully."""
        if failure.check(NoSuchCluster):
            maaslog.error("Rack '%s' unknown.", localIdent)
        else:
            log.err(failure)
```

### Example 2: Network Interface Monitoring

**Region Command** ([src/provisioningserver/rpc/region.py](src/provisioningserver/rpc/region.py)):

```python
class UpdateControllerState(amp.Command):
    """Called by rack to update its state in the region."""
    
    arguments = [
        (b"system_id", amp.Unicode()),
        (b"scope", amp.Unicode()),  # "interfaces", "services", etc
        (b"state", StructureAsJSON()),  # Complex JSON data
    ]
    response = []
    errors = {NoSuchNode: b"NoSuchNode", NoSuchScope: b"NoSuchScope"}
```

**Region Handler** ([src/maasserver/rpc/regionservice.py](src/maasserver/rpc/regionservice.py)):

```python
@region.UpdateControllerState.responder
def update_controller_state(self, system_id, scope, state):
    """Handle rack state updates.
    
    The scope indicates what type of state is being updated:
    - "interfaces": Network interface changes
    - "services": Service status changes
    """
    d = deferToDatabase(
        rackcontrollers.update_state, 
        system_id, scope, state
    )
    return d.addCallback(lambda _: {})
```

**Rack Service** ([src/provisioningserver/rackdservices/networks_monitoring_service.py](src/provisioningserver/rackdservices/networks_monitoring_service.py)):

```python
@inlineCallbacks
def update_region_interfaces(self):
    """Report interface changes to region."""
    client = yield self.rpc_service.getEventLoop()
    
    # Gather current interfaces
    interfaces = yield get_all_interfaces_definition()
    
    # Send to region
    yield client(
        region.UpdateControllerState,
        system_id=MAAS_ID.get(),
        scope="interfaces",
        state=interfaces,
    )
```

### Example 3: Rack Registration

**Region Command** ([src/provisioningserver/rpc/region.py](src/provisioningserver/rpc/region.py)):

```python
class RegisterRackController(amp.Command):
    """First RPC call: rack registers itself with region."""

    arguments = [
        (b"system_id", amp.Unicode(optional=True)),
        (b"hostname", amp.Unicode()),
        (b"interfaces", StructureAsJSON()),
        (b"url", ParsedURL(optional=True)),
        (b"beacon_support", amp.Boolean(optional=True)),
        (b"version", amp.Unicode(optional=True)),
        (b"agent_uuid", amp.Unicode(optional=True)),
    ]
    response = [
        (b"system_id", amp.Unicode()),
        (b"encrypted_cluster_certificate", amp.Unicode(optional=True)),
        (b"beacon_support", amp.Boolean(optional=True)),
        (b"version", amp.Unicode(optional=True)),
        (b"uuid", amp.Unicode(optional=True)),
    ]
    errors = {CannotRegisterRackController: b"CannotRegisterRackController"}
```

**Rack Caller** ([src/provisioningserver/rpc/clusterservice.py](src/provisioningserver/rpc/clusterservice.py)):

```python
@inlineCallbacks
def registerRackWithRegion(self):
    """Register this rack controller with the region."""
    system_id = MAAS_ID.get() or ""
    interfaces = get_all_interfaces_definition()
    hostname = gethostname()
    parsed_url = urlparse(self.service.maas_url)
    version = str(get_running_version())

    try:
        # Call RegisterRackController RPC
        data = yield self.callRemote(
            region.RegisterRackController,
            system_id=system_id,
            hostname=hostname,
            interfaces=interfaces,
            url=parsed_url,
            beacon_support=True,
            version=version,
            agent_uuid=agent_uuid,
        )
        
        # Extract response
        self.localIdent = data["system_id"]
        MAAS_ID.set(self.localIdent)
        
        # Handle encrypted certificate
        encrypted_cert = data.get("encrypted_cluster_certificate")
        if encrypted_cert:
            decoded = json.loads(fernet_decrypt_psk(encrypted_cert))
            certificate = Certificate.from_pem(
                decoded["key"],
                decoded["cert"],
                ca_certs_material=decoded["cacerts"],
            )
            store_maas_cluster_cert_tuple(
                private_key=certificate.private_key_pem().encode(),
                certificate=certificate.certificate_pem().encode(),
                cacerts=certificate.ca_certificates_pem().encode(),
            )
        
        log.msg(f"Rack registered with system_id: {self.localIdent}")
        return True
        
    except exceptions.CannotRegisterRackController:
        log.msg("Registration rejected by region")
        return False
```

## 5. Common RPC Commands

### Rack Registration & Configuration
| Command | Direction | Purpose |
|---------|-----------|---------|
| `RegisterRackController` | Rack→Region | Register rack with region |
| `GetControllerType` | Rack→Region | Check if node is region/rack |
| `GetTimeConfiguration` | Rack→Region | Fetch NTP configuration |
| `GetDNSConfiguration` | Rack→Region | Fetch DNS settings |
| `GetProxyConfiguration` | Rack→Region | Fetch HTTP proxy settings |
| `GetSyslogConfiguration` | Rack→Region | Fetch syslog settings |
| `UpdateControllerState` | Rack→Region | Report rack state changes |

### Boot & Provisioning
| Command | Direction | Purpose |
|---------|-----------|---------|
| `GetBootConfig` | Rack→Region | Get boot config for machine |
| `RequestNodeInfoByMACAddress` | Rack→Region | Lookup node by MAC (discovery) |
| `MarkNodeFailed` | Rack→Region | Report provisioning failure |

### Power Management
| Command | Direction | Purpose |
|---------|-----------|---------|
| `ListNodePowerParameters` | Rack→Region | Get list of nodes to query power for |
| `UpdateNodePowerState` | Rack→Region | Report power state of node |

### Events & Monitoring
| Command | Direction | Purpose |
|---------|-----------|---------|
| `SendEvent` | Rack→Region | Log event on system_id |
| `SendEventMACAddress` | Rack→Region | Log event on MAC address |
| `SendEventIPAddress` | Rack→Region | Log event on IP address |
| `RegisterEventType` | Rack→Region | Register custom event type |
| `ReportForeignDHCPServer` | Rack→Region | Report foreign DHCP found |
| `ReportMDNSEntries` | Rack→Region | Report discovered mDNS |
| `ReportNeighbours` | Rack→Region | Report neighbor devices |

### Node Management
| Command | Direction | Purpose |
|---------|-----------|---------|
| `CreateNode` | Rack→Region | Create node from discovery |
| `CommissionNode` | Rack→Region | Trigger node commissioning |
| `GetDiscoveryState` | Rack→Region | Get monitoring state |

## 6. Key Patterns & Best Practices

### Pattern 1: Simple RPC with Return Value
```python
# Rack side
response = yield client(CommandName, arg1=value1, arg2=value2)
result = response["field_name"]

# Region side
@region.CommandName.responder
def command_name(self, arg1, arg2):
    d = deferToDatabase(some_function, arg1, arg2)
    d.addCallback(lambda result: {"field_name": result})
    return d
```

### Pattern 2: Looping RPC Calls
```python
# Keep calling until region returns empty
while True:
    response = yield client(CommandName, uuid=some_id)
    items = response["items"]
    if not items:
        break
    yield process_items(items)
```

### Pattern 3: Error Handling
```python
try:
    client = getRegionClient()
except NoConnectionsAvailable:
    log.warning("Region unavailable")
else:
    d = self.do_rpc_call(client)
    d.addErrback(self.handle_error, context_info)
```

### Pattern 4: Fire-and-Forget
```python
# Region side: queue work without waiting
@region.SendEvent.responder
def send_event(self, system_id, message):
    dbtasks = eventloop.services.getServiceNamed("database-tasks")
    dbtasks.addTask(log_event, system_id, message)
    return succeed({})  # Return immediately
```

## Testing RPC

See [src/provisioningserver/rackdservices/tests/](src/provisioningserver/rackdservices/tests/) for examples:

```python
from provisioningserver.rackdservices.testing import prepareRegion
from provisioningserver.rpc import region

# Mock the region responses
fixture = test.useFixture(MockLiveClusterToRegionRPCFixture())
protocol, connecting = fixture.makeEventLoop(
    region.ListNodePowerParameters,
    region.GetBootConfig,
)

protocol.ListNodePowerParameters.side_effect = always_succeed_with(
    {"nodes": [node_params_1, node_params_2]}
)
```

## References

- **AMP Protocol**: [Twisted AMP documentation](https://twistedmatrix.com/documents/current/core/howto/amp.html)
- **Twisted Deferreds**: [Deferred documentation](https://twistedmatrix.com/documents/current/core/howto/defer.html)
- **MAAS Source**: See files listed above in repo
