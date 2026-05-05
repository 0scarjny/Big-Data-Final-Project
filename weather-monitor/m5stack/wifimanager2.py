"""Implementation of a controller to connect to preferred wifi network(s) [For ESP8266, micro-python]

Config is loaded from a file kept by default in '/networks.json'

Priority of networks is determined implicitly by order in array, first being the highest.
It will go through the list of preferred networks, connecting to the ones it detects present.

Default behaviour is to always start the webrepl after setup,
and only start the access point if we can't connect to a known access point ourselves.

Future scope is to use BSSID instead of SSID when micropython allows it,
this would allow multiple access points with the same name, and we can select by signal strength.


"""
__version__ = "1.0.2"

import json
import time
import os

# Micropython modules
import network
try:
    import webrepl
except ImportError:
    pass
try:
    import uasyncio as asyncio
except ImportError:
    pass

# Robust logger setup
try:
    import logging
    log = logging.getLogger("wifi_manager")
except ImportError:
    # Try ulogging (some ports bundle it as ulogging)
    try:
        import ulogging as logging
        log = logging.getLogger("wifi_manager")
    except (ImportError, AttributeError):
        # Last resort: minimal stub
        class StubLog:
            def __init__(self, name): self.name = name
            def _log(self, level, *args):
                print(f"[{level}] {self.name}:", *args)
            def debug(self, *args):    self._log("DEBUG", *args)
            def info(self, *args):     self._log(" INFO", *args)
            def warning(self, *args):  self._log(" WARN", *args)
            def error(self, *args):    self._log("ERROR", *args)
            def critical(self, *args): self._log("CRIT",  *args)
        log = StubLog("wifi_manager")

class WifiManager:
    webrepl_triggered = False
    _ap_start_policy = "never"
    config_file = '/networks.json'
    _config_server_enabled = False
    _config_server_password = "micropython"
    _connection_callbacks = []
    _last_connection_state = None
    
    # Mobile-friendly tabbed UI — Wi-Fi + Device settings
    _config_html = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>M5Stack Setup</title>
<style>
body{font-family:-apple-system,Arial,sans-serif;margin:0;padding:16px;max-width:480px;background:#f5f5f7;color:#222;}
h1{font-size:1.4em;margin:0 0 12px;}
.tabs{display:flex;gap:4px;margin-bottom:16px;}
.tab{flex:1;padding:10px;text-align:center;border-radius:8px;cursor:pointer;background:#e0e0e0;font-size:.95em;border:none;font-family:inherit;}
.tab.active{background:#0a7;color:#fff;}
.pane{display:none;}
.pane.active{display:block;}
.card{background:#fff;border-radius:10px;padding:16px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.08);}
h2{font-size:1.05em;margin:0 0 12px;color:#444;}
label{display:block;font-size:.9em;margin:10px 0 4px;color:#555;}
input[type=text],input[type=password],input[type=number]{width:100%;padding:10px;font-size:1em;border:1px solid #ccc;border-radius:6px;box-sizing:border-box;}
button{padding:10px 16px;font-size:1em;border:none;border-radius:6px;background:#0a7;color:#fff;cursor:pointer;margin-top:12px;width:100%;font-family:inherit;}
button.sm{background:#c33;padding:6px 10px;font-size:.85em;margin:0;width:auto;}
.row{display:flex;align-items:center;justify-content:space-between;padding:8px 0;border-bottom:1px solid #eee;}
.row:last-child{border-bottom:0;}
.empty{color:#888;font-style:italic;padding:8px 0;}
.msg{padding:10px;border-radius:6px;margin-top:12px;font-size:.9em;display:none;}
.msg.ok{background:#dfd;color:#162;display:block;}
.msg.err{background:#fdd;color:#922;display:block;}
.toggle{font-size:.85em;color:#06a;cursor:pointer;margin-top:6px;display:inline-block;}
.chk{display:flex;align-items:center;gap:10px;margin:10px 0;}
.chk label{margin:0;flex:1;}
input[type=checkbox]{width:20px;height:20px;cursor:pointer;flex-shrink:0;}
.hint{font-size:.8em;color:#888;margin:4px 0 0;}
</style></head>
<body>
<h1>M5Stack Config</h1>
<div class="tabs">
  <button id="t-wifi" class="tab active" onclick="show('wifi')">Wi-Fi</button>
  <button id="t-dev" class="tab" onclick="show('dev')">Device</button>
</div>

<div id="p-wifi" class="pane active">
<div class="card">
  <h2>Saved networks</h2>
  <div id="networks"><div class="empty">Loading...</div></div>
</div>
<div class="card">
  <h2>Add a network</h2>
  <label>Network name (SSID)
    <input type="text" id="ssid" autocomplete="off" autocapitalize="none" autocorrect="off" spellcheck="false">
  </label>
  <label>Password
    <input type="password" id="pwd">
  </label>
  <span class="toggle" onclick="togglePwd()">Show password</span>
  <button onclick="addNet()">Save &amp; reconnect</button>
</div>
<div id="wmsg" class="msg"></div>
</div>

<div id="p-dev" class="pane">
<div class="card">
  <h2>Location</h2>
  <label>Override (blank = auto-detect from IP)
    <input type="text" id="loc" placeholder="e.g. Lausanne" autocomplete="off" autocorrect="off" spellcheck="false">
  </label>
</div>
<div class="card">
  <h2>LEDs</h2>
  <div class="chk">
    <input type="checkbox" id="led">
    <label for="led">Enable LED signals during data upload</label>
  </div>
</div>
<div class="card">
  <h2>Clock &amp; schedule</h2>
  <label>Timezone (e.g. GMT+2)
    <input type="text" id="tz" placeholder="GMT+2" autocomplete="off" autocorrect="off" spellcheck="false">
  </label>
  <label>Send interval in seconds (10–3600)
    <input type="number" id="iv" min="10" max="3600" placeholder="60">
  </label>
  <p class="hint">Sends align to wall-clock boundaries: 60 s fires at :00 of each minute, 300 s fires every 5 min.</p>
</div>
<button onclick="saveDev()">Save device settings</button>
<p class="hint" style="text-align:center;margin-top:8px;">Changes take effect after reboot.</p>
<div id="dmsg" class="msg"></div>
</div>

<script>
function show(n){
  ['wifi','dev'].forEach(k=>{
    document.getElementById('t-'+k).classList.toggle('active',k===n);
    document.getElementById('p-'+k).classList.toggle('active',k===n);
  });
}
var cfg=null;
function wMsg(m,e){var s=document.getElementById('wmsg');s.textContent=m;s.className='msg'+(e?' err':' ok');}
function togglePwd(){var p=document.getElementById('pwd');p.type=p.type==='password'?'text':'password';}
function renderNets(){
  var c=document.getElementById('networks'),l=(cfg&&cfg.known_networks)||[];
  if(!l.length){c.innerHTML='<div class="empty">No saved networks yet.</div>';return;}
  c.innerHTML='';
  l.forEach(function(n,i){
    var r=document.createElement('div');r.className='row';
    var sp=document.createElement('span');sp.textContent=n.ssid;
    var b=document.createElement('button');b.className='sm';b.textContent='Remove';
    b.onclick=(function(idx){return function(){removeNet(idx);};})(i);
    r.appendChild(sp);r.appendChild(b);c.appendChild(r);
  });
}
function saveWifi(msg){
  return fetch('/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)})
  .then(function(r){if(!r.ok)return r.text().then(function(t){throw new Error(t);});wMsg(msg,false);return true;})
  .catch(function(e){wMsg('Save failed: '+e.message,true);return false;});
}
function addNet(){
  var ssid=document.getElementById('ssid').value.trim(),pwd=document.getElementById('pwd').value;
  if(!ssid){wMsg('SSID is required',true);return;}
  if(!cfg){wMsg('Config not loaded yet',true);return;}
  if(!cfg.known_networks)cfg.known_networks=[];
  var i=cfg.known_networks.findIndex(function(n){return n.ssid===ssid;});
  var e={ssid:ssid,password:pwd,enables_webrepl:false};
  if(i>=0)cfg.known_networks[i]=e;else cfg.known_networks.unshift(e);
  saveWifi('Saved. Reset the device to reconnect.').then(function(ok){
    if(ok){document.getElementById('ssid').value='';document.getElementById('pwd').value='';renderNets();}
  });
}
function removeNet(i){cfg.known_networks.splice(i,1);saveWifi('Network removed.').then(function(ok){if(ok)renderNets();});}
fetch('/config').then(function(r){return r.json();}).then(function(d){cfg=d;if(!cfg.known_networks)cfg.known_networks=[];renderNets();})
.catch(function(e){wMsg('Failed to load Wi-Fi config: '+e,true);});
function dMsg(m,e){var s=document.getElementById('dmsg');s.textContent=m;s.className='msg'+(e?' err':' ok');}
function loadDev(){
  fetch('/settings').then(function(r){return r.json();}).then(function(s){
    document.getElementById('loc').value=s.location_override||'';
    document.getElementById('led').checked=s.led_signals_enabled!==false;
    document.getElementById('tz').value=s.timezone||'GMT+2';
    document.getElementById('iv').value=s.send_interval_s||60;
  }).catch(function(e){dMsg('Failed to load device settings: '+e,true);});
}
function saveDev(){
  var iv=parseInt(document.getElementById('iv').value,10)||60;
  if(iv<10||iv>3600){dMsg('Send interval must be 10–3600 seconds',true);return;}
  var body={
    location_override:document.getElementById('loc').value.trim()||null,
    led_signals_enabled:document.getElementById('led').checked,
    timezone:document.getElementById('tz').value.trim()||'GMT+2',
    send_interval_s:iv
  };
  fetch('/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
  .then(function(r){if(!r.ok)return r.text().then(function(t){throw new Error(t);});dMsg('Saved. Reboot to apply.',false);})
  .catch(function(e){dMsg('Save failed: '+e.message,true);});
}
loadDev();
</script>
</body></html>"""

    # Starts the managing call as a co-op async activity
    @classmethod
    def start_managing(cls):
        loop = asyncio.get_event_loop()
        loop.create_task(cls.manage()) # Schedule ASAP
        # Make sure you loop.run_forever() (we are a guest here)

    # Checks the status and configures if needed
    @classmethod
    async def manage(cls):
        while True:
            # Check for connection state changes and notify callbacks
            cls._check_and_notify_connection_state()
            
            status = cls.wlan().status()
            # ESP32 does not currently return
            if (status != network.STAT_GOT_IP) or \
            (cls.wlan().ifconfig()[0] == '0.0.0.0'):  # temporary till #3967
                log.info("Network not connected: managing")
                # Ignore connecting status for now.. ESP32 is a bit strange
                # if status != network.STAT_CONNECTING: <- do not care yet
                cls.setup_network()
            await asyncio.sleep(10)  # Pause 10 seconds between checks

    @classmethod
    def wlan(cls):
        return network.WLAN(network.STA_IF)

    @classmethod
    def accesspoint(cls):
        return network.WLAN(network.AP_IF)

    @classmethod
    def wants_accesspoint(cls) -> bool:
        static_policies = {"never": False, "always": True}
        if cls._ap_start_policy in static_policies:
            return static_policies[cls._ap_start_policy]
        # By default, that leaves "Fallback"
        return cls.wlan().status() != network.STAT_GOT_IP  # Discard intermediate states and check for not connected/ok

    @classmethod
    def setup_network(cls) -> bool:
        # now see our prioritised list of networks and find the first available network
        try:
            with open(cls.config_file, "r") as f:
                config = json.loads(f.read())
                cls.preferred_networks = config['known_networks']
                cls.ap_config = config["access_point"]
                
                # Check for config server settings
                if "config_server" in config:
                    server_config = config["config_server"]
                    if server_config.get("enabled", False):
                        password = server_config.get("password", "micropython")
                        cls.start_config_server(password)
                
                if config.get("schema", 0) != 2:
                    log.warning("Did not get expected schema [2] in JSON config.")
        except Exception as e:
            log.error("Failed to load config file: {}. No known networks selected".format(e))
            cls.preferred_networks = []
            cls.ap_config = {"config": {"essid": "MicroPython-AP", "password": "micropython"}, 
                           "enables_webrepl": False, "start_policy": "never"}
            return False

        # set things up
        cls.webrepl_triggered = False  # Until something wants it
        cls.wlan().active(True)

        # scan what's available
        available_networks = []
        try:
            scan_results = cls.wlan().scan()
            for network in scan_results:
                try:
                    ssid = network[0].decode("utf-8")
                    bssid = network[1]
                    strength = network[3]
                    available_networks.append(dict(ssid=ssid, bssid=bssid, strength=strength))
                except (IndexError, UnicodeDecodeError) as e:
                    log.warning("Failed to parse network scan result: {}".format(e))
                    continue
        except OSError as e:
            log.error("Network scan failed: {}".format(e))
            return False
        # Sort fields by strongest first in case of multiple SSID access points
        available_networks.sort(key=lambda station: station["strength"], reverse=True)

        # Get the ranked list of BSSIDs to connect to, ranked by preference and strength amongst duplicate SSID
        candidates = []
        for aPreference in cls.preferred_networks:
            for aNetwork in available_networks:
                if aPreference["ssid"] == aNetwork["ssid"]:
                    connection_data = {
                        "ssid": aNetwork["ssid"],
                        "bssid": aNetwork["bssid"],  # NB: One day we might allow collection by exact BSSID
                        "password": aPreference["password"],
                        "enables_webrepl": aPreference["enables_webrepl"]}
                    candidates.append(connection_data)

        connected = False
        for new_connection in candidates:
            log.info("Attempting to connect to network {0}...".format(new_connection["ssid"]))
            # Micropython 1.9.3+ supports BSSID specification so let's use that
            if cls.connect_to(ssid=new_connection["ssid"], password=new_connection["password"],
                              bssid=new_connection["bssid"]):
                log.info("Successfully connected {0}".format(new_connection["ssid"]))
                cls.webrepl_triggered = new_connection["enables_webrepl"]
                
                # Notify successful connection
                try:
                    ifconfig = cls.wlan().ifconfig()
                    ip = ifconfig[0] if ifconfig else "unknown"
                    cls._notify_connection_change("connected", ssid=new_connection["ssid"], ip=ip)
                except Exception as e:
                    log.warning(f"Failed to notify connection: {e}")
                
                connected = True
                break  # We are connected so don't try more
        
        # If no connection was successful and we have candidates, notify failure
        if not connected and candidates:
            try:
                failed_ssids = [c["ssid"] for c in candidates]
                cls._notify_connection_change("connection_failed", attempted_networks=failed_ssids)
            except Exception as e:
                log.warning(f"Failed to notify connection failure: {e}")


        # Check if we are to start the access point
        cls._ap_start_policy = cls.ap_config.get("start_policy", "never")
        should_start_ap = cls.wants_accesspoint()
        try:
            cls.accesspoint().active(should_start_ap)
            if should_start_ap:  # Only bother setting the config if it WILL be active
                log.info("Enabling your access point...")
                cls.accesspoint().config(**cls.ap_config["config"])
                cls.webrepl_triggered = cls.ap_config["enables_webrepl"]
                
                # Notify AP started
                try:
                    essid = cls.ap_config["config"].get("essid", "unknown")
                    cls._notify_connection_change("ap_started", essid=essid)
                except Exception as e:
                    log.warning(f"Failed to notify AP start: {e}")
                    
            cls.accesspoint().active(cls.wants_accesspoint())  # It may be DEACTIVATED here
        except OSError as e:
            log.error("Failed to configure access point: {}".format(e))

        # may need to reload the config if access points trigger it

        # start the webrepl according to the rules
        if cls.webrepl_triggered:
            try:
                webrepl.start()
            except (NameError, TypeError) as e:
                log.warning(f"Could not start WebREPL: {e}")

        # return the success status, which is ultimately if we connected to managed and not ad hoc wifi.
        return cls.wlan().isconnected()

    @classmethod
    def connect_to(cls, *, ssid, password, **kwargs) -> bool:
        try:
            cls.wlan().connect(ssid, password, **kwargs)
        except OSError as e:
            log.error("Failed to initiate connection to {}: {}".format(ssid, e))
            return False

        for check in range(0, 20):  # Wait a maximum of 20 times (20 * 500ms = 10 seconds) for success
            try:
                if cls.wlan().isconnected():
                    return True
            except OSError as e:
                log.warning("Connection check failed for {}: {}".format(ssid, e))
                break
            time.sleep_ms(500)
        return False

    @classmethod
    def _check_basic_auth(cls, request):
        """Check HTTP Basic Authentication"""
        if not cls._config_server_password:
            return True  # No password required
            
        auth_header = None
        for line in request.split('\r\n'):
            if line.lower().startswith('authorization: basic '):
                auth_header = line.split(' ', 2)[2]
                break
        
        if not auth_header:
            return False
            
        try:
            # Decode base64 credentials
            import ubinascii
            decoded = ubinascii.a2b_base64(auth_header).decode()
            if ':' in decoded:
                username, password = decoded.split(':', 1)
                return username == "admin" and password == cls._config_server_password
        except:
            pass
        return False

    @classmethod
    def _handle_config_request(cls, request: str) -> str:
        """
        Handle HTTP requests for the configuration web server.
        Supports:
          - GET /config       → returns JSON config
          - POST /config      → updates JSON config
          - GET / or /index   → returns HTML editor
        Requires Basic Auth username “admin” and password cls._config_server_password,
        unless password is None or empty (in which case auth is skipped).
        """
        # 1) Authentication
        if cls._config_server_password:
            # look for “Authorization: Basic …”
            auth = None
            for line in request.split('\r\n'):
                if line.lower().startswith("authorization: basic "):
                    auth = line.split(" ", 2)[2]
                    break
            if not auth:
                return (
                    "HTTP/1.1 401 Unauthorized\r\n"
                    "WWW-Authenticate: Basic realm=\"WiFi Config\"\r\n"
                    "Content-Type: text/plain\r\n"
                    "\r\n"
                    "Authentication required"
                )
            # decode and verify
            try:
                import ubinascii
                user_pass = ubinascii.a2b_base64(auth).decode()
                user, pwd = user_pass.split(":", 1)
                if user != "admin" or pwd != cls._config_server_password:
                    raise ValueError
            except Exception:
                return (
                    "HTTP/1.1 401 Unauthorized\r\n"
                    "WWW-Authenticate: Basic realm=\"WiFi Config\"\r\n"
                    "Content-Type: text/plain\r\n"
                    "\r\n"
                    "Invalid credentials"
                )

        # 2) POST /settings → update device settings
        if request.startswith("POST /settings"):
            idx = request.find("\r\n\r\n")
            if idx < 0:
                return "HTTP/1.1 400 Bad Request\r\nContent-Type: text/plain\r\n\r\nNo request body"
            body = request[idx+4:]
            try:
                import device_settings as _ds
                data = json.loads(body)
                _ds.save(data)
                return "HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\nSettings saved"
            except Exception as e:
                return (
                    "HTTP/1.1 400 Bad Request\r\n"
                    "Content-Type: text/plain\r\n"
                    f"\r\nFailed to save settings: {e}"
                )

        # 3) GET /settings → serve device settings JSON
        if request.startswith("GET /settings"):
            try:
                import device_settings as _ds
                return (
                    "HTTP/1.1 200 OK\r\n"
                    "Content-Type: application/json\r\n"
                    "\r\n"
                    + json.dumps(_ds.load())
                )
            except Exception as e:
                return (
                    "HTTP/1.1 500 Internal Server Error\r\n"
                    "Content-Type: text/plain\r\n"
                    f"\r\nCould not read settings: {e}"
                )

        # 4) POST /config → update wifi config
        if request.startswith("POST /config"):
            # extract body
            idx = request.find("\r\n\r\n")
            if idx < 0:
                return "HTTP/1.1 400 Bad Request\r\nContent-Type: text/plain\r\n\r\nNo request body"
            body = request[idx+4:]
            # parse JSON
            try:
                cfg = json.loads(body)
                if "known_networks" not in cfg or "access_point" not in cfg:
                    raise ValueError("Missing required keys")
            except ValueError as ve:
                return (
                    "HTTP/1.1 400 Bad Request\r\n"
                    "Content-Type: text/plain\r\n"
                    f"\r\nInvalid JSON: {ve}"
                )
            except Exception as e:
                return (
                    "HTTP/1.1 400 Bad Request\r\n"
                    "Content-Type: text/plain\r\n"
                    f"\r\nJSON parse error: {e}"
                )
            # write file
            try:
                with open(cls.config_file, "w") as f:
                    f.write(body)
                log.info("Configuration updated via web interface")
                # reconfigure network immediately
                try:
                    cls.setup_network()
                except Exception as e:
                    log.warning(f"Network re-setup failed: {e}")
                return "HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\nConfiguration updated successfully"
            except Exception as e:
                return (
                    "HTTP/1.1 500 Internal Server Error\r\n"
                    "Content-Type: text/plain\r\n"
                    f"\r\nFailed to save config: {e}"
                )

        # 5) GET /config → serve wifi config JSON
        if request.startswith("GET /config"):
            try:
                with open(cls.config_file, "r") as f:
                    data = f.read()
                return (
                    "HTTP/1.1 200 OK\r\n"
                    "Content-Type: application/json\r\n"
                    "\r\n"
                    f"{data}"
                )
            except Exception as e:
                return (
                    "HTTP/1.1 500 Internal Server Error\r\n"
                    "Content-Type: text/plain\r\n"
                    f"\r\nCould not read config: {e}"
                )

        # 6) GET / or /index → serve HTML editor
        if request.startswith("GET / ") or "GET /index" in request:
            return (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/html\r\n"
                "\r\n"
                f"{cls._config_html}"
            )

        # 7) anything else → 404
        return (
            "HTTP/1.1 404 Not Found\r\n"
            "Content-Type: text/plain\r\n"
            "\r\n"
            "Not found"
        )

    @classmethod
    async def _run_config_server(cls):
        """Run the configuration web server.

        Re-entrant: stays alive for the lifetime of the process. The outer
        loop watches `_config_server_enabled` — when False the socket is
        closed and the task idles cheaply (200 ms checks); when True the
        socket is (re)bound and accept() polled with a tight 50 ms timeout
        so the asyncio loop stays responsive for ui_task while a user is
        on the configuration page."""
        import socket
        server_socket = None
        while True:
            if not cls._config_server_enabled:
                if server_socket is not None:
                    try: server_socket.close()
                    except Exception: pass
                    server_socket = None
                    log.info("Config server stopped")
                await asyncio.sleep_ms(200)
                continue

            if server_socket is None:
                try:
                    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    server_socket.bind(('0.0.0.0', 8080))
                    server_socket.listen(1)
                    server_socket.settimeout(0.05)  # short — keep ui_task responsive
                    log.info("Config server started on port 8080")
                except Exception as e:
                    log.error("Config server bind failed: {}".format(e))
                    server_socket = None
                    await asyncio.sleep_ms(1000)
                    continue

            try:
                conn, addr = server_socket.accept()
                log.debug("Config server connection from {}".format(addr))

                conn.settimeout(5.0)
                request = conn.recv(4096).decode()

                response = cls._handle_config_request(request)

                conn.send(response.encode())
                conn.close()

            except OSError:
                # accept() timeout or no connection — yield control quickly
                await asyncio.sleep_ms(50)
            except Exception as e:
                log.warning("Config server request error: {}".format(e))
                await asyncio.sleep_ms(50)

    @classmethod
    def start_config_server(cls, password="micropython"):
        """Start the configuration web server"""
        if not asyncio:
            log.error("Config server requires asyncio")
            return False
            
        cls._config_server_password = password
        cls._config_server_enabled = True
        
        # Start server as async task
        loop = asyncio.get_event_loop()
        loop.create_task(cls._run_config_server())
        
        log.info("Config server starting on http://[device-ip]:8080")
        return True

    @classmethod
    def stop_config_server(cls):
        """Stop the configuration web server"""
        cls._config_server_enabled = False

    @classmethod
    def on_connection_change(cls, callback):
        """Register a callback function for connection state changes
        
        Callback will be called with (event, **kwargs) where event is one of:
        - 'connected': Successfully connected to a network
        - 'disconnected': Lost connection to network  
        - 'ap_started': Access point was activated
        - 'connection_failed': All connection attempts failed
        
        Example:
            def my_callback(event, **kwargs):
                if event == 'connected':
                    print(f"Connected to {kwargs.get('ssid')} with IP {kwargs.get('ip')}")
                elif event == 'disconnected':
                    print("Lost connection")
            
            WifiManager.on_connection_change(my_callback)
        """
        if callback not in cls._connection_callbacks:
            cls._connection_callbacks.append(callback)
            log.debug(f"Registered connection callback: {callback}")

    @classmethod 
    def remove_connection_callback(cls, callback):
        """Remove a previously registered connection callback"""
        if callback in cls._connection_callbacks:
            cls._connection_callbacks.remove(callback)
            log.debug(f"Removed connection callback: {callback}")

    @classmethod
    def _notify_connection_change(cls, event, **kwargs):
        """Notify all registered callbacks of a connection state change"""
        log.debug(f"Connection event: {event} with args: {kwargs}")
        
        for callback in cls._connection_callbacks:
            try:
                callback(event, **kwargs)
            except Exception as e:
                log.warning(f"Connection callback error: {e}")
        
        # Update last known state for state change detection
        cls._last_connection_state = event

    @classmethod
    def _check_and_notify_connection_state(cls):
        """Check current connection state and notify if changed"""
        try:
            is_connected = cls.wlan().isconnected()
            current_state = "connected" if is_connected else "disconnected"
            
            # Only notify on state changes
            if cls._last_connection_state != current_state:
                if is_connected:
                    # Get connection details
                    ifconfig = cls.wlan().ifconfig()
                    ip = ifconfig[0] if ifconfig else "unknown"
                    # Try to get connected SSID (not all MicroPython versions support this)
                    ssid = "unknown"
                    try:
                        config = cls.wlan().config('ssid')
                        if config:
                            ssid = config
                    except:
                        pass
                    
                    cls._notify_connection_change("connected", ssid=ssid, ip=ip)
                else:
                    cls._notify_connection_change("disconnected")
                    
        except Exception as e:
            log.warning(f"Connection state check failed: {e}")