import streamlit as st
import time
import queue
import json
import re
import paho.mqtt.client as mqtt
from streamlit_autorefresh import st_autorefresh
import warnings

warnings.filterwarnings("ignore")
st.markdown(
    "[📄 View Zero Export Control Documentation](https://docs.google.com/document/d/19t-4g3MpZiy0W-6FyBcOZS9UOEp6_FumMR7E_fBK4PU/edit?usp=sharing)"
)

st.set_page_config("Solax Zero Export Control", layout="centered")

MQTT_BROKER = "ecozen.ai"
MQTT_PORT = 1883

AUTO_REFRESH_MS = 500
MAX_LOG_LINES = 100
TIMEOUT = 6

TOPIC_PREFIX = "EZMCOGX"
DEVICE_TOPICS = [f"{TOPIC_PREFIX}{i:06d}" for i in range(1, 301)]

# =====================================================
# SESSION STATE INIT
# =====================================================
def init_state():
    defaults = {
        # mqtt
        "mqtt_client": None,
        "rx_queue": queue.Queue(),

        # connection
        "state": "IDLE",     # IDLE | CONNECTING | CONNECTED | READ_CT | READ_EXPORT | ENABLE | DISABLE
        "command_topic": None,

        # data
        "ct_power": None,
        "export_limit": None,

        # parsing
        "pending_register": None,
        "pending_since": None,
        "parsed_payloads": set(),

        # logs
        "response_log": [],
        "parse_debug": [],

        # validation
        "expected_export_value": None,

        # write
        "write_mode": None,          # "enable" | "disable"
        "write_password": "",
        "write_unlocked": False,
        "write_value": None,
        "lock_sent_at": None,
        "response_cursor": 0
    }

    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


init_state()
# =====================================================
# MQTT SETUP
# =====================================================
def mqtt_connect(device_id):
    if st.session_state.mqtt_client:
        return

    cmd = f"/AC/5/{device_id}/Command"
    rsp = f"/AC/5/{device_id}/Response"

    rx_queue = st.session_state.rx_queue  # 👈 capture once

    client = mqtt.Client()

    def on_connect(client, userdata, flags, rc):
        if rc == 0:
            client.subscribe(rsp)
            rx_queue.put(("CONNECTED", None))  # ✅ SAFE

    def on_message(client, userdata, msg):
        rx_queue.put(("MSG", msg.payload.decode(errors="ignore")))  # ✅ SAFE

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(MQTT_BROKER, MQTT_PORT, 60)
    client.loop_start()

    st.session_state.mqtt_client = client
    st.session_state.command_topic = cmd
    st.session_state.state = "CONNECTING"

def publish(cmd):
    # 🔍 DEBUG: log every outgoing command
    ts = time.time()
    st.session_state.parse_debug.append(f"📤 [{ts:.3f}] SENT → {cmd}")

    st.session_state.mqtt_client.publish(
        st.session_state.command_topic,
        cmd,
        qos=1
    )


# =====================================================
# RX QUEUE DRAIN
# =====================================================
def drain_rx_queue():
    while not st.session_state.rx_queue.empty():
        event, payload = st.session_state.rx_queue.get()

        if event == "CONNECTED":
            st.session_state.state = "CONNECTED"

        elif event == "MSG":
            st.session_state.response_log.append((time.time(), payload))
            st.session_state.response_log = st.session_state.response_log[-MAX_LOG_LINES:]

# =====================================================
# RESPONSE PARSING
# =====================================================
def extract_register(payload, register):
    dbg = st.session_state.parse_debug

    dbg.append("---- PAYLOAD ----")
    dbg.append(payload)

    try:
        rsp = json.loads(payload).get("rsp", "")
        dbg.append("JSON OK")
    except Exception:
        m = re.search(r'"rsp"\s*:\s*"([\s\S]*)"\s*}', payload)
        if not m:
            dbg.append("No rsp")
            return None
        rsp = m.group(1)

    if "READ PROCESSING" in rsp:
        dbg.append("Processing → ignore")
        return None

    for line in rsp.splitlines():
        if line.startswith(f"{register}:"):
            val = int(line.split(":")[1])
            dbg.append(f"FOUND {register}={val}")
            return val

    return None

def is_up_processed(payload: str) -> bool:
    try:
        rsp = json.loads(payload).get("rsp", "")
    except Exception:
        return False
    return "UP PROCESSED" in rsp
# =====================================================
# EVENT-DRIVEN PARSER
# =====================================================
def run_state_machine():
    if (
        not st.session_state.pending_register
        and st.session_state.state not in (
            "WAIT_UP_PROCESSED",
            "VERIFY_EXPORT_DELAY",
        )
    ):
        return

    # ⏱ timeout guard
    if time.time() - st.session_state.pending_since > TIMEOUT:
        if st.session_state.state == "WAIT_UP_PROCESSED":
            st.session_state.parse_debug.append("⏱ TIMEOUT waiting for UP PROCESSED")
        else:
            st.session_state.parse_debug.append("⏱ TIMEOUT waiting for register")
    
        st.session_state.pending_register = None
        st.session_state.state = "CONNECTED"
        st.session_state.parsed_payloads.clear()
        return

    if st.session_state.state == "VERIFY_EXPORT_DELAY":
        if time.time() < st.session_state.verify_at:
            return
    
        publish("READ03**12345##1234567890,0802")
        st.session_state.state = "VERIFY_EXPORT_ONCE"
        st.session_state.pending_register = "0802"
        st.session_state.pending_since = time.time()
        st.session_state.parsed_payloads.clear()
        return

    for ts, payload in st.session_state.response_log[st.session_state.response_cursor:]:
        st.session_state.response_cursor += 1  # ✅ advance once per payload
    
        if payload in st.session_state.parsed_payloads:
            continue
    
        st.session_state.parsed_payloads.add(payload)

        # =====================================================
        # WAIT FOR *FINAL* UP PROCESSED (after LOCK)
        # =====================================================
        if st.session_state.state == "WAIT_UP_PROCESSED":

            if ts < st.session_state.lock_sent_at:
                continue
        
            if is_up_processed(payload):
                st.session_state.parse_debug.append(
                    "🔐 Final UP PROCESSED received → settling before verify"
                )
            
                st.session_state.verify_at = time.time() + 0.8
                st.session_state.state = "VERIFY_EXPORT_DELAY"
                st.session_state.parsed_payloads.clear()
                break        
            continue
    
        # =====================================================
        # REGISTER-BASED STATES
        # =====================================================
        value = extract_register(payload, st.session_state.pending_register)
        if value is None:
            continue

        # =====================================================
        # UPDATE FLOW
        # =====================================================
        if st.session_state.state == "UPDATE_CT":
            st.session_state.ct_power = value

            publish("READ03**12345##1234567890,0802")
            st.session_state.pending_register = "0802"
            st.session_state.pending_since = time.time()
            st.session_state.state = "UPDATE_EXPORT"
            st.session_state.parsed_payloads.clear()
            break

        elif st.session_state.state == "UPDATE_EXPORT":
            st.session_state.export_limit = value

            st.session_state.pending_register = None
            st.session_state.state = "CONNECTED"
            st.session_state.parsed_payloads.clear()
            break

        # =====================================================
        # VERIFY EXPORT (SINGLE READ ONLY)
        # =====================================================
        # elif st.session_state.state == "VERIFY_EXPORT_ONCE":
        
        #     if value == st.session_state.write_value:
        #         st.session_state.export_limit = value
        #         st.success("Export limit updated successfully")
        
        #     else:
        #         st.error(
        #             f"Export verification failed. "
        #             f"Expected {st.session_state.write_value}, got {value}"
        #         )
        
        #     # ✅ End verification — no retries
        #     st.session_state.state = "CONNECTED"
        #     st.session_state.pending_register = None
        #     st.session_state.write_unlocked = False
        #     st.session_state.write_value = None
        #     st.session_state.parsed_payloads.clear()
        #     break
        elif st.session_state.state == "VERIFY_EXPORT_ONCE":

            if value == st.session_state.write_value:
                st.session_state.export_limit = value
        
                st.success(
                    f"✅ Export limit successfully set to {value} W"
                )
        
                st.session_state.parse_debug.append(
                    f"✔ Verification success: 0802={value}"
                )
        
            else:
                st.error(
                    f"❌ Export verification failed. "
                    f"Expected {st.session_state.write_value}, got {value}"
                )
        
                st.session_state.parse_debug.append(
                    f"✖ Verification failed: expected {st.session_state.write_value}, got {value}"
                )
        
            # ✅ End verification — no retries
            st.session_state.state = "CONNECTED"
            st.session_state.pending_register = None
            st.session_state.write_unlocked = False
            st.session_state.write_value = None
            st.session_state.parsed_payloads.clear()
            break

            
if st.session_state.mqtt_client:
    st_autorefresh(interval=AUTO_REFRESH_MS, key="mqtt_refresh")
    drain_rx_queue()
    run_state_machine()

# =====================================================
# UI
# =====================================================
st.title("🔌 Solax Inverter – Zero Export Control")

device = st.selectbox("Select Device", DEVICE_TOPICS)

if st.button("Connect", disabled=st.session_state.state != "IDLE"):
    mqtt_connect(device)

if st.session_state.state in ("CONNECTING", "IDLE"):
    st.warning("Connecting…")
else:
    st.success("Connected")

# =====================================================
# DEBUG
# =====================================================
with st.expander("📡 Raw MQTT Responses"):
    st.text_area(
        "Responses",
        value="\n\n---\n\n".join(p for _, p in st.session_state.response_log),
        height=300
    )

with st.expander("🧪 Parsing Debug Trace"):
    st.text_area(
        "Parser activity",
        value="\n".join(st.session_state.parse_debug),
        height=400
    )

# =====================================================
# INVERTER SETTINGS
# =====================================================
st.divider()
st.subheader("Inverter Settings")

if st.button("Update", disabled=st.session_state.state != "CONNECTED"):
    st.session_state.parse_debug.clear()
    st.session_state.parsed_payloads.clear()
    st.session_state.response_cursor = len(st.session_state.response_log)

    publish("READ04**12345##1234567890,1032")
    st.session_state.pending_register = "1032"
    st.session_state.pending_since = time.time()
    st.session_state.state = "UPDATE_CT"

ct_enabled = "Yes" if st.session_state.ct_power not in (None, 0) else "No"

st.text_input("CT Enabled", ct_enabled, disabled=True)
st.text_input(
    "Export Limit (W)",
    str(st.session_state.export_limit) if st.session_state.export_limit is not None else "",
    disabled=True
)

# =====================================================
# ZERO EXPORT CONTROL
# =====================================================
st.divider()
st.subheader("Zero Export Control")

can_control = (
    st.session_state.ct_power not in (None, 0)
    and st.session_state.export_limit is not None
)

col1, col2 = st.columns(2)

with col1:
    enable_clicked = st.button("Enable Zero Export", disabled=not can_control)
    st.info("For changing the export limit to 1 W")
with col2:
    disable_clicked = st.button("Disable Zero Export", disabled=not can_control)
    st.info("For changing the export limit to 10000 W")

def start_write_flow(mode):
    st.session_state.write_mode = mode
    st.session_state.write_unlocked = False

    # 🔥 HARD RESET of read pipeline
    st.session_state.pending_register = None
    st.session_state.pending_since = None
    st.session_state.parsed_payloads.clear()
    st.session_state.response_cursor = len(st.session_state.response_log)

    st.session_state.state = "WRITE_PASSWORD"

if enable_clicked:
    start_write_flow("enable")

if disable_clicked:
    start_write_flow("disable")
# -------------------Enter Password-----------------------------
if st.session_state.state == "WRITE_PASSWORD":
    st.subheader("🔐 Enter Inverter Password")

    pwd = st.text_input("Password", type="password")

    if st.button("Unlock"):
        padded = pwd.zfill(5)
        st.session_state.write_password = padded

        publish(f"UP#,1536:{padded}")

        if padded == "02014":
            st.session_state.write_unlocked = True
            st.session_state.state = "WRITE_VALUE"
            st.success("Unlocked successfully")
        else:
            st.error("Invalid password")
# --------------- Enter Export Limit Value ------------------------
if st.session_state.state == "WRITE_VALUE" and st.session_state.write_unlocked:
    st.subheader("⚙️ Set Export Limit")

    # ENABLE → fixed to 1 W
    if st.session_state.write_mode == "enable":
        value = 1
        st.session_state.write_value = 1
        st.info("Zero export enabled → Export limit fixed to 1 W")

        if st.button("Set Export Limit"):
            publish("UP#,1540:00001")
            st.session_state.state = "WRITE_LOCK"

    # DISABLE → user chooses value
    else:
        value = st.number_input(
            "Export Limit (W)",
            min_value=1,
            max_value=61000,
            value=10000
        )

        if st.button("Set Export Limit"):
            padded_val = f"{value:05d}"
            st.session_state.write_value = value

            publish(f"UP#,1540:{padded_val}")
            st.session_state.state = "WRITE_LOCK"
# -------------- LOCK ---------------------------------------
if st.session_state.state == "WRITE_LOCK":
    st.subheader("🔒 Lock Settings")

    if st.button("Lock & Apply"):
        lock_ts = time.time()
        publish("UP#,1536:00001")

        st.session_state.lock_sent_at = lock_ts          
        st.session_state.state = "WAIT_UP_PROCESSED"
        st.session_state.pending_register = None
        st.session_state.pending_since = lock_ts

        st.session_state.parsed_payloads.clear()
        st.session_state.response_cursor = len(st.session_state.response_log)






