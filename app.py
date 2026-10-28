# -*- coding: utf-8 -*-
"""
Bot NBA - Multi-usuario (InferSports PRO + Respaldo + Garbage Time + Recency Bias + Anchor Line + FIX Halftime)
"""
import os, sqlite3, requests
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from apscheduler.schedulers.background import BackgroundScheduler
import uvicorn

POLL_INTERVAL = 120  # 2 minutos
MIN_DIFF = 7.0
DB = "nba.db"
ADMIN_PASS = "admin123"  # 👈 CAMBIA ESTO

# 🔑 CLAVES
INFER_API_KEY = "isk_sbSsZl0rFrEbscbN0NHb4_K_qEwfF5Kl" 
THEODDS_API_KEY = "2cc2dfc1b911c94ebd39d2909668831c"

def db():
    con = sqlite3.connect(DB, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id TEXT PRIMARY KEY,
            odds_key TEXT DEFAULT "",
            tg_token TEXT DEFAULT "",
            tg_chat_id TEXT DEFAULT "",
            active INTEGER DEFAULT 0,
            auto_mode INTEGER DEFAULT 0,
            is_blocked INTEGER DEFAULT 0,
            created_at TEXT);
        CREATE TABLE IF NOT EXISTS selection(
            user_id TEXT, matchup TEXT, active INTEGER, sent INTEGER DEFAULT 0,
            PRIMARY KEY(user_id, matchup));
        CREATE TABLE IF NOT EXISTS signals(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT, matchup TEXT, decision TEXT, line REAL, cuota REAL,
            proyeccion REAL, diff REAL, confianza INTEGER,
            total INTEGER, period INTEGER, created_at TEXT,
            status TEXT DEFAULT 'PENDING',
            final_total INTEGER);
        CREATE TABLE IF NOT EXISTS anchors(
            matchup TEXT PRIMARY KEY,
            initial_line REAL);
        """)
        for col in ["auto_mode INTEGER DEFAULT 0", "is_blocked INTEGER DEFAULT 0"]:
            try: con.execute(f"ALTER TABLE users ADD COLUMN {col}")
            except: pass
        try: con.execute("ALTER TABLE signals ADD COLUMN status TEXT DEFAULT 'PENDING'")
        except: pass
        try: con.execute("ALTER TABLE signals ADD COLUMN final_total INTEGER")
        except: pass

def espn_games():
    try:
        r = requests.get("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard", timeout=15)
        out = []
        for ev in r.json().get("events", []):
            comp = ev.get("competitions", [{}])[0]
            cs = comp.get("competitors", [])
            if len(cs) < 2: continue
            home = next((c for c in cs if c.get("homeAway") == "home"), cs[0])
            away = next((c for c in cs if c.get("homeAway") == "away"), cs[1])
            ht = home.get("team", {}).get("displayName", "Home")
            at = away.get("team", {}).get("displayName", "Away")
            st = ev.get("status", {})
            
            q_home = home.get("linescores", [])
            q_away = away.get("linescores", [])
            home_score = sum(int(q.get("value", 0)) for q in q_home)
            away_score = sum(int(q.get("value", 0)) for q in q_away)
            
            out.append({
                "matchup": f"{at} vs {ht}",
                "status": st.get("type", {}).get("description", ""),
                "state": st.get("type", {}).get("state", ""),
                "period": int(st.get("period", 0) or 0),
                "clock": st.get("displayClock", "") or "",
                "total": home_score + away_score,
                "home_score": home_score,
                "away_score": away_score,
                "q_home": q_home,
                "q_away": q_away,
            })
        return out
    except Exception as e:
        print(f"[espn] {e}")
        return []

def get_anchor_line(con, matchup, current_line):
    row = con.execute("SELECT initial_line FROM anchors WHERE matchup=?", (matchup,)).fetchone()
    if row:
        return row["initial_line"]
    else:
        con.execute("INSERT INTO anchors(matchup, initial_line) VALUES(?,?)", (matchup, current_line))
        con.commit()
        return current_line

def odds_totals_infer():
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json',
            'Authorization': f'Bearer {INFER_API_KEY}'
        }
        r = requests.get("https://api.infersports.dev/v1/events/live", headers=headers, timeout=15)
        if r.status_code != 200: return {}
        
        data = r.json()
        eventos_infer = [ev for ev in data.get("data", []) if ev.get("sport") == "basketball" and ev.get("league", {}).get("name") == "NBA"]
        
        odds_dict = {}
        for ev in eventos_infer:
            event_id = ev["id"]
            home_short = ev["home_team"]["name"].split(".")[-1].strip()
            away_short = ev["away_team"]["name"].split(".")[-1].strip()
            
            r_odds = requests.get(f"https://api.infersports.dev/v1/events/{event_id}/odds?format=decimal", headers=headers, timeout=15)
            if r_odds.status_code != 200: continue
                
            odds_data = r_odds.json()
            mejor_linea = None
            mejor_cuota_over = None
            mejor_cuota_under = None
            mejor_time = None
            
            for odd in odds_data.get("odds", []):
                if odd.get("market_type") == "totals" and odd.get("period") == "full_time" and odd.get("status") == "open":
                    as_of_str = odd.get("as_of")
                    try: as_of_time = datetime.fromisoformat(as_of_str.replace('Z', '+00:00'))
                    except: continue
                        
                    if mejor_time is None or as_of_time > mejor_time:
                        mejor_time = as_of_time
                        mejor_linea = odd.get("line")
                        mejor_cuota_over = odd["prices"].get("over")
                        mejor_cuota_under = odd["prices"].get("under")
            
            if mejor_linea is not None:
                now = datetime.now(timezone.utc)
                age_min = (now - mejor_time).total_seconds() / 60.0
                odds_dict[f"{away_short} vs {home_short}"] = {
                    "line": float(mejor_linea),
                    "over": float(mejor_cuota_over),
                    "under": float(mejor_cuota_under),
                    "bookmaker": "InferSports",
                    "age_min": age_min
                }
        return odds_dict
    except Exception as e:
        print(f"[odds infer error] {e}")
        return {}

def odds_totals_theoddsapi(api_key):
    try:
        r = requests.get("https://api.the-odds-api.com/v4/sports/basketball_nba/odds/", 
            params={"apiKey": api_key, "regions": "us", "markets": "totals", "oddsFormat": "decimal"}, timeout=15)
        if r.status_code != 200: return {}
        out = {}
        for m in r.json():
            matchup = f"{m['away_team']} vs {m['home_team']}"
            bm = next((b for b in m.get("bookmakers", []) if b.get("key") == "pinnacle"), None) or (m.get("bookmakers") or [None])[0]
            if not bm: continue
            mk = next((x for x in bm.get("markets", []) if x.get("key") == "totals"), None)
            if not mk: continue
            line = over = under = None
            for o in mk.get("outcomes", []):
                if "point" in o and "price" in o:
                    line = float(o["point"])
                    if o.get("name", "").lower() == "over": over = float(o["price"])
                    elif o.get("name", "").lower() == "under": under = float(o["price"])
            if line is not None:
                out[matchup] = {"line": line, "over": over, "under": under, "bookmaker": bm.get("title", "TheOddsAPI"), "age_min": 0.0}
        return out
    except Exception as e:
        print(f"[TheOddsAPI error] {e}")
        return {}

def minutes_played(period, clock, status=""):
    if period == 0: return 0.0
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
                if ml > 12.0: ml = ml / 60.0
            plen = 12.0 if period <= 4 else 5.0
            mins += max(0.0, plen - ml)
        except Exception: pass
    return mins

def project_total_hibrida(total, minutes, period, status, q_home, q_away, anchor_line):
    if minutes <= 0.1: return None
    
    # 1. Calcular cuartos completados correctamente
    completed_quarters = max(0, period - 1)
    if "Half" in status or "Halftime" in status:
        completed_quarters = 2
    elif "End" in status:
        completed_quarters = period
        
    q_pts = []
    for i in range(min(completed_quarters, len(q_home), len(q_away))):
        try: q_pts.append(int(q_home[i].get("value", 0)) + int(q_away[i].get("value", 0)))
        except: pass
        
    # 2. Calcular Proyección en Vivo con Recency Bias
    if q_pts:
        weights = list(range(1, len(q_pts) + 1))
        weighted_sum = sum(p * w for p, w in zip(q_pts, weights))
        total_weight = sum(weights)
        avg_weighted = weighted_sum / total_weight
        cuartos_restantes = max(0, 4 - len(q_pts))
        live_proj = total + (avg_weighted * cuartos_restantes)
    else:
        total_min = 48.0 if period <= 4 else 48.0 + (period - 4) * 5.0
        live_proj = (total / minutes) * total_min

    # 3. Mezclar con la Línea Ancla
    weight_live = min(1.0, max(0.0, minutes / 48.0)) 
    final_proj = (live_proj * weight_live) + (anchor_line * (1.0 - weight_live))
    
    return final_proj

def send_tg(token, chat_id, msg):
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"}, timeout=15)
        return r.status_code == 200
    except: return False

def format_signal(matchup, g, ln, decision, proj, diff, conf, auto=False):
    arrow = "⬆️ OVER" if decision == "OVER" else "⬇️ UNDER"
    cuota = ln["over"] if decision == "OVER" else ln["under"]
    cuota_str = f"{cuota:.2f}" if cuota else "N/A"
    color = "🟢" if decision == "OVER" else "🔴"
    modo = "AUTO" if auto else "MANUAL"
    
    return (
        f"📡 <b>SEÑAL EN VIVO — NBA ({modo})</b>\n\n"
        f"🏀 <b>{matchup}</b>\n"
        f"📌 <b>Línea ({ln['bookmaker']}):</b> {ln['line']:.1f}\n"
        f"🔄 <b>Actualizada:</b> hace {ln.get('age_min', 0):.1f} min\n"
        f"⏱️ <b>Estado:</b> P{g['period']} ({g['clock']})\n\n"
        f"📈 <b>Proyección final:</b> {proj:.1f}\n"
        f"🔻 <b>Diferencia:</b> {diff:+.1f} pts\n"
        f"💡 <b>Recomendación:</b> <b>{arrow} {ln['line']:.1f}</b>\n"
        f"🪙 <b>Cuota @{cuota_str}</b>\n"
        f"{color} <b>Confianza:</b> {conf}%\n\n"
        f"🕑 <i>Señal generada automáticamente.</i>"
    )

def check_results():
    try:
        with db() as con:
            pending = con.execute("SELECT * FROM signals WHERE status='PENDING'").fetchall()
        if not pending: return
        games = {g["matchup"]: g for g in espn_games()}
        for sig in pending:
            g = games.get(sig["matchup"])
            if g and g["state"] == "post":
                final_total = g["total"]
                line = sig["line"]
                decision = sig["decision"]
                status = "PUSH"
                if decision == "OVER":
                    if final_total > line: status = "WON"
                    elif final_total < line: status = "LOST"
                elif decision == "UNDER":
                    if final_total < line: status = "WON"
                    elif final_total > line: status = "LOST"
                with db() as con:
                    con.execute("UPDATE signals SET status=?, final_total=? WHERE id=?", (status, final_total, sig["id"]))
                    con.commit()
                print(f"[RESULT] {sig['matchup']} {decision} {line} -> Final {final_total} ({status})")
    except Exception as e:
        print(f"[check_results error] {e}")

def poll():
    try:
        with db() as con:
            users = con.execute("SELECT * FROM users WHERE (active = 1 OR auto_mode = 1) AND is_blocked = 0").fetchall()
        if not users: return

        games = {g["matchup"]: g for g in espn_games()}
        if not games: return

        partidos_validos = []
        for g in games.values():
            is_ht = "Half" in g["status"]
            is_q3_q4_live = (g["period"] in (3, 4) and g["state"] == "in")
            
            if not (is_ht or is_q3_q4_live):
                continue

            # FILTRO ANTI GARBAGE TIME
            if g["period"] == 4 and g["state"] == "in":
                margen = abs(g["home_score"] - g["away_score"])
                min_faltan = 12.0
                try:
                    if ':' in g["clock"]:
                        mm, ss = g["clock"].split(":")
                        min_faltan = int(mm) + int(ss) / 60.0
                    else:
                        min_faltan = float(g["clock"])
                        if min_faltan > 12.0: min_faltan = min_faltan / 60.0
                except: pass
                
                if margen > 18 and min_faltan < 6.0:
                    print(f"[GARBAGE TIME] {g['matchup']} | Margen: {margen} pts | Faltan {min_faltan:.1f} min. Se ignora.")
                    continue
            
            partidos_validos.append(g)
                
        if not partidos_validos: return

        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] --- INICIANDO REVISIÓN ({len(partidos_validos)} partidos válidos) ---")
        
        infer_odds = odds_totals_infer()
        
        matched_odds = {}
        for g in partidos_validos:
            real_match = g["matchup"]
            for short_match, ln in infer_odds.items():
                away_short = short_match.split(" vs ")[0].split()[-1].lower()
                home_short = short_match.split(" vs ")[1].split()[-1].lower()
                real_lower = real_match.lower()
                if away_short in real_lower and home_short in real_lower:
                    matched_odds[real_match] = ln
                    break
        
        faltantes = [g for g in partidos_validos if g["matchup"] not in matched_odds]
        if faltantes:
            print(f"[PLAN B] InferSports falló o no tiene cuotas para {len(faltantes)} partidos. Usando TheOddsAPI...")
            api_key = users[0]["odds_key"] if users[0]["odds_key"] else THEODDS_API_KEY
            theodds_data = odds_totals_theoddsapi(api_key)
            
            for g in faltantes:
                matchup = g["matchup"]
                if matchup in theodds_data:
                    matched_odds[matchup] = theodds_data[matchup]
                    print(f"[PLAN B OK] Cuota de respaldo obtenida para {matchup}")

        if not matched_odds:
            print("[POLL] No se pudo obtener cuotas de ninguna fuente.")
            return

        print(f"[POLL] Cuotas totales obtenidas: {len(matched_odds)}")

        for u in users:
            try:
                if u["auto_mode"]:
                    for g in partidos_validos:
                        matchup = g["matchup"]
                        ln = matched_odds.get(matchup)
                        if not ln: continue
                        
                        with db() as con:
                            already_sent = con.execute("SELECT id FROM signals WHERE user_id=? AND matchup=?", (u["id"], matchup)).fetchone()
                        if already_sent: continue
                            
                        mins = minutes_played(g["period"], g["clock"], g["status"])
                        
                        with db() as con:
                            anchor = get_anchor_line(con, matchup, ln["line"])
                            
                        # PASAMOS EL STATUS A LA FUNCIÓN CORREGIDA
                        proj = project_total_hibrida(g["total"], mins, g["period"], g["status"], g["q_home"], g["q_away"], anchor)
                        
                        if proj is None: continue
                        diff = proj - ln["line"]
                        
                        print(f"[EVAL AUTO] {matchup} | Anchor: {anchor:.1f} | Proy: {proj:.1f} vs Línea: {ln['line']:.1f} | Diff: {diff:+.1f}")
                        if abs(diff) < MIN_DIFF: continue

                        decision = "OVER" if diff > 0 else "UNDER"
                        cuota = ln["over"] if decision == "OVER" else ln["under"]
                        conf = min(99, max(10, int((abs(diff) / 15) * 100 * (mins / 48))))
                        msg = format_signal(matchup, g, ln, decision, proj, diff, conf, auto=True)

                        with db() as con:
                            con.execute("INSERT INTO signals(user_id,matchup,decision,line,cuota,proyeccion,diff,confianza,total,period,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                (u["id"], matchup, decision, ln["line"], cuota, proj, diff, conf, g["total"], g["period"], datetime.now().isoformat()))
                            con.commit()
                        
                        if u["tg_token"] and u["tg_chat_id"]:
                            if send_tg(u["tg_token"], u["tg_chat_id"], msg):
                                print(f"[AUTO SIGNAL + TG] {u['id'][:8]} {matchup} {decision} {ln['line']:.1f}")
                        else:
                            print(f"[AUTO SIGNAL] {u['id'][:8]} {matchup} {decision} {ln['line']:.1f} (Web)")
                else:
                    with db() as con:
                        sel = [r["matchup"] for r in con.execute("SELECT matchup FROM selection WHERE user_id=? AND active=1 AND sent=0", (u["id"],))]
                    
                    live = [m for m in sel if m in games and games[m]["state"] == "in" and "Half" not in games[m]["status"] and "End" not in games[m]["status"]]
                    if not live: continue

                    for matchup in live:
                        g = games[matchup]
                        ln = matched_odds.get(matchup)
                        if not ln: continue
                            
                        with db() as con:
                            already_sent = con.execute("SELECT id FROM signals WHERE user_id=? AND matchup=?", (u["id"], matchup)).fetchone()
                        if already_sent:
                            with db() as con:
                                con.execute("UPDATE selection SET sent=1 WHERE user_id=? AND matchup=?", (u["id"], matchup))
                                con.commit()
                            continue
                            
                        mins = minutes_played(g["period"], g["clock"], g["status"])
                        
                        with db() as con:
                            anchor = get_anchor_line(con, matchup, ln["line"])
                            
                        proj = project_total_hibrida(g["total"], mins, g["period"], g["status"], g["q_home"], g["q_away"], anchor)
                        
                        if proj is None: continue
                        diff = proj - ln["line"]
                        
                        print(f"[EVAL MANUAL] {matchup} | Anchor: {anchor:.1f} | Proy: {proj:.1f} vs Línea: {ln['line']:.1f} | Diff: {diff:+.1f}")
                        if abs(diff) < MIN_DIFF: continue

                        decision = "OVER" if diff > 0 else "UNDER"
                        cuota = ln["over"] if decision == "OVER" else ln["under"]
                        conf = min(99, max(10, int((abs(diff) / 15) * 100 * (mins / 48))))
                        msg = format_signal(matchup, g, ln, decision, proj, diff, conf, auto=False)

                        with db() as con:
                            con.execute("INSERT INTO signals(user_id,matchup,decision,line,cuota,proyeccion,diff,confianza,total,period,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                (u["id"], matchup, decision, ln["line"], cuota, proj, diff, conf, g["total"], g["period"], datetime.now().isoformat()))
                            con.execute("UPDATE selection SET sent=1 WHERE user_id=? AND matchup=?", (u["id"], matchup))
                            con.commit()
                        
                        if u["tg_token"] and u["tg_chat_id"]:
                            if send_tg(u["tg_token"], u["tg_chat_id"], msg):
                                print(f"[SIGNAL + TG] {u['id'][:8]} {matchup} {decision} {ln['line']:.1f}")
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
    odds_key: str = ""
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
    sch.add_job(check_results, "interval", seconds=300)
    sch.start()
    app.state.sch = sch

@app.post("/api/user/test")
def test_user(body: UserCfg):
    errors = []
    if body.tg_token:
        try:
            r = requests.get(f"https://api.telegram.org/bot{body.tg_token}/getMe", timeout=10)
            if r.status_code != 200: errors.append("Telegram inválido")
        except: errors.append("Error de Telegram")
    return {"ok": not errors, "errors": errors, "tg_user": "OK", "odds_remaining": "InferSports PRO + Respaldo"}

@app.post("/api/user")
def save_user(body: UserCfg, x_user_id: str = Header(None)):
    if not x_user_id: raise HTTPException(401, "No user id")
    with db() as con:
        existing = con.execute("SELECT is_blocked FROM users WHERE id=?", (x_user_id,)).fetchone()
        if existing and existing["is_blocked"]: raise HTTPException(403, "Bloqueado")
        con.execute("INSERT INTO users(id, odds_key, tg_token, tg_chat_id, created_at) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET tg_token=excluded.tg_token, tg_chat_id=excluded.tg_chat_id",
            (x_user_id, body.odds_key, body.tg_token, body.tg_chat_id, datetime.now().isoformat()))
        con.commit()
    return {"ok": True}

@app.get("/api/user")
def get_user(x_user_id: str = Header(None)):
    if not x_user_id: raise HTTPException(401, "No user id")
    with db() as con:
        u = con.execute("SELECT * FROM users WHERE id=?", (x_user_id,)).fetchone()
    if not u: raise HTTPException(404, "not configured")
    if u["is_blocked"]: raise HTTPException(403, "Bloqueado")
    def mask(s): return (s[:4] + "..." + s[-4:]) if s and len(s) >= 10 else "***"
    return {"configured": True, "odds_key": mask(u["odds_key"]), "tg_token": mask(u["tg_token"]), "tg_chat_id": u["tg_chat_id"], "active": bool(u["active"]), "auto_mode": bool(u["auto_mode"])}

@app.post("/api/user/toggle")
def toggle_user(x_user_id: str = Header(None)):
    if not x_user_id: raise HTTPException(401, "No user id")
    with db() as con:
        u = con.execute("SELECT active, is_blocked FROM users WHERE id=?", (x_user_id,)).fetchone()
        if not u or u["is_blocked"]: raise HTTPException(403, "Error")
        new_state = 0 if u["active"] else 1
        con.execute("UPDATE users SET active=? WHERE id=?", (new_state, x_user_id))
        con.commit()
    return {"active": bool(new_state)}

@app.post("/api/user/toggle_auto")
def toggle_auto(x_user_id: str = Header(None)):
    if not x_user_id: raise HTTPException(401, "No user id")
    with db() as con:
        u = con.execute("SELECT auto_mode, is_blocked FROM users WHERE id=?", (x_user_id,)).fetchone()
        if not u or u["is_blocked"]: raise HTTPException(403, "Error")
        new_state = 0 if u["auto_mode"] else 1
        con.execute("UPDATE users SET auto_mode=? WHERE id=?", (new_state, x_user_id))
        con.commit()
    return {"auto_mode": bool(new_state)}

@app.get("/api/games")
def games(x_user_id: str = Header(None)):
    if not x_user_id: raise HTTPException(401, "No user id")
    with db() as con:
        u = con.execute("SELECT is_blocked FROM users WHERE id=?", (x_user_id,)).fetchone()
        if not u or u["is_blocked"]: raise HTTPException(403, "Error")
        sel = {r["matchup"]: dict(r) for r in con.execute("SELECT * FROM selection WHERE user_id=?", (x_user_id,))}
    result = []
    for g in espn_games():
        s = sel.get(g["matchup"], {"active": 0, "sent": 0})
        g["selected"] = bool(s.get("active", 0))
        g["sent"] = bool(s.get("sent", 0))
        result.append(g)
    return result

@app.post("/api/selection")
def set_selection(body: Sel, x_user_id: str = Header(None)):
    if not x_user_id: raise HTTPException(401, "No user id")
    with db() as con:
        u = con.execute("SELECT is_blocked FROM users WHERE id=?", (x_user_id,)).fetchone()
        if not u or u["is_blocked"]: raise HTTPException(403, "Error")
        con.execute("UPDATE selection SET active=0 WHERE user_id=?", (x_user_id,))
        for m in body.matchups:
            row = con.execute("SELECT matchup FROM selection WHERE user_id=? AND matchup=?", (x_user_id, m)).fetchone()
            if row: con.execute("UPDATE selection SET active=1, sent=0 WHERE user_id=? AND matchup=?", (x_user_id, m))
            else: con.execute("INSERT INTO selection(user_id, matchup, active, sent) VALUES(?,?,1,0)", (x_user_id, m))
        con.commit()
    return {"ok": True, "selected": body.matchups}

@app.get("/api/signals")
def signals(x_user_id: str = Header(None)):
    if not x_user_id: raise HTTPException(401, "No user id")
    with db() as con:
        u = con.execute("SELECT is_blocked FROM users WHERE id=?", (x_user_id,)).fetchone()
        if u and u["is_blocked"]: raise HTTPException(403, "Bloqueado")
        rows = [dict(r) for r in con.execute("SELECT * FROM signals WHERE user_id=? ORDER BY id DESC LIMIT 50", (x_user_id,))]
    return rows

@app.get("/api/stats")
def get_stats(x_user_id: str = Header(None)):
    if not x_user_id: raise HTTPException(401, "No user id")
    with db() as con:
        res = con.execute("""
            SELECT COUNT(*) as total, SUM(CASE WHEN status='WON' THEN 1 ELSE 0 END) as won, SUM(CASE WHEN status='LOST' THEN 1 ELSE 0 END) as lost, SUM(CASE WHEN status='PUSH' THEN 1 ELSE 0 END) as push FROM signals WHERE user_id=?
        """, (x_user_id,)).fetchone()
    total_valid = (res["won"] or 0) + (res["lost"] or 0)
    win_rate = round(((res["won"] or 0) / total_valid * 100), 1) if total_valid > 0 else 0
    return {"total": res["total"] or 0, "won": res["won"] or 0, "lost": res["lost"] or 0, "push": res["push"] or 0, "win_rate": win_rate}

@app.get("/api/admin/users")
def admin_users(passwd: str = Query(None)):
    if passwd != ADMIN_PASS: raise HTTPException(401, "Contraseña incorrecta")
    with db() as con:
        users = [dict(r) for r in con.execute("SELECT * FROM users ORDER BY created_at DESC")]
    for u in users:
        u["odds_key"] = (u["odds_key"][:4] + "..." + u["odds_key"][-4:]) if len(u["odds_key"]) > 8 else "***"
        u["tg_token"] = (u["tg_token"][:4] + "..." + u["tg_token"][-4:]) if u["tg_token"] and len(u["tg_token"]) > 8 else "***"
    return users

@app.post("/api/admin/block/{user_id}")
def admin_block(user_id: str, passwd: str = Query(None)):
    if passwd != ADMIN_PASS: raise HTTPException(401, "Contraseña incorrecta")
    with db() as con:
        u = con.execute("SELECT is_blocked FROM users WHERE id=?", (user_id,)).fetchone()
        if not u: raise HTTPException(404, "No encontrado")
        new_state = 0 if u["is_blocked"] else 1
        con.execute("UPDATE users SET is_blocked=? WHERE id=?", (new_state, user_id))
        con.commit()
    return {"blocked": bool(new_state)}

@app.get("/admin", response_class=HTMLResponse)
def admin_page(passwd: str = Query(None)):
    if passwd != ADMIN_PASS: return HTMLResponse("<h1>401 - Acceso Denegado</h1>", status_code=401)
    return ADMIN_HTML

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
    <p class="hint">Ya no necesitas clave de TheOddsAPI, usamos InferSports (Gratis).</p>
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
    🎯 PILOTO AUTOMÁTICO APAGADO (HT, Q3 y Q4)
  </button>

  <h2>Rendimiento</h2>
  <div id="stats" class="empty">Cargando...</div>

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
    id = (crypto.randomUUID ? crypto.randomUUID() : ([1e7]+-1e3+-4e3+-8e3+-1e11).replace(/[018]/g, c => (c ^ crypto.getRandomValues(new Uint8Array(1))[0] & 15 >> c / 4).toString(16))).replace(/-/g,'');
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
    if (r.status === 404 || r.status === 403) {
      document.getElementById('settings').classList.remove('hidden');
      document.getElementById('main').classList.add('hidden');
      if (r.status === 403) {
          const msg = document.getElementById('settingsMsg');
          msg.className = 'msg err';
          msg.textContent = 'Tu cuenta ha sido bloqueada por el administrador.';
      }
      return false;
    }
    if (r.status === 200) {
      const data = await r.json();
      document.getElementById('tgToken').value = '';
      document.getElementById('tgChat').value = data.tg_chat_id || '';
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
        autoBtn.innerHTML = '🎯 PILOTO AUTOMÁTICO ENCENDIDO (HT, Q3 y Q4)';
      } else {
        autoBtn.style.background = '#475569';
        autoBtn.innerHTML = '🎯 PILOTO AUTOMÁTICO APAGADO (HT, Q3 y Q4)';
      }

      document.getElementById('main').classList.remove('hidden');
      return true;
    }
  } catch (e) { console.error(e); }
  return false;
}

async function saveUser() {
  const tk = document.getElementById('tgToken').value.trim();
  const chat = document.getElementById('tgChat').value.trim();
  const msg = document.getElementById('settingsMsg');
  
  if ((tk && !chat) || (!tk && chat)) {
    msg.className = 'msg err';
    msg.textContent = 'Si usas Telegram, debes llenar Token y Chat ID';
    return;
  }

  const r = await api('/api/user', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({tg_token: tk, tg_chat_id: chat})
  });
  if (r.ok) {
    msg.className = 'msg ok';
    msg.textContent = 'Guardado. Probando...';
    await testUser();
    await loadUser();
    loadGames();
    loadStats();
  } else {
    msg.className = 'msg err';
    msg.textContent = 'Error guardando (¿Estás bloqueado?)';
  }
}

async function testUser() {
  const tk = document.getElementById('tgToken').value.trim();
  const chat = document.getElementById('tgChat').value.trim();
  const msg = document.getElementById('settingsMsg');
  
  msg.className = 'msg';
  msg.textContent = 'Probando...';
  const r = await api('/api/user/test', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({tg_token: tk, tg_chat_id: chat})
  });
  const d = await r.json();
  if (d.ok) {
    msg.className = 'msg ok';
    let extra = d.tg_user ? '<br>Bot: @' + d.tg_user : '<br>Telegram: No configurado';
    msg.innerHTML = 'OK' + extra + '<br>API Cuotas: ' + d.odds_remaining;
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
  } catch (e) { console.error(e); }
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
      btn.innerHTML = '🎯 PILOTO AUTOMÁTICO ENCENDIDO (HT, Q3 y Q4)';
    } else {
      btn.style.background = '#475569';
      btn.innerHTML = '🎯 PILOTO AUTOMÁTICO APAGADO (HT, Q3 y Q4)';
    }
  } catch (e) { console.error(e); }
  btn.disabled = false;
}

async function loadStats() {
  try {
    const r = await api('/api/stats');
    if (!r.ok) return;
    const d = await r.json();
    const div = document.getElementById('stats');
    const color = d.win_rate >= 50 ? '#22c55e' : (d.total === 0 ? '#94a3b8' : '#ef4444');
    div.className = '';
    div.innerHTML = `
      <div class="card" style="text-align:center;">
        <div style="font-size:40px; font-weight:bold; color:${color};">${d.win_rate}%</div>
        <div style="color:#94a3b8; font-size:12px; text-transform:uppercase; letter-spacing:1px; margin-bottom:16px;">Tasa de Acierto</div>
        <div style="display:flex; justify-content:space-around; font-size:14px;">
          <div><span style="color:#22c55e;">✅</span> ${d.won} Ganadas</div>
          <div><span style="color:#ef4444;">❌</span> ${d.lost} Perdidas</div>
          <div><span style="color:#64748b;">⚖️</span> ${d.push} Empates</div>
        </div>
      </div>
    `;
  } catch (e) { console.error(e); }
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
      let statusBadge = '';
      if (s.status === 'WON') statusBadge = '<div style="color:#22c55e; font-weight:bold; margin-top:4px;">✅ GANADA (Total final: ' + s.final_total + ')</div>';
      else if (s.status === 'LOST') statusBadge = '<div style="color:#ef4444; font-weight:bold; margin-top:4px;">❌ PERDIDA (Total final: ' + s.final_total + ')</div>';
      else if (s.status === 'PUSH') statusBadge = '<div style="color:#64748b; font-weight:bold; margin-top:4px;">⚖️ EMPATE (Total final: ' + s.final_total + ')</div>';
      else statusBadge = '<div style="color:#eab308; font-weight:bold; margin-top:4px;">⏳ PENDIENTE</div>';

      return `<div class="card signal ${s.decision.toLowerCase()}">
        <div><b>${s.matchup}</b></div>
        <div>${s.decision} ${s.line.toFixed(1)} @ ${s.cuota ? s.cuota.toFixed(2) : 'N/A'}</div>
        <div class="meta">Proyeccion ${s.proyeccion.toFixed(1)} - Diff ${s.diff > 0 ? '+' : ''}${s.diff.toFixed(1)} - Conf ${s.confianza}%</div>
        ${statusBadge}
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
    loadStats();
    setInterval(loadGames, 30000);
    setInterval(loadSignals, 15000);
    setInterval(loadStats, 60000);
  }
}

init();
</script>
</body></html>
"""

ADMIN_HTML = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Admin</title><style>:root{--bg:#0f172a;--card:#1e293b;--txt:#e2e8f0;--muted:#94a3b8;--red:#ef4444;--green:#22c55e;}body{font-family:-apple-system,system-ui;margin:0;padding:16px;background:var(--bg);color:var(--txt);}h1{font-size:22px;margin-bottom:16px;}table{width:100%;border-collapse:collapse;margin-top:16px;background:var(--card);border-radius:8px;overflow:hidden;}th,td{padding:12px;text-align:left;border-bottom:1px solid #334155;font-size:14px;}th{background:#1e293b;color:var(--muted);font-weight:600;text-transform:uppercase;font-size:12px;}tr:last-child td{border-bottom:none;}.badge{padding:4px 8px;border-radius:4px;font-size:12px;font-weight:bold;}.bg-green{background:#14532d;color:#86efac;}.bg-red{background:#7f1d1d;color:#fca5a5;}button{padding:6px 12px;border:none;border-radius:4px;cursor:pointer;font-weight:bold;color:#fff;}.btn-block{background:var(--red);}.btn-unblock{background:var(--green);}</style></head><body><h1>🕵️‍♂️ Panel de Administrador</h1><p class="hint">Lista de usuarios registrados en el bot.</p><table><thead><tr><th>ID Usuario</th><th>Fecha Registro</th><th>Estado</th><th>Acciones</th></tr></thead><tbody id="usersBody"><tr><td colspan="4" style="text-align:center;">Cargando...</td></tr></tbody></table><script>const urlParams = new URLSearchParams(window.location.search);const passwd = urlParams.get('passwd');async function loadUsers() {const res = await fetch(`/api/admin/users?passwd=${passwd}`);if (!res.ok) {document.getElementById('usersBody').innerHTML = '<tr><td colspan="4" style="color:red;">Error de autenticación. Revisa tu contraseña.</td></tr>';return;}const users = await res.json();const tbody = document.getElementById('usersBody');if (users.length === 0) {tbody.innerHTML = '<tr><td colspan="4" style="text-align:center;">No hay usuarios registrados.</td></tr>';return;}tbody.innerHTML = users.map(u => {const status = u.is_blocked ? '<span class="badge bg-red">BLOQUEADO</span>' : '<span class="badge bg-green">ACTIVO</span>';const botState = u.active ? '🟢' : '⚪';const autoState = u.auto_mode ? '🎯' : '';const date = new Date(u.created_at).toLocaleString();return `<tr><td><code>${u.id}</code></td><td>${date}</td><td>${status} ${botState} ${autoState}</td><td><button class="${u.is_blocked ? 'btn-unblock' : 'btn-block'}" onclick="toggleBlock('${u.id}', ${u.is_blocked})">${u.is_blocked ? 'Desbloquear' : 'Bloquear'}</button></td></tr>`;}).join('');}async function toggleBlock(userId, isCurrentlyBlocked) {const action = isCurrentlyBlocked ? 'desbloquear' : 'bloquear';if (!confirm(`¿Estás seguro de que quieres ${action} a este usuario?`)) return;const res = await fetch(`/api/admin/block/${userId}?passwd=${passwd}`, { method: 'POST' });if (res.ok) {loadUsers();} else {alert('Error al cambiar el estado del usuario.');}}loadUsers();</script></body></html>"""

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
