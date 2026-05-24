"""
GB/T 27930 charging communication protocol definitions and parsing.

GB/T 27930 uses CAN 2.0B extended frames (29-bit IDs) at 250 kbit/s,
based on SAE J1939 application layer.

CAN ID structure (29-bit): P(3) | R(1) | DP(1) | PF(8) | PS(8) | SA(8)
On the wire, extended frames have bit 31 set (CAN_EFF_FLAG = 0x80000000).
"""

# --- Node addresses (SA = Source Address) ---
ADDR_CHARGER = 0x56  # 充电机
ADDR_BMS = 0xF4      # BMS (车辆)

CAN_BAUDRATE = 250000  # standard GB/T 27930 baud rate

# CAN frame flags (from ZLG canframe.h)
CAN_EFF_FLAG = 0x80000000  # extended frame format flag
CAN_RTR_FLAG = 0x40000000  # remote transmission request
CAN_ERR_FLAG = 0x20000000  # error message frame

# --- PDU Format (PF) values for each message ---
# Charger → BMS messages
PF_CHM = 38  # Charger Handshake Message
PF_CRM = 1   # Charger Recognition Message
PF_CTS = 7   # Charger Time Sync
PF_CCS = 18  # Charger Charging Status
PF_CST = 26  # Charger Stop Charging

# BMS → Charger messages
PF_BHM = 39  # BMS Handshake Message
PF_BRM = 2   # BMS Recognition Message
PF_BCP = 6   # Battery Charging Parameters
PF_BCL = 16  # Battery Charging Demand
PF_BCS = 17  # Battery Charging Status
PF_BSM = 19  # Battery Status Message
PF_BST = 25  # BMS Stop Charging

# Emergency / broadcast
PF_CEM = 28  # Charger Emergency Message
PF_BEM = 29  # BMS Emergency Message

# --- Message name lookup ---
MESSAGE_NAMES = {
    PF_CHM: ("CHM", "Charger Handshake", "充电机握手"),
    PF_BHM: ("BHM", "BMS Handshake", "BMS握手"),
    PF_CRM: ("CRM", "Charger Recognition", "充电机辨识"),
    PF_BRM: ("BRM", "BMS Recognition", "BMS辨识"),
    PF_BCP: ("BCP", "Battery Charging Params", "电池充电参数"),
    PF_BCL: ("BCL", "Battery Charging Demand", "电池充电需求"),
    PF_BCS: ("BCS", "Battery Charging Status", "电池充电状态"),
    PF_CCS: ("CCS", "Charger Charging Status", "充电机充电状态"),
    PF_BSM: ("BSM", "Battery Status Message", "电池状态信息"),
    PF_CTS: ("CTS", "Charger Time Sync", "充电机时间同步"),
    PF_BST: ("BST", "BMS Stop Charging", "BMS停止充电"),
    PF_CST: ("CST", "Charger Stop Charging", "充电机停止充电"),
    PF_CEM: ("CEM", "Charger Emergency", "充电机急停"),
    PF_BEM: ("BEM", "BMS Emergency", "BMS急停"),
}


def build_can_id(pf: int, ps: int, sa: int, priority: int = 6) -> int:
    """Build an extended CAN ID from PGN components.

    Returns the raw CAN ID (without EFF flag). Use make_wire_id() to add it.
    """
    p = priority & 0x7
    r = 0
    dp = 0
    return (p << 26) | (r << 25) | (dp << 24) | (pf << 16) | (ps << 8) | sa


def make_wire_id(raw_id: int) -> int:
    """Add the extended frame flag for on-wire transmission."""
    return raw_id | CAN_EFF_FLAG


def strip_eff(wire_id: int) -> int:
    """Remove CAN_EFF_FLAG from a wire ID to get the raw 29-bit ID."""
    return wire_id & 0x1FFFFFFF


def is_extended(wire_id: int) -> bool:
    """Check if a wire CAN ID is an extended frame."""
    return bool(wire_id & CAN_EFF_FLAG)


def is_charger_msg(wire_id: int) -> bool:
    """Check if message is from the charger (SA = 0x56)."""
    return (wire_id & 0xFF) == ADDR_CHARGER


def is_bms_msg(wire_id: int) -> bool:
    """Check if message is from the BMS (SA = 0xF4)."""
    return (wire_id & 0xFF) == ADDR_BMS


def get_pf(wire_id: int) -> int:
    """Extract PDU Format (PF) from a wire CAN ID."""
    return (wire_id >> 16) & 0xFF


def get_ps(wire_id: int) -> int:
    """Extract PDU Specific (PS) from a wire CAN ID."""
    return (wire_id >> 8) & 0xFF


def get_sa(wire_id: int) -> int:
    """Extract Source Address (SA) from a wire CAN ID."""
    return wire_id & 0xFF


def get_direction(wire_id: int) -> str:
    """Get message direction: 'C→BMS' or 'BMS→C'."""
    sa = get_sa(wire_id)
    if sa == ADDR_CHARGER:
        return "C→BMS"
    elif sa == ADDR_BMS:
        return "BMS→C"
    return f"0x{sa:02X}→?"


def parse_message(wire_id: int, data: bytes) -> dict:
    """Parse a GB/T 27930 CAN message.

    Returns a dict with at minimum:
      {'name': short_name, 'desc': description, 'direction': dir, 'fields': {...}}
    """
    raw_id = strip_eff(wire_id)
    pf = get_pf(raw_id)
    direction = get_direction(raw_id)
    info = MESSAGE_NAMES.get(pf)
    name = info[0] if info else f"PF={pf}"
    desc = info[1] if info else "Unknown"
    cn_desc = info[2] if info else "未知"

    result = {
        "name": name,
        "desc": desc,
        "cn_desc": cn_desc,
        "direction": direction,
        "raw_id": f"0x{raw_id:08X}",
        "wire_id": f"0x{wire_id:08X}",
        "pf": pf,
        "len": len(data),
        "data_hex": data.hex(" ").upper(),
        "fields": {},
    }

    # Parse data fields based on message type
    if pf == PF_CHM:
        result["fields"] = _parse_chm(data)
    elif pf == PF_BHM:
        result["fields"] = _parse_bhm(data)
    elif pf == PF_CRM:
        result["fields"] = _parse_crm(data)
    elif pf == PF_BRM:
        result["fields"] = _parse_brm(data)
    elif pf == PF_BCP:
        result["fields"] = _parse_bcp(data)
    elif pf == PF_BCL:
        result["fields"] = _parse_bcl(data)
    elif pf == PF_BCS:
        result["fields"] = _parse_bcs(data)
    elif pf == PF_CCS:
        result["fields"] = _parse_ccs(data)
    elif pf == PF_BSM:
        result["fields"] = _parse_bsm(data)
    elif pf == PF_BST:
        result["fields"] = _parse_bst(data)
    elif pf == PF_CST:
        result["fields"] = _parse_cst(data)

    return result


def format_message(parsed: dict) -> str:
    """Format a parsed GB/T 27930 message for display."""
    fields_str = ""
    if parsed["fields"]:
        fields_str = " | " + " | ".join(
            f"{k}={v}" for k, v in parsed["fields"].items()
        )
    return (
        f"{parsed['name']:4s} | {parsed['direction']:5s} | {parsed['desc']:24s}"
        f" | ID={parsed['raw_id']} | data=[{parsed['data_hex']}]"
        f"{fields_str}"
    )


# --- Data field parsers for each message type ---

def _get_u16_le(data: bytes, offset: int) -> int:
    """16-bit little-endian unsigned int."""
    if offset + 1 >= len(data):
        return 0
    return data[offset] | (data[offset + 1] << 8)


def _get_u16_be(data: bytes, offset: int) -> int:
    """16-bit big-endian unsigned int."""
    if offset + 1 >= len(data):
        return 0
    return (data[offset] << 8) | data[offset + 1]


def _get_u8(data: bytes, offset: int) -> int:
    """8-bit unsigned int."""
    if offset >= len(data):
        return 0
    return data[offset]


def _parse_chm(data: bytes) -> dict:
    """Charger Handshake Message — protocol version."""
    if len(data) < 3:
        return {}
    return {
        "protocol_ver": f"{chr(data[0])}.{chr(data[1])}.{chr(data[2])}"  # e.g. "V1.2"
    }


def _parse_bhm(data: bytes) -> dict:
    """BMS Handshake Message — max voltage."""
    if len(data) < 2:
        return {}
    max_voltage = _get_u16_be(data, 0) * 0.1
    return {"max_voltage": f"{max_voltage:.1f}V"}


def _parse_crm(data: bytes) -> dict:
    """Charger Recognition Message — charger number."""
    if len(data) < 4:
        return {}
    charger_no = _get_u16_le(data, 0)
    return {"charger_no": charger_no, "has_battery_comp": bool(data[2] & 0x01)}


def _parse_brm(data: bytes) -> dict:
    """BMS Recognition Message — BMS version, battery type."""
    if len(data) < 8:
        return {}
    bms_ver = f"{data[0]}.{data[1]}.{data[2]}"
    bat_type = {1: "Lead-acid", 2: "NiMH", 3: "LiFePO4", 4: "Li(NCM/LCO/LMO)",
                 5: "Li-ion(others)", 6: "Supercapacitor"}.get(data[3], f"Type{data[3]}")
    rated_capacity = _get_u16_be(data, 4) * 0.1
    rated_voltage = _get_u16_be(data, 6) * 0.1
    return {"bms_ver": bms_ver, "bat_type": bat_type,
            "rated_capacity": f"{rated_capacity:.1f}Ah",
            "rated_voltage": f"{rated_voltage:.1f}V"}


def _parse_bcp(data: bytes) -> dict:
    """Battery Charging Parameters — max voltage, current, etc."""
    if len(data) < 10:
        return {}
    max_total_voltage = _get_u16_be(data, 0) * 0.1
    max_charge_current = _get_u16_be(data, 2) * 0.1 - 400
    max_discharge_current = _get_u16_be(data, 4) * 0.1 - 400
    nominal_energy = _get_u16_be(data, 6) * 0.1
    max_charge_power = _get_u16_be(data, 8) * 0.1
    return {
        "max_voltage": f"{max_total_voltage:.1f}V",
        "max_chg_current": f"{max_charge_current:.1f}A",
        "max_dchg_current": f"{max_discharge_current:.1f}A",
        "nominal_energy": f"{nominal_energy:.1f}kWh",
        "max_chg_power": f"{max_charge_power:.1f}kW",
    }


def _parse_bcl(data: bytes) -> dict:
    """Battery Charging Demand — target voltage/current, charging mode."""
    if len(data) < 5:
        return {}
    demand_voltage = _get_u16_be(data, 0) * 0.1
    demand_current = _get_u16_be(data, 2) * 0.1 - 400
    charge_mode = {1: "CC", 2: "CV", 3: "Idle"}.get(data[4] & 0x03, f"Mode{data[4]}")
    return {
        "demand_voltage": f"{demand_voltage:.1f}V",
        "demand_current": f"{demand_current:.1f}A",
        "charge_mode": charge_mode,
    }


def _parse_bcs(data: bytes) -> dict:
    """Battery Charging Status — current voltage/current, SOC."""
    if len(data) < 9:
        return {}
    measured_voltage = _get_u16_be(data, 0) * 0.1
    measured_current = _get_u16_be(data, 2) * 0.1 - 400
    soc = _get_u8(data, 6)
    remaining_time = _get_u16_be(data, 7)
    return {
        "voltage": f"{measured_voltage:.1f}V",
        "current": f"{measured_current:.1f}A",
        "SOC": f"{soc}%",
        "remaining": f"{remaining_time}min",
    }


def _parse_ccs(data: bytes) -> dict:
    """Charger Charging Status — output voltage/current, elapsed time."""
    if len(data) < 7:
        return {}
    output_voltage = _get_u16_be(data, 0) * 0.1
    output_current = _get_u16_be(data, 2) * 0.1 - 400
    elapsed = _get_u16_be(data, 4)
    allow_charge = (data[6] & 0x01) != 0
    return {
        "output_voltage": f"{output_voltage:.1f}V",
        "output_current": f"{output_current:.1f}A",
        "elapsed": f"{elapsed}min",
        "allow_charge": allow_charge,
    }


def _parse_bsm(data: bytes) -> dict:
    """Battery Status Message — cell voltages, temperatures."""
    if len(data) < 7:
        return {}
    cell_count = data[0]
    if cell_count > 0 and len(data) >= 2 + cell_count * 2:
        cell_voltages = []
        for i in range(min(cell_count, (len(data) - 2) // 2)):
            cell_voltages.append(_get_u16_be(data, 1 + i * 2) * 0.001)
    else:
        cell_voltages = []

    fields = {"cells": cell_count}
    if cell_voltages:
        fields["v_min"] = f"{min(cell_voltages):.3f}V"
        fields["v_max"] = f"{max(cell_voltages):.3f}V"
        fields["v_avg"] = f"{sum(cell_voltages) / len(cell_voltages):.3f}V"

    temp_offset = 1 + cell_count * 2
    if len(data) > temp_offset + 1:
        temp_count = data[temp_offset]
        if temp_count > 0 and len(data) > temp_offset + 1 + temp_count:
            temps = [data[temp_offset + 1 + i] - 50 for i in range(temp_count)]
            fields["temp_min"] = f"{min(temps)}°C"
            fields["temp_max"] = f"{max(temps)}°C"
    return fields


def _parse_bst(data: bytes) -> dict:
    """BMS Stop Charging — stop reason."""
    reasons = {
        1: "SOC达到目标", 2: "单体电压过高", 3: "温度异常",
        4: "BMS故障", 5: "绝缘故障", 6: "连接器故障",
    }
    reason = data[0] if len(data) > 0 else 0
    return {"stop_reason": reasons.get(reason, f"Reason({reason})")}


def _parse_cst(data: bytes) -> dict:
    """Charger Stop Charging — stop reason."""
    reasons = {
        1: "正常达到目标", 2: "人工中止", 3: "急停",
        4: "充电机故障", 5: "通讯超时", 6: "绝缘故障",
    }
    reason = data[0] if len(data) > 0 else 0
    return {"stop_reason": reasons.get(reason, f"Reason({reason})")}


# --- CAN ID lookup for verification ---
# Pre-compute the raw (non-wire) CAN IDs for all known messages
CHARGER_IDS = {
    build_can_id(PF_CHM, ADDR_BMS, ADDR_CHARGER): "CHM",
    build_can_id(PF_CRM, ADDR_BMS, ADDR_CHARGER): "CRM",
    build_can_id(PF_CTS, ADDR_BMS, ADDR_CHARGER): "CTS",
    build_can_id(PF_CCS, ADDR_BMS, ADDR_CHARGER): "CCS",
    build_can_id(PF_CST, ADDR_BMS, ADDR_CHARGER): "CST",
    build_can_id(PF_CEM, ADDR_BMS, ADDR_CHARGER): "CEM",
}

BMS_IDS = {
    build_can_id(PF_BHM, ADDR_CHARGER, ADDR_BMS): "BHM",
    build_can_id(PF_BRM, ADDR_CHARGER, ADDR_BMS): "BRM",
    build_can_id(PF_BCP, ADDR_CHARGER, ADDR_BMS): "BCP",
    build_can_id(PF_BCL, ADDR_CHARGER, ADDR_BMS): "BCL",
    build_can_id(PF_BCS, ADDR_CHARGER, ADDR_BMS): "BCS",
    build_can_id(PF_BSM, ADDR_CHARGER, ADDR_BMS): "BSM",
    build_can_id(PF_BST, ADDR_CHARGER, ADDR_BMS): "BST",
    build_can_id(PF_BEM, ADDR_CHARGER, ADDR_BMS): "BEM",
}


def identify_can_id(raw_id: int) -> str | None:
    """Return the GB/T 27930 message name for a raw CAN ID, or None."""
    if raw_id in CHARGER_IDS:
        return CHARGER_IDS[raw_id]
    if raw_id in BMS_IDS:
        return BMS_IDS[raw_id]
    return None
