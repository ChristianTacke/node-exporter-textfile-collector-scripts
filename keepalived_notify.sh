#!/usr/bin/env bash
#
# Description: Write node_exporter textfile metrics on keepalived VRRP state
# changes. Intended to be used as a keepalived notify script.
#
# keepalived calls this script with the following positional arguments:
#   $1  type      "INSTANCE" or "GROUP"
#   $2  name      name of the VRRP instance or sync group
#   $3  state     new state: "MASTER", "BACKUP", or "FAULT"
#   $4  priority  current VRRP priority
#
# The script writes one .prom file per VRRP instance/group to
# TEXTFILE_COLLECTOR_DIR (default: /var/lib/node_exporter/textfile_collector).
# The file is written atomically using a temporary file and mv(1).
#
# keepalived.conf example — generic notify (recommended):
#
#   vrrp_instance VI_1 {
#       ...
#       notify /usr/local/bin/keepalived_notify.sh
#   }
#
# See keepalived-example.conf for a complete configuration example.
#
# Metrics produced:
#   keepalived_vrrp_state                           gauge  0=INIT 1=BACKUP 2=MASTER 3=FAULT
#   keepalived_vrrp_priority                        gauge
#   keepalived_vrrp_state_transition_timestamp_seconds  gauge  Unix epoch of last transition
#

set -eu

# ── arguments ────────────────────────────────────────────────────────────────

if [ "$#" -ne 4 ]; then
    echo "Usage: $0 TYPE NAME STATE PRIORITY" >&2
    exit 1
fi

TYPE="$1"
NAME="$2"
STATE="$3"
PRIORITY="$4"

# ── configuration ─────────────────────────────────────────────────────────────

TEXTFILE_COLLECTOR_DIR="${TEXTFILE_COLLECTOR_DIR:-/var/lib/node_exporter/textfile_collector}"

# ── map state string → integer ────────────────────────────────────────────────
# Matches keepalived's internal vrrp_state enum:
#   VRRP_STATE_INIT  = 0
#   VRRP_STATE_BACK  = 1  (BACKUP)
#   VRRP_STATE_MAST  = 2  (MASTER)
#   VRRP_STATE_FAULT = 3

case "${STATE}" in
    MASTER) STATE_INT=2 ;;
    BACKUP) STATE_INT=1 ;;
    FAULT)  STATE_INT=3 ;;
    *)      STATE_INT=0 ;;
esac

TIMESTAMP=$(date +%s)

# ── sanitise name for use as a filename ───────────────────────────────────────

SAFE_NAME=$(printf '%s' "${NAME}" | tr -c '[:alnum:]' '_')

# ── write metrics atomically ──────────────────────────────────────────────────

PROM_FILE="${TEXTFILE_COLLECTOR_DIR}/keepalived_${SAFE_NAME}.prom"
TEMP_FILE=$(mktemp "${TEXTFILE_COLLECTOR_DIR}/.keepalived_${SAFE_NAME}.XXXXXX")

# Ensure the temporary file is removed on any unexpected exit.
# shellcheck disable=SC2064
trap "rm -f '${TEMP_FILE}'" EXIT

cat > "${TEMP_FILE}" << EOF
# HELP keepalived_vrrp_state Current keepalived VRRP state (0=INIT, 1=BACKUP, 2=MASTER, 3=FAULT).
# TYPE keepalived_vrrp_state gauge
keepalived_vrrp_state{name="${NAME}",type="${TYPE}"} ${STATE_INT}
# HELP keepalived_vrrp_priority Current keepalived VRRP priority.
# TYPE keepalived_vrrp_priority gauge
keepalived_vrrp_priority{name="${NAME}",type="${TYPE}"} ${PRIORITY}
# HELP keepalived_vrrp_state_transition_timestamp_seconds Unix timestamp of the last keepalived VRRP state transition.
# TYPE keepalived_vrrp_state_transition_timestamp_seconds gauge
keepalived_vrrp_state_transition_timestamp_seconds{name="${NAME}",type="${TYPE}"} ${TIMESTAMP}
EOF

mv "${TEMP_FILE}" "${PROM_FILE}"
