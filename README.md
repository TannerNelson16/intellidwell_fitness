# IntelliDwell Fitness Companion

An interactive fitness and health-tracking companion web application. Log workouts, chart performance metrics, and manage custom exercise lists.

---

## 🚀 Key Features
* Tailored custom software architecture optimized for NelsonServer.
* Robust configuration models with clean environment variable fallback systems.
* Seamless integration with self-hosted bare repository networks and off-site cloud backups.

---

## 🛠️ Technology Stack
* **Core**: Python, Flask, SQLite, Chart.js telemetry graphs, Glassmorphic UI design
* **Environment**: Linux Server deployment compatibility.

---

## 📦 Local Installation & Setup

1. Set up virtual environment and install requirements: `pip install -r requirements.txt`
2. Start Flask development server: `python app.py`
3. Access via `http://localhost:5000`

---

## 📡 NelsonServer Dual-Push Deployment Configuration

This repository is configured with **automatic local & cloud synchronization**! 

* **Local bare repository**: `/srv/git/intellidwell_fitness.git`
* **GitHub remote**: `git@github.com:TannerNelson16/intellidwell_fitness.git`

### Secure Dual-Push
Whenever you run `git push origin`, it instantly and securely uploads your commits to **both** your local private bare repository on NelsonServer and your off-site GitHub account in a single step!

```bash
git push origin main
```
*Your secrets, `.env` config credentials, and local databases are protected locally in your `.gitignore` shield, ensuring only clean source code reaches GitHub.*
