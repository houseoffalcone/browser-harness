"""Tab hygiene: the harness acts only on tabs it opened, reuses them per task,
closes them at task end after a URL check, and prefers a dedicated agent Chrome."""
import asyncio

from browser_harness import admin, daemon, helpers, run


class _CDP:
    def __init__(self, targets=None):
        self.calls = []
        self.targets = targets or []
        self.created = 0

    async def send_raw(self, method, params=None, session_id=None):
        self.calls.append((method, params, session_id))
        if method == "Target.getTargets":
            return {"targetInfos": self.targets}
        if method == "Target.createTarget":
            self.created += 1
            tid = f"created-{self.created}"
            self.targets.append({"targetId": tid, "url": "about:blank", "type": "page"})
            return {"targetId": tid}
        if method == "Target.attachToTarget":
            return {"sessionId": f"session-for-{params['targetId']}"}
        if method == "Runtime.evaluate":
            return {"value": params["expression"]}
        return {}


def _daemon(targets=None, current=None, name="default"):
    d = daemon.Daemon()
    d.cdp = _CDP(targets)
    d.session = "session-current"
    d.target_id = current
    return d


def _handle(d, req):
    return asyncio.run(d.handle(req))


# --- daemon guard ---

def test_guard_refuses_actions_on_a_tab_the_harness_did_not_open():
    d = _daemon(current="user-tab")
    r = _handle(d, {"method": "Runtime.evaluate", "params": {"expression": "1"}})
    assert "refused" in r["error"] and "user-tab" in r["error"] and "new_tab" in r["error"]
    assert not any(m == "Runtime.evaluate" for m, _p, _s in d.cdp.calls)


def test_guard_allows_owned_tab_and_records_where_the_harness_sent_it():
    d = _daemon(current="mine")
    d.own("mine", task="t")
    r = _handle(d, {"method": "Page.navigate", "params": {"url": "https://example.com/a"}})
    assert "result" in r
    assert d.owned["mine"]["urls"] == ["https://example.com/a"]
    assert d.owned["mine"]["task"] == "t"


def test_guard_closes_only_owned_tabs_and_forgets_them():
    d = _daemon(current="mine")
    d.own("mine")
    refused = _handle(d, {"method": "Target.closeTarget", "params": {"targetId": "user-tab"}})
    assert "refused" in refused["error"]
    ok = _handle(d, {"method": "Target.closeTarget", "params": {"targetId": "mine"}})
    assert "result" in ok and "mine" not in d.owned


def test_allow_user_tabs_flag_bypasses_the_guard():
    d = _daemon(current="user-tab")
    r = _handle(d, {"method": "Runtime.evaluate", "params": {"expression": "1"}, "allow_user_tabs": True})
    assert r == {"result": {"value": "1"}}


def test_browser_level_calls_pass_regardless_of_ownership():
    d = _daemon(targets=[{"targetId": "user-tab", "url": "https://x", "type": "page"}], current="user-tab")
    assert "result" in _handle(d, {"method": "Target.getTargets", "params": {}})


def test_explicit_session_on_a_known_user_tab_is_refused_unknown_sessions_pass():
    d = _daemon(current="mine")
    d.own("mine")
    _handle(d, {"method": "Target.attachToTarget", "params": {"targetId": "user-tab", "flatten": True}})
    r = _handle(d, {"method": "Runtime.evaluate", "params": {"expression": "1"}, "session_id": "session-for-user-tab"})
    assert "refused" in r["error"]
    r = _handle(d, {"method": "Runtime.evaluate", "params": {"expression": "1"}, "session_id": "iframe-session"})
    assert r == {"result": {"value": "1"}}


# --- registry ---

def test_owned_tabs_meta_returns_live_tabs_per_task_and_prunes_closed_ones():
    d = _daemon(targets=[{"targetId": "live", "url": "https://a/", "title": "A", "type": "page"}])
    d.own("live", task="t1", url="https://a/")
    d.own("gone", task="t1")
    d.own("other", task="t2")
    r = _handle(d, {"meta": "owned_tabs", "task": "t1"})
    assert [t["targetId"] for t in r["tabs"]] == ["live"]
    assert r["tabs"][0]["url"] == "https://a/" and r["tabs"][0]["urls"] == ["https://a/"]
    assert "gone" not in d.owned and "other" not in d.owned  # pruned: not open any more


def test_own_tab_meta_retags_the_task_and_disown_forgets():
    d = _daemon()
    _handle(d, {"meta": "own_tab", "target_id": "t", "task": "a"})
    _handle(d, {"meta": "own_tab", "target_id": "t", "task": "b"})
    assert d.owned["t"]["task"] == "b"
    _handle(d, {"meta": "disown_tab", "target_id": "t"})
    assert d.owned == {}


def test_registry_survives_a_daemon_restart():
    d = _daemon()
    d.own("kept", task="t", url="https://a/")
    again = daemon.Daemon()
    assert again.owned["kept"]["task"] == "t" and again.owned["kept"]["urls"] == ["https://a/"]


# --- recovery never lands on the user's page when the harness has a tab ---

def test_default_recovery_stays_on_the_current_tab_when_it_still_exists(monkeypatch):
    monkeypatch.setattr(daemon, "NAME", "default")
    monkeypatch.setattr(daemon, "REMOTE_ID", None)
    monkeypatch.setattr(daemon, "BROWSER_KIND", "cdp")
    d = _daemon(targets=[
        {"targetId": "user-tab", "url": "https://user/", "type": "page"},
        {"targetId": "cur", "url": "https://mine/", "type": "page"},
    ], current="cur")
    page = asyncio.run(d.attach_first_page())
    assert page["targetId"] == "cur" and d.target_id == "cur"


def test_default_recovery_prefers_an_owned_page_over_the_users_first_page(monkeypatch):
    monkeypatch.setattr(daemon, "NAME", "default")
    monkeypatch.setattr(daemon, "REMOTE_ID", None)
    monkeypatch.setattr(daemon, "BROWSER_KIND", "cdp")
    d = _daemon(targets=[
        {"targetId": "user-tab", "url": "https://user/", "type": "page"},
        {"targetId": "mine", "url": "https://mine/", "type": "page"},
    ], current="closed-tab")
    d.own("mine")
    page = asyncio.run(d.attach_first_page())
    assert page["targetId"] == "mine"
    assert "user-tab" not in d.owned


def test_daemon_owns_the_blank_tab_it_creates_but_not_a_users_page(monkeypatch):
    monkeypatch.setattr(daemon, "NAME", "default")
    monkeypatch.setattr(daemon, "REMOTE_ID", None)
    monkeypatch.setattr(daemon, "BROWSER_KIND", "cdp")
    d = _daemon()
    asyncio.run(d.attach_first_page())
    assert "created-1" in d.owned
    d2 = _daemon(targets=[{"targetId": "user-tab", "url": "https://user/", "type": "page"}])
    asyncio.run(d2.attach_first_page())
    assert d2.target_id == "user-tab" and "user-tab" not in d2.owned


# --- helpers ---

def _fake_helpers(monkeypatch, owned, calls):
    monkeypatch.setattr(helpers, "owned_tabs", lambda task=helpers.TASK: [
        t for t in owned if task is None or t["task"] == task])
    monkeypatch.setattr(helpers, "_send", lambda request: calls.append(("ipc", request)) or {})
    monkeypatch.setattr(helpers, "switch_tab", lambda target: calls.append(("switch_tab", target)))
    monkeypatch.setattr(helpers, "goto_url", lambda url: calls.append(("goto_url", url)))

    def fake_cdp(method, **kwargs):
        calls.append((method, kwargs))
        if method == "Target.createTarget":
            return {"targetId": "created"}
        return {}
    monkeypatch.setattr(helpers, "cdp", fake_cdp)


def test_new_tab_reuses_this_tasks_tab_on_the_same_site(monkeypatch):
    calls = []
    _fake_helpers(monkeypatch, [{"targetId": "t1", "url": "https://www.linkedin.com/feed/", "task": helpers.TASK, "urls": []}], calls)
    assert helpers.new_tab("https://linkedin.com/in/x") == "t1"
    assert ("switch_tab", "t1") in calls and ("goto_url", "https://linkedin.com/in/x") in calls
    assert not any(c[0] == "Target.createTarget" for c in calls)


def test_new_tab_creates_and_registers_when_nothing_matches(monkeypatch):
    calls = []
    _fake_helpers(monkeypatch, [{"targetId": "t1", "url": "https://instagram.com/", "task": helpers.TASK, "urls": []}], calls)
    assert helpers.new_tab("https://linkedin.com/") == "created"
    assert ("Target.createTarget", {"url": "about:blank", "background": True}) in calls
    assert ("ipc", {"meta": "own_tab", "target_id": "created", "task": helpers.TASK}) in calls
    assert not any(c[0] == "Target.activateTarget" for c in calls)


def test_new_tab_reuse_false_always_creates(monkeypatch):
    calls = []
    _fake_helpers(monkeypatch, [{"targetId": "t1", "url": "https://linkedin.com/", "task": helpers.TASK, "urls": []}], calls)
    assert helpers.new_tab("https://linkedin.com/", reuse=False) == "created"


def test_close_task_tabs_closes_on_site_and_blank_tabs_and_keeps_strays(monkeypatch):
    calls = []
    _fake_helpers(monkeypatch, [
        {"targetId": "on-site", "url": "https://www.linkedin.com/feed/", "task": helpers.TASK, "urls": ["https://linkedin.com/in/x"]},
        {"targetId": "sub", "url": "https://m.facebook.com/x", "task": helpers.TASK, "urls": ["https://facebook.com/"]},
        {"targetId": "stray", "url": "https://accounts.google.com/signin", "task": helpers.TASK, "urls": ["https://youtube.com/"]},
        {"targetId": "blank", "url": "about:blank", "task": helpers.TASK, "urls": []},
        {"targetId": "other-task", "url": "https://x.com/", "task": "other", "urls": ["https://x.com/"]},
    ], calls)
    out = helpers.close_task_tabs()
    assert out["closed"] == ["on-site", "sub", "blank"]
    assert [k["targetId"] for k in out["kept"]] == ["stray"]
    assert "accounts.google.com" in out["kept"][0]["reason"] and "youtube.com" in out["kept"][0]["reason"]
    closed = [kw["targetId"] for m, kw in calls if m == "Target.closeTarget"]
    assert closed == ["on-site", "sub", "blank"]  # never other-task, never stray


def test_ensure_real_tab_never_attaches_to_a_users_tab(monkeypatch):
    calls = []
    _fake_helpers(monkeypatch, [], calls)
    monkeypatch.setattr(helpers, "current_tab", lambda: {"targetId": "user-tab", "url": "https://user/", "title": ""})
    monkeypatch.setattr(helpers, "new_tab", lambda url="about:blank", reuse=True: calls.append(("new_tab", url)) or "fresh")
    assert helpers.ensure_real_tab()["targetId"] == "fresh"
    assert ("new_tab", "about:blank") in calls and not any(c[0] == "switch_tab" for c in calls)


def test_ensure_real_tab_keeps_the_current_tab_when_owned(monkeypatch):
    calls = []
    _fake_helpers(monkeypatch, [{"targetId": "mine", "url": "https://mine/", "task": "any", "urls": []}], calls)
    monkeypatch.setattr(helpers, "current_tab", lambda: {"targetId": "mine", "url": "https://mine/", "title": ""})
    assert helpers.ensure_real_tab()["targetId"] == "mine"
    assert calls == []


def test_list_tabs_marks_the_tabs_the_harness_owns(monkeypatch):
    calls = []
    _fake_helpers(monkeypatch, [{"targetId": "mine", "url": "https://mine/", "task": "t", "urls": []}], calls)
    monkeypatch.setattr(helpers, "cdp", lambda method, **kw: {"targetInfos": [
        {"targetId": "mine", "url": "https://mine/", "title": "", "type": "page"},
        {"targetId": "user", "url": "https://user/", "title": "", "type": "page"},
    ]})
    assert [(t["targetId"], t["owned"]) for t in helpers.list_tabs()] == [("mine", True), ("user", False)]


# --- dedicated agent Chrome preference ---

def _reset_names(monkeypatch):
    for m in (run, admin, helpers):
        monkeypatch.setattr(m, "NAME", m.NAME)
    for k in ("BU_CDP_URL", "BU_CDP_WS", "BU_NAME", "BH_PREFER_NAME"):
        monkeypatch.delenv(k, raising=False)


def test_prefer_dedicated_chrome_when_it_is_up(monkeypatch):
    _reset_names(monkeypatch)
    monkeypatch.setenv("BH_PREFER_CDP_URL", "http://127.0.0.1:9223/")
    probed = []
    monkeypatch.setattr(run, "_devtools_alive", lambda url: probed.append(url) or True)
    assert run._prefer_dedicated_chrome() == "claude"
    assert probed == ["http://127.0.0.1:9223"]
    assert run.os.environ["BU_CDP_URL"] == "http://127.0.0.1:9223" and run.os.environ["BU_NAME"] == "claude"
    assert run.NAME == admin.NAME == helpers.NAME == "claude"


def test_prefer_dedicated_chrome_is_a_noop_when_it_is_down_or_configured(monkeypatch):
    _reset_names(monkeypatch)
    monkeypatch.setenv("BH_PREFER_CDP_URL", "http://127.0.0.1:9223")
    monkeypatch.setattr(run, "_devtools_alive", lambda url: False)
    assert run._prefer_dedicated_chrome() is None and "BU_NAME" not in run.os.environ
    monkeypatch.setattr(run, "_devtools_alive", lambda url: True)
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:9222")
    assert run._prefer_dedicated_chrome() is None and "BU_NAME" not in run.os.environ
    monkeypatch.delenv("BU_CDP_URL")
    monkeypatch.setenv("BU_NAME", "worker")
    assert run._prefer_dedicated_chrome() is None and "BU_CDP_URL" not in run.os.environ


def test_prefer_dedicated_chrome_disabled_by_empty_url(monkeypatch):
    _reset_names(monkeypatch)
    monkeypatch.setenv("BH_PREFER_CDP_URL", "")
    monkeypatch.setattr(run, "_devtools_alive", lambda url: (_ for _ in ()).throw(AssertionError("must not probe")))
    assert run._prefer_dedicated_chrome() is None
