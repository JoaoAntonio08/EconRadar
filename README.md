# EconRadar

> **Real-time economic intelligence platform with AI-powered market insights**

[![Python](https://img.shields.io/badge/Python-3.9+-3776ab?style=flat-square&logo=python)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688?style=flat-square&logo=fastapi)](https://fastapi.tiangolo.com/)
[![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)](LICENSE)
[![Status](https://img.shields.io/badge/Status-Production%20Ready-brightgreen?style=flat-square)]()

---

## 📋 Overview

**EconRadar** is a cutting-edge economic intelligence platform that empowers investors and traders with real-time market data, AI-driven sentiment analysis, and personalized portfolio monitoring. Built with modern technologies, the platform combines robust backend services with an intuitive, high-performance frontend interface.

### Key Features

- 🤖 **AI-Powered Chat Interface** - OpenRouter-integrated conversational AI for market insights
- 📊 **Real-time Market Data** - Integration with Finnhub API for live quotes and market indicators
- 📈 **Multi-asset Monitoring** - Track forex, commodities, stocks, and cryptocurrencies simultaneously
- ⚡ **Intelligent Alerts** - Customizable threshold-based notifications with volatility analysis
- 🔐 **Enterprise-Grade Security** - JWT authentication, bcrypt password hashing, CORS middleware
- 💾 **Persistent Storage** - JSON-based data persistence with automatic backups
- 🌐 **Cross-platform** - Seamless localhost and production deployment with ngrok tunneling support
- 🎨 **Modern UI/UX** - Dark-themed, responsive design with real-time updates

---

## 🏗️ Architecture

### System Design

```
┌─────────────────────────────────────────────────────────────┐
│                    Frontend (HTML5/JS)                      │
│  ├── Landing Page (landing.html)                            │
│  ├── Dashboard (index.html)                                 │
│  └── API Client (api.js)                                    │
└────────────────────┬────────────────────────────────────────┘
                     │ HTTP/CORS
┌────────────────────▼────────────────────────────────────────┐
│             FastAPI Backend (Python 3.9+)                   │
│  ├── Authentication & Authorization (JWT/Bearer)           │
│  ├── Chat Management (OpenRouter AI)                        │
│  ├── Market Data Pipeline (Finnhub)                         │
│  ├── User Profile & Configuration                           │
│  └── Static File Serving                                    │
└────────────────────┬────────────────────────────────────────┘
                     │
        ┌────────────┴────────────┐
        │                         │
    ┌───▼────┐          ┌────────▼──────┐
    │ Finnhub│          │   OpenRouter   │
    │  API   │          │  (AI Models)   │
    └────────┘          └────────────────┘
```

### Technology Stack

| Layer | Technology | Version | Purpose |
|-------|-----------|---------|---------|
| **Frontend** | HTML5, CSS3, JavaScript (Vanilla) | Latest | UI/UX, Client Logic |
| **Backend** | FastAPI | 0.115+ | API Server, Business Logic |
| **Server** | Uvicorn | 0.30.6 | ASGI Application Server |
| **Authentication** | JWT + python-jose | 3.3.0 | Secure Token Management |
| **Password Security** | bcrypt | via passlib 1.7.4 | Credential Hashing |
| **HTTP Client** | httpx | 0.27.2 | Async External API Calls |
| **Data Validation** | Pydantic | 2.9.2 | Request/Response Schema |
| **Environment** | python-dotenv | 1.0.1 | Configuration Management |
| **File Handling** | aiofiles | 23.2.1 | Async File I/O Operations |

---

## 🚀 Quick Start

### Prerequisites

- **Python 3.9+** installed on your system
- **pip** (Python package manager)
- **ngrok** account (optional, for public URL tunneling)
- API Keys:
  - Finnhub API Key (for market data)
  - OpenRouter API Key (for AI features)

### Installation

#### 1. Clone the Repository
```bash
git clone https://github.com/JoaoAntonio08/EconRadar.git
cd EconRadar
```

#### 2. Environment Configuration
Create a `.env` file in the `backend/` directory:
```bash
# backend/.env
SECRET_KEY=your-secret-key-here
TOKEN_EXPIRE_H=24
OPENROUTER_API_KEY=sk-or-v1-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
FINNHUB_API_KEY=d87jkq1r01qmhakg2qrg
OR_MODEL=nvidia/nemotron-3-nano-30b-a3b:free
ALLOWED_ORIGINS=http://localhost:8000,http://localhost:5500
```

#### 3. Windows: One-Command Startup
```batch
iniciar.bat
```

This script automatically:
- Creates a Python virtual environment (if needed)
- Installs all dependencies
- Starts the FastAPI backend on `http://localhost:8000`
- Initializes ngrok tunneling for public access
- Opens the frontend interface

#### 4. Manual Startup (All Platforms)

**Terminal 1 - Backend:**
```bash
cd backend
python -m venv venv

# On Windows:
venv\Scripts\activate
# On macOS/Linux:
source venv/bin/activate

pip install -r requirements.txt
python main.py
```

**Terminal 2 - Frontend:**
Open your browser and navigate to:
- Local: `http://localhost:8000`
- Remote: Check ngrok console for public URL

---

## 🔑 Default Credentials

| Field | Value | Notes |
|-------|-------|-------|
| Username | `Admin` | Fixed user account |
| Password | `12345` | Default password - **CHANGE in production** |

⚠️ **Security Warning**: Change default credentials immediately in production environments.

---

## 📂 Project Structure

```
EconRadar/
├── backend/
│   ├── main.py              # FastAPI application entry point
│   ├── requirements.txt      # Python dependencies
│   ├── venv/                # Python virtual environment (auto-created)
│   └── data/
│       └── data.json        # User profiles, chat history, config storage
├── frontend/
│   ├── index.html           # Main dashboard interface
│   ├── landing.html         # Landing page with custom cursor
│   └── api.js               # Client-side API wrapper
├── iniciar.bat              # Windows startup automation script
└── README.md               # This file
```

---

## 🔌 API Endpoints

### Authentication
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/auth/login` | POST | User login (returns JWT token) |
| `/api/auth/refresh` | POST | Refresh expired JWT token |

### User Management
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/users/profile` | GET | Retrieve user profile |
| `/api/users/profile` | PUT | Update user profile |
| `/api/users/config` | GET | Get user configuration |
| `/api/users/config` | PUT | Update user configuration |

### Chat & AI
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/chat/sessions` | GET | List all chat sessions |
| `/api/chat/sessions` | POST | Create new chat session |
| `/api/chat/sessions/{id}` | DELETE | Delete chat session |
| `/api/chat/sessions/{id}/messages` | GET | Retrieve session messages |
| `/api/chat/send` | POST | Send message and get AI response |

### Market Data
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/summary` | POST | Generate overnight market summary |

---

## ⚙️ Configuration

### User Settings (Stored in `data.json`)

```json
{
  "config": {
    "autorefresh": true,          // Enable auto-refresh
    "interval_sec": 60,           // Refresh interval in seconds
    "currency": "BRL",            // Display currency
    "threshold": 2.0,             // Alert threshold (%)
    "accent_color": "#4f8dff",    // UI accent color
    "compact_mode": false,        // Compact UI mode
    "show_instab": true,          // Show instability indicators
    "animations": true,           // Enable UI animations
    "alert_strong": true,         // Alert on strong moves
    "alert_interest": true,       // Alert on watched assets
    "news_interest": true,        // Include news in analysis
    "cache_enabled": true         // Enable response caching
  }
}
```

### Supported Assets

**Forex:** USD, EUR, GBP, JPY, AUD, CAD  
**Commodities:** XAU (Gold), XAG (Silver), CRUDE (Oil)  
**Stocks:** SPY, QQQ, IBOV, and 1000+ symbols via Finnhub  
**Cryptocurrencies:** BTC, ETH, XRP, and 500+ coins

---

## 🛡️ Security Considerations

### Current Implementation
- ✅ JWT token-based authentication (HS256 algorithm)
- ✅ Bcrypt password hashing (12-round salt)
- ✅ CORS middleware with whitelisted origins
- ✅ Bearer token authorization on all protected endpoints
- ✅ Environment-based sensitive configuration

### Production Recommendations
- 🔴 Change `SECRET_KEY` to a cryptographically secure random string (32+ chars)
- 🔴 Update default credentials immediately
- 🔴 Use HTTPS/TLS for all communications
- 🔴 Implement database instead of JSON storage
- 🔴 Add rate limiting and request throttling
- 🔴 Enable logging and monitoring
- 🔴 Implement proper error handling without exposing stack traces
- 🔴 Use secrets manager for API keys
- 🔴 Add audit trails for user actions

---

## 📡 External API Integration

### Finnhub Market Data
```javascript
// Fetches real-time quotes, historical data, and company fundamentals
// Rate limit: Depends on subscription tier
// Base URL: https://api.finnhub.io
```

### OpenRouter AI
```javascript
// Provides access to multiple LLM models (Nvidia Nemotron default)
// Supports streaming responses
// Base URL: https://openrouter.ai/api/v1
```

---

## 🎨 UI/UX Features

### Design System
- **Color Palette:** Dark theme with gold and blue accents
- **Typography:** Syne (headings), DM Sans (body text)
- **Responsive Grid:** CSS Grid + Flexbox
- **Animations:** CSS transitions, smooth scrolling
- **Custom Cursor:** Gold-themed interactive cursor with trail effect
- **Real-time Updates:** WebSocket-ready architecture

### Components
- Sidebar navigation with collapsible menu
- Interactive dashboard with live charts
- Modal-based chat interface
- Alert notification system
- Profile and settings panels

---

## 🧪 Testing

### Manual Testing Workflow
1. Start backend and frontend
2. Navigate to login page (`landing.html`)
3. Enter credentials: `Admin` / `12345`
4. Test market data refresh functionality
5. Create chat sessions and test AI responses
6. Modify configuration settings
7. Verify data persistence across sessions

### Integration Points to Test
- [ ] JWT token generation and validation
- [ ] Finnhub API connectivity and data accuracy
- [ ] OpenRouter AI model responses
- [ ] CORS and cross-origin requests
- [ ] Static file serving
- [ ] Data.json read/write operations
- [ ] Chat session management
- [ ] User profile updates

---

## 🚢 Deployment

### Local Network
```bash
# Windows (Automatic)
iniciar.bat

# Manual - specify allowed origins
ALLOWED_ORIGINS="http://192.168.1.100:8000" python main.py
```

### Public Internet (via ngrok)
1. Install ngrok: [ngrok.com](https://ngrok.com)
2. Run `iniciar.bat` - it automatically configures ngrok
3. Copy public URL from ngrok console
4. Share URL with team members

### Production Server
```bash
# Use production ASGI server
pip install gunicorn
gunicorn -w 4 -k uvicorn.workers.UvicornWorker main:app --bind 0.0.0.0:8000
```

---

## 📊 Data Persistence

All user data is stored in `backend/data/data.json`:

```json
{
  "user": { /* authentication credentials */ },
  "profile": { /* user display info and preferences */ },
  "config": { /* application settings */ },
  "chat_sessions": [ /* conversation history */ ]
}
```

**Backup Strategy:**
- Manual: Copy `data.json` to safe location
- Automated: Implement cron job for daily backups
- Cloud: Sync to cloud storage service

---

## 🔄 Update & Maintenance

### Updating Dependencies
```bash
cd backend
source venv/bin/activate  # or venv\Scripts\activate on Windows
pip install --upgrade -r requirements.txt
```

### Checking for Security Updates
```bash
pip check
```

### Clearing Cache
```bash
# Remove pycache
find . -type d -name __pycache__ -exec rm -rf {} +
```

---

## 📝 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

---

## 🤝 Contributing

Contributions are welcome! Please follow these guidelines:

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/amazing-feature`
3. Commit changes: `git commit -m 'Add amazing feature'`
4. Push to branch: `git push origin feature/amazing-feature`
5. Open a Pull Request

---

## 📞 Support & Contact

- **Issues:** [GitHub Issues](https://github.com/JoaoAntonio08/EconRadar/issues)
- **Email:** [Contact via GitHub Profile]
- **Documentation:** Comprehensive inline code comments provided

---

## 🗺️ Roadmap

- [ ] Database migration (PostgreSQL)
- [ ] Multi-user support with role-based access
- [ ] Advanced portfolio analytics
- [ ] Machine learning price predictions
- [ ] Mobile app (React Native)
- [ ] Webhook notifications
- [ ] Custom technical indicators
- [ ] Backtesting engine
- [ ] API rate limiting and throttling
- [ ] Docker containerization

---

## 📚 Additional Resources

- [FastAPI Documentation](https://fastapi.tiangolo.com/)
- [Finnhub API Docs](https://finnhub.io/docs/api)
- [OpenRouter API Docs](https://openrouter.ai/docs)
- [JWT Best Practices](https://tools.ietf.org/html/rfc7519)

---

<div align="center">

**Built with ❤️ by [JoaoAntonio08](https://github.com/JoaoAntonio08)**

⭐ If this project helped you, please consider giving it a star on GitHub!

</div>
