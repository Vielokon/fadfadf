from dataclasses import dataclass
from typing import Literal
from config import POWER_PROFILES, ENERGY_OVERHEAD, ENERGY_ENCRYPTION_OVERHEAD, ENERGY_RETRY_RATE, SERVER_NETWORK_W, SERVER_SHARE

NetworkKind = Literal["wifi", "lte", "5g", "ethernet"]

@dataclass
class EnergyInput:
    total_bytes: int
    duration_s: float | None
    rtt_ms: float | None = None
    network: NetworkKind | Literal["auto"] = "auto"

def _guess_network(throughput_mbps: float) -> NetworkKind:
    # очень грубая эвристика
    if throughput_mbps >= 150: return "5g"
    if throughput_mbps >= 40:  return "wifi"
    if throughput_mbps >= 3:   return "lte"
    return "lte"

def estimate_energy(inp: EnergyInput):
    bytes_payload = max(0, int(inp.total_bytes or 0))
    if not inp.duration_s or inp.duration_s <= 0:
        return {
            "has_duration": False,
            "note": "Нет данных о длительности, оцениваем только размер и накладные расходы.",
            "bytes_effective": bytes_payload * (1 + ENERGY_OVERHEAD + ENERGY_ENCRYPTION_OVERHEAD + ENERGY_RETRY_RATE),
        }

    bits_per_sec = (bytes_payload * 8) / inp.duration_s
    mbps = bits_per_sec / 1_000_000
    net = inp.network
    if net == "auto":
        net = _guess_network(mbps)
    prof = POWER_PROFILES[net]

    bytes_effective = bytes_payload * (1 + ENERGY_OVERHEAD + ENERGY_ENCRYPTION_OVERHEAD + ENERGY_RETRY_RATE)

    # duty_cycle: насколько активно радио нагружено относительно «ёмкости» канала
    duty = min(1.0, (mbps / prof["capacity_mbps"]) if prof["capacity_mbps"] > 0 else 1.0)

    # базовая модель устройства: (CPU + Radio*duty)*T  + «хвост» радиоканала (tail)
    cpu_j = prof["cpu_w"]   * inp.duration_s
    radio_j = prof["radio_w"] * duty * inp.duration_s
    tail_j = prof["radio_w"] * 0.5 * prof["tail_s"]  # половинная мощность на хвост
    handshake_j = 0.0
    if (inp.rtt_ms or 0) > 0:
        # скажем, 1/10 времени RTT в активном состоянии радиоканала
        handshake_j = prof["radio_w"] * 0.1 * (inp.rtt_ms/1000.0)

    device_j = cpu_j + radio_j + tail_j + handshake_j

    # «доля сервера/сети», пропорционально времени передачи
    server_j = SERVER_NETWORK_W * SERVER_SHARE * inp.duration_s

    total_j = device_j + server_j
    mb = bytes_effective / (1024*1024)
    bytes_per_j = bytes_effective / total_j if total_j > 0 else None
    mb_per_j = mb / total_j if total_j > 0 else None
    j_per_mb = total_j / mb if mb > 0 else None

    return {
        "has_duration": True,
        "network": net,
        "throughput_mbps": mbps,
        "bytes_effective": bytes_effective,
        "device_j": device_j,
        "server_j": server_j,
        "total_j": total_j,
        "bytes_per_j": bytes_per_j,
        "mb_per_j": mb_per_j,
        "j_per_mb": j_per_mb,
        "duty_cycle": duty,
    }