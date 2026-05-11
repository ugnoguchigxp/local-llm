#!/usr/bin/env bash
set -eu

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo -e "${BLUE}=== Local LLM Project Environment Diagnosis ===${NC}\n"

# 1. PATH Check
echo -n "Checking PATH for scripts... "
if [[ ":$PATH:" == *":${SCRIPT_DIR}:"* ]]; then
    echo -e "${GREEN}OK${NC}"
else
    echo -e "${RED}FAILED${NC}"
    echo -e "   -> ${SCRIPT_DIR} is not in your PATH."
fi

echo "Checking script permissions:"
COMMANDS=("gemma4" "qwen" "bonsai")
for cmd in "${COMMANDS[@]}"; do
    if [ -x "${SCRIPT_DIR}/${cmd}" ]; then
        echo -e "   - $cmd: ${GREEN}Executable${NC}"
    else
        echo -e "   - $cmd: ${RED}NOT Executable or missing${NC}"
    fi
done

# 3. Virtual Environment Check
echo -n "Checking virtual environment... "
if [ -d "${ROOT_DIR}/.venv" ]; then
    echo -e "${GREEN}Found${NC}"
else
    echo -e "${RED}NOT Found${NC} (Run setup.sh or create .venv)"
fi

# 4. Dependency Check
echo -n "Checking dependencies (requests)... "
# 仮想環境のPythonが存在すればそれを使う
PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"
if [ ! -f "$PYTHON_BIN" ]; then
    PYTHON_BIN="python3"
fi

if "$PYTHON_BIN" -c "import requests" &> /dev/null; then
    echo -e "${GREEN}Installed${NC}"
else
    echo -e "${RED}Missing${NC}"
fi

echo -e "\n${BLUE}Diagnosis Complete.${NC}"
echo "To run automated backend tests, execute: python3 test_commands.py"
