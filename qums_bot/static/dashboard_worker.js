/**
 * QUMS Bot — Dashboard Live-Data Worker
 * Runs entirely off the main thread. Zero UI lag.
 * Polls /dashboard/live-data and posts raw JSON payload
 * back to the main thread for DOM patching.
 */

var _running    = false;
var _intervalMs = 1000;
var _role       = "public";
var _timer      = null;

self.onmessage = function (e) {
    var msg = e.data || {};
    switch (msg.cmd) {
        case "start":
            _intervalMs = Math.max(500, Number(msg.intervalMs) || 1000);
            _role       = msg.role || "public";
            _running    = true;
            _schedulePoll();
            break;
        case "stop":
            _running = false;
            if (_timer) { clearTimeout(_timer); _timer = null; }
            break;
        case "poll_now":
            if (_timer) { clearTimeout(_timer); _timer = null; }
            _doPoll();
            break;
    }
};

function _schedulePoll() {
    if (!_running) { return; }
    if (_timer)    { clearTimeout(_timer); }
    _timer = setTimeout(function () {
        _timer = null;
        _doPoll();
    }, _intervalMs);
}

function _doPoll() {
    if (!_running) { return; }

    var url  = "/dashboard/live-data?_ts=" + Date.now();
    var opts = {
        cache:   "no-store",
        headers: {
            "X-Requested-With":       "XMLHttpRequest",
            "Accept":                 "application/json",
            "Cache-Control":          "no-cache",
            "X-Dashboard-Auth-Role":  _role
        }
    };

    fetch(url, opts)
        .then(function (r) {
            /* Redirect / session expiry */
            if (r.redirected) {
                self.postMessage({ type: "redirect", url: r.url });
                return null;
            }
            if (r.status === 401) {
                return r.json().then(function (body) {
                    self.postMessage({ type: "auth_required", payload: body });
                    return null;
                });
            }
            if (!r.ok) {
                self.postMessage({ type: "error", status: r.status });
                return null;
            }
            var ct = r.headers.get("content-type") || "";
            if (ct.indexOf("application/json") === -1) {
                self.postMessage({ type: "error", status: r.status });
                return null;
            }
            return r.json();
        })
        .then(function (data) {
            if (data) {
                self.postMessage({ type: "data", payload: data });
            }
        })
        .catch(function (err) {
            self.postMessage({ type: "error", message: String(err) });
        })
        .finally(function () {
            /* Always re-schedule — worker keeps running regardless */
            _schedulePoll();
        });
}
