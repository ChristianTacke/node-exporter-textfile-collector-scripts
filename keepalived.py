#!/usr/bin/env python3
"""Collect keepalived VRRP metrics for the node_exporter textfile collector.

Signals keepalived to produce a JSON state dump, then parses the dump file and
writes metrics to stdout in the Prometheus text exposition format.

Requirements:
  - keepalived >= 2.0 compiled with JSON support (./configure --enable-json)
  - Sufficient privileges to send signals to the keepalived process (typically root)

Dependencies: none beyond the Python 3 standard library

Usage (cron, every minute):
  * * * * * root /usr/local/bin/keepalived.py | \\
      sponge /var/lib/node_exporter/textfile_collector/keepalived.prom

Environment variables:
  KEEPALIVED_PID_FILE   Path to the keepalived PID file
                        (default: /run/keepalived.pid)
  KEEPALIVED_JSON_FILE  Path to the keepalived JSON dump file
                        (default: /tmp/keepalived.json; matches keepalived's
                        compiled-in default unless json_dump_file is set in
                        keepalived.conf)

Metrics produced (per VRRP instance):
  keepalived_vrrp_state                              gauge   0=INIT 1=BACKUP 2=MASTER 3=FAULT
  keepalived_vrrp_info                               gauge   always 1; labels carry instance info
  keepalived_vrrp_priority_base                      gauge   configured base priority
  keepalived_vrrp_priority_effective                 gauge   effective priority (may be reduced by
                                                             tracking scripts)
  keepalived_vrrp_last_transition_timestamp_seconds  gauge   Unix timestamp of last state transition
  keepalived_vrrp_advert_interval_seconds            gauge   VRRP advertisement interval
  keepalived_vrrp_advertisements_received_total      counter
  keepalived_vrrp_advertisements_sent_total          counter
  keepalived_vrrp_became_master_total                counter
  keepalived_vrrp_released_master_total              counter
  keepalived_vrrp_packet_len_errors_total            counter
  keepalived_vrrp_advert_interval_errors_total       counter
  keepalived_vrrp_ip_ttl_errors_total                counter
  keepalived_vrrp_invalid_type_received_total        counter
  keepalived_vrrp_addr_list_errors_total             counter
  keepalived_vrrp_invalid_authtype_total             counter
  keepalived_vrrp_priority_zero_received_total       counter
  keepalived_vrrp_priority_zero_sent_total           counter

Note: statistics metrics (counters) are only emitted when keepalived provides
them in the JSON dump (requires the stats subsystem to be active).

See keepalived-example.conf for configuration examples.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path


# ── constants ─────────────────────────────────────────────────────────────────

KEEPALIVED_PID_FILE = Path(
    os.environ.get("KEEPALIVED_PID_FILE", "/run/keepalived.pid")
)
KEEPALIVED_JSON_FILE = Path(
    os.environ.get("KEEPALIVED_JSON_FILE", "/tmp/keepalived.json")
)

# Keepalived internal VRRP state enum (vrrp.h)
VRRP_STATES = {0: "INIT", 1: "BACKUP", 2: "MASTER", 3: "FAULT"}

# Seconds to wait for keepalived to write the JSON dump after signalling it.
JSON_DUMP_TIMEOUT = 5.0

METRIC_PREFIX = "keepalived_vrrp_"


# ── helpers ───────────────────────────────────────────────────────────────────

def _die(message):
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def get_pid():
    """Return the keepalived process PID."""
    try:
        return int(KEEPALIVED_PID_FILE.read_text().strip())
    except FileNotFoundError:
        print(f"WARNING: keepalived not running ({KEEPALIVED_PID_FILE} not found)",
              file=sys.stderr)
        sys.exit(0)
    except ValueError as exc:
        _die(f"Invalid content in {KEEPALIVED_PID_FILE}: {exc}")


def get_json_signum():
    """Return the signal number keepalived uses for JSON dumps."""
    result = subprocess.run(
        ["keepalived", "--signum=JSON"],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(result.stdout.strip())


def request_json_dump(pid, signum):
    """Signal keepalived to write a fresh JSON dump and return the parsed data.

    Waits up to JSON_DUMP_TIMEOUT seconds for the dump file to be updated.
    """
    mtime_before = (
        KEEPALIVED_JSON_FILE.stat().st_mtime
        if KEEPALIVED_JSON_FILE.exists()
        else 0.0
    )

    try:
        os.kill(pid, signum)
    except ProcessLookupError:
        print(f"WARNING: keepalived not running (PID {pid} not found)", file=sys.stderr)
        sys.exit(0)
    except PermissionError:
        _die(f"No permission to signal keepalived process (PID {pid})")

    deadline = time.monotonic() + JSON_DUMP_TIMEOUT
    while time.monotonic() < deadline:
        try:
            mtime_now = KEEPALIVED_JSON_FILE.stat().st_mtime
            if mtime_now > mtime_before:
                return json.loads(KEEPALIVED_JSON_FILE.read_text())
        except FileNotFoundError:
            pass
        except json.JSONDecodeError:
            pass  # File may still be partially written; retry.
        time.sleep(0.1)

    _die(
        f"Timed out waiting {JSON_DUMP_TIMEOUT}s for {KEEPALIVED_JSON_FILE}. "
        "Ensure keepalived was compiled with --enable-json."
    )


def get_instances(data):
    """Return the list of VRRP instance objects from the parsed JSON.

    Supports both keepalived JSON_VERSION_V1 (array at root) and
    JSON_VERSION_V2 (object with a \"vrrp\" key).
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "vrrp" in data:
        return data["vrrp"]
    _die("Unexpected structure in keepalived JSON dump")


# ── output ────────────────────────────────────────────────────────────────────

def emit(name, help_text, metric_type, samples):
    """Print HELP, TYPE, and sample lines for one metric family."""
    print(f"# HELP {name} {help_text}")
    print(f"# TYPE {name} {metric_type}")
    for lbl, value in samples:
        if lbl:
            lbl_str = ",".join(f'{k}="{v}"' for k, v in lbl.items())
            print(f"{name}{{{lbl_str}}} {value}")
        else:
            print(f"{name} {value}")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    pid = get_pid()
    signum = get_json_signum()
    data = request_json_dump(pid, signum)
    instances = get_instances(data)

    # Accumulate samples per metric family so we can emit each family's HELP
    # and TYPE header exactly once before its samples.
    state_s = []
    info_s = []
    prio_base_s = []
    prio_eff_s = []
    last_trans_s = []
    advert_int_s = []
    advert_rcvd_s = []
    advert_sent_s = []
    became_master_s = []
    released_master_s = []
    pkt_len_err_s = []
    advert_int_err_s = []
    ip_ttl_err_s = []
    invalid_type_s = []
    addr_list_err_s = []
    invalid_auth_s = []
    pri_zero_rcvd_s = []
    pri_zero_sent_s = []

    for instance in instances:
        d = instance.get("data", {})
        s = instance.get("stats")

        name = d.get("iname", "unknown")
        state = int(d.get("state", 0))
        vrid = d.get("vrid", "")
        # Prefer the real interface name; fall back to the VMAC interface.
        intf = d.get("ifp_ifname") or d.get("vmac_ifname", "")
        base_priority = d.get("base_priority", 0)
        effective_priority = d.get("effective_priority", 0)
        last_transition = d.get("last_transition", 0.0)
        adver_int = d.get("adver_int", 0.0)
        version = d.get("version", 0)
        nopreempt = int(bool(d.get("nopreempt", False)))

        base_lbl = {"name": name}
        info_lbl = {
            "name": name,
            "intf": intf,
            "vrid": str(vrid),
            "version": str(version),
            "nopreempt": str(nopreempt),
        }

        state_s.append((base_lbl, state))
        info_s.append((info_lbl, 1))
        prio_base_s.append((base_lbl, base_priority))
        prio_eff_s.append((base_lbl, effective_priority))
        if last_transition:
            last_trans_s.append((base_lbl, last_transition))
        advert_int_s.append((base_lbl, adver_int))

        if s is not None:
            advert_rcvd_s.append((base_lbl, s.get("advert_rcvd", 0)))
            advert_sent_s.append((base_lbl, s.get("advert_sent", 0)))
            became_master_s.append((base_lbl, s.get("become_master", 0)))
            released_master_s.append((base_lbl, s.get("release_master", 0)))
            pkt_len_err_s.append((base_lbl, s.get("packet_len_err", 0)))
            advert_int_err_s.append((base_lbl, s.get("advert_interval_err", 0)))
            ip_ttl_err_s.append((base_lbl, s.get("ip_ttl_err", 0)))
            invalid_type_s.append((base_lbl, s.get("invalid_type_rcvd", 0)))
            addr_list_err_s.append((base_lbl, s.get("addr_list_err", 0)))
            invalid_auth_s.append((base_lbl, s.get("invalid_authtype", 0)))
            pri_zero_rcvd_s.append((base_lbl, s.get("pri_zero_rcvd", 0)))
            pri_zero_sent_s.append((base_lbl, s.get("pri_zero_sent", 0)))

    emit(
        METRIC_PREFIX + "state",
        "Current keepalived VRRP state (0=INIT, 1=BACKUP, 2=MASTER, 3=FAULT).",
        "gauge",
        state_s,
    )
    emit(
        METRIC_PREFIX + "info",
        "keepalived VRRP instance metadata. Always 1.",
        "gauge",
        info_s,
    )
    emit(
        METRIC_PREFIX + "priority_base",
        "Configured keepalived VRRP base priority.",
        "gauge",
        prio_base_s,
    )
    emit(
        METRIC_PREFIX + "priority_effective",
        "Current effective keepalived VRRP priority "
        "(may be lower than base when tracking scripts reduce it).",
        "gauge",
        prio_eff_s,
    )
    if last_trans_s:
        emit(
            METRIC_PREFIX + "last_transition_timestamp_seconds",
            "Unix timestamp of the last keepalived VRRP state transition.",
            "gauge",
            last_trans_s,
        )
    emit(
        METRIC_PREFIX + "advert_interval_seconds",
        "keepalived VRRP advertisement interval in seconds.",
        "gauge",
        advert_int_s,
    )

    if advert_rcvd_s:
        emit(
            METRIC_PREFIX + "advertisements_received_total",
            "Total keepalived VRRP advertisement packets received.",
            "counter",
            advert_rcvd_s,
        )
        emit(
            METRIC_PREFIX + "advertisements_sent_total",
            "Total keepalived VRRP advertisement packets sent.",
            "counter",
            advert_sent_s,
        )
        emit(
            METRIC_PREFIX + "became_master_total",
            "Total number of times this keepalived VRRP instance became MASTER.",
            "counter",
            became_master_s,
        )
        emit(
            METRIC_PREFIX + "released_master_total",
            "Total number of times this keepalived VRRP instance released the MASTER role.",
            "counter",
            released_master_s,
        )
        emit(
            METRIC_PREFIX + "packet_len_errors_total",
            "Total keepalived VRRP packets received with an invalid length.",
            "counter",
            pkt_len_err_s,
        )
        emit(
            METRIC_PREFIX + "advert_interval_errors_total",
            "Total keepalived VRRP packets received with a mismatched advertisement interval.",
            "counter",
            advert_int_err_s,
        )
        emit(
            METRIC_PREFIX + "ip_ttl_errors_total",
            "Total keepalived VRRP packets received with an incorrect IP TTL.",
            "counter",
            ip_ttl_err_s,
        )
        emit(
            METRIC_PREFIX + "invalid_type_received_total",
            "Total keepalived VRRP packets received with an invalid type field.",
            "counter",
            invalid_type_s,
        )
        emit(
            METRIC_PREFIX + "addr_list_errors_total",
            "Total keepalived VRRP packets received with a mismatched address list.",
            "counter",
            addr_list_err_s,
        )
        emit(
            METRIC_PREFIX + "invalid_authtype_total",
            "Total keepalived VRRP packets received with an invalid authentication type.",
            "counter",
            invalid_auth_s,
        )
        emit(
            METRIC_PREFIX + "priority_zero_received_total",
            "Total keepalived VRRP packets received with priority zero "
            "(used to signal MASTER resignation).",
            "counter",
            pri_zero_rcvd_s,
        )
        emit(
            METRIC_PREFIX + "priority_zero_sent_total",
            "Total keepalived VRRP packets sent with priority zero "
            "(used to signal MASTER resignation).",
            "counter",
            pri_zero_sent_s,
        )


if __name__ == "__main__":
    main()
