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
import sys
import time
from pathlib import Path
from subprocess import run


# ── constants ─────────────────────────────────────────────────────────────────

KEEPALIVED_PID_FILE = Path(
    os.environ.get("KEEPALIVED_PID_FILE", "/run/keepalived.pid")
)
KEEPALIVED_JSON_FILE = Path(
    os.environ.get("KEEPALIVED_JSON_FILE", "/tmp/keepalived.json")
)

# Seconds to wait for keepalived to write the JSON dump after signalling it.
JSON_DUMP_TIMEOUT = 5.0

METRIC_NAMESPACE = "keepalived_vrrp_"


# ── helpers ───────────────────────────────────────────────────────────────────

def _die(message):
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def _warn_exit(message):
    print(f"WARNING: {message}", file=sys.stderr)
    sys.exit(0)


def get_pid():
    """Return the keepalived process PID."""
    try:
        return int(KEEPALIVED_PID_FILE.read_text().strip())
    except FileNotFoundError:
        _warn_exit(f"keepalived not running ({KEEPALIVED_PID_FILE} not found)")
    except ValueError as exc:
        _die(f"Invalid content in {KEEPALIVED_PID_FILE}: {exc}")


def get_json_signum():
    """Return the signal number keepalived uses for JSON dumps."""
    result = run(
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
    try:
        mtime_before = KEEPALIVED_JSON_FILE.stat().st_mtime
    except FileNotFoundError:
        mtime_before = 0.0

    try:
        os.kill(pid, signum)
    except ProcessLookupError:
        _warn_exit(f"keepalived not running (PID {pid} not found)")
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

def emit(name, help_text, samples):
    """Print HELP, TYPE, and sample lines for one metric family.

    The metric type is inferred from the name: names ending in ``_total``
    are ``counter``; everything else is ``gauge``.  This follows the
    Prometheus naming convention and makes it impossible for the type to
    diverge from the name.

    Does nothing when samples is empty (avoids bare HELP/TYPE headers).
    """
    if not samples:
        return
    full_name = METRIC_NAMESPACE + name
    metric_type = "counter" if name.endswith("_total") else "gauge"
    print(f"# HELP {full_name} {help_text}")
    print(f"# TYPE {full_name} {metric_type}")
    for lbl, value in samples:
        lbl_str = ",".join(f'{k}="{v}"' for k, v in lbl.items())
        print(f"{full_name}{{{lbl_str}}} {value}")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    pid = get_pid()
    signum = get_json_signum()
    data = request_json_dump(pid, signum)

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

    for instance in get_instances(data):
        d = instance.get("data", {})
        s = instance.get("stats")

        name = d.get("iname", "unknown")
        last_transition = d.get("last_transition", 0.0)

        base_lbl = {"name": name}
        info_lbl = {
            "name": name,
            # Prefer the real interface name; fall back to the VMAC interface.
            "intf": d.get("ifp_ifname") or d.get("vmac_ifname", ""),
            "vrid": str(d.get("vrid", "")),
            "version": str(d.get("version", 0)),
            "nopreempt": str(int(bool(d.get("nopreempt", False)))),
        }

        def record(acc, src, key):
            acc.append((base_lbl, int(src.get(key, 0))))

        info_s.append((info_lbl, 1))
        if last_transition:
            last_trans_s.append((base_lbl, last_transition))
        advert_int_s.append((base_lbl, d.get("adver_int", 0.0)))

        record(state_s, d, "state")
        record(prio_base_s, d, "base_priority")
        record(prio_eff_s, d, "effective_priority")

        if s is not None:
            record(advert_rcvd_s, s, "advert_rcvd")
            record(advert_sent_s, s, "advert_sent")
            record(became_master_s, s, "become_master")
            record(released_master_s, s, "release_master")
            record(pkt_len_err_s, s, "packet_len_err")
            record(advert_int_err_s, s, "advert_interval_err")
            record(ip_ttl_err_s, s, "ip_ttl_err")
            record(invalid_type_s, s, "invalid_type_rcvd")
            record(addr_list_err_s, s, "addr_list_err")
            record(invalid_auth_s, s, "invalid_authtype")
            record(pri_zero_rcvd_s, s, "pri_zero_rcvd")
            record(pri_zero_sent_s, s, "pri_zero_sent")

    emit(
        "state",
        "Current keepalived VRRP state (0=INIT, 1=BACKUP, 2=MASTER, 3=FAULT).",
        state_s,
    )
    emit(
        "info",
        "keepalived VRRP instance metadata. Always 1.",
        info_s,
    )
    emit(
        "priority_base",
        "Configured keepalived VRRP base priority.",
        prio_base_s,
    )
    emit(
        "priority_effective",
        "Current effective keepalived VRRP priority "
        "(may be lower than base when tracking scripts reduce it).",
        prio_eff_s,
    )
    emit(
        "last_transition_timestamp_seconds",
        "Unix timestamp of the last keepalived VRRP state transition.",
        last_trans_s,
    )
    emit(
        "advert_interval_seconds",
        "keepalived VRRP advertisement interval in seconds.",
        advert_int_s,
    )
    emit(
        "advertisements_received_total",
        "Total keepalived VRRP advertisement packets received.",
        advert_rcvd_s,
    )
    emit(
        "advertisements_sent_total",
        "Total keepalived VRRP advertisement packets sent.",
        advert_sent_s,
    )
    emit(
        "became_master_total",
        "Total number of times this keepalived VRRP instance became MASTER.",
        became_master_s,
    )
    emit(
        "released_master_total",
        "Total number of times this keepalived VRRP instance released the MASTER role.",
        released_master_s,
    )
    emit(
        "packet_len_errors_total",
        "Total keepalived VRRP packets received with an invalid length.",
        pkt_len_err_s,
    )
    emit(
        "advert_interval_errors_total",
        "Total keepalived VRRP packets received with a mismatched advertisement interval.",
        advert_int_err_s,
    )
    emit(
        "ip_ttl_errors_total",
        "Total keepalived VRRP packets received with an incorrect IP TTL.",
        ip_ttl_err_s,
    )
    emit(
        "invalid_type_received_total",
        "Total keepalived VRRP packets received with an invalid type field.",
        invalid_type_s,
    )
    emit(
        "addr_list_errors_total",
        "Total keepalived VRRP packets received with a mismatched address list.",
        addr_list_err_s,
    )
    emit(
        "invalid_authtype_total",
        "Total keepalived VRRP packets received with an invalid authentication type.",
        invalid_auth_s,
    )
    emit(
        "priority_zero_received_total",
        "Total keepalived VRRP packets received with priority zero "
        "(used to signal MASTER resignation).",
        pri_zero_rcvd_s,
    )
    emit(
        "priority_zero_sent_total",
        "Total keepalived VRRP packets sent with priority zero "
        "(used to signal MASTER resignation).",
        pri_zero_sent_s,
    )


if __name__ == "__main__":
    main()
