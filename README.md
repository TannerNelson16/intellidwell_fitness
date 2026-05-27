# 🏋️ IntelliDwell Fitness Tracker & HealthKit Companion

An interactive, self-hosted fitness tracking companion within the IntelliDwell ecosystem. Tracks workouts, traces weight fluctuations, logs meal calories, and leverages VAPID web-push notifications alongside mobile HealthKit API synchronization.

---

## 🔬 Core Features & Architecture
### 📊 Analytics & Push Architecture
* **Pywebpush Native HUD**: Features interactive VAPID browser notifications managed by a local Service Worker (`static/js/sw.js`) and scheduled by `BackgroundScheduler`.
* **Gram-Specific Calorie Logger**: Database models tracking specific meal logs broken down into breakfast, lunch, dinner, and snack entries (`cal_breakfast`, `cal_lunch`, etc.).
* **Progression Photo Cabinet**: File management directories caching weekly weight progress images with secure hashes.
* **Mobile Sync Endpoint**: Secure POST routes (`/api/healthkit`) to consume telemetry data directly from Apple HealthKit.

---

## 🛠️ Technology Stack & Environment
* **Core Technologies**: Flask (Python), SQLAlchemy (SQLite), pywebpush (VAPID), APScheduler, HealthKit REST API
* **Deployment Workspace**: NelsonServer private self-hosted infrastructure.

---

## 📦 Setup & Local Installation
1. Configure requirements in a virtual environment: `pip install -r requirements.txt`
2. Generate VAPID push keys and record them in your local `.env`:
   * `VAPID_PUBLIC_KEY` & `VAPID_PRIVATE_KEY`
   * `HEALTHKIT_TOKEN` (for authenticating mobile synchronization uploads)
3. Launch database bootstrap & webserver: `python3 app.py` (runs on default port `5000` or as system service `fitness.service`)

---

## 📡 NelsonServer Dual-Push Configuration

This repository is permanently configured with a dual-remote pipeline:
* **Local Bare Server Repository**: `/srv/git/intellidwell_fitness.git`
* **GitHub Repository**: `git@github.com:TannerNelson16/intellidwell_fitness.git`

### Unified Push
Whenever you make commits, a single:
```bash
git push origin main
```
instantly synchronizes your codebase with **both** your local private server and your GitHub account at the same time!

*All private configuration credentials (`.env`), databases, and large media files are completely isolated locally via `.gitignore` shields, ensuring only pristine source code reaches GitHub.*
