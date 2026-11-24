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


def test_guard_refuses_to_foreground_a_tab_the_harness_did_not_open():
    d = _daemon(current="mine")
    d.own("mine")
    assert "refused" in _handle(d, {"method": "Target.activateTarget", "params": {"targetId": "user-tab"}})["error"]
    assert "result" in _handle(d, {"method": "Target.activateTarget", "params": {"targetId": "mine"}})


def test_named_daemon_reuses_its_own_tab_after_an_ungraceful_restart(monkeypatch):
    monkeypatch.setattr(daemon, "NAME", "claude")
    monkeypatch.setattr(daemon, "REMOTE_ID", None)
    monkeypatch.setattr(daemon, "BROWSER_KIND", "cdp")
    d = _daemon(targets=[
        {"targetId": "user-tab", "url": "https://user/", "type": "page"},
        {"targetId": "left-over", "url": "about:blank", "type": "page"},
    ])
    d.own("left-over")  # task "daemon": the previous run's dedicated tab
    page = asyncio.run(d.attach_first_page())
    assert page["targetId"] == "left-over" and d.dedicated_target_id == "left-over" and d.cdp.created == 0


def test_allow_user_tabs_flag_bypasses_the_guard():
    d = _daemon(current="user-tab")
    r = _handle(d, {"method": "Runtime.evaluate", "params": {"expression": "1"}, "allow_user_tabs": True})
    assert r == {"result": {"value": "1"}}


def test_browser_level_calls_pass_regardless_of_ownership():
    d = _daemon(targets=[{"targetId": "user-tab", "url": "https://x", "type": "page"}], current="user-tab")
    assert "result" in _handle(d, {"method": "Target.getTargets", "params": {}})


def test_explicit_session_on_a_known_user_tab_is_refused_and_unknown_sessions_too():
    d = _daemon(current="mine")
    d.own("mine")
    _handle(d, {"method": "Target.attachToTarget", "params": {"targetId": "user-tab", "flatten": True}})
    r = _handle(d, {"method": "Runtime.evaluate", "params": {"expression": "1"}, "session_id": "session-for-user-tab"})
    assert "refused" in r["error"]
    # A session the daemon never attached cannot be told apart from a user's tab: fail closed.
    r = _handle(d, {"method": "Runtime.evaluate", "params": {"expression": "1"}, "session_id": "never-seen"})
    assert "refused" in r["error"] and "never-seen" in r["error"]
    assert not any(m == "Runtime.evaluate" for m, _p, _s in d.cdp.calls)
    _handle(d, {"method": "Target.attachToTarget", "params": {"targetId": "mine", "flatten": True}})
    r = _handle(d, {"method": "Runtime.evaluate", "params": {"expression": "1"}, "session_id": "session-for-mine"})
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
    d.own("t")  # the daemon's scratch tab
    _handle(d, {"meta": "own_tab", "target_id": "t", "task": "a"})
    _handle(d, {"meta": "own_tab", "target_id": "t", "task": "a", "url": "https://a/"})
    assert d.owned["t"]["task"] == "a" and d.owned["t"]["urls"] == ["https://a/"]
    _handle(d, {"meta": "disown_tab", "target_id": "t"})
    assert d.owned == {}


def test_registry_survives_a_daemon_restart():
    d = _daemon()
    d.own("kept", task="t", url="https://a/")
    again = daemon.Daemon()
    assert again.owned["kept"]["task"] == "t" and again.owned["kept"]["urls"] == ["https://a/"]


def test_raw_create_target_is_owned_with_the_request_task():
    d = _daemon(current="mine")
    d.own("mine")
    r = _handle(d, {"method": "Target.createTarget", "params": {"url": "about:blank", "background": True}, "task": "t"})
    assert r["result"]["targetId"] == "created-1" and d.owned["created-1"]["task"] == "t"


def test_marker_is_never_scheduled_for_a_tab_the_harness_does_not_own():
    async def run():
        d = _daemon(current="user-tab")
        assert d._schedule_tab_marker(d.session) is None
        d.own("user-tab")
        task = d._schedule_tab_marker(d.session)
        await task
        return d
    d = asyncio.run(run())
    assert any(m == "Runtime.evaluate" for m, _p, _s in d.cdp.calls)


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


def test_daemon_does_not_own_a_users_blank_or_new_tab_page_but_owns_cloud_pages(monkeypatch):
    monkeypatch.setattr(daemon, "NAME", "default")
    monkeypatch.setattr(daemon, "REMOTE_ID", None)
    monkeypatch.setattr(daemon, "BROWSER_KIND", "cdp")
    d = _daemon(targets=[{"targetId": "user-blank", "url": "chrome://newtab/", "type": "page"}])
    asyncio.run(d.attach_first_page())
    assert d.target_id == "created-1" and "user-blank" not in d.owned  # local: never the user's tab, not even a blank one
    monkeypatch.setattr(daemon, "REMOTE_ID", "cloud-browser")
    monkeypatch.setattr(daemon, "BROWSER_KIND", "cloud")
    d = _daemon(targets=[{"targetId": "cloud-blank", "url": "about:blank", "type": "page"}])
    asyncio.run(d.attach_first_page())
    assert "cloud-blank" in d.owned


def test_recovery_never_replays_a_request_on_a_users_page(monkeypatch):
    """The owned tab was closed; the only page left is the user's. Recovery opens its own
    scratch tab (never the user's) and the pending click/JS runs nowhere: its tab is gone."""
    monkeypatch.setattr(daemon, "NAME", "default")
    monkeypatch.setattr(daemon, "REMOTE_ID", None)
    monkeypatch.setattr(daemon, "BROWSER_KIND", "cdp")

    class _StaleCDP(_CDP):
        async def send_raw(self, method, params=None, session_id=None):
            if method == "Runtime.evaluate" and session_id == "session-current":
                self.calls.append((method, params, session_id))
                raise RuntimeError("Session with given id not found")
            return await super().send_raw(method, params, session_id)

    d = _daemon(targets=[{"targetId": "user-tab", "url": "https://user/", "type": "page"}], current="mine-closed")
    d.cdp = _StaleCDP(d.cdp.targets)
    d.own("mine-closed")
    r = _handle(d, {"method": "Runtime.evaluate", "params": {"expression": "click()"}})
    assert "refused" in r["error"] and "mine-closed" in r["error"] and "closed" in r["error"]
    assert d.target_id == "created-1"
    assert not any(m == "Target.attachToTarget" and p.get("targetId") == "user-tab" for m, p, _s in d.cdp.calls)
    assert [s for m, _p, s in d.cdp.calls if m == "Runtime.evaluate"] == ["session-current"]  # only the stale try


def test_daemon_owns_the_blank_tab_it_creates_but_not_a_users_page(monkeypatch):
    monkeypatch.setattr(daemon, "NAME", "default")
    monkeypatch.setattr(daemon, "REMOTE_ID", None)
    monkeypatch.setattr(daemon, "BROWSER_KIND", "cdp")
    d = _daemon()
    asyncio.run(d.attach_first_page())
    assert "created-1" in d.owned
    d2 = _daemon(targets=[{"targetId": "user-tab", "url": "https://user/", "type": "page"}])
    asyncio.run(d2.attach_first_page())
    assert d2.target_id == "created-1" and "user-tab" not in d2.owned


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
    assert not any(c[0] == "Target.activateTarget" for c in calls)


def test_every_request_carries_the_task_so_created_targets_are_registered(monkeypatch):
    seen = []

    class _Sock:
        def settimeout(self, t): pass
        def close(self): pass
    monkeypatch.setattr(helpers.ipc, "connect", lambda name, timeout: (_Sock(), None))
    monkeypatch.setattr(helpers.ipc, "request", lambda c, token, req: seen.append(req) or {"result": {}})
    helpers.cdp("Target.createTarget", url="about:blank")
    assert seen[0]["task"] == helpers.TASK


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
    _fake_helpers(monkeypatch, [{"targetId": "mine", "url": "https://mine/", "task": helpers.TASK, "urls": []}], calls)
    monkeypatch.setattr(helpers, "current_tab", lambda: {"targetId": "mine", "url": "https://mine/", "title": ""})
    assert helpers.ensure_real_tab()["targetId"] == "mine"
    assert calls == []


def test_ensure_real_tab_and_blank_reuse_never_take_another_tasks_tab(monkeypatch):
    calls = []
    other = [{"targetId": "theirs", "url": "https://3dlook/", "task": "other-task", "urls": []},
             {"targetId": "their-blank", "url": "about:blank", "task": "other-task", "urls": []}]
    real_new_tab = helpers.new_tab
    _fake_helpers(monkeypatch, other, calls)
    monkeypatch.setattr(helpers, "current_tab", lambda: {"targetId": "theirs", "url": "https://3dlook/", "title": ""})
    monkeypatch.setattr(helpers, "new_tab", lambda url="about:blank", reuse=True: calls.append(("new_tab", url)) or "fresh")
    assert helpers.ensure_real_tab()["targetId"] == "fresh"
    assert not any(c[0] == "switch_tab" for c in calls)
    calls.clear()
    monkeypatch.setattr(helpers, "new_tab", real_new_tab)
    assert helpers.new_tab("https://example.com/") == "created"  # their blank tab is not reused
    assert not any(c[0] == "ipc" and c[1].get("meta") == "own_tab" for c in calls)


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


def test_reload_stops_the_daemon_the_scripts_actually_use(monkeypatch):
    """`browser-harness --reload` with the dedicated Chrome up must stop the `claude` daemon."""
    import sys
    from unittest.mock import patch
    _reset_names(monkeypatch)
    monkeypatch.setenv("BH_PREFER_CDP_URL", "http://127.0.0.1:9223")
    monkeypatch.setattr(run, "_devtools_alive", lambda url: True)
    seen = []
    with patch.object(sys, "argv", ["browser-harness", "--reload"]), \
         patch("browser_harness.run.restart_daemon", side_effect=lambda: seen.append(admin.NAME)):
        run._run(["--reload"])
    assert seen == ["claude"]


def test_prefer_dedicated_chrome_disabled_by_empty_url(monkeypatch):
    _reset_names(monkeypatch)
    monkeypatch.setenv("BH_PREFER_CDP_URL", "")
    monkeypatch.setattr(run, "_devtools_alive", lambda url: (_ for _ in ()).throw(AssertionError("must not probe")))
    assert run._prefer_dedicated_chrome() is None


# --- tasks sharing one daemon (2026-11-24: a 3DLook worker read another task's tab) ---

def _stale_daemon(monkeypatch, targets, current):
    monkeypatch.setattr(daemon, "NAME", "default")
    monkeypatch.setattr(daemon, "REMOTE_ID", None)
    monkeypatch.setattr(daemon, "BROWSER_KIND", "cdp")

    class _StaleCDP(_CDP):
        async def send_raw(self, method, params=None, session_id=None):
            if method == "Runtime.evaluate" and session_id == "session-current":
                self.calls.append((method, params, session_id))
                raise RuntimeError("Session with given id not found")
            return await super().send_raw(method, params, session_id)

    d = _daemon(targets=targets, current=current)
    d.cdp = _StaleCDP(d.cdp.targets)
    return d


def test_a_closed_tab_never_replays_the_read_on_another_tasks_tab(monkeypatch):
    """The reproduction: tasks A and B share the default daemon; B's tab is closed mid-task;
    B's next read used to re-attach to A's tab (owned by the daemon) and return A's page."""
    d = _stale_daemon(monkeypatch, [{"targetId": "a-tab", "url": "https://3dlook/a", "type": "page"}], current="b-closed")
    d.own("a-tab", task="taskA")
    d.own("b-closed", task="taskB")
    d._session_targets["session-current"] = "b-closed"
    r = _handle(d, {"method": "Runtime.evaluate", "params": {"expression": "document.title"}, "task": "taskB"})
    assert "refused" in r["error"] and "b-closed" in r["error"] and "closed" in r["error"]
    assert not any(m == "Runtime.evaluate" and s == "session-for-a-tab" for m, _p, s in d.cdp.calls)
    assert d.target_id != "a-tab"  # recovery parks on the daemon's own scratch tab, not A's
    later = _handle(d, {"method": "Runtime.evaluate", "params": {"expression": "1"}, "task": "taskB",
                        "session_id": "session-for-a-tab"})
    assert "refused" in later["error"]


def test_guard_is_per_task_and_the_daemon_scratch_tab_is_shared(monkeypatch):
    d = _daemon(targets=[{"targetId": "a-tab", "url": "https://a/", "type": "page"},
                         {"targetId": "scratch", "url": "about:blank", "type": "page"}], current="a-tab")
    d.own("a-tab", task="taskA")
    d.own("scratch")  # task "daemon"
    assert "result" in _handle(d, {"method": "Runtime.evaluate", "params": {"expression": "1"}, "task": "taskA"})
    r = _handle(d, {"method": "Runtime.evaluate", "params": {"expression": "1"}, "task": "taskB"})
    assert "refused" in r["error"] and "taskA" in r["error"]
    assert "refused" in _handle(d, {"method": "Target.closeTarget", "params": {"targetId": "a-tab"}, "task": "taskB"})["error"]
    d.target_id = "scratch"
    assert "result" in _handle(d, {"method": "Runtime.evaluate", "params": {"expression": "1"}, "task": "taskB"})
    d.target_id = "a-tab"  # a request with no task (older helpers) keeps the daemon-wide rule
    assert "result" in _handle(d, {"method": "Runtime.evaluate", "params": {"expression": "1"}})


def test_navigating_the_scratch_tab_makes_it_the_tasks(monkeypatch):
    d = _daemon(current="scratch")
    d.own("scratch")
    assert "result" in _handle(d, {"method": "Page.navigate", "params": {"url": "https://a/"}, "task": "taskA"})
    assert d.owned["scratch"]["task"] == "taskA"
    assert "refused" in _handle(d, {"method": "Runtime.evaluate", "params": {"expression": "1"}, "task": "taskB"})["error"]


def test_set_session_takes_the_tab_from_the_session_it_names(monkeypatch):
    d = _daemon(targets=[{"targetId": "mine2", "url": "https://m/", "type": "page"}], current="mine")
    d.own("mine")
    d.own("mine2")
    _handle(d, {"method": "Target.attachToTarget", "params": {"targetId": "mine2", "flatten": True}})
    _handle(d, {"meta": "set_session", "session_id": "session-for-mine2", "target_id": "mine"})  # a wrong target_id
    assert d.target_id == "mine2"  # the session decides, not the caller's word


def test_session_map_trim_keeps_the_current_session():
    d = _daemon(current="mine")
    d.session = "live"
    d._session_targets["live"] = "mine"
    for i in range(300):
        d._session_targets[f"s{i}"] = "mine"
    d._trim_session_targets()
    assert len(d._session_targets) == 256 and d._session_targets["live"] == "mine"


# --- Codex round 1 (PR #3) ---

def test_claiming_the_scratch_tab_stops_shutdown_from_closing_it():
    d = _daemon(current="scratch")
    d.own("scratch"); d.dedicated_target_id = "scratch"
    assert _handle(d, {"meta": "own_tab", "target_id": "scratch", "task": "taskA"}) == {"ok": True}
    assert d.dedicated_target_id is None and d.owned["scratch"]["task"] == "taskA"


def test_attach_and_set_session_on_a_tab_not_this_tasks_are_refused():
    d = _daemon(targets=[{"targetId": "user-tab", "url": "https://user/", "type": "page"},
                         {"targetId": "a-tab", "url": "https://a/", "type": "page"},
                         {"targetId": "a-frame", "url": "https://a/f", "type": "iframe", "parentId": "a-tab"}],
                current="a-tab")
    d.own("a-tab", task="taskA")
    r = _handle(d, {"method": "Target.attachToTarget", "params": {"targetId": "user-tab", "flatten": True}, "task": "taskA"})
    assert "refused" in r["error"]
    r = _handle(d, {"method": "Target.attachToTarget", "params": {"targetId": "a-tab", "flatten": True}, "task": "taskB"})
    assert "refused" in r["error"]
    assert not any(m == "Target.attachToTarget" for m, _p, _s in d.cdp.calls)
    assert "result" in _handle(d, {"method": "Target.attachToTarget", "params": {"targetId": "a-frame", "flatten": True}, "task": "taskA"})
    assert "refused" in _handle(d, {"meta": "set_session", "session_id": "made-up", "task": "taskA"})["error"]
    d._session_targets["sess-a"] = "a-tab"
    assert "refused" in _handle(d, {"meta": "set_session", "session_id": "sess-a", "task": "taskB"})["error"]
    assert d.session == "session-current"
    assert "error" not in _handle(d, {"meta": "set_session", "session_id": "sess-a", "task": "taskA"})
    assert d.session == "sess-a" and d.target_id == "a-tab"


def test_second_task_cannot_retag_the_first_tasks_tab():
    d = _daemon()
    d.own("scratch")
    assert _handle(d, {"meta": "own_tab", "target_id": "scratch", "task": "taskA"}) == {"ok": True}
    r = _handle(d, {"meta": "own_tab", "target_id": "scratch", "task": "taskB"})
    assert "refused" in r["error"] and d.owned["scratch"]["task"] == "taskA"
    assert "refused" in _handle(d, {"meta": "own_tab", "target_id": "user-tab", "task": "taskA"})["error"]


def test_new_tab_opens_its_own_when_the_scratch_claim_is_refused(monkeypatch):
    calls = []
    _fake_helpers(monkeypatch, [{"targetId": "scratch", "url": "about:blank", "task": "daemon", "urls": []}], calls)
    def refuse(request):
        calls.append(("ipc", request))
        raise RuntimeError("refused: tab scratch belongs to task 'taskA'")
    monkeypatch.setattr(helpers, "_send", refuse)
    assert helpers.new_tab("https://example.com/") == "created"
    assert ("switch_tab", "created") in calls and ("switch_tab", "scratch") not in calls


def test_restart_reuses_the_left_scratch_tab_and_still_closes_it(monkeypatch):
    monkeypatch.setattr(daemon, "NAME", "default")
    monkeypatch.setattr(daemon, "REMOTE_ID", None)
    monkeypatch.setattr(daemon, "BROWSER_KIND", "cdp")
    d = _daemon(targets=[{"targetId": "left", "url": "about:blank", "type": "page"}])
    d.own("left")
    asyncio.run(d.attach_first_page())
    assert d.target_id == "left" and d.dedicated_target_id == "left" and d.cdp.created == 0


# --- Codex round 2 (PR #3): each task keeps its own current tab ---

def test_after_another_tasks_recovery_a_task_is_put_back_on_its_own_tab(monkeypatch):
    d = _stale_daemon(monkeypatch, [{"targetId": "a-tab", "url": "https://3dlook/a", "type": "page"}], current="b-closed")
    d.own("a-tab", task="taskA"); d.own("b-closed", task="taskB")
    d._session_targets["session-current"] = "b-closed"
    d._task_target["taskA"] = "a-tab"
    assert "closed" in _handle(d, {"method": "Runtime.evaluate", "params": {"expression": "1"}, "task": "taskB"})["error"]
    assert d.target_id != "a-tab"  # B's recovery parked on the scratch tab
    r = _handle(d, {"method": "Runtime.evaluate", "params": {"expression": "document.title"}, "task": "taskA"})
    assert "result" in r  # ran on A's own tab, through A's own session; the global one is untouched
    assert ("Runtime.evaluate", {"expression": "document.title"}, "session-for-a-tab") in d.cdp.calls


def test_after_a_restart_a_task_continues_on_its_newest_live_tab():
    d = _daemon(targets=[{"targetId": "old", "url": "https://a/1", "type": "page"},
                         {"targetId": "new", "url": "https://a/2", "type": "page"},
                         {"targetId": "scratch", "url": "about:blank", "type": "page"}], current="scratch")
    d.own("scratch")
    d.own("old", task="taskA"); d.owned["old"]["opened"] = 1
    d.own("new", task="taskA"); d.owned["new"]["opened"] = 2
    d.own("gone", task="taskA"); d.owned["gone"]["opened"] = 3  # closed while the daemon was down
    r = _handle(d, {"method": "Runtime.evaluate", "params": {"expression": "1"}, "task": "taskA"})
    assert "result" in r and ("Runtime.evaluate", {"expression": "1"}, "session-for-new") in d.cdp.calls
    assert asyncio.run(d._task_tab("taskA")) == ("session-for-new", "new")


def test_a_task_with_no_tab_stays_on_the_scratch_tab_without_extra_calls():
    d = _daemon(targets=[{"targetId": "scratch", "url": "about:blank", "type": "page"}], current="scratch")
    d.own("scratch")
    assert "result" in _handle(d, {"method": "Runtime.evaluate", "params": {"expression": "1"}, "task": "solo"})
    assert [m for m, _p, _s in d.cdp.calls] == ["Runtime.evaluate"]


def test_cloud_recovery_never_takes_another_tasks_page(monkeypatch):
    monkeypatch.setattr(daemon, "NAME", "default")
    monkeypatch.setattr(daemon, "REMOTE_ID", "cloud-browser")
    monkeypatch.setattr(daemon, "BROWSER_KIND", "cloud")
    d = _daemon(targets=[{"targetId": "a-tab", "url": "https://a/", "type": "page"},
                         {"targetId": "free", "url": "https://free/", "type": "page"}], current="b-closed")
    d.own("a-tab", task="taskA")
    asyncio.run(d.attach_first_page())
    assert d.target_id == "free"



# --- Codex round 3 (PR #3): concurrent tasks and status requests ---

class _YieldingCDP(_CDP):
    """Yields on every call, so concurrent handlers really interleave."""
    async def send_raw(self, method, params=None, session_id=None):
        await asyncio.sleep(0)
        if method == "Target.getTargetInfo":
            t = next(t for t in self.targets if t["targetId"] == params["targetId"])
            return {"targetInfo": {**t, "title": t["targetId"]}}
        return await super().send_raw(method, params, session_id)


def _two_tasks():
    d = _daemon(targets=[{"targetId": "a-tab", "url": "https://a/", "type": "page"},
                         {"targetId": "b-tab", "url": "https://b/", "type": "page"},
                         {"targetId": "scratch", "url": "about:blank", "type": "page"}], current="scratch")
    d.cdp = _YieldingCDP(d.cdp.targets)
    d.own("scratch"); d.own("a-tab", task="taskA"); d.own("b-tab", task="taskB")
    return d


def test_concurrent_tasks_each_run_on_their_own_tab():
    d = _two_tasks()
    async def run():
        return await asyncio.gather(*[d.handle({"method": "Runtime.evaluate", "params": {"expression": t},
                                                "task": t}) for t in ("taskA", "taskB", "taskA", "taskB")])
    out = asyncio.run(run())
    assert all("result" in r for r in out), out
    ran = [(p["expression"], s) for m, p, s in d.cdp.calls if m == "Runtime.evaluate"]
    assert sorted(set(ran)) == [("taskA", "session-for-a-tab"), ("taskB", "session-for-b-tab")]
    assert d.session == "session-current" and d.target_id == "scratch"  # the global session never moved
    assert sum(1 for m, p, _s in d.cdp.calls if m == "Target.attachToTarget") == 2  # one attach per task


def test_status_requests_answer_for_the_asking_task():
    d = _two_tasks()
    assert _handle(d, {"meta": "current_tab", "task": "taskA"})["targetId"] == "a-tab"
    assert _handle(d, {"meta": "current_tab", "task": "taskB"})["targetId"] == "b-tab"
    assert _handle(d, {"meta": "session", "task": "taskB"})["session_id"] == "session-for-b-tab"
    assert _handle(d, {"meta": "connection_status", "task": "taskA"})["target_id"] == "a-tab"
    assert _handle(d, {"meta": "current_tab", "task": "nobody"})["targetId"] == "scratch"


def test_a_stale_task_session_recovers_on_its_own_tab_while_another_task_works():
    d = _two_tasks()
    d._target_session["b-tab"] = "b-stale"; d._session_targets["b-stale"] = "b-tab"; d._task_target["taskB"] = "b-tab"

    class _StaleB(_YieldingCDP):
        async def send_raw(self, method, params=None, session_id=None):
            if session_id == "b-stale":
                self.calls.append((method, params, session_id))
                raise RuntimeError("Session with given id not found")
            return await super().send_raw(method, params, session_id)
    d.cdp = _StaleB(d.cdp.targets)
    async def run():
        return await asyncio.gather(
            d.handle({"method": "Runtime.evaluate", "params": {"expression": "b"}, "task": "taskB"}),
            d.handle({"method": "Runtime.evaluate", "params": {"expression": "a"}, "task": "taskA"}))
    rb, ra = asyncio.run(run())
    assert "result" in rb and "result" in ra
    assert ("Runtime.evaluate", {"expression": "b"}, "session-for-b-tab") in d.cdp.calls  # replayed on B's own tab
    assert ("Runtime.evaluate", {"expression": "a"}, "session-for-a-tab") in d.cdp.calls


def test_owned_tabs_none_is_sent_as_none(monkeypatch):
    sent = []
    monkeypatch.setattr(helpers.ipc, "connect", lambda name, timeout: (_Conn(), "tok"))
    monkeypatch.setattr(helpers.ipc, "request", lambda c, token, req: sent.append(req) or {"tabs": []})
    helpers.owned_tabs(None)
    helpers.owned_tabs()
    assert sent[0]["task"] is None and sent[1]["task"] == helpers.TASK


class _Conn:
    def settimeout(self, t): pass
    def close(self): pass


# --- Codex round 4 (PR #3): nothing shared between tasks leaks or misleads ---

def test_a_task_whose_tab_was_pruned_is_refused_not_redirected_to_scratch():
    d = _two_tasks()
    d.cdp.targets[:] = [t for t in d.cdp.targets if t["targetId"] != "b-tab"]  # B's tab closed outside
    _handle(d, {"meta": "owned_tabs", "task": None})  # the listing prunes it
    assert "b-tab" not in d.owned and "taskB" not in d._task_locks
    r = _handle(d, {"method": "Runtime.evaluate", "params": {"expression": "1"}, "task": "taskB"})
    assert "closed" in r["error"]
    assert not any(m == "Runtime.evaluate" for m, _p, _s in d.cdp.calls)
    d.own("b-new", task="taskB")  # a new tab of its own lifts it
    assert "taskB" not in d._task_closed


def test_events_and_dialogs_stay_with_their_task():
    d = _two_tasks()
    asyncio.run(d._task_tab("taskA")); asyncio.run(d._task_tab("taskB"))
    d._record_event("Network.requestWillBeSent", {"requestId": "a1"}, "session-for-a-tab")
    d._record_event("Network.requestWillBeSent", {"requestId": "b1"}, "session-for-b-tab")
    d._record_event("Page.javascriptDialogOpening", {"message": "B's secret"}, "session-for-b-tab")
    got_a = _handle(d, {"meta": "drain_events", "task": "taskA"})["events"]
    assert [e["params"].get("requestId") for e in got_a] == ["a1"]
    assert len(d.events) == 2  # B's events are still there for B
    assert _handle(d, {"meta": "pending_dialog", "task": "taskA"})["dialog"] is None
    assert _handle(d, {"meta": "pending_dialog", "task": "taskB"})["dialog"] == {"message": "B's secret"}


def test_switching_tabs_never_disables_network_on_another_tasks_session():
    d = _two_tasks()
    asyncio.run(d._task_tab("taskA"))
    d.session, d.target_id = "session-for-a-tab", "a-tab"; d._task_target["taskA"] = "a-tab"
    d._session_targets["sess-b2"] = "b-tab"
    _handle(d, {"meta": "set_session", "session_id": "sess-b2", "task": "taskB"})
    assert ("Network.disable", None, "session-for-a-tab") not in d.cdp.calls


# --- Codex round 5 (PR #3) ---

def test_a_tab_found_closed_by_recovery_keeps_refusing_the_retry(monkeypatch):
    d = _two_tasks()
    d._target_session["b-tab"] = "b-stale"; d._session_targets["b-stale"] = "b-tab"; d._task_target["taskB"] = "b-tab"
    d.cdp.targets[:] = [t for t in d.cdp.targets if t["targetId"] != "b-tab"]

    class _StaleB(_YieldingCDP):
        async def send_raw(self, method, params=None, session_id=None):
            if session_id == "b-stale":
                raise RuntimeError("Session with given id not found")
            return await super().send_raw(method, params, session_id)
    d.cdp = _StaleB(d.cdp.targets)
    for _ in range(2):  # the first try and the retry
        r = _handle(d, {"method": "Runtime.evaluate", "params": {"expression": "1"}, "task": "taskB"})
        assert "closed" in r["error"]
    assert not any(m == "Runtime.evaluate" and s != "b-stale" for m, _p, s in d.cdp.calls)


def test_a_task_without_a_tab_never_sees_another_tasks_session():
    d = _two_tasks()
    asyncio.run(d._task_tab("taskA"))
    d.session, d.target_id = "session-for-a-tab", "a-tab"  # the global session sits on A's tab
    d._record_event("Network.requestWillBeSent", {"requestId": "a1"}, "session-for-a-tab")
    d._record_event("Page.javascriptDialogOpening", {"message": "A's"}, "session-for-a-tab")
    assert _handle(d, {"meta": "drain_events", "task": "fresh"})["events"] == []
    assert _handle(d, {"meta": "pending_dialog", "task": "fresh"})["dialog"] is None
    assert _handle(d, {"meta": "session", "task": "fresh"})["session_id"] is None
    assert len(d.events) == 2
    d.session, d.target_id = "session-current", "scratch"  # on the scratch tab a tab-less task may look
    assert _handle(d, {"meta": "session", "task": "fresh"})["session_id"] == "session-current"


# --- Codex round 6 (PR #3) ---

def test_a_task_whose_tabs_all_closed_while_the_daemon_was_down_is_refused():
    d = _two_tasks()
    d.own("b-gone", task="taskB")
    d.cdp.targets[:] = [t for t in d.cdp.targets if t["targetId"] != "b-tab"]  # both B tabs gone
    r = _handle(d, {"method": "Runtime.evaluate", "params": {"expression": "1"}, "task": "taskB"})
    assert "closed" in r["error"]
    assert not any(m == "Runtime.evaluate" for m, _p, _s in d.cdp.calls)
    assert _handle(d, {"meta": "session", "task": "taskB"})["session_id"] is None


def test_the_title_marker_goes_on_the_tab_that_loaded():
    async def run():
        d = _two_tasks()
        await d._task_tab("taskA"); await d._task_tab("taskB")
        d.session, d.target_id = "session-for-b-tab", "b-tab"
        task = d._schedule_tab_marker("session-for-a-tab")
        await task
        return d
    d = asyncio.run(run())
    marks = [s for m, p, s in d.cdp.calls if m == "Runtime.evaluate" and p.get("expression") == daemon.TAB_MARKER_JS]
    assert marks == ["session-for-a-tab"]
