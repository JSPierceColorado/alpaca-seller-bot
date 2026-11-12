"""Private, read-only Alpaca performance service. No trading/transfer methods."""
import base64,csv,html,hmac,io,json,logging,os,sqlite3,threading,urllib.parse,urllib.request
from datetime import datetime,timedelta,timezone
from collections import defaultdict,deque
from decimal import Decimal
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from zoneinfo import ZoneInfo
from apscheduler.schedulers.background import BackgroundScheduler

NY=ZoneInfo("America/New_York")
LOG=logging.getLogger("analytics")
DATA="/data/performance.sqlite3"
LOCK=threading.RLock()
RUN=threading.Lock()
LATEST_ERROR=""

def dec(v):
    try:
        x=Decimal(str(v))
        return x if x.is_finite() else Decimal(0)
    except Exception:return Decimal(0)

def dateval(v):
    try:
        if isinstance(v,(float,int)):return datetime.fromtimestamp(v,timezone.utc).astimezone(NY).date().isoformat()
        v=str(v)
        if len(v)==10:return v
        t=datetime.fromisoformat(v.replace("Z","+00:00"))
        return t.astimezone(NY).date().isoformat()
    except Exception:return None

def get(path,params=None,market=False):
    paper=os.getenv("ALPACA_PAPER","false").lower() in ("true","1")
    host="https://data.alpaca.markets" if market else ("https://paper-api.alpaca.markets" if paper else "https://api.alpaca.markets")
    uri=host+path+("?" +urllib.parse.urlencode(params) if params else "")
    req=urllib.request.Request(uri,headers={
      "APCA-API-KEY-ID":os.environ["APCA_API_KEY_ID"],
      "APCA-API-SECRET-KEY":os.environ["APCA_API_SECRET_KEY"]},method="GET")
    with urllib.request.urlopen(req,timeout=30) as r:return json.load(r)

def connect():
    os.makedirs("/data",exist_ok=True)
    c=sqlite3.connect(DATA,timeout=30)
    c.row_factory=sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE IF NOT EXISTS equity(day TEXT PRIMARY KEY,value REAL,source TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS account(day TEXT PRIMARY KEY,updated TEXT,equity REAL,cash REAL,unrealized REAL)")
    c.execute("CREATE TABLE IF NOT EXISTS positions(day TEXT,symbol TEXT,data TEXT,PRIMARY KEY(day,symbol))")
    c.execute("CREATE TABLE IF NOT EXISTS activities(id TEXT PRIMARY KEY,kind TEXT,day TEXT,data TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY,cid TEXT,data TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS spy(day TEXT PRIMARY KEY,price REAL)")
    c.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,val TEXT)")
    c.commit()
    return c

def activities():
    rows=[];token=None;complete=False
    for i in range(120):
        q={"page_size":100,"direction":"asc"}
        if token:q["page_token"]=token
        batch=get("/v2/account/activities",q)
        if not isinstance(batch,list):raise ValueError("activities schema")
        rows.extend(batch)
        if len(batch)<100:
            complete=True;break
        new=batch[-1].get("id")
        if not new or new==token:break
        token=new
    return rows,complete

def orders():
    rows=[];until=None;complete=False;seen=set()
    for i in range(15):
        q={"limit":500,"status":"all","direction":"desc"}
        if until:q["until"]=until
        batch=get("/v2/orders",q)
        if not isinstance(batch,list):raise ValueError("orders schema")
        for x in batch:
            if x.get("id") not in seen:
                rows.append(x);seen.add(x.get("id"))
        if len(batch)<500:
            complete=True;break
        new=batch[-1].get("submitted_at")
        if not new or new==until:break
        until=new
    return rows,complete

def refresh():
    global LATEST_ERROR
    if not RUN.acquire(blocking=False):return
    try:
        now=datetime.now(timezone.utc);day=now.astimezone(NY).date().isoformat()
        acct=get("/v2/account")
        if dec(acct.get("equity"))<=0:raise ValueError("account equity not positive/available")
        pos=get("/v2/positions")
        if not isinstance(pos,list):raise ValueError("positions schema")
        trades,at_complete=activities()
        recent_orders,or_complete=orders()
        try:
            hist=get("/v2/account/portfolio/history",{"period":"1A","timeframe":"1D","pnl_reset":"no_reset"})
            histrows=[(dateval(t),float(dec(e))) for t,e in zip(hist.get("timestamp",[]),hist.get("equity",[])) if dateval(t) and dec(e)>0]
        except Exception as e:
            LOG.warning("HISTORY_UNAVAILABLE type=%s",type(e).__name__)
            histrows=[]
        start=(now-timedelta(days=370)).strftime("%Y-%m-%dT00:00:00Z")
        end=(now+timedelta(days=2)).strftime("%Y-%m-%dT00:00:00Z")
        try:
            bars=get("/v2/stocks/SPY/bars",{"timeframe":"1Day","start":start,"end":end,"feed":"iex","adjustment":"all","limit":10000},market=True)
            spyrows=[(dateval(x.get("t")),float(dec(x.get("c")))) for x in bars.get("bars",[]) if dateval(x.get("t")) and dec(x.get("c"))>0]
        except Exception as e:
            LOG.warning("SPY_UNAVAILABLE type=%s",type(e).__name__)
            spyrows=[]
        with LOCK:
            c=connect()
            try:
                c.execute("BEGIN")
                for d,val in histrows:c.execute("INSERT OR REPLACE INTO equity VALUES(?,?,?)",(d,val,"broker"))
                c.execute("INSERT OR REPLACE INTO equity VALUES(?,?,?)",(day,float(dec(acct["equity"])),"live"))
                unreal=sum((dec(x.get("unrealized_pl")) for x in pos),Decimal(0))
                c.execute("INSERT OR REPLACE INTO account VALUES(?,?,?,?,?)",(day,now.isoformat(),float(dec(acct["equity"])),float(dec(acct.get("cash"))),float(unreal)))
                c.execute("DELETE FROM positions WHERE day=?",(day,))
                for x in pos:c.execute("INSERT OR REPLACE INTO positions VALUES(?,?,?)",(day,str(x.get("symbol")),json.dumps(x)))
                for x in trades:
                    if x.get("id"):c.execute("INSERT OR REPLACE INTO activities VALUES(?,?,?,?)",(str(x["id"]),str(x.get("activity_type")),dateval(x.get("transaction_time") or x.get("date")),json.dumps(x)))
                for x in recent_orders:
                    if x.get("id"):c.execute("INSERT OR REPLACE INTO orders VALUES(?,?,?)",(str(x["id"]),str(x.get("client_order_id") or ""),json.dumps(x)))
                for d,px in spyrows:c.execute("INSERT OR REPLACE INTO spy VALUES(?,?)",(d,px))
                c.execute("INSERT OR REPLACE INTO meta VALUES(?,?)",("quality",json.dumps({"activities_complete":at_complete,"orders_complete":or_complete,"activities_received":len(trades),"orders_received":len(recent_orders),"spy_price_source":"Alpaca IEX dividend/split adjusted","history_days":len(histrows),"spy_days":len(spyrows)})))
                c.commit()
            finally:c.close()
        LATEST_ERROR=""
        rep=report()
        LOG.info("ANALYTICS_SUMMARY %s",json.dumps({"date":day,"equity":rep["account"]["equity"],"unrealized":rep["account"]["unrealized"],"covered_realized":rep["trades"]["realized"],"win_rate":rep["trades"]["win_rate"],"closed_orders":rep["trades"]["closed_orders"],"benchmark":rep["benchmark"]["last"] if rep["benchmark"] else None,"quality":rep["quality"]},separators=(",",":")))
    except Exception as e:
        LATEST_ERROR=type(e).__name__
        LOG.error("ANALYTICS_REFRESH_FAILED type=%s",type(e).__name__)
    finally:RUN.release()

def fifo(acts,ids):
    inventory=defaultdict(deque);sold=defaultdict(lambda:{"pnl":Decimal(0),"unknown":False,"matched":False,"by_source":defaultdict(lambda:Decimal(0))})
    fills=sorted((x for x in acts if x.get("activity_type")=="FILL"),key=lambda x:(str(x.get("transaction_time")),str(x.get("id"))))
    for x in fills:
        sym=str(x.get("symbol") or "").upper();qty=dec(x.get("qty"));price=dec(x.get("price"))
        if not sym or qty<=0 or price<=0:continue
        order=str(x.get("order_id") or x.get("id"));cid=ids.get(order,"")
        source="V2" if cid.startswith("v2b-") else ("ATLAS" if cid.startswith("aitr-") else "Other")
        side=str(x.get("side") or "").lower()
        if side=="buy":inventory[sym].append([qty,price,source])
        elif side=="sell":
            remaining=qty
            while remaining>0 and inventory[sym]:
                lot=inventory[sym][0];taken=min(remaining,lot[0]);gain=taken*(price-lot[1])
                sold[order]["pnl"]+=gain;sold[order]["matched"]=True
                sold[order]["by_source"][lot[2]]+=gain
                remaining-=taken;lot[0]-=taken
                if lot[0]<=0:inventory[sym].popleft()
            if remaining>0:sold[order]["unknown"]=True
    groups=[x for x in sold.values() if x["matched"] and not x["unknown"]]
    win=sum(x["pnl"]>0 for x in groups);loss=sum(x["pnl"]<0 for x in groups)
    plus=sum((max(x["pnl"],Decimal(0)) for x in groups),Decimal(0))
    minus=sum((max(-x["pnl"],Decimal(0)) for x in groups),Decimal(0))
    sources=defaultdict(lambda:Decimal(0))
    for group in groups:
        for source,value in group["by_source"].items():sources[source]+=value
    return {"closed_orders":len(groups),"wins":win,"losses":loss,"breakeven":len(groups)-win-loss,"win_rate":round(win/(win+loss)*100,2) if win+loss else None,
      "realized":round(float(sum((x["pnl"] for x in groups),Decimal(0))),2),
      "gross_wins":round(float(plus),2),"gross_losses":round(float(minus),2),
      "profit_factor":round(float(plus/minus),3) if minus else None,
      "unmatched_sells":sum(x["unknown"] for x in sold.values()),
      "source_realized":{k:round(float(v),2) for k,v in sources.items()},"fill_records":len(fills)}

def report():
    with LOCK:
        c=connect()
        try:
            last=c.execute("SELECT * FROM account ORDER BY day DESC LIMIT 1").fetchone()
            if not last:return {"status":"awaiting_first_snapshot"}
            acct=dict(last)
            positions=[json.loads(x["data"]) for x in c.execute("SELECT data FROM positions WHERE day=?",(last["day"],))]
            acts=[json.loads(x["data"]) for x in c.execute("SELECT data FROM activities")]
            ids={x["id"]:x["cid"] for x in c.execute("SELECT id,cid FROM orders")}
            equities=[(x["day"],x["value"]) for x in c.execute("SELECT day,value FROM equity ORDER BY day")]
            spy={x["day"]:x["price"] for x in c.execute("SELECT day,price FROM spy")}
            q=c.execute("SELECT val FROM meta WHERE key='quality'").fetchone()
            quality=json.loads(q["val"]) if q else {}
        finally:c.close()
    flows=defaultdict(float);transfers=0
    for a in acts:
        kind=a.get("activity_type")
        if kind in ("CSD","CSW"):
            day=dateval(a.get("transaction_time") or a.get("date"))
            if day:flows[day]+=(1 if kind=="CSD" else -1)*abs(float(dec(a.get("net_amount"))))
        if kind in ("ACATS","ACATC","JNLS","JNLC","REORG","SSP","SSO","MA","SC"):transfers+=1
    covered=fifo(acts,ids)
    series=[];eq=[(d,e) for d,e in equities if d in spy and e>0 and spy[d]>0]
    dd=0
    if len(eq)>=2:
        baseline,initial=eq[0];shares=initial/spy[baseline];net=0;unit=100;top=100;prev=initial
        for i,(day,value) in enumerate(eq):
            flow=flows[day] if i else 0
            if i:
                net+=flow;shares+=flow/spy[day]
                growth=(value-flow)/prev if prev else 1
                if 0<growth<10:unit*=growth
            top=max(top,unit);dd=min(dd,(unit/top-1)*100)
            hyp=shares*spy[day]
            series.append({"date":day,"equity":round(value,2),"spy_equity":round(hyp,2),
               "account_net":round(value-initial-net,2),"spy_net":round(hyp-initial-net,2),
               "account_index":round(unit,4),"spy_index":round(100*spy[day]/spy[baseline],4)})
            prev=value
    warnings=["Observed FIFO realized P&L is provisional, before fees/taxes, not authoritative tax-lot accounting.",
      "Matched-cashflow SPY portfolio is hypothetical, with IEX adjusted close prices and end-of-day external flows."]
    if not quality.get("activities_complete"):warnings.append("Activity history is incomplete; exclude unverified results from decisions.")
    if not quality.get("orders_complete"):warnings.append("Order history incomplete; bot attribution may be missing.")
    if covered["unmatched_sells"]:warnings.append("Sells without observed matching buy lots excluded from realized results and win rate.")
    if transfers:warnings.append("Transfers/corporate actions detected: benchmark and FIFO basis require reconciliation.")
    if not series:warnings.append("SPY benchmark unavailable until matching price and equity dates are present.")
    return {"status":"ok","as_of":acct["updated"],"account":{"day":acct["day"],"equity":round(acct["equity"],2),"cash":round(acct["cash"],2),"unrealized":round(acct["unrealized"],2),"positions":len(positions)},
      "positions":[{"symbol":x.get("symbol"),"qty":x.get("qty"),"value":float(dec(x.get("market_value"))),"unrealized":float(dec(x.get("unrealized_pl")))} for x in positions],
      "trades":covered,"benchmark":{"baseline":series[0]["date"],"last":series[-1],"max_drawdown_pct_est":round(dd,2)} if series else None,
      "series":series,"quality":{"checks":quality,"warnings":warnings},"last_error":LATEST_ERROR}

def graph(points,keys):
    if len(points)<2:return '<p class="muted">Not enough comparable observations.</p>'
    w,h=930,256;xs=[];vals=[float(row[key]) for row in points for key,label in keys];low=min(vals);high=max(vals)
    if high==low:high+=1;low-=1
    pad=(high-low)*.1;low-=pad;high+=pad
    sx=lambda i:70+(w-85)*i/(len(points)-1)
    sy=lambda v:18+(h-55)*(1-(v-low)/(high-low))
    colors=["#2dd4bf","#a78bfa","#f59e0b"]
    parts=[]
    for j,(key,label) in enumerate(keys):
        path=" ".join((str(round(sx(i),2))+","+str(round(sy(float(row[key])),2))) for i,row in enumerate(points))
        parts.append('<polyline points="'+path+'" fill="none" stroke="'+colors[j%3]+'" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>')
    for j in range(5):
        v=low+(high-low)*j/4;y=sy(v)
        parts.append('<line x1="70" y1="'+str(y)+'" x2="915" y2="'+str(y)+'" stroke="#334155" stroke-dasharray="4,5"/>')
        parts.append('<text x="5" y="'+str(y+4)+'" fill="#94a3b8" font-size="12">'+format(v,",.0f")+'</text>')
    for i in set([0,len(points)//2,len(points)-1]):
        parts.append('<text x="'+str(sx(i))+'" y="246" text-anchor="middle" fill="#94a3b8" font-size="12">'+html.escape(points[i]["date"])+'</text>')
    legend=" ".join('<span style="color:'+colors[i%3]+'">● '+html.escape(label)+'</span>' for i,(key,label) in enumerate(keys))
    return '<div class="legend">'+legend+'</div><svg viewBox="0 0 930 256" role="img" aria-label="Performance chart">'+"".join(parts)+'</svg>'

def dashboard(rep,rangeval):
    if rep.get("status")!="ok":return "<h1>Performance Analytics</h1><p>Awaiting first broker sync.</p>"
    p=rep["series"]
    if rangeval in ("1m","3m","6m","1y"):
        days={"1m":31,"3m":92,"6m":184,"1y":366}[rangeval]
        cutoff=(datetime.now(NY).date()-timedelta(days=days)).isoformat()
        sub=[x for x in p if x["date"]>=cutoff]
        if len(sub)>1:p=sub
    a=rep["account"];t=rep["trades"];b=rep["benchmark"]
    currency=lambda v:"—" if v is None else "$"+format(float(v),",.2f")
    percent=lambda v:"—" if v is None else format(float(v),".2f")+"%"
    def box(title,v,detail=""):return '<div class="card"><small>'+html.escape(title)+'</small><b>'+html.escape(str(v))+'</b><small>'+html.escape(detail)+'</small></div>'
    vals=[
      ("Total equity",currency(a["equity"])),("Unrealized P&L",currency(a["unrealized"])),
      ("Covered FIFO realized P&L",currency(t["realized"])),("Win rate",percent(t["win_rate"])),
      ("Closed sell orders covered",str(t["closed_orders"])),("Profit factor",str(t["profit_factor"]) if t["profit_factor"] else "—")]
    boxes="".join(box(x,y) for x,y in vals)
    bench=b["last"] if b else None
    nav=" ".join('<a href="/?range='+x+'">'+y+'</a>' for x,y in [("1m","1M"),("3m","3M"),("6m","6M"),("1y","1Y"),("all","All")])
    tops=sorted(rep["positions"],key=lambda x:abs(x["unrealized"]),reverse=True)
    rows="".join('<tr><td>'+html.escape(str(x["symbol"]))+'</td><td>'+currency(x["value"])+'</td><td>'+currency(x["unrealized"])+'</td></tr>' for x in tops[:30])
    warnings="".join("<li>"+html.escape(x)+"</li>" for x in rep["quality"]["warnings"])
    return ('<header><p class="accent">READ ONLY · ALPACA ACCOUNT</p><h1>Investment Performance Control Center</h1><p class="muted">Broker snapshot '+html.escape(rep["as_of"])+'</p></header>'
      +'<section class="grid">'+boxes+'</section>'
      +'<nav>'+nav+' <a href="/api/report">Full JSON</a> <a href="/api/summary">Summary API</a> <a href="/export/snapshots.csv">CSV</a> <a href="/refresh">Refresh now</a></nav>'
      +'<section class="panel"><h2>Cash-flow-adjusted return index (start = 100)</h2><p class="muted">Approximate daily time-weighted Alpaca performance vs SPY dividend-adjusted return.</p>'+graph(p,[("account_index","Alpaca"),("spy_index","SPY")])+'</section>'
      +'<section class="panel"><h2>Actual equity vs hypothetical SPY equity</h2><p class="muted">Same starting balance and recorded deposits/withdrawals. Baseline '+html.escape(b["baseline"] if b else "pending")+'.</p>'+graph(p,[("equity","Alpaca"),("spy_equity","SPY")])+'</section>'
      +'<section class="panel"><h2>Net P&L after external cash flows</h2>'+graph(p,[("account_net","Alpaca"),("spy_net","SPY")])+'</section>'
      +'<section class="panel"><h2>Covered realized P&L by purchase source</h2><pre>'+html.escape(json.dumps(t["source_realized"],indent=2))+'</pre></section>'
      +'<section class="panel"><h2>Open holdings and unrealized P&L</h2><table><tr><th>Symbol</th><th>Value</th><th>Unrealized</th></tr>'+rows+'</table></section>'
      +'<section class="panel"><h2>Data quality / methodology</h2><ul>'+warnings+'</ul><p class="muted">No order placement or trading features in this service.</p></section>')

STYLE="""
body{background:#071321;color:#e2e8f0;font:15px/1.5 system-ui,-apple-system,sans-serif;margin:0}
main{max-width:1180px;margin:auto;padding:32px 20px 90px}h1{font-size:36px;margin:0}h2{margin:0 0 8px;font-size:20px}.muted,small{color:#94a3b8}.accent{color:#2dd4bf;font-weight:700;letter-spacing:.15em;font-size:12px}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:25px 0}.card,.panel{background:#12243b;border:1px solid #30435a;border-radius:14px;padding:20px}.card b{display:block;font-size:26px;margin:6px 0}.card small{display:block}.panel{margin:15px 0}.panel svg{width:100%;height:auto}.legend{display:flex;gap:22px;margin-top:20px}nav{display:flex;gap:8px;flex-wrap:wrap;margin:20px 0}nav a{border:1px solid #3c5068;border-radius:9px;padding:8px 13px;color:#e2e8f0;text-decoration:none}table{width:100%;border-collapse:collapse;text-align:left}td,th{padding:9px;border-bottom:1px solid #30435a}pre{white-space:pre-wrap}li{margin:6px 0}@media(max-width:700px){.grid{grid-template-columns:repeat(2,1fr)}}@media(max-width:450px){.grid{grid-template-columns:1fr}}
"""

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def authed(self):
        header=self.headers.get("Authorization","")
        if not header.startswith("Basic "):return False
        try:
            u,p=base64.b64decode(header[6:],validate=True).decode().split(":",1)
            return hmac.compare_digest(u,os.getenv("DASH_USERNAME","owner")) and hmac.compare_digest(p,os.environ.get("DASH_PASSWORD",""))
        except Exception:return False
    def send(self,body,ctype="text/html; charset=utf-8",status=200):
        self.send_response(status);self.send_header("Content-Type",ctype)
        self.send_header("Cache-Control","no-store, private")
        self.send_header("X-Content-Type-Options","nosniff")
        self.send_header("X-Frame-Options","DENY")
        self.send_header("Referrer-Policy","no-referrer")
        self.send_header("Content-Security-Policy","default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'")
        self.end_headers();self.wfile.write(body.encode())
    def do_GET(self):
        target=urllib.parse.urlparse(self.path)
        if target.path=="/health":
            self.send('{"ready":true,"read_only":true}',"application/json");return
        if not self.authed():
            self.send_response(401);self.send_header("WWW-Authenticate",'Basic realm="Private Performance Dashboard"')
            self.send_header("Cache-Control","no-store");self.end_headers();return
        if target.path=="/refresh":
            threading.Thread(target=refresh,daemon=True).start()
            self.send_response(303);self.send_header("Location","/");self.end_headers();return
        rep=report()
        if target.path in ("/api/report","/api/summary"):
            if target.path=="/api/summary":
                rep={k:v for k,v in rep.items() if k not in ("series","positions")}
            self.send(json.dumps(rep),"application/json");return
        if target.path=="/export/snapshots.csv":
            sio=io.StringIO();w=csv.writer(sio)
            w.writerow(["day","equity","cash","unrealized","updated"])
            with LOCK:
                c=connect()
                try:
                    for r in c.execute("SELECT * FROM account ORDER BY day"):
                        w.writerow([r["day"],r["equity"],r["cash"],r["unrealized"],r["updated"]])
                finally:c.close()
            self.send(sio.getvalue(),"text/csv");return
        if target.path!="/":self.send("Not found","text/plain",404);return
        value=urllib.parse.parse_qs(target.query).get("range",["all"])[0]
        content=dashboard(rep,value)
        self.send('<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Private Investment Analytics</title><style>'+STYLE+'</style></head><body><main>'+content+'</main></body></html>')

def main():
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
    if not os.getenv("APCA_API_KEY_ID") or not os.getenv("APCA_API_SECRET_KEY"):raise SystemExit("missing Alpaca credentials")
    if len(os.getenv("DASH_PASSWORD",""))<24:raise SystemExit("dashboard password too short")
    c=connect();c.close()
    scheduler=BackgroundScheduler(timezone=NY,job_defaults={"coalesce":True,"max_instances":1})
    scheduler.add_job(refresh,"cron",day_of_week="mon-fri",hour="9-16",minute=40)
    scheduler.add_job(refresh,"cron",day_of_week="mon-fri",hour=18,minute=30)
    scheduler.start()
    threading.Thread(target=refresh,daemon=True).start()
    port=int(os.getenv("PORT","8080"))
    LOG.info("ANALYTICS_SERVICE_STARTED read_only=TRUE port=%d cron=market_hourly_and_after_close",port)
    ThreadingHTTPServer(("0.0.0.0",port),Handler).serve_forever()

if __name__=="__main__":main()
