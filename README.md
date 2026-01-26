# MyVlessBot - Professional VLESS Sales Solution

**MyVlessBot** is a robust, production-ready Telegram bot for automating the sale and management of VLESS VPN configurations. Integrated seamlessly with **3x-ui Panel**, it offers a complete billing and user management system.

## 🚀 Key Features

*   **Automated Sales**: Instant key issuance upon payment.
*   **Web Dashboard**: Full control over plans, servers, and users.
*   **Multi-Server Support**: Scale your business with unlimited nodes.
*   **Flexible Billing**: Recurring subscriptions, trial periods, and referral system.
*   **Payment & Support**:
    *   **Gateways**: YooKassa, CryptoBot, Heleket, TonConnect.
    *   **Support**: Built-in ticket system (Support Bot).

---

## 🛠 One-Line Installation

Run this command on your **Ubuntu/Debian** server to set up everything automatically:

```bash
git clone https://github.com/Bogdan199719/myvlessbottg.git && cd myvlessbottg && bash install.sh
```

The installer will:
1.  Check for dependencies (Docker, Git).
2.  Clone the repository.
3.  **Interactively ask** for your Bot Token and settings.
4.  Generate a secure `.env` file.
5.  Launch the application.

---

## 🔧 Environment Variables

The project uses a `.env` file for configuration. Below are the available options:

| Variable | Description | Required | Default |
| :--- | :--- | :---: | :--- |
| `TELEGRAM_BOT_TOKEN` | Your Telegram Bot API Token | ✅ | - |
| `ADMIN_TELEGRAM_ID` | Telegram ID of the Super Admin | ✅ | - |
| `DOMAIN` | Domain name for the Web Panel | ✅ | - |
| `PANEL_LOGIN` | Web Panel Login | ❌ | `admin` |
| `PANEL_PASSWORD` | Web Panel Password | ❌ | `admin` |
| `YOOKASSA_ENABLED` | Enable YooKassa Payments | ❌ | `false` |
| `CRYPTOBOT_ENABLED` | Enable CryptoBot Payments | ❌ | `false` |
| `TONCONNECT_ENABLED` | Enable TON Wallet Payments | ❌ | `false` |

*(See `.env.example` for the full list)*

---

## 📦 Deployment via Docker

If you prefer manual deployment:

```bash
# 1. Clone
git clone https://github.com/Bogdan199719/myvlessbottg.git
cd myvlessbottg

# 2. Configure
cp .env.example .env
nano .env  # Edit your settings

# 3. Launch
docker-compose up -d --build
```

## 📄 License
This project is proprietary software maintained by **Bogdan199719**. All rights reserved.
