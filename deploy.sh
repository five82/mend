#!/bin/bash
# Deploy the working tree into Mend's isolated uv tool environment.
# Run the development checks first; this script does not test what it ships.

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

print_step() {
    echo -e "\n${BLUE}:: $1${NC}"
}

print_success() {
    echo -e "${GREEN}   $1${NC}"
}

print_error() {
    echo -e "${RED}   $1${NC}"
}

deployment_failed() {
    print_error "$1"
    echo "   No restoration was attempted."
    exit 1
}

cd "$(dirname "$0")"

print_step "Locating the installed mend"
if TARGET=$(command -v mend 2>/dev/null); then
    print_success "$TARGET"
else
    TARGET="$(uv tool dir --bin)/mend"
    if [ -x "$TARGET" ]; then
        print_success "$TARGET"
    else
        print_success "$TARGET (first install)"
    fi
fi

print_step "Installing"
if ! uv tool install --force .; then
    deployment_failed "uv tool installation failed"
fi
TARGET="$(uv tool dir --bin)/mend"
if [ ! -x "$TARGET" ]; then
    deployment_failed "uv did not install the mend command at $TARGET"
fi
print_success "installed $TARGET"

print_step "Setting up the VapourSynth runtime"
if ! "$TARGET" setup; then
    deployment_failed "VapourSynth setup failed"
fi
print_success "native plugins are ready"

print_step "Verifying installation"
if ! "$TARGET" --help >/dev/null; then
    deployment_failed "installed mend command failed its smoke check"
fi
print_success "installed command passed its smoke check"

echo -e "\n${GREEN}Deployed${NC}"
