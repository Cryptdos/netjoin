"""HTTP API + static UI, served on 127.0.0.1 only."""

from __future__ import annotations

import json

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import APP_NAME, __version__, paths
from .engine import Engine, JoinError
from .scan import ScanError, ScanManager
from .scan import npcap as npcap_mod
from .store import StoreError

ENGINE: Engine | None = None
SCANS: ScanManager | None = None
PORT = 8765


class LocalOnly(BaseHTTPMiddleware):
    """Protect a local admin tool from other websites in the same browser.

    - Host must be localhost (blocks DNS-rebinding attacks).
    - Anything that changes state needs the X-NetJoin header, which a foreign web page
      can't send without a CORS pre-flight that we never approve.
    """

    async def dispatch(self, request: Request, call_next):
        host = (request.headers.get("host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            return JSONResponse({"error": "Forbidden host"}, status_code=403)
        if request.method not in ("GET", "HEAD") and request.headers.get("x-netjoin") != "1":
            return JSONResponse({"error": "Missing X-NetJoin header"}, status_code=403)
        resp = await call_next(request)
        resp.headers["Cache-Control"] = "no-store"
        return resp


def eng() -> Engine:
    assert ENGINE is not None
    return ENGINE


async def body(request: Request) -> dict:
    try:
        data = await request.json()
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def api(fn):
    """Run a blocking handler in a thread and turn known errors into 400s."""
    async def wrapper(request: Request):
        data = await body(request) if request.method in ("POST", "PUT") else {}
        try:
            result = await run_in_threadpool(fn, request, data)
        except (StoreError, JoinError, ScanError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        if isinstance(result, Response):
            return result
        return JSONResponse(result)
    return wrapper


# ---------- handlers ----------
def ping(req, data):
    return {"app": APP_NAME, "version": __version__}


def status(req, data):
    s = eng().status()
    s["data_dir"] = str(paths.data_dir())
    s["portable"] = paths.is_portable()
    s["npcap"] = npcap_mod.status() if eng().backend.kind == "windows" else \
        {"installed": False, "scapy": False, "available": False, "note": "Demo mode – Npcap not used"}
    return s


def adapters(req, data):
    show_all = req.query_params.get("all") == "1"
    return [a.to_dict() for a in eng().backend.list_adapters() if show_all or not a.virtual]


def select_adapter(req, data):
    a = eng().select_adapter(data.get("name") or "")
    eng().log.add(f"Adapter selected: {a.name}" + (f" ({a.description})" if a.description else ""))
    return {"ok": True}


def tree(req, data):
    return eng().store.tree()


def create_node(req, data):
    pid = data.get("parent_id")
    n = eng().store.create_node(data.get("kind", ""), int(pid) if pid is not None else None, data)
    eng().log.add(f"Added {n['kind']} '{n['name']}'")
    return n


def node_detail(req, data):
    nid = int(req.path_params["id"])
    s = eng().store
    n = s.node(nid)
    if not n:
        raise StoreError("Not found")
    out = {**n, "path": s.path_names(nid),
           "crumbs": [{"id": a["id"], "name": a["name"], "kind": a["kind"]} for a in reversed(s.ancestors(nid)[1:])]}
    if n["kind"] == "network":
        out["resolved"] = s.resolve_gateway_dns(nid)
        nx = eng().next_ip(n)
        out["next_ip"], out["next_how"] = nx["ip"], nx["how"]
        out["last_worked"] = s.last_worked_ip(nid)
        out["last_internet"] = s.last_internet(nid)
        out["history"] = s.ip_history(nid)
        st = eng().status()
        prim = st.get("primary")
        try:
            import ipaddress
            out["on_subnet"] = bool(prim) and ipaddress.IPv4Address(prim["ip"]) in ipaddress.IPv4Network(n["subnet"])
        except ValueError:
            out["on_subnet"] = False
        out["last_scan"] = s.get_setting(f"lastscan:{n['subnet']}")
    return out


def update_node(req, data):
    n = eng().store.update_node(int(req.path_params["id"]), data)
    eng().log.add(f"Updated {n['kind']} '{n['name']}'")
    return n


def delete_node(req, data):
    nid = int(req.path_params["id"])
    n = eng().store.node(nid)
    eng().store.delete_node(nid)
    if n:
        eng().log.add(f"Deleted {n['kind']} '{n['name']}'")
    return {"ok": True}


def join(req, data):
    return eng().join(int(data.get("network_id", 0)), data.get("ip") or None)


def dhcp(req, data):
    return eng().dhcp()


def rejoin(req, data):
    return eng().rejoin()


def internet(req, data):
    return eng().internet_check()


def scan_status(req, data):
    return {**SCANS.status(), "options": SCANS.options()}


def scan_start(req, data):
    nid = data.get("network_id")
    return SCANS.start(data.get("kind", "quick"), int(nid) if nid is not None else None)


def scan_cancel(req, data):
    SCANS.cancel()
    return {"ok": True}


def discover_start(req, data):
    return SCANS.start_discover(int(data.get("seconds", 20)), bool(data.get("whois", True)), data.get("adapter") or None)


def discover_status(req, data):
    return {"state": SCANS.status(), "result": SCANS.discover_result, "npcap": SCANS.npcap_ok(),
            "demo": eng().backend.kind == "fake"}


def devices(req, data):
    subnet = req.query_params.get("subnet", "")
    rows = eng().store.devices(subnet)
    return {"subnet": subnet, "devices": rows, "last_scan": eng().store.get_setting(f"lastscan:{subnet}")}


def clear_devices(req, data):
    subnet = req.query_params.get("subnet", "")
    n = eng().store.clear_devices(subnet)
    eng().log.add(f"Cleared {n} device(s) for {subnet}")
    return {"cleared": n}


def log(req, data):
    return eng().log.since(int(req.query_params.get("after", 0)))


def get_global(req, data):
    return eng().store.global_net_settings()


def put_global(req, data):
    from . import ipcalc
    g = {"gw_override": 1 if data.get("gw_override") else 0, "gateway": (data.get("gateway") or "").strip(),
         "dns_override": 1 if data.get("dns_override") else 0, "dns": (data.get("dns") or "").strip()}
    if g["gateway"] and not ipcalc.valid_ip(g["gateway"]):
        raise StoreError("Gateway isn't a valid IPv4 address")
    for d in [x.strip() for x in g["dns"].replace(";", ",").split(",") if x.strip()]:
        if not ipcalc.valid_ip(d):
            raise StoreError(f"DNS server isn't a valid IPv4 address: {d}")
    eng().store.set_setting("global", g)
    eng().log.add("Global gateway / DNS settings saved")
    return g


def export(req, data):
    payload = eng().store.export()
    return Response(json.dumps(payload, indent=2), media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="netjoin-sites.json"'})


def import_(req, data):
    n = eng().store.import_(data)
    eng().log.add(f"Imported {n} sites / areas / networks")
    return {"imported": n}


async def index(request):
    return FileResponse(paths.static_dir() / "index.html")


def build_app(engine: Engine, port: int, scans: ScanManager) -> Starlette:
    global ENGINE, PORT, SCANS
    ENGINE, PORT, SCANS = engine, port, scans
    routes = [
        Route("/", index),
        Route("/api/ping", api(ping)),
        Route("/api/status", api(status)),
        Route("/api/adapters", api(adapters)),
        Route("/api/adapter", api(select_adapter), methods=["POST"]),
        Route("/api/tree", api(tree)),
        Route("/api/nodes", api(create_node), methods=["POST"]),
        Route("/api/nodes/{id:int}", api(node_detail)),
        Route("/api/nodes/{id:int}", api(update_node), methods=["PUT"]),
        Route("/api/nodes/{id:int}", api(delete_node), methods=["DELETE"]),
        Route("/api/join", api(join), methods=["POST"]),
        Route("/api/dhcp", api(dhcp), methods=["POST"]),
        Route("/api/rejoin", api(rejoin), methods=["POST"]),
        Route("/api/internet", api(internet), methods=["POST"]),
        Route("/api/scan", api(scan_status)),
        Route("/api/scan", api(scan_start), methods=["POST"]),
        Route("/api/scan/cancel", api(scan_cancel), methods=["POST"]),
        Route("/api/discover", api(discover_status)),
        Route("/api/discover", api(discover_start), methods=["POST"]),
        Route("/api/devices", api(devices)),
        Route("/api/devices", api(clear_devices), methods=["DELETE"]),
        Route("/api/log", api(log)),
        Route("/api/settings/global", api(get_global)),
        Route("/api/settings/global", api(put_global), methods=["PUT"]),
        Route("/api/export", api(export)),
        Route("/api/import", api(import_), methods=["POST"]),
        Mount("/static", StaticFiles(directory=str(paths.static_dir())), name="static"),
    ]
    return Starlette(routes=routes, middleware=[Middleware(LocalOnly)])
