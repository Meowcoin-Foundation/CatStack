#!/usr/bin/env bash
set -euo pipefail

CDN_URL="https://cdn.catstack.sh"
MFARM_REPO="https://github.com/Meowcoin-Foundation/CatStack"

RESET="\033[0m"
BOLD="\033[1m"
GREEN="\033[32m"
CYAN="\033[36m"
RED="\033[31m"
YELLOW="\033[33m"
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
warn()    { echo -e "${YELLOW}${BOLD}!  $*${RESET}"; }
die()     { echo -e "${RED}${BOLD}✗  $*${RESET}"; exit 1; }

check_deps() {
    for cmd in python3 pip3 ssh; do
        command -v "$cmd" &>/dev/null || die "Required: $cmd (not found)"
    done
}

install_mfarm() {
    check_deps

    if command -v mfarm &>/dev/null; then
        warn "mfarm is already installed ($(mfarm --version 2>/dev/null || echo 'unknown version'))"
        read -rp "  Reinstall anyway? [y/N] " CONFIRM
        [[ "${CONFIRM:-N}" =~ ^[Yy] ]] || return
    fi

    info "Installing mfarm..."
    pip3 install --quiet --upgrade pip
    pip3 install --quiet "git+${MFARM_REPO}.git" \
        || die "Failed to install mfarm. Try: pip3 install git+${MFARM_REPO}.git"
    success "mfarm installed"

    setup_rig
    print_next_steps
}

update_mfarm() {
    command -v mfarm &>/dev/null || die "mfarm is not installed. Run the installer first."

    info "Updating mfarm..."
    pip3 install --quiet --upgrade pip
    pip3 install --quiet --upgrade "git+${MFARM_REPO}.git" \
        || die "Update failed. Try: pip3 install --upgrade git+${MFARM_REPO}.git"
    success "mfarm updated to $(mfarm --version 2>/dev/null || echo 'unknown version')"
}

uninstall_mfarm() {
    command -v mfarm &>/dev/null || die "mfarm is not installed."

    warn "This will remove mfarm from your system."
    read -rp "  Are you sure? [y/N] " CONFIRM
    [[ "${CONFIRM:-N}" =~ ^[Yy] ]] || { echo "Aborted."; return; }

    info "Removing mfarm..."
    pip3 uninstall mfarm -y \
        || die "Uninstall failed. Try: pip3 uninstall mfarm"
    success "mfarm removed"

    if [[ -d "${HOME}/.mfarm" ]]; then
        echo ""
        warn "Found farm data at ~/.mfarm (rigs, flight sheets, config)"
        read -rp "  Remove it too? [y/N] " REMOVE_DATA
        if [[ "${REMOVE_DATA:-N}" =~ ^[Yy] ]]; then
            rm -rf "${HOME}/.mfarm"
            success "Farm data removed"
        else
            echo -e "  ${DIM}Kept at ~/.mfarm${RESET}"
        fi
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

menu() {
    echo -e "${BOLD}What would you like to do?${RESET}"
    echo ""
    echo -e "  ${CYAN}1)${RESET} Install"
    echo -e "  ${CYAN}2)${RESET} Update"
    echo -e "  ${CYAN}3)${RESET} Uninstall"
    echo -e "  ${CYAN}4)${RESET} Exit"
    echo ""
    read -rp "  Choose [1-4]: " CHOICE

    case "${CHOICE}" in
        1) install_mfarm ;;
        2) update_mfarm ;;
        3) uninstall_mfarm ;;
        4) exit 0 ;;
        *) die "Invalid choice" ;;
    esac
}

# Ensure interactive prompts work even when piped (curl | bash)
exec </dev/tty


print_logo
menu
