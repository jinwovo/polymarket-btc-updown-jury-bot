#!/bin/bash
set -e

echo "========================================="
echo "  Polymarket Bot - Server Setup"
echo "========================================="

# 1. System packages
echo "[1/8] Installing system packages..."
apt update -y && apt upgrade -y
apt install -y python3 python3-pip python3-venv mariadb-server git curl build-essential \
    libssl-dev pkg-config nodejs npm chromium-browser xvfb

# 2. MariaDB setup
echo "[2/8] Configuring MariaDB..."
systemctl enable mariadb
systemctl start mariadb
mysql -u root -e "CREATE DATABASE IF NOT EXISTS polymarket_btc_updown CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
mysql -u root -e "ALTER USER 'root'@'localhost' IDENTIFIED BY 'hana1234'; FLUSH PRIVILEGES;"

# 3. Rust
echo "[3/8] Installing Rust..."
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
source "$HOME/.cargo/env"

# 4. Clone repo
echo "[4/8] Cloning repository..."
cd /root
if [ -d "polymarket-bot" ]; then
    cd polymarket-bot && git pull
else
    git clone https://github.com/jinwovo/polymarket-btc-updown-jury-bot.git polymarket-bot
    cd polymarket-bot
fi

# 5. Python deps
echo "[5/8] Installing Python dependencies..."
pip3 install --break-system-packages -r requirements.txt 2>/dev/null || pip3 install --break-system-packages pymysql python-dotenv httpx websockets numpy py-clob-client playwright

# Install Playwright browsers
python3 -m playwright install chromium

# 6. Rust FAK binary
echo "[6/8] Building Rust FAK binary..."
if [ -d "rust_order" ]; then
    cd rust_order
    cargo build --release
    cd ..
    echo "Rust binary built at rust_order/target/release/rust_order"
fi

# 7. Node.js / Next.js
echo "[7/8] Building Next.js dashboard..."
npm install
npm run build

# 8. Environment files
echo "[8/8] Setting up environment..."
cat > .env.secrets << 'ENVEOF'
DB_BACKEND=mariadb
MARIADB_HOST=127.0.0.1
MARIADB_PORT=3306
MARIADB_USER=root
MARIADB_PASSWORD=hana1234
MARIADB_DATABASE=polymarket_btc_updown
ENVEOF

echo ""
echo "========================================="
echo "  Setup complete!"
echo "========================================="
echo ""
echo "NEXT STEPS:"
echo "1. Edit .env.secrets to add your Polymarket keys:"
echo "   nano /root/polymarket-bot/.env.secrets"
echo ""
echo "2. Start the bot:"
echo "   cd /root/polymarket-bot"
echo "   python3 dashboard_server.py --host 0.0.0.0 --port 8790 &"
echo "   python3 data_collector.py &"
echo "   next start -p 3100 &"
echo ""
echo "MariaDB port: 3306 (default, not 3400)"
echo "Dashboard: http://$(hostname -I | awk '{print $1}'):3100"
echo ""
