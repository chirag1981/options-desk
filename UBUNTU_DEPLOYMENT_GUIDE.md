# 🐧 Options Desk — Ubuntu Server Production Deployment & Update Guide

[![Ubuntu](https://img.shields.io/badge/Ubuntu-20.04%20%7C%2022.04%20%7C%2024.04%20LTS-E95420?style=for-the-badge&logo=ubuntu&logoColor=white)](https://ubuntu.com/)
[![Python](https://img.shields.io/badge/Python-3.10%20%7C%203.11%20%7C%203.12-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![Gunicorn](https://img.shields.io/badge/WSGI-Gunicorn-499848?style=for-the-badge&logo=gunicorn&logoColor=white)](https://gunicorn.org/)
[![Nginx](https://img.shields.io/badge/Proxy-Nginx-009639?style=for-the-badge&logo=nginx&logoColor=white)](https://nginx.org/)
[![FYERS](https://img.shields.io/badge/Broker-FYERS%20API%20v3-0078D7?style=for-the-badge)](https://fyers.in/)

Complete production-grade guide for running **Options Desk** on an Ubuntu Linux server with 24/7 background operation, automated process recovery, isolated scheduler locking, and SSL encryption.

---

## 📑 Table of Contents
1. [⚡ Fast Track: Updating an Existing Server](#1--fast-track-updating-an-existing-server)
2. [🛠️ Fresh Server Setup: Zero-to-Hero Installation](#2-️-fresh-server-setup-zero-to-hero-installation)
3. [⚙️ Environment Configuration (`config/.env`)](#3-️-environment-configuration-configenv)
4. [🔄 Systemd Service Setup (24/7 Auto-Start)](#4--systemd-service-setup-247-auto-start)
5. [🌐 Nginx Reverse Proxy & Free SSL Setup](#5--nginx-reverse-proxy--free-ssl-setup)
6. [📊 Maintenance, Logs & Operational Commands](#6--maintenance-logs--operational-commands)
7. [🔍 Troubleshooting & Common Issues](#7--troubleshooting--common-issues)

---

## 1. ⚡ Fast Track: Updating an Existing Server

> [!TIP]
> If Options Desk is already cloned on your server, follow this quick 6-step update procedure:

```bash
# Step 1: Go to your project directory
cd /var/www/options-desk

# Step 2: Pull the latest updates from master
git pull origin master

# Step 3: Activate your virtual environment and install new dependencies
source venv/bin/activate
pip install -U pip
pip install -r requirements.txt

# Step 4: Verify your environment credentials
nano config/.env

# Step 5: Restart the background service
sudo systemctl restart options-desk

# Step 6: Verify service status and inspect live logs
sudo systemctl status options-desk
sudo journalctl -u options-desk -f -n 50
```

---

## 2. 🛠️ Fresh Server Setup: Zero-to-Hero Installation

### Step 1: SSH into your server
```bash
ssh root@your_server_ip
# Or non-root sudo user:
# ssh ubuntu@your_server_ip
```

### Step 2: Update system packages & install prerequisites
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-pip python3-venv git tzdata curl ufw
```

### Step 3: Configure Timezone to Indian Standard Time (Asia/Kolkata)
> [!IMPORTANT]
> Market hours, option expiry, and theta decay calculations require the server clock to match Indian Standard Time (`IST`).

```bash
sudo timedatectl set-timezone Asia/Kolkata
timedatectl
```

### Step 4: Create deployment directory & assign ownership
```bash
sudo mkdir -p /var/www/options-desk
sudo chown -R $USER:$USER /var/www/options-desk
cd /var/www/options-desk
```

### Step 5: Clone the repository
```bash
git clone https://github.com/chirag1981/options-desk.git .
```

### Step 6: Create Python Virtual Environment & Install Dependencies
```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

---

## 3. ⚙️ Environment Configuration (`config/.env`)

Create your private production configuration file:
```bash
mkdir -p config
nano config/.env
```

Paste the following template and replace with your actual FYERS API credentials:

```env
# Server & Flask Settings
PORT=5001
FLASK_DEBUG=false
SECRET_KEY=generate_a_random_32_character_string_here

# FYERS API v3 Credentials (Automated Zero-Click TOTP Authentication)
FYERS_ID=XC04484
PIN=your_4_digit_pin
APP_ID=MXPA3JHTVP
APP_TYPE=100
APP_SECRET=your_fyers_app_secret
TOTP_KEY=your_totp_secret_key
REDIRECT_URI=http://127.0.0.1:5001/
```

> [!CAUTION]
> Lock down file permissions so only your user can read credentials:
> ```bash
> chmod 600 config/.env
> ```

---

## 4. 🔄 Systemd Service Setup (24/7 Auto-Start)

Create a systemd unit file to ensure the app runs continuously and automatically restarts on system reboots or unhandled errors:

```bash
sudo nano /etc/systemd/system/options-desk.service
```

Paste the following configuration:

```ini
[Unit]
Description=Options Desk Trading Terminal (Gunicorn + FYERS API v3)
After=network.target

[Service]
User=ubuntu
Group=ubuntu
WorkingDirectory=/var/www/options-desk
Environment="PATH=/var/www/options-desk/venv/bin"
EnvironmentFile=/var/www/options-desk/config/.env

# Gunicorn Execution:
# IMPORTANT: Use 1 worker with 4 threads (--workers 1 --threads 4).
# This ensures the background options scheduler lock is unique and non-competing.
ExecStart=/var/www/options-desk/venv/bin/gunicorn \
    --workers 1 \
    --threads 4 \
    --worker-class gthread \
    --bind 0.0.0.0:5001 \
    --timeout 120 \
    --access-logfile /var/www/options-desk/access.log \
    --error-logfile /var/www/options-desk/error.log \
    run:app

Restart=always
RestartSec=5
KillMode=mixed
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```

*(If your server username is not `ubuntu`, update `User=` and `Group=` to match your user).*

### Enable and Start Service:
```bash
sudo systemctl daemon-reload
sudo systemctl enable options-desk
sudo systemctl start options-desk
sudo systemctl status options-desk
```

---

## 5. 🌐 Nginx Reverse Proxy & Free SSL Setup

To access your trading desk securely over standard ports (80/443) or via a domain name:

### Step 1: Install Nginx & Certbot
```bash
sudo apt install -y nginx certbot python3-certbot-nginx
```

### Step 2: Create Nginx Site Configuration
```bash
sudo nano /etc/nginx/sites-available/options-desk
```

```nginx
server {
    listen 80;
    server_name your_domain.com;   # Or your server IP

    location / {
        proxy_pass http://127.0.0.1:5001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_connect_timeout 120s;
        proxy_read_timeout 120s;
        proxy_send_timeout 120s;

        # WebSocket & Long-Polling Support
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

### Step 3: Enable Site & Restart Nginx
```bash
sudo ln -s /etc/nginx/sites-available/options-desk /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl restart nginx
```

### Step 4: Configure UFW Firewall
```bash
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw --force enable
```

### Step 5: (Optional) Install Free SSL Certificate
```bash
sudo certbot --nginx -d your_domain.com
```

---

## 6. 📊 Maintenance, Logs & Operational Commands

| Task | Command |
| :--- | :--- |
| **Stream Live Logs** | `sudo journalctl -u options-desk -f` |
| **Inspect Last 100 Logs** | `sudo journalctl -u options-desk -n 100 --no-pager` |
| **Restart Service** | `sudo systemctl restart options-desk` |
| **Stop Service** | `sudo systemctl stop options-desk` |
| **Start Service** | `sudo systemctl start options-desk` |
| **Run Pytest Suite** | `cd /var/www/options-desk && venv/bin/pytest` |
| **Check DB Integrity** | `cd /var/www/options-desk && venv/bin/python -c "import sqlite3; conn = sqlite3.connect('instance/options_signals.db'); print(conn.execute('PRAGMA integrity_check').fetchall())"` |
| **Export Journal CSV** | `curl -o trade_journal.csv http://127.0.0.1:5001/api/options-desk/signals/export-csv` |

---

## 7. 🔍 Troubleshooting & Common Issues

### ❌ Issue 1: Port `5001` Conflict (`Address already in use`)
```bash
# Identify conflicting process
sudo lsof -i :5001
# Kill stuck process
sudo fuser -k 5001/tcp
# Restart clean service
sudo systemctl restart options-desk
```

### ❌ Issue 2: FYERS Authentication / Expired Token
```bash
# 1. Check credentials in config/.env
cat config/.env

# 2. Clear cached token file to force fresh zero-click TOTP login
rm -f instance/fyers_token.json

# 3. Restart service
sudo systemctl restart options-desk
```

### ❌ Issue 3: Timezone Lag / Stale OI Times
```bash
# Set server clock to Indian Standard Time
sudo timedatectl set-timezone Asia/Kolkata
timedatectl
```

### ❌ Issue 4: SQLite Database Permission Denied
```bash
# Reassign instance directory ownership to your service user
sudo chown -R $USER:$USER /var/www/options-desk/instance
```
