#!/usr/bin/env bash
set -euo pipefail

SUPABASE_URL="https://tbxiptvtxvnmtvfgrylj.supabase.co"
BUCKET="catstack"
MFARM_REPO="https://github.com/Meowcoin-Foundation/CatStack"

RESET="\033[0m"
BOLD="\033[1m"
GREEN="\033[32m"
CYAN="\033[36m"
RED="\033[31m"
DIM="\033[2m"

print_logo() {
cat << 'EOF'

                 _uP~"b          d"u,
                dP'   "b       ,d"  "o
               d"    , `b     d"'    "b
              l] [    " `l,  d"       lb
              Ol ?     "  "b`"=uoqo,_  "l
            ,dBb "b        "b,    `"~~TObup,_

         CatStack — Mining Farm Manager

EOF
}

info()    { echo -e "${CYAN}${BOLD}==> ${RESET}${BOLD}$*${RESET}"; }
success() { echo -e "${GREEN}${BOLD}✓  $*${RESET}"; }
die()     { echo -e "${RED}${BOLD}✗  $*${RESET}"; exit 1; }

check_deps() {
    for cmd in python3 pip3 ssh; do
        command -v "$cmd" &>/dev/null || die "Required: $cmd (not found)"
    done
}

install_mfarm() {
    info "Installing mfarm..."
    if pip3 install --quiet "git+${MFARM_REPO}.git"; then
        success "mfarm installed"
    else
        die "Failed to install mfarm. Try: pip3 install git+${MFARM_REPO}.git"
    fi
}

setup_rig() {
    echo ""
    echo -e "${BOLD}Add your first rig${RESET}"
    echo -e "${DIM}(You can add more later with: mfarm rig add)${RESET}"
    echo ""

    read -rp "  Rig name (e.g. rig-01): " RIG_NAME
    read -rp "  Rig IP address:          " RIG_HOST
    read -rp "  SSH user [root]:         " RIG_USER
    RIG_USER="${RIG_USER:-root}"

    info "Adding rig '${RIG_NAME}' (${RIG_HOST})..."
    mfarm rig add "$RIG_NAME" "$RIG_HOST" --user "$RIG_USER" \
        || die "Failed to add rig. Check the IP and SSH access."
    success "Rig added"

    echo ""
    read -rp "Deploy mfarm agent to ${RIG_NAME} now? [Y/n] " DEPLOY
    DEPLOY="${DEPLOY:-Y}"

    if [[ "$DEPLOY" =~ ^[Yy] ]]; then
        info "Deploying agent..."
        mfarm deploy agent "$RIG_NAME" \
            || die "Deploy failed. Check SSH access to ${RIG_HOST}."
        success "Agent deployed and running"
    fi
}

print_next_steps() {
    echo ""
    echo -e "${BOLD}Next steps:${RESET}"
    echo ""
    echo -e "  ${CYAN}mfarm flight create${RESET} <name> --coin MEWC --algo scrypt --miner xmrig \\"
    echo -e "    --pool stratum+tcp://pool.example.com:3333 --wallet YOUR_WALLET"
    echo ""
    echo -e "  ${CYAN}mfarm flight apply${RESET} <name> ${RIG_NAME:-your-rig}"
    echo ""
    echo -e "  ${CYAN}mfarm dashboard${RESET}          # live terminal dashboard"
    echo -e "  ${CYAN}mfarm web${RESET}                # web UI at http://localhost:8080"
    echo -e "  ${CYAN}mfarm status${RESET}             # quick fleet status"
    echo ""
}

main() {
    print_logo
    check_deps

    if command -v mfarm &>/dev/null; then
        success "mfarm already installed ($(mfarm --version 2>/dev/null || echo 'unknown version'))"
    else
        install_mfarm
    fi

    setup_rig
    print_next_steps
}

main "$@"
