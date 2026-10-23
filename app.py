# -*- coding: utf-8 -*-
"""
Bot NBA - Multi-usuario (Full Game + Piloto Automático HT/Q3)
Cada usuario pone SU TheOddsAPI key y SU Telegram.
"""
import os, sqlite3, requests
from datetime import datetime
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from apscheduler.schedulers.background import BackgroundScheduler
import uvicorn

POLL_INTERVAL = 30
MIN_DIFF = 7.0
DB = "nba.db"

def db():
    con = sqlite3.connect(DB, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id TEXT PRIMARY KEY,
            odds_key TEXT NOT NULL,
            tg_token TEXT DEFAULT "",
            tg_chat_id TEXT DEFAULT "",
            active INTEGER DEFAULT 0,
            auto_mode INTEGER DEFAULT 0,
            created_at TEXT);
        CREATE TABLE IF NOT EXISTS selection(
            user_id TEXT, matchup TEXT, active INTEGER, sent INTEGER DEFAULT 0,
            PRIMARY KEY(user_id, matchup));
        CREATE TABLE IF NOT EXISTS signals(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT, matchup TEXT, decision TEXT, line REAL, cuota REAL,
            proyeccion REAL, diff REAL, confianza INTEGER,
            total INTEGER, period INTEGER, created_at TEXT);
        """)
        # Añadir columna auto_mode si la base de datos ya existía
        try:
            con.execute("ALTER TABLE users ADD COLUMN auto_mode INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass

def espn_games():
    try:
        r = requests.get(
            "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
            timeout=15)
        out = []
        for ev in r.json().get("events", []):
            comp = ev.get("competitions", [{}])[0]
            cs = comp.get("competitors", [])
            if len(cs) < 2:
                continue
            home = next((c for c in cs if c.get("homeAway") == "home"), cs[0])
            away = next((c for c in cs if c.get("homeAway") == "away"), cs[1])
            ht = home.get("team", {}).get("displayName", "Home")
            at = away.get("team", {}).get("displayName", "Away")
            st = ev.get("status", {})
            out.append({
                "matchup": f"{at} vs {ht}",
                "status": st.get("type", {}).get("description", ""),
                "state": st.get("type", {}).get("state", ""),
                "period": int(st.get("period", 0) or 0),
                "clock": st.get("displayClock", "") or "",
                "total": sum(int(q.get("value", 0))
                             for q in (home.get("linescores", []) + away.get("linescores", []))),
            })
        return out
    except Exception as e:
        print(f"[espn] {e}")
        return []

def odds_totals(api_key):
    try:
        r = requests.get(
            "https://api.the-odds-api.com/v4/sports/basketball_nba/odds/",
            params={"apiKey": api_key, "regions": "us,eu",
                    "markets": "totals", "oddsFormat": "decimal"},
            timeout=15)
        if r.status_code != 200:
            return {}
        out = {}
        for m in r.json():
            matchup = f"{m['away_team']} vs {m['home_team']}"
            bm = next((b for b in m.get("bookmakers", [])
                       if b.get("key") == "pinnacle"), None) \
                 or (m.get("bookmakers") or [None])[0]
            if not bm:
                continue
            mk = next((x for x in bm.get("markets", [])
                       if x.get("key") == "totals"), None)
            if not mk:
                continue
            line = over = under = None
            for o in mk.get("outcomes", []):
                if "point" in o and "price" in o:
                    line = float(o["point"])
                    if o.get("name", "").lower() == "over":
                        over = float(o["price"])
                    elif o.get("name", "").lower() == "under":
                        under = float(o["price"])
            if line is not None:
                out[matchup] = {"line": line, "over": over, "under": under}
        return out
    except Exception as e:
        print(f"[odds] {e}")
        return {}

def minutes_played(period, clock, status=""):
    if period == 0:
        return 0.0
    completed = max(0, period - 1)
    reg, ot = min(4, completed), max(0, completed - 4)
    mins = reg * 12 + ot * 5
    
    if "Half" in status or "End" in status:
        if period == 2: return 24.0
        if period == 4: return 48.0
    
    if clock:
        try:
            if ':' in clock:
                mm, ss = clock.split(":")
                ml = int(mm) + int(ss) / 60.0
            else:
                ml = float(clock)
            plen = 12.0 if period <= 4 else 5.0
            mins += max(0.0, plen - ml)
        except Exception:
            pass
    return mins

def project_total(total, minutes, period):
    if minutes <= 0.1:
        return None
    total_min = 48.0 if period <= 4 else 48.0 + (period - 4) * 5.0
    return (total / minutes) * total_min

def send_tg(token, chat_id, msg):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
            timeout=15)
        return r.status_code == 200
    except Exception:
        return False

def format_signal(matchup, g, ln, decision, proj, diff, conf, auto=False):
    arrow = "⬆️ OVER" if decision == "OVER" else "⬇️ UNDER"
    cuota = ln["over"] if decision == "OVER" else ln["under"]
    cuota_str = f"{cuota:.2f}" if cuota else "N/A"
    color = "🟢" if decision == "OVER" else "🔴"
    modo = "AUTO (HT / Q3)" if auto else "FULL GAME"
    
    return (
        f"📡 <b>SEÑAL EN VIVO — NBA ({modo})</b>\n\n"
        f"🏀 <b>{matchup}</b>\n"
        f"📌 <b>Línea Totales:</b> {ln['line']:.1f}\n"
        f"⏱️ <b>Estado:</b> P{g['period']} ({g['clock']})\n\n"
        f"📈 <b>Proyección final:</b> {proj:.1f}\n"
        f"💡 <b>Recomendación:</b> <b>{arrow} {ln['line']:.1f}</b>\n"
        f"🪙 <b>Cuota @{cuota_str}</b>\n"
        f"{color} <b>Confianza:</b> {conf}%\n\n"
        f"🕑 <i>Señal generada automáticamente.</i>"
    )

def poll():
    try:
        with db() as con:
            users = con.execute("SELECT * FROM users WHERE active = 1").fetchall()
            
        if not users:
            return

        games = {g["matchup"]: g for g in espn_games()}
        if not games:
            return

        for u in users:
            try:
                if u["auto_mode"]:
                    # MODO PILOTO AUTOMÁTICO (Solo HT y Fin de Q3)
                    target_games = []
                    for g in games.values():
                        is_ht = "Half" in g["status"]
                        is_q3_end = ("End" in g["status"] and g["period"] == 3) or (g["period"] == 3 and g["clock"] in ("0.0", "0:00"))
                        if is_ht or is_q3_end:
                            target_games.append(g)
                    
                    if not target_games:
                        continue
                        
                    odds = odds_totals(u["odds_key"])
                    for g in target_games:
                        matchup = g["matchup"]
                        ln = odds.get(matchup)
                        if not ln:
                            continue
                        
                        current_phase = 2 if "Half" in g["status"] else 3
                        
                        # Verificar si ya enviamos señal en esta fase
                        with db() as con:
                            already_sent = con.execute(
                                "SELECT id FROM signals WHERE user_id=? AND matchup=? AND period=?", 
                                (u["id"], matchup, current_phase)
                            ).fetchone()
                        if already_sent:
                            continue
                            
                        mins = minutes_played(g["period"], g["clock"], g["status"])
                        proj = project_total(g["total"], mins, g["period"])
                        if proj is None:
                            continue
                        diff = proj - ln["line"]
                        if abs(diff) < MIN_DIFF:
                            continue

                        decision = "OVER" if diff > 0 else "UNDER"
                        cuota = ln["over"] if decision == "OVER" else ln["under"]
                        conf = min(99, max(10, int((abs(diff) / 20) * 100 * (mins / 48))))
                        msg = format_signal(matchup, g, ln, decision, proj, diff, conf, auto=True)

                        with db() as con:
                            con.execute(
                                "INSERT INTO signals(user_id,matchup,decision,line,cuota,"
                                "proyeccion,diff,confianza,total,period,created_at) "
                                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                (u["id"], matchup, decision, ln["line"], cuota,
                                 proj, diff, conf, g["total"], g["period"],
                                 datetime.now().isoformat()))
                            con.commit()
                        
                        if u["tg_token"] and u["tg_chat_id"]:
                            if send_tg(u["tg_token"], u["tg_chat_id"], msg):
                                print(f"[AUTO SIGNAL + TG] {u['id'][:8]} {matchup} {decision} {ln['line']:.1f}")
                        else:
                            print(f"[AUTO SIGNAL] {u['id'][:8]} {matchup} {decision} {ln['line']:.1f} (Web)")
                else:
                    # MODO MANUAL (Partidos seleccionados)
                    with db() as con:
                        sel = [r["matchup"] for r in con.execute(
                            "SELECT matchup FROM selection "
                            "WHERE user_id=? AND active=1 AND sent=0", (u["id"],))]
                    
                    live = [m for m in sel if m in games and games[m]["state"] == "in" and "Half" not in games[m]["status"] and "End" not in games[m]["status"]]
                    if not live:
                        continue

                    odds = odds_totals(u["odds_key"])
                    for matchup in live:
                        g = games[matchup]
                        ln = odds.get(matchup)
                        if not ln:
                            continue
                        mins = minutes_played(g["period"], g["clock"], g["status"])
                        proj = project_total(g["total"], mins, g["period"])
                        if proj is None:
                            continue
                        diff = proj - ln["line"]
                        if abs(diff) < MIN_DIFF:
                            continue

                        decision = "OVER" if diff > 0 else "UNDER"
                        cuota = ln["over"] if decision == "OVER" else ln["under"]
                        conf = min(99, max(10, int((abs(diff) / 20) * 100 * (mins / 48))))
                        msg = format_signal(matchup, g, ln, decision, proj, diff, conf, auto=False)

                        with db() as con:
                            con.execute(
                                "INSERT INTO signals(user_id,matchup,decision,line,cuota,"
                                "proyeccion,diff,confianza,total,period,created_at) "
                                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                (u["id"], matchup, decision, ln["line"], cuota,
                                 proj, diff, conf, g["total"], g["period"],
                                 datetime.now().isoformat()))
                            con.execute(
                                "UPDATE selection SET sent=1 "
                                "WHERE user_id=? AND matchup=?", (u["id"], matchup))
                            con.commit()
                        
                        if u["tg_token"] and u["tg_chat_id"]:
                            if send_tg(u["tg_token"], u["tg_chat_id"], msg):
                                print(f"[SIGNAL + TG] {u['id'][:8]} {matchup} {decision} {ln['line']:.1f}")
                            else:
                                print(f"[SIGNAL] {u['id'][:8]} {matchup} (Fallo envio TG)")
                        else:
                            print(f"[SIGNAL] {u['id'][:8]} {matchup} (Guardada solo en Web)")
                            
            except Exception as e:
                print(f"[poll user {u['id'][:8]}] {e}")
    except Exception as e:
        print(f"[poll error] {e}")

# =========================
# FASTAPI APP
# =========================
class UserCfg(BaseModel):
    odds_key: str
    tg_token: str = ""
    tg_chat_id: str = ""

class Sel(BaseModel):
    matchups: list

app = FastAPI()

@app.on_event("startup")
def startup():
    init_db()
    sch = BackgroundScheduler()
    sch.add_job(poll, "interval", seconds=POLL_INTERVAL)
    sch.start()
    app.state.sch = sch

@app.post("/api/user/test")
def test_user(body: UserCfg):
    errors, tg_user = [], None
    
    if body.tg_token:
        try:
            r = requests.get(f"https://api.telegram.org/bot{body.tg_token}/getMe", timeout=10)
            d = r.json()
            if r.status_code == 200 and d.get("ok"):
                tg_user = d["result"]["username"]
            else:
                errors.append(f"Telegram invalido ({r.status_code})")
        except Exception as e:
            errors.append(f"Telegram: {e}")

    remaining = None
    try:
        r = requests.get(
            "https://api.the-odds-api.com/v4/sports/basketball_nba/odds/",
            params={"apiKey": body.odds_key, "regions": "us", "markets": "totals"},
            timeout=15)
        if r.status_code == 200:
            remaining = r.headers.get("x-requests-remaining")
        else:
            errors.append(f"TheOddsAPI {r.status_code}")
    except Exception as e:
        errors.append(f"TheOddsAPI: {e}")

    return {"ok": not errors, "errors": errors, "tg_user": tg_user, "odds_remaining": remaining}

@app.post("/api/user")
def save_user(body: UserCfg, x_user_id: str = Header(None)):
    if not x_user_id:
        raise HTTPException(401, "No user id")
    with db() as con:
        con.execute(
            "INSERT INTO users(id, odds_key, tg_token, tg_chat_id, created_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "odds_key=excluded.odds_key, tg_token=excluded.tg_token, "
            "tg_chat_id=excluded.tg_chat_id",
            (x_user_id, body.odds_key, body.tg_token, body.tg_chat_id,
             datetime.now().isoformat()))
        con.commit()
    return {"ok": True}

@app.get("/api/user")
def get_user(x_user_id: str = Header(None)):
    if not x_user_id:
        raise HTTPException(401, "No user id")
    with db() as con:
        u = con.execute("SELECT * FROM users WHERE id=?", (x_user_id,)).fetchone()
    if not u:
        raise HTTPException(404, "not configured")
    def mask(s):
        return (s[:4] + "..." + s[-4:]) if s and len(s) >= 10 else "***"
    return {"configured": True, "odds_key": mask(u["odds_key"]),
            "tg_token": mask(u["tg_token"]), "tg_chat_id": u["tg_chat_id"],
            "active": bool(u["active"]), "auto_mode": bool(u["auto_mode"])}

@app.post("/api/user/toggle")
def toggle_user(x_user_id: str = Header(None)):
    if not x_user_id:
        raise HTTPException(401, "No user id")
    with db() as con:
        u = con.execute("SELECT active FROM users WHERE id=?", (x_user_id,)).fetchone()
        if not u:
            raise HTTPException(404, "not configured")
        new_state = 0 if u["active"] else 1
        con.execute("UPDATE users SET active=? WHERE id=?", (new_state, x_user_id))
        con.commit()
    return {"active": bool(new_state)}

@app.post("/api/user/toggle_auto")
def toggle_auto(x_user_id: str = Header(None)):
    if not x_user_id:
        raise HTTPException(401, "No user id")
    with db() as con:
        u = con.execute("SELECT auto_mode FROM users WHERE id=?", (x_user_id,)).fetchone()
        if not u:
            raise HTTPException(404, "not configured")
        new_state = 0 if u["auto_mode"] else 1
        con.execute("UPDATE users SET auto_mode=? WHERE id=?", (new_state, x_user_id))
        con.commit()
    return {"auto_mode": bool(new_state)}

@app.get("/api/games")
def games(x_user_id: str = Header(None)):
    if not x_user_id:
        raise HTTPException(401, "No user id")
    with db() as con:
        if not con.execute("SELECT id FROM users WHERE id=?", (x_user_id,)).fetchone():
            raise HTTPException(404, "not configured")
        sel = {r["matchup"]: dict(r) for r in con.execute(
            "SELECT * FROM selection WHERE user_id=?", (x_user_id,))}
    result = []
    for g in espn_games():
        s = sel.get(g["matchup"], {"active": 0, "sent": 0})
        g["selected"] = bool(s.get("active", 0))
        g["sent"] = bool(s.get("sent", 0))
        result.append(g)
    return result

@app.post("/api/selection")
def set_selection(body: Sel, x_user_id: str = Header(None)):
    if not x_user_id:
        raise HTTPException(401, "No user id")
    with db() as con:
        if not con.execute("SELECT id FROM users WHERE id=?", (x_user_id,)).fetchone():
            raise HTTPException(404, "not configured")
        con.execute("UPDATE selection SET active=0 WHERE user_id=?", (x_user_id,))
        for m in body.matchups:
            row = con.execute(
                "SELECT matchup FROM selection WHERE user_id=? AND matchup=?",
                (x_user_id, m)).fetchone()
            if row:
                con.execute(
                    "UPDATE selection SET active=1, sent=0 "
                    "WHERE user_id=? AND matchup=?", (x_user_id, m))
            else:
                con.execute(
                    "INSERT INTO selection(user_id, matchup, active, sent) "
                    "VALUES(?,?,1,0)", (x_user_id, m))
        con.commit()
    return {"ok": True, "selected": body.matchups}

@app.get("/api/signals")
def signals(x_user_id: str = Header(None)):
    if not x_user_id:
        raise HTTPException(401, "No user id")
    with db() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM signals WHERE user_id=? ORDER BY id DESC LIMIT 50",
            (x_user_id,))]
    return rows

@app.get("/", response_class=HTMLResponse)
def index():
    return HTML_PAGE

HTML_PAGE = r"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="NBA Signals">
<title>NBA Signals</title>
<style>
  :root{--bg:#0f172a;--card:#1e293b;--txt:#e2e8f0;--muted:#94a3b8;--blue:#3b82f6;--green:#22c55e;--red:#ef4444;--purple:#8b5cf6;}
  *{box-sizing:border-box;}
  body{font-family:-apple-system,system-ui;margin:0;padding:16px;background:var(--bg);color:var(--txt);}
  header{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;}
  h1{font-size:20px;margin:0;}
  h2{font-size:13px;margin:24px 0 8px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;}
  .icon{background:none;border:none;font-size:22px;cursor:pointer;padding:4px 8px;color:var(--txt);}
  .card{background:var(--card);padding:12px;border-radius:10px;margin-bottom:8px;}
  .game{display:flex;align-items:center;gap:12px;cursor:pointer;}
  .game input{width:20px;height:20px;flex-shrink:0;margin:0;}
  .meta{font-size:12px;color:var(--muted);margin-top:2px;}
  .signal{border-left:4px solid var(--green);}
  .signal.under{border-left-color:var(--red);}
  button.primary{background:var(--blue);color:#fff;border:none;padding:14px;border-radius:8px;font-size:16px;width:100%;margin-top:12px;font-weight:600;cursor:pointer;}
  button.primary:disabled{background:#475569;}
  button.secondary{background:#334155;color:#fff;border:none;padding:10px;border-radius:8px;font-size:14px;width:100%;margin-top:8px;cursor:pointer;}
  input.field{width:100%;padding:12px;border-radius:8px;border:1px solid #334155;background:#0f172a;color:var(--txt);font-size:14px;margin-bottom:8px;}
  .hidden{display:none !important;}
  .empty{color:#64748b;font-size:13px;padding:12px;text-align:center;}
  .msg{font-size:13px;margin-top:8px;padding:8px;border-radius:6px;}
  .msg.ok{background:#14532d;color:#86efac;}
  .msg.err{background:#7f1d1d;color:#fca5a5;}
  .hint{font-size:12px;color:var(--muted);margin-bottom:8px;}
  a.token-link{color:var(--blue);font-size:12px;word-break:break-all;}
</style>
</head><body>

<header>
  <h1>NBA Signals</h1>
  <button class="icon" onclick="toggleSettings()">&#9881;</button>
</header>

<div id="settings" class="hidden">
  <div class="card">
    <h2>Tus credenciales</h2>
    <p class="hint">Cada usuario usa sus propias claves. Se guardan en este servidor.</p>
    <label class="hint">TheOddsAPI key (Obligatoria)</label>
    <input class="field" id="oddsKey" placeholder="ej: 8685233a768b4d44...">
    <label class="hint">Telegram bot token (Opcional)</label>
    <input class="field" id="tgToken" placeholder="ej: 7726757580:AAH...">
    <label class="hint">Telegram chat ID (Opcional)</label>
    <input class="field" id="tgChat" placeholder="ej: 1058217995">
    <button class="primary" onclick="saveUser()">Guardar credenciales</button>
    <button class="secondary" onclick="testUser()">Probar credenciales</button>
    <div id="settingsMsg"></div>
    <hr style="border-color:#334155;margin:16px 0;">
    <p class="hint">Para usar tu cuenta en otro dispositivo, abre este enlace:</p>
    <a class="token-link" id="tokenLink" href="#">-</a>
  </div>
</div>

<div id="main" class="hidden">
  <button id="vigilanciaBtn" class="primary" style="background:#ef4444; margin-bottom: 8px;" onclick="toggleVigilancia()">
    🔴 VIGILANCIA APAGADA (Haz clic para encender)
  </button>
  <button id="autoBtn" class="primary" style="background:#475569; margin-bottom: 20px;" onclick="toggleAuto()">
    🎯 PILOTO AUTOMÁTICO APAGADO (Solo HT & Fin Q3)
  </button>
  <h2>Partidos (Modo Manual)</h2>
  <div id="games" class="empty">Cargando...</div>
  <button class="primary" id="saveBtn" onclick="saveSel()">Guardar seleccion</button>

  <h2>Senales</h2>
  <div id="signals" class="empty">Sin senales aun</div>
</div>

<script>
let userId = null;

function getOrCreateUserId() {
  const params = new URLSearchParams(window.location.search);
  const fromUrl = params.get('u');
  if (fromUrl) {
    localStorage.setItem('nba_user_id', fromUrl);
    window.history.replaceState({}, '', '/');
    return fromUrl;
  }
  let id = localStorage.getItem('nba_user_id');
  if (!id) {
    id = (crypto.randomUUID ? crypto.randomUUID() : ([1e7]+-1e3+-4e3+-8e3+-1e11).replace(/[018]/g, c =>
      (c ^ crypto.getRandomValues(new Uint8Array(1))[0] & 15 >> c / 4).toString(16))).replace(/-/g,'');
    localStorage.setItem('nba_user_id', id);
  }
  return id;
}

async function api(path, opts) {
  opts = opts || {};
  const headers = Object.assign({}, opts.headers || {}, {'X-User-Id': userId});
  return fetch(path, Object.assign({}, opts, {headers: headers}));
}

function toggleSettings() {
  document.getElementById('settings').classList.toggle('hidden');
}

function escapeAttr(s){
  return String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;');
}

async function loadUser() {
  try {
    const r = await api('/api/user');
    if (r.status === 404) {
      document.getElementById('settings').classList.remove('hidden');
      document.getElementById('main').classList.add('hidden');
      return false;
    }
    if (r.status === 200) {
      const data = await r.json();
      document.getElementById('oddsKey').value = '';
      document.getElementById('tgToken').value = '';
      document.getElementById('tgChat').value = data.tg_chat_id || '';
      document.getElementById('oddsKey').placeholder = 'Guardada: ' + data.odds_key;
      document.getElementById('tgToken').placeholder = 'Guardado: ' + data.tg_token;
      
      const btn = document.getElementById('vigilanciaBtn');
      if (data.active) {
        btn.style.background = '#22c55e';
        btn.innerHTML = '🟢 VIGILANCIA ENCENDIDA (Haz clic para apagar)';
      } else {
        btn.style.background = '#ef4444';
        btn.innerHTML = '🔴 VIGILANCIA APAGADA (Haz clic para encender)';
      }

      const autoBtn = document.getElementById('autoBtn');
      if (data.auto_mode) {
        autoBtn.style.background = '#8b5cf6';
        autoBtn.innerHTML = '🎯 PILOTO AUTOMÁTICO ENCENDIDO (Solo HT & Fin Q3)';
      } else {
        autoBtn.style.background = '#475569';
        autoBtn.innerHTML = '🎯 PILOTO AUTOMÁTICO APAGADO (Solo HT & Fin Q3)';
      }

      document.getElementById('main').classList.remove('hidden');
      return true;
    }
  } catch (e) { console.error(e); }
  return false;
}

async function saveUser() {
  const odds = document.getElementById('oddsKey').value.trim();
  const tk = document.getElementById('tgToken').value.trim();
  const chat = document.getElementById('tgChat').value.trim();
  const msg = document.getElementById('settingsMsg');
  
  if (!odds) {
    msg.className = 'msg err';
    msg.textContent = 'La clave de TheOddsAPI es obligatoria';
    return;
  }
  if ((tk && !chat) || (!tk && chat)) {
    msg.className = 'msg err';
    msg.textContent = 'Si usas Telegram, debes llenar Token y Chat ID';
    return;
  }

  const r = await api('/api/user', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({odds_key: odds, tg_token: tk, tg_chat_id: chat})
  });
  if (r.ok) {
    msg.className = 'msg ok';
    msg.textContent = 'Guardado. Probando...';
    await testUser();
    await loadUser();
    loadGames();
  } else {
    msg.className = 'msg err';
    msg.textContent = 'Error guardando';
  }
}

async function testUser() {
  const odds = document.getElementById('oddsKey').value.trim();
  const tk = document.getElementById('tgToken').value.trim();
  const chat = document.getElementById('tgChat').value.trim();
  const msg = document.getElementById('settingsMsg');
  
  if (!odds) {
    msg.className = 'msg err';
    msg.textContent = 'La clave de TheOddsAPI es obligatoria';
    return;
  }
  
  msg.className = 'msg';
  msg.textContent = 'Probando...';
  const r = await api('/api/user/test', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({odds_key: odds, tg_token: tk, tg_chat_id: chat})
  });
  const d = await r.json();
  if (d.ok) {
    msg.className = 'msg ok';
    let extra = d.tg_user ? '<br>Bot: @' + d.tg_user : '<br>Telegram: No configurado';
    msg.innerHTML = 'OK' + extra + '<br>Creditos restantes: ' + d.odds_remaining;
  } else {
    msg.className = 'msg err';
    msg.textContent = 'Error: ' + d.errors.join(' | ');
  }
}

let selected = new Set();

async function toggleVigilancia() {
  const btn = document.getElementById('vigilanciaBtn');
  btn.disabled = true;
  btn.innerHTML = 'Cambiando...';
  try {
    const r = await api('/api/user/toggle', {method: 'POST'});
    const d = await r.json();
    if (d.active) {
      btn.style.background = '#22c55e';
      btn.innerHTML = '🟢 VIGILANCIA ENCENDIDA (Haz clic para apagar)';
    } else {
      btn.style.background = '#ef4444';
      btn.innerHTML = '🔴 VIGILANCIA APAGADA (Haz clic para encender)';
    }
  } catch (e) {
    console.error(e);
    btn.innerHTML = 'Error al cambiar';
  }
  btn.disabled = false;
}

async function toggleAuto() {
  const btn = document.getElementById('autoBtn');
  btn.disabled = true;
  btn.innerHTML = 'Cambiando...';
  try {
    const r = await api('/api/user/toggle_auto', {method: 'POST'});
    const d = await r.json();
    if (d.auto_mode) {
      btn.style.background = '#8b5cf6';
      btn.innerHTML = '🎯 PILOTO AUTOMÁTICO ENCENDIDO (Solo HT & Fin Q3)';
    } else {
      btn.style.background = '#475569';
      btn.innerHTML = '🎯 PILOTO AUTOMÁTICO APAGADO (Solo HT & Fin Q3)';
    }
  } catch (e) {
    console.error(e);
    btn.innerHTML = 'Error al cambiar';
  }
  btn.disabled = false;
}

async function loadGames() {
  try {
    const r = await api('/api/games');
    if (!r.ok) return;
    const games = await r.json();
    const div = document.getElementById('games');
    if (!games.length) {
      div.className = 'empty';
      div.innerHTML = 'No hay partidos ahora';
      return;
    }
    div.className = '';
    selected = new Set(games.filter(function(g){ return g.selected; }).map(function(g){ return g.matchup; }));
    div.innerHTML = games.map(function(g){
      const safe = g.matchup.replace(/[^a-z0-9]/gi, '_');
      const checked = g.selected ? 'checked' : '';
      const badge = g.sent ? ' - YA ENVIADA' : '';
      return `<div class="card">
        <label class="game">
          <input type="checkbox" id="${safe}" ${checked} data-m="${escapeAttr(g.matchup)}"
            onchange="toggle(this.getAttribute('data-m'), this.checked)">
          <div>
            <div>${g.matchup}</div>
            <div class="meta">${g.status} - P${g.period} - ${g.total} pts${badge}</div>
          </div>
        </label>
      </div>`;
    }).join('');
  } catch (e) { console.error(e); }
}

function toggle(m, on) {
  if (on) selected.add(m); else selected.delete(m);
}

async function saveSel() {
  const btn = document.getElementById('saveBtn');
  btn.disabled = true; btn.textContent = 'Guardando...';
  await api('/api/selection', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({matchups: Array.from(selected)})
  });
  btn.disabled = false; btn.textContent = 'Guardar seleccion';
  loadGames();
}

async function loadSignals() {
  try {
    const r = await api('/api/signals');
    if (!r.ok) return;
    const sigs = await r.json();
    const div = document.getElementById('signals');
    if (!sigs.length) {
      div.className = 'empty';
      div.innerHTML = 'Sin senales aun';
      return;
    }
    div.className = '';
    div.innerHTML = sigs.map(function(s){
      return `<div class="card signal ${s.decision.toLowerCase()}">
        <div><b>${s.matchup}</b></div>
        <div>${s.decision} ${s.line.toFixed(1)} @ ${s.cuota ? s.cuota.toFixed(2) : 'N/A'}</div>
        <div class="meta">Proyeccion ${s.proyeccion.toFixed(1)} - Diff ${s.diff > 0 ? '+' : ''}${s.diff.toFixed(1)} - Conf ${s.confianza}%</div>
        <div class="meta">${new Date(s.created_at).toLocaleString()}</div>
      </div>`;
    }).join('');
  } catch (e) {}
}

async function init() {
  userId = getOrCreateUserId();
  document.getElementById('tokenLink').href = window.location.origin + '/?u=' + userId;
  document.getElementById('tokenLink').textContent = window.location.origin + '/?u=' + userId;
  const configured = await loadUser();
  if (configured) {
    loadGames();
    loadSignals();
    setInterval(loadGames, 30000);
    setInterval(loadSignals, 15000);
  }
}

init();
</script>
</body></html>
"""

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
