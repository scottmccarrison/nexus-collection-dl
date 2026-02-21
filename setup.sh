#!/usr/bin/env bash
set -euo pipefail

echo "=== nexus-collection-dl setup ==="

# Check Python version
PYTHON=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        version=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
        major=$("$cmd" -c "import sys; print(sys.version_info.major)")
        minor=$("$cmd" -c "import sys; print(sys.version_info.minor)")
        if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ]; then
            PYTHON="$cmd"
            echo "Found Python $version ($cmd)"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "Error: Python 3.10+ is required but not found."
    exit 1
fi

# Create virtual environment
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    $PYTHON -m venv venv
fi

echo "Activating virtual environment..."
source venv/bin/activate

# Install the tool
echo "Installing nexus-collection-dl..."
pip install -e . --quiet

# Check for Rust and try to build libloot
if command -v cargo &>/dev/null && command -v rustc &>/dev/null; then
    echo ""
    echo "Rust toolchain detected. Attempting to build libloot (LOOT plugin sorter)..."
    echo "This enables automatic plugin sorting for Bethesda games."
    echo ""

    if pip install maturin --quiet 2>/dev/null; then
        TEMP_DIR=$(mktemp -d)
        if git clone --depth 1 https://github.com/loot/libloot.git "$TEMP_DIR/libloot" 2>/dev/null; then
            cd "$TEMP_DIR/libloot/python"
            if maturin develop --release 2>/dev/null; then
                echo "libloot installed successfully! LOOT sorting is enabled."
            else
                echo "libloot build failed. LOOT sorting will not be available."
                echo "The tool will still work — it just won't do automatic plugin sorting."
            fi
            cd - >/dev/null
        fi
        rm -rf "$TEMP_DIR"
    fi
else
    echo ""
    echo "Note: Rust toolchain not found. LOOT plugin sorting will not be available."
    echo "This is optional — the tool works fine without it."
    echo "To enable LOOT sorting later, install Rust (https://rustup.rs) and re-run this script."
fi

echo ""
echo "=== Setup complete! ==="
echo ""
echo "Usage:"
echo "  source venv/bin/activate"
echo "  export NEXUS_API_KEY='your-key-here'"
echo "  nexus-dl sync 'https://next.nexusmods.com/GAME/collections/SLUG' ./mods"
